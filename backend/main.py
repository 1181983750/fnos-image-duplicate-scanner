import asyncio
import hashlib
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pyvips
from fastapi import FastAPI, HTTPException, Query
from fastapi import BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".jxl"}
CONVERT_EXTENSIONS = {".jpg", ".jpeg", ".png"}
CPU_THREADS = os.cpu_count() or 4
DEFAULT_WORKERS = min(4, CPU_THREADS)
MAX_WORKERS = max(DEFAULT_WORKERS, min(64, CPU_THREADS))
SLOW_STEP_SECONDS = float(os.getenv("SLOW_STEP_SECONDS", "1.0"))
SLOW_FILE_SECONDS = float(os.getenv("SLOW_FILE_SECONDS", "2.0"))
LOG_EVERY_FILES = int(os.getenv("LOG_EVERY_FILES", "1000"))
DB_PATH = Path(os.getenv("SCANNER_DB", "/data/scanner.db"))
SCAN_ROOTS = [
    Path(root).resolve()
    for root in os.getenv("SCAN_ROOTS", "/scan").split(":")
    if root.strip()
]
SOURCE_ROOTS = [
    Path(root).resolve()
    for root in os.getenv("SOURCE_ROOTS", "/source:/scan").split(":")
    if root.strip()
]

app = FastAPI(title="Image Duplicate Scanner")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
jobs_lock = threading.Lock()
db_lock = threading.Lock()
jobs: Dict[str, "ScanJob"] = {}


class ScanRequest(BaseModel):
    directories: List[str] = Field(min_length=1)
    convert: bool = False
    full_scan: bool = False
    workers: Optional[int] = Field(default=None, ge=1, le=64)


class DeleteRequest(BaseModel):
    paths: List[str] = Field(min_length=1)


class ImportRequest(BaseModel):
    path: str
    workers: Optional[int] = Field(default=None, ge=1, le=64)


@dataclass
class ImageRecord:
    path: str
    size: int
    modified_at: float
    sha256: Optional[str] = None
    phash: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    converted_from: Optional[str] = None
    conversion_error: Optional[str] = None
    scan_error: Optional[str] = None
    sha_seconds: float = 0.0
    decode_seconds: float = 0.0
    phash_prepare_seconds: float = 0.0
    phash_dct_seconds: float = 0.0
    phash_seconds: float = 0.0
    phash_skipped: bool = False
    cache_hit: bool = False


@dataclass
class DuplicateGroup:
    key_type: str
    key: str
    files: List[ImageRecord]


@dataclass
class ScanJob:
    id: str
    status: str = "queued"
    message: str = "等待开始"
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    total_files: int = 0
    discovered_files: int = 0
    converted_files: int = 0
    scanned_files: int = 0
    processed_files: int = 0
    failed_files: int = 0
    phash_skipped_files: int = 0
    cache_hit_files: int = 0
    queue_size: int = 0
    active_workers: int = 0
    discovery_done: bool = False
    duplicates: List[DuplicateGroup] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)
    cancel_requested: bool = False
    sha_seconds_total: float = 0.0
    decode_seconds_total: float = 0.0
    phash_prepare_seconds_total: float = 0.0
    phash_dct_seconds_total: float = 0.0
    phash_seconds_total: float = 0.0
    convert_seconds_total: float = 0.0
    sha_index: Dict[str, List[ImageRecord]] = field(default_factory=dict, repr=False)
    phash_index: Dict[str, List[ImageRecord]] = field(default_factory=dict, repr=False)
    failed_records: List[ImageRecord] = field(default_factory=list)


class ScanCancelled(Exception):
    pass


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS image_cache (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                modified_at REAL NOT NULL,
                sha256 TEXT,
                phash TEXT,
                width INTEGER,
                height INTEGER,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                started_at REAL NOT NULL,
                snapshot TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_updated_at ON jobs(updated_at)")


def cache_get(path: Path, size: int, modified_at: float) -> Optional[dict]:
    with db_lock, sqlite3.connect(DB_PATH, timeout=30) as conn:
        row = conn.execute(
            """
            SELECT sha256, phash, width, height
            FROM image_cache
            WHERE path = ? AND size = ? AND modified_at = ?
            """,
            (str(path), size, modified_at),
        ).fetchone()
    if not row:
        return None
    return {"sha256": row[0], "phash": row[1], "width": row[2], "height": row[3]}


def cache_find_duplicates(sha256: Optional[str], phash: Optional[str]) -> List[dict]:
    clauses = []
    params = []
    if sha256:
        clauses.append("sha256 = ?")
        params.append(sha256)
    if phash:
        clauses.append("phash = ?")
        params.append(phash)
    if not clauses:
        return []
    with db_lock, sqlite3.connect(DB_PATH, timeout=30) as conn:
        rows = conn.execute(
            f"""
            SELECT path, size, modified_at, sha256, phash, width, height
            FROM image_cache
            WHERE {" OR ".join(clauses)}
            ORDER BY path
            LIMIT 100
            """,
            params,
        ).fetchall()
    return [
        {
            "path": row[0],
            "size": row[1],
            "modified_at": row[2],
            "sha256": row[3],
            "phash": row[4],
            "width": row[5],
            "height": row[6],
            "match_type": "sha256" if sha256 and row[3] == sha256 else "phash",
        }
        for row in rows
    ]


def cache_put(record: ImageRecord) -> None:
    if record.scan_error or not record.sha256:
        return
    with db_lock, sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute(
            """
            INSERT INTO image_cache(path, size, modified_at, sha256, phash, width, height, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                size = excluded.size,
                modified_at = excluded.modified_at,
                sha256 = excluded.sha256,
                phash = excluded.phash,
                width = excluded.width,
                height = excluded.height,
                updated_at = excluded.updated_at
            """,
            (
                record.path,
                record.size,
                record.modified_at,
                record.sha256,
                record.phash,
                record.width,
                record.height,
                time.time(),
            ),
        )


def cache_delete_paths(paths: List[str]) -> None:
    if not paths:
        return
    with db_lock, sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.executemany("DELETE FROM image_cache WHERE path = ?", [(path,) for path in paths])


def record_from_dict(data: dict) -> ImageRecord:
    fields = ImageRecord.__dataclass_fields__
    return ImageRecord(**{key: value for key, value in data.items() if key in fields})


def job_to_dict_unlocked(job: ScanJob) -> dict:
    total = job.total_files if job.discovery_done else job.discovered_files
    return {
            "id": job.id,
            "status": job.status,
            "message": job.message,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "total_files": job.total_files,
            "discovered_files": job.discovered_files,
            "converted_files": job.converted_files,
            "scanned_files": job.scanned_files,
            "processed_files": job.processed_files,
            "failed_files": job.failed_files,
            "phash_skipped_files": job.phash_skipped_files,
            "cache_hit_files": job.cache_hit_files,
            "queue_size": job.queue_size,
            "active_workers": job.active_workers,
            "discovery_done": job.discovery_done,
            "duplicates": [
                {
                    "key_type": group.key_type,
                    "key": group.key,
                    "files": [record.__dict__ for record in group.files],
                }
                for group in job.duplicates
            ],
            "failed_records": [record.__dict__ for record in job.failed_records],
            "errors": list(job.errors),
            "logs": list(job.logs),
            "cancel_requested": job.cancel_requested,
            "timings": {
                "sha_seconds_total": round(job.sha_seconds_total, 3),
                "decode_seconds_total": round(job.decode_seconds_total, 3),
                "phash_prepare_seconds_total": round(job.phash_prepare_seconds_total, 3),
                "phash_dct_seconds_total": round(job.phash_dct_seconds_total, 3),
                "phash_seconds_total": round(job.phash_seconds_total, 3),
                "convert_seconds_total": round(job.convert_seconds_total, 3),
            },
            "progress": {
                "discovery": job.discovered_files,
                "conversion": percent(job.converted_files, total),
                "scan": percent(job.scanned_files, total),
                "processed": percent(job.processed_files, total),
            },
            "max_workers": MAX_WORKERS,
            "default_workers": DEFAULT_WORKERS,
        }


def job_snapshot(job: ScanJob) -> dict:
    with jobs_lock:
        return job_to_dict_unlocked(job)


def persist_job_snapshot(snapshot: dict) -> None:
    with db_lock, sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute(
            """
            INSERT INTO jobs(id, started_at, snapshot, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                snapshot = excluded.snapshot,
                updated_at = excluded.updated_at
            """,
            (snapshot["id"], snapshot["started_at"], json.dumps(snapshot), time.time()),
        )


def persist_job(job: ScanJob) -> None:
    persist_job_snapshot(job_snapshot(job))


def restore_job(snapshot: dict) -> ScanJob:
    job = ScanJob(id=snapshot["id"])
    scalar_fields = {
        "status",
        "message",
        "started_at",
        "finished_at",
        "total_files",
        "discovered_files",
        "converted_files",
        "scanned_files",
        "processed_files",
        "failed_files",
        "phash_skipped_files",
        "cache_hit_files",
        "queue_size",
        "active_workers",
        "discovery_done",
        "errors",
        "logs",
        "cancel_requested",
        "sha_seconds_total",
        "decode_seconds_total",
        "phash_prepare_seconds_total",
        "phash_dct_seconds_total",
        "phash_seconds_total",
        "convert_seconds_total",
        "failed_records",
    }
    for field_name in scalar_fields:
        if field_name in snapshot:
            setattr(job, field_name, snapshot[field_name])
    timings = snapshot.get("timings") or {}
    job.sha_seconds_total = timings.get("sha_seconds_total", job.sha_seconds_total)
    job.decode_seconds_total = timings.get("decode_seconds_total", job.decode_seconds_total)
    job.phash_prepare_seconds_total = timings.get(
        "phash_prepare_seconds_total",
        job.phash_prepare_seconds_total,
    )
    job.phash_dct_seconds_total = timings.get("phash_dct_seconds_total", job.phash_dct_seconds_total)
    job.phash_seconds_total = timings.get("phash_seconds_total", job.phash_seconds_total)
    job.convert_seconds_total = timings.get("convert_seconds_total", job.convert_seconds_total)
    if job.status == "running":
        job.status = "cancelled"
        job.message = "后端重启，之前的扫描任务已停止"
        job.finished_at = job.finished_at or time.time()
        job.active_workers = 0
        job.queue_size = 0
        job.cancel_requested = True
    for group_data in snapshot.get("duplicates", []):
        files = [record_from_dict(item) for item in group_data.get("files", [])]
        group = DuplicateGroup(group_data["key_type"], group_data["key"], files)
        job.duplicates.append(group)
        for record in files:
            if record.sha256:
                job.sha_index.setdefault(record.sha256, []).append(record)
            if record.phash:
                job.phash_index.setdefault(record.phash, []).append(record)
    job.failed_records = [
        record_from_dict(item)
        for item in snapshot.get("failed_records", [])
        if item.get("path")
    ]
    return job


def load_persisted_jobs() -> None:
    with db_lock, sqlite3.connect(DB_PATH, timeout=30) as conn:
        rows = conn.execute(
            "SELECT snapshot FROM jobs ORDER BY updated_at DESC LIMIT 10"
        ).fetchall()
    with jobs_lock:
        for (snapshot_json,) in rows:
            try:
                job = restore_job(json.loads(snapshot_json))
                jobs[job.id] = job
            except Exception:
                continue


init_db()
load_persisted_jobs()


def percent(done: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(done / total * 100, 1)


def set_job(job: ScanJob, **updates) -> None:
    with jobs_lock:
        for key, value in updates.items():
            setattr(job, key, value)


def append_log(job: ScanJob, message: str) -> None:
    timestamp = time.strftime("%H:%M:%S")
    with jobs_lock:
        job.logs.append(f"{timestamp} {message}")
        if len(job.logs) > 500:
            del job.logs[:-500]


def append_error(job: ScanJob, message: str) -> None:
    with jobs_lock:
        job.errors.append(message)
        if len(job.errors) > 500:
            del job.errors[:-500]
        job.failed_files += 1
    append_log(job, f"错误: {message}")


def add_timing(job: ScanJob, field_name: str, seconds: float) -> None:
    with jobs_lock:
        setattr(job, field_name, getattr(job, field_name) + seconds)


def is_cancelled(job: ScanJob) -> bool:
    with jobs_lock:
        return job.cancel_requested


def ensure_not_cancelled(job: ScanJob) -> None:
    if is_cancelled(job):
        raise ScanCancelled()


def cancel_pending(futures) -> None:
    for future in futures:
        future.cancel()


def put_queue(scan_queue: queue.Queue, item, job: ScanJob) -> None:
    while True:
        ensure_not_cancelled(job)
        try:
            scan_queue.put(item, timeout=0.25)
            with jobs_lock:
                job.queue_size = scan_queue.qsize()
            return
        except queue.Full:
            with jobs_lock:
                job.queue_size = scan_queue.qsize()


def get_queue(scan_queue: queue.Queue, job: ScanJob):
    while True:
        ensure_not_cancelled(job)
        try:
            item = scan_queue.get(timeout=0.25)
            with jobs_lock:
                job.queue_size = scan_queue.qsize()
            return item
        except queue.Empty:
            with jobs_lock:
                job.queue_size = scan_queue.qsize()


def put_sentinels(scan_queue: queue.Queue, worker_count: int, sentinel) -> None:
    for _ in range(worker_count):
        for _attempt in range(20):
            try:
                scan_queue.put(sentinel, timeout=0.25)
                break
            except queue.Full:
                continue


def drain_queue(scan_queue: queue.Queue) -> None:
    while True:
        try:
            scan_queue.get_nowait()
            scan_queue.task_done()
        except queue.Empty:
            return


def index_record(job: ScanJob, key_type: str, key: Optional[str], record: ImageRecord) -> None:
    if not key:
        return

    index = job.sha_index if key_type == "sha256" else job.phash_index
    files = index.setdefault(key, [])
    files.append(record)
    if len(files) < 2:
        return

    for group in job.duplicates:
        if group.key_type == key_type and group.key == key:
            group.files = sorted(files, key=lambda item: item.path)
            return
    job.duplicates.append(DuplicateGroup(key_type, key, sorted(files, key=lambda item: item.path)))
    job.duplicates.sort(key=lambda group: (-len(group.files), group.key_type, group.key))


def index_record_unlocked(job: ScanJob, key_type: str, key: Optional[str], record: ImageRecord) -> None:
    if not key:
        return
    index = job.sha_index if key_type == "sha256" else job.phash_index
    files = index.setdefault(key, [])
    files.append(record)
    if len(files) < 2:
        return
    for group in job.duplicates:
        if group.key_type == key_type and group.key == key:
            group.files = sorted(files, key=lambda item: item.path)
            return
    job.duplicates.append(DuplicateGroup(key_type, key, sorted(files, key=lambda item: item.path)))
    job.duplicates.sort(key=lambda group: (-len(group.files), group.key_type, group.key))


def add_record(job: ScanJob, record: ImageRecord) -> None:
    with jobs_lock:
        if record.scan_error:
            job.errors.append(f"扫描失败 {record.path}: {record.scan_error}")
            job.failed_records.append(record)
            job.failed_files += 1
        else:
            job.scanned_files += 1
            if record.cache_hit:
                job.cache_hit_files += 1
            if record.phash_skipped:
                job.phash_skipped_files += 1
            index_record_unlocked(job, "sha256", record.sha256, record)
            index_record_unlocked(job, "phash", record.phash, record)
    cache_put(record)


def remove_deleted_records_from_jobs(paths: List[str]) -> List[dict]:
    deleted = set(paths)
    snapshots = []
    with jobs_lock:
        for job in jobs.values():
            job.failed_records = [
                record for record in job.failed_records if record.path not in deleted
            ]
            job.errors = [
                error
                for error in job.errors
                if not any(path in error for path in deleted)
            ]
            if job.failed_records:
                job.failed_files = len(job.failed_records)
            elif job.failed_files:
                job.failed_files = len([error for error in job.errors if error.startswith("扫描失败 ")])
            next_groups = []
            for group in job.duplicates:
                files = [record for record in group.files if record.path not in deleted]
                if len(files) > 1:
                    group.files = files
                    next_groups.append(group)
            job.duplicates = sorted(next_groups, key=lambda group: (-len(group.files), group.key_type, group.key))
            job.sha_index = {}
            job.phash_index = {}
            for group in job.duplicates:
                for record in group.files:
                    if record.sha256:
                        job.sha_index.setdefault(record.sha256, []).append(record)
                    if record.phash:
                        job.phash_index.setdefault(record.phash, []).append(record)
            snapshots.append(job_to_dict_unlocked(job))
    return snapshots


def is_allowed_path(path: Path) -> bool:
    resolved = path.resolve()
    return any(resolved == root or root in resolved.parents for root in SCAN_ROOTS)


def validate_user_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser().resolve()
    if not is_allowed_path(path):
        allowed = ", ".join(str(root) for root in SCAN_ROOTS)
        raise HTTPException(status_code=403, detail=f"路径不在允许范围内: {allowed}")
    return path


def is_allowed_source_path(path: Path) -> bool:
    resolved = path.resolve()
    return any(resolved == root or root in resolved.parents for root in SOURCE_ROOTS)


def validate_source_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser().resolve()
    if not is_allowed_source_path(path):
        allowed = ", ".join(str(root) for root in SOURCE_ROOTS)
        raise HTTPException(status_code=403, detail=f"源文件路径不在允许范围内: {allowed}")
    return path


def discover_images(directories: List[str], job: ScanJob, scan_queue: queue.Queue, worker_count: int, sentinel) -> None:
    seen = set() if len(directories) > 1 else None
    for raw_dir in directories:
        ensure_not_cancelled(job)
        directory = validate_user_path(raw_dir)
        if not directory.exists() or not directory.is_dir():
            raise HTTPException(status_code=400, detail=f"目录不存在: {directory}")
        append_log(job, f"开始发现目录: {directory}")
        for root, _, names in os.walk(directory):
            ensure_not_cancelled(job)
            for name in names:
                ensure_not_cancelled(job)
                path = Path(root) / name
                if path.suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
                resolved = path.resolve()
                if seen is not None:
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                put_queue(scan_queue, resolved, job)
                with jobs_lock:
                    job.discovered_files += 1
                    job.total_files = job.discovered_files
                    job.message = f"正在扫描，已发现 {job.discovered_files} 个图片文件"

    with jobs_lock:
        job.discovery_done = True
        job.total_files = job.discovered_files
        job.message = f"目录发现完成，正在处理 {job.discovered_files} 个图片文件"
    append_log(job, f"目录发现完成: {job.discovered_files} 个图片文件")
    for _ in range(worker_count):
        put_queue(scan_queue, sentinel, job)


def convert_image(path: Path) -> Optional[Path]:
    ext = path.suffix.lower()
    if ext not in CONVERT_EXTENSIONS:
        return None

    out_path = path.with_suffix(".jxl")
    if out_path.exists() and out_path.stat().st_mtime >= path.stat().st_mtime:
        return out_path

    tmp_out = out_path.with_suffix(out_path.suffix + f".{uuid.uuid4().hex}.tmp")
    if ext in {".jpg", ".jpeg"}:
        cmd = ["cjxl", str(path), str(tmp_out), "--lossless_jpeg=1", "-e", "7"]
    else:
        cmd = ["cjxl", str(path), str(tmp_out), "-q", "90", "-e", "7"]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=600)
        tmp_out.replace(out_path)
        return out_path
    except subprocess.CalledProcessError as exc:
        if tmp_out.exists():
            tmp_out.unlink(missing_ok=True)
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise RuntimeError(detail[-500:])
    except Exception:
        if tmp_out.exists():
            tmp_out.unlink(missing_ok=True)
        raise


def jxl_import_target(source_path: Path, source_sha256: str) -> Path:
    root = SCAN_ROOTS[0]
    directory = root / source_sha256[:2] / source_sha256[2:4]
    base_name = f"{source_path.stem}.jxl"
    target = directory / base_name
    if not target.exists():
        return target
    target = directory / f"{source_path.stem}-{source_sha256[:12]}.jxl"
    if not target.exists():
        return target
    return directory / f"{source_path.stem}-{uuid.uuid4().hex[:8]}.jxl"


def convert_source_to_jxl(source_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = target_path.with_suffix(target_path.suffix + f".{uuid.uuid4().hex}.tmp")
    ext = source_path.suffix.lower()
    if ext == ".jxl":
        shutil.copy2(source_path, tmp_out)
    elif ext in {".jpg", ".jpeg"}:
        subprocess.run(
            ["cjxl", str(source_path), str(tmp_out), "--lossless_jpeg=1", "-e", "7"],
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
    else:
        subprocess.run(
            ["cjxl", str(source_path), str(tmp_out), "-q", "90", "-e", "7"],
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
    tmp_out.replace(target_path)


def import_one_source_path(source_path: Path) -> dict:
    if not source_path.exists():
        raise FileNotFoundError(f"源文件不存在: {source_path}")
    if not source_path.is_file():
        raise IsADirectoryError(f"请输入具体源文件路径，不是目录: {source_path}")
    if source_path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ValueError(f"不支持导入非图片文件: {source_path}")

    source_record = scan_image(source_path, ignore_cache=True)
    if source_record.scan_error:
        raise RuntimeError(f"源文件扫描失败: {source_record.scan_error}")

    duplicates = cache_find_duplicates(source_record.sha256, source_record.phash)
    if duplicates:
        return {
            "status": "duplicate",
            "source": source_record.__dict__,
            "duplicates": duplicates,
        }

    target_path = jxl_import_target(source_path, source_record.sha256 or uuid.uuid4().hex)
    convert_source_to_jxl(source_path, target_path)
    imported_record = scan_image(target_path, converted_from=source_path, ignore_cache=True)
    if imported_record.scan_error:
        target_path.unlink(missing_ok=True)
        raise RuntimeError(f"导入后扫描失败: {imported_record.scan_error}")
    cache_put(imported_record)
    return {
        "status": "imported",
        "source": source_record.__dict__,
        "imported": imported_record.__dict__,
        "target": str(target_path),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=1)
def dct_matrix(size: int = 32) -> np.ndarray:
    matrix = np.zeros((size, size), dtype=np.float32)
    factor = np.pi / (2 * size)
    scale0 = np.sqrt(1 / size)
    scale = np.sqrt(2 / size)
    for row in range(size):
        row_scale = scale0 if row == 0 else scale
        for column in range(size):
            matrix[row, column] = row_scale * np.cos((2 * column + 1) * row * factor)
    return matrix


def load_vips_image(path: Path) -> pyvips.Image:
    try:
        return pyvips.Image.new_from_file(str(path), access="sequential").autorot()
    except pyvips.Error:
        if path.suffix.lower() != ".jxl":
            raise

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        subprocess.run(
            ["djxl", str(path), str(tmp_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        return pyvips.Image.new_from_file(str(tmp_path), access="sequential").autorot()
    finally:
        tmp_path.unlink(missing_ok=True)


def phash_from_memory(memory: bytes) -> tuple[str, float]:
    dct_started = time.perf_counter()
    pixels = np.frombuffer(memory, dtype=np.uint8).reshape(32, 32).astype(np.float32)
    dct = dct_matrix(32)
    coeffs = dct @ pixels @ dct.T
    block = coeffs[:8, :8].copy()
    median = np.median(block[1:, 1:])
    bits = (block > median).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    dct_seconds = time.perf_counter() - dct_started
    return f"{value:016x}", dct_seconds


def materialize_phash_source(image: pyvips.Image) -> bytes:
    if image.bands > 3:
        image = image.extract_band(0, n=3)
    if image.bands > 1:
        image = image.colourspace("b-w")
    image = image.cast("uchar")
    return image.write_to_memory()


def phash_vips_image(image: pyvips.Image) -> tuple[str, float, float]:
    prepare_started = time.perf_counter()
    image = image.thumbnail_image(32, height=32, size=pyvips.enums.Size.FORCE)
    memory = materialize_phash_source(image)
    prepare_seconds = time.perf_counter() - prepare_started

    phash, dct_seconds = phash_from_memory(memory)
    return phash, prepare_seconds, dct_seconds


def phash_vips_file(path: Path) -> tuple[str, int, int, float, float, float]:
    open_started = time.perf_counter()
    original = load_vips_image(path)
    width = original.width
    height = original.height
    open_seconds = time.perf_counter() - open_started

    prepare_started = time.perf_counter()
    try:
        image = pyvips.Image.thumbnail(
            str(path),
            32,
            height=32,
            size=pyvips.enums.Size.FORCE,
            auto_rotate=True,
        )
        memory = materialize_phash_source(image)
        prepare_seconds = time.perf_counter() - prepare_started
        phash, dct_seconds = phash_from_memory(memory)
        return phash, width, height, open_seconds, prepare_seconds, dct_seconds
    except pyvips.Error:
        phash, prepare_seconds, dct_seconds = phash_vips_image(original)
        return phash, width, height, open_seconds, prepare_seconds, dct_seconds


def sha_already_seen(job: ScanJob, sha256: Optional[str]) -> bool:
    if not sha256:
        return False
    with jobs_lock:
        return sha256 in job.sha_index


def scan_image(
    path: Path,
    converted_from: Optional[Path] = None,
    job: Optional[ScanJob] = None,
    ignore_cache: bool = False,
) -> ImageRecord:
    stat = path.stat()
    record = ImageRecord(
        path=str(path),
        size=stat.st_size,
        modified_at=stat.st_mtime,
        converted_from=str(converted_from) if converted_from else None,
    )
    try:
        cached = None if ignore_cache else cache_get(path, record.size, record.modified_at)
        if cached:
            record.sha256 = cached["sha256"]
            record.phash = cached["phash"]
            record.width = cached["width"]
            record.height = cached["height"]
            record.cache_hit = True
            return record

        started = time.perf_counter()
        record.sha256 = sha256_file(path)
        record.sha_seconds = time.perf_counter() - started
        if job is not None and sha_already_seen(job, record.sha256):
            record.phash_skipped = True
            try:
                open_started = time.perf_counter()
                image = load_vips_image(path)
                record.width = image.width
                record.height = image.height
                record.decode_seconds = time.perf_counter() - open_started
            except Exception:
                pass
            return record

        (
            record.phash,
            record.width,
            record.height,
            record.decode_seconds,
            record.phash_prepare_seconds,
            record.phash_dct_seconds,
        ) = phash_vips_file(path)
        record.phash_seconds = (
            record.decode_seconds + record.phash_prepare_seconds + record.phash_dct_seconds
        )
    except Exception as exc:
        record.scan_error = str(exc)
    return record


def process_image_path(job: ScanJob, path: Path, convert: bool, full_scan: bool) -> None:
    ensure_not_cancelled(job)
    file_started = time.perf_counter()
    targets = [(path, None)]
    convert_seconds = 0.0
    if convert and path.suffix.lower() in CONVERT_EXTENSIONS:
        try:
            started = time.perf_counter()
            converted = convert_image(path)
            convert_seconds = time.perf_counter() - started
            add_timing(job, "convert_seconds_total", convert_seconds)
            if converted:
                targets.append((converted, path))
                with jobs_lock:
                    job.converted_files += 1
                if convert_seconds >= SLOW_STEP_SECONDS:
                    append_log(job, f"转换耗时 {convert_seconds:.3f}s: {path} -> {converted}")
        except Exception as exc:
            append_error(job, f"转换失败 {path}: {exc}")

    for target, source in targets:
        ensure_not_cancelled(job)
        record = scan_image(target, source, job, ignore_cache=full_scan)
        add_timing(job, "sha_seconds_total", record.sha_seconds)
        add_timing(job, "decode_seconds_total", record.decode_seconds)
        add_timing(job, "phash_prepare_seconds_total", record.phash_prepare_seconds)
        add_timing(job, "phash_dct_seconds_total", record.phash_dct_seconds)
        add_timing(job, "phash_seconds_total", record.phash_seconds)
        if record.sha_seconds >= SLOW_STEP_SECONDS:
            append_log(job, f"SHA256文件读取+哈希耗时 {record.sha_seconds:.3f}s: {target}")
        if record.decode_seconds >= SLOW_STEP_SECONDS:
            append_log(job, f"图片打开(懒加载)耗时 {record.decode_seconds:.3f}s: {target}")
        if record.phash_prepare_seconds >= SLOW_STEP_SECONDS:
            append_log(job, f"pHash物化解码/灰度/缩放耗时 {record.phash_prepare_seconds:.3f}s: {target}")
        if record.phash_dct_seconds >= SLOW_STEP_SECONDS:
            append_log(job, f"pHash DCT计算耗时 {record.phash_dct_seconds:.3f}s: {target}")
        if record.phash_skipped:
            append_log(job, f"SHA256已命中重复，跳过pHash解码: {target}")
        if record.phash_seconds >= SLOW_STEP_SECONDS:
            append_log(
                job,
                (
                    f"pHash总耗时 {record.phash_seconds:.3f}s "
                    f"(open_lazy={record.decode_seconds:.3f}s, "
                    f"materialize_resize={record.phash_prepare_seconds:.3f}s, "
                    f"dct={record.phash_dct_seconds:.3f}s): {target}"
                ),
            )
        add_record(job, record)

    elapsed = time.perf_counter() - file_started
    with jobs_lock:
        job.processed_files += 1
        processed = job.processed_files
        scanned = job.scanned_files
        sha_total = job.sha_seconds_total
        decode_total = job.decode_seconds_total
        prepare_total = job.phash_prepare_seconds_total
        dct_total = job.phash_dct_seconds_total
        phash_total = job.phash_seconds_total
        phash_skipped = job.phash_skipped_files
        cache_hits = job.cache_hit_files
        convert_total = job.convert_seconds_total
        job.message = (
            f"正在处理图片，已处理 {job.processed_files} / "
            f"{job.total_files if job.discovery_done else job.discovered_files}"
        )
    if elapsed >= SLOW_FILE_SECONDS:
        append_log(job, f"单文件总耗时 {elapsed:.3f}s: {path}")
    if processed and processed % LOG_EVERY_FILES == 0:
        append_log(
            job,
            (
                f"耗时汇总 processed={processed} scanned={scanned} "
                f"cache_hits={cache_hits} "
                f"phash_skipped={phash_skipped} "
                f"sha_avg={sha_total / max(scanned, 1):.3f}s "
                f"open_lazy_avg={decode_total / max(scanned, 1):.3f}s "
                f"materialize_resize_avg={prepare_total / max(scanned, 1):.3f}s "
                f"dct_avg={dct_total / max(scanned, 1):.3f}s "
                f"phash_total_avg={phash_total / max(scanned, 1):.3f}s "
                f"convert_total={convert_total:.1f}s"
            ),
        )
    if processed % 25 == 0:
        persist_job(job)


def scan_worker(job: ScanJob, scan_queue: queue.Queue, sentinel, convert: bool, full_scan: bool) -> None:
    with jobs_lock:
        job.active_workers += 1
    try:
        while True:
            item = get_queue(scan_queue, job)
            try:
                if item is sentinel:
                    return
                process_image_path(job, item, convert, full_scan)
            finally:
                scan_queue.task_done()
    except ScanCancelled:
        return
    finally:
        with jobs_lock:
            job.active_workers -= 1
            job.queue_size = scan_queue.qsize()


def build_duplicate_groups(records: List[ImageRecord]) -> List[DuplicateGroup]:
    groups: List[DuplicateGroup] = []
    used_keys = set()
    by_hash: Dict[str, List[ImageRecord]] = {}
    by_phash: Dict[str, List[ImageRecord]] = {}

    for record in records:
        if record.sha256:
            by_hash.setdefault(record.sha256, []).append(record)
        if record.phash:
            by_phash.setdefault(record.phash, []).append(record)

    for key, files in by_hash.items():
        if len(files) > 1:
            groups.append(DuplicateGroup("sha256", key, sorted(files, key=lambda item: item.path)))
            used_keys.update((item.path, "sha256") for item in files)

    for key, files in by_phash.items():
        if len(files) > 1:
            unique = sorted(files, key=lambda item: item.path)
            if all((item.path, "sha256") in used_keys for item in unique):
                continue
            groups.append(DuplicateGroup("phash", key, unique))

    return sorted(groups, key=lambda group: (-len(group.files), group.key_type, group.key))


def run_import_directory(job: ScanJob, directory: Path, worker_count: int) -> None:
    import_queue: queue.Queue = queue.Queue(maxsize=max(256, worker_count * 8))
    sentinel = object()
    producer_thread: Optional[threading.Thread] = None
    worker_threads: List[threading.Thread] = []
    producer_error: List[Exception] = []

    def producer() -> None:
        try:
            for root, _, names in os.walk(directory):
                ensure_not_cancelled(job)
                for name in names:
                    ensure_not_cancelled(job)
                    path = Path(root) / name
                    if path.suffix.lower() not in IMAGE_EXTENSIONS:
                        continue
                    put_queue(import_queue, path, job)
                    with jobs_lock:
                        job.discovered_files += 1
                        job.total_files = job.discovered_files
                        job.message = f"正在导入，已发现 {job.discovered_files} 个源图片"
        except ScanCancelled:
            pass
        except Exception as exc:
            producer_error.append(exc)
        finally:
            with jobs_lock:
                job.discovery_done = True
            for _ in range(worker_count):
                put_queue(import_queue, sentinel, job)

    def worker() -> None:
        with jobs_lock:
            job.active_workers += 1
        try:
            while True:
                item = get_queue(import_queue, job)
                try:
                    if item is sentinel:
                        return
                    try:
                        result = import_one_source_path(item)
                        with jobs_lock:
                            job.processed_files += 1
                            if result["status"] == "duplicate":
                                job.cache_hit_files += 1
                                job.message = f"正在导入，已跳过 {job.cache_hit_files} 个重复源图片"
                            else:
                                record = record_from_dict(result["imported"])
                                job.converted_files += 1
                                job.scanned_files += 1
                                index_record_unlocked(job, "sha256", record.sha256, record)
                                index_record_unlocked(job, "phash", record.phash, record)
                                job.message = f"正在导入，已导入 {job.converted_files} 个源图片"
                        if result["status"] == "imported":
                            append_log(job, f"导入成功: {item} -> {result['target']}")
                        else:
                            append_log(job, f"发现重复，跳过导入: {item}")
                    except Exception as exc:
                        append_error(job, f"导入失败 {item}: {exc}")
                        with jobs_lock:
                            job.processed_files += 1
                    if job.processed_files and job.processed_files % 25 == 0:
                        persist_job(job)
                finally:
                    import_queue.task_done()
        except ScanCancelled:
            return
        finally:
            with jobs_lock:
                job.active_workers -= 1
                job.queue_size = import_queue.qsize()

    try:
        set_job(job, status="running", message=f"正在发现源目录: {directory}")
        append_log(job, f"开始导入源目录: {directory} workers={worker_count}")
        persist_job(job)

        producer_thread = threading.Thread(target=producer, name=f"import-producer-{job.id}", daemon=True)
        producer_thread.start()
        for index in range(worker_count):
            thread = threading.Thread(target=worker, name=f"import-worker-{job.id}-{index}", daemon=True)
            worker_threads.append(thread)
            thread.start()

        while producer_thread.is_alive() or any(thread.is_alive() for thread in worker_threads):
            ensure_not_cancelled(job)
            if producer_error:
                raise producer_error[0]
            with jobs_lock:
                job.queue_size = import_queue.qsize()
            time.sleep(0.25)

        if producer_error:
            raise producer_error[0]

        set_job(
            job,
            status="done",
            message=(
                f"导入完成，导入 {job.converted_files} 个，"
                f"重复跳过 {job.cache_hit_files} 个，失败 {job.failed_files} 个"
            ),
            discovery_done=True,
            finished_at=time.time(),
        )
        append_log(
            job,
            (
                f"导入完成 discovered={job.discovered_files} processed={job.processed_files} "
                f"imported={job.converted_files} duplicates={job.cache_hit_files} failed={job.failed_files}"
            ),
        )
        persist_job(job)
    except ScanCancelled:
        set_job(job, status="cancelled", message="已停止导入任务", finished_at=time.time())
        append_log(job, f"导入停止 processed={job.processed_files} imported={job.converted_files}")
        persist_job(job)
    except Exception as exc:
        set_job(job, status="failed", message=str(exc), finished_at=time.time())
        append_log(job, f"导入失败: {exc}")
        persist_job(job)
    finally:
        if is_cancelled(job) or producer_error:
            drain_queue(import_queue)
            put_sentinels(import_queue, worker_count, sentinel)
        if producer_thread:
            producer_thread.join(timeout=2)
        for thread in worker_threads:
            thread.join(timeout=2)
        with jobs_lock:
            job.queue_size = import_queue.qsize()
            if job.cancel_requested:
                job.status = "cancelled"
                job.message = "已停止导入任务"
                job.finished_at = job.finished_at or time.time()
        persist_job(job)


def run_scan(job: ScanJob, request: ScanRequest) -> None:
    worker_threads: List[threading.Thread] = []
    producer_thread: Optional[threading.Thread] = None
    producer_error: List[Exception] = []
    sentinel = object()
    worker_count = request.workers or DEFAULT_WORKERS
    scan_queue: queue.Queue = queue.Queue(maxsize=max(256, worker_count * 8))

    def producer() -> None:
        try:
            discover_images(request.directories, job, scan_queue, worker_count, sentinel)
        except ScanCancelled:
            pass
        except Exception as exc:
            producer_error.append(exc)
            with jobs_lock:
                job.discovery_done = True
                job.message = f"目录发现失败: {exc}"
            put_sentinels(scan_queue, worker_count, sentinel)

    try:
        ensure_not_cancelled(job)
        set_job(job, status="running", message="正在发现并处理图片")
        append_log(
            job,
            (
                f"任务开始 workers={worker_count} convert={request.convert} full_scan={request.full_scan} "
                f"queue_max={scan_queue.maxsize} dirs={', '.join(request.directories)}"
            ),
        )
        persist_job(job)

        producer_thread = threading.Thread(target=producer, name=f"scan-producer-{job.id}", daemon=True)
        producer_thread.start()

        for index in range(worker_count):
            thread = threading.Thread(
                target=scan_worker,
                args=(job, scan_queue, sentinel, request.convert, request.full_scan),
                name=f"scan-worker-{job.id}-{index}",
                daemon=True,
            )
            worker_threads.append(thread)
            thread.start()

        while producer_thread.is_alive() or any(thread.is_alive() for thread in worker_threads):
            ensure_not_cancelled(job)
            if producer_error:
                raise producer_error[0]
            with jobs_lock:
                job.queue_size = scan_queue.qsize()
            time.sleep(0.25)

        if producer_error:
            raise producer_error[0]

        set_job(
            job,
            status="done",
            message=f"扫描完成，发现 {len(job.duplicates)} 组重复图片",
            finished_at=time.time(),
        )
        append_log(
            job,
            (
                f"任务完成 discovered={job.discovered_files} processed={job.processed_files} "
                f"scanned={job.scanned_files} converted={job.converted_files} duplicates={len(job.duplicates)}"
            ),
        )
        persist_job(job)
    except ScanCancelled:
        set_job(job, status="cancelled", message="已停止扫描任务", finished_at=time.time())
        append_log(job, f"任务停止 processed={job.processed_files} scanned={job.scanned_files}")
        persist_job(job)
    except Exception as exc:
        set_job(job, status="failed", message=str(exc), finished_at=time.time())
        append_log(job, f"任务失败: {exc}")
        persist_job(job)
    finally:
        if is_cancelled(job) or producer_error:
            drain_queue(scan_queue)
            put_sentinels(scan_queue, worker_count, sentinel)
        if producer_thread:
            producer_thread.join(timeout=2)
        for thread in worker_threads:
            thread.join(timeout=2)
        if is_cancelled(job):
            drain_queue(scan_queue)
        with jobs_lock:
            job.queue_size = scan_queue.qsize()
            if job.cancel_requested:
                job.status = "cancelled"
                job.message = "已停止扫描任务"
                job.finished_at = job.finished_at or time.time()
        persist_job(job)


@app.post("/api/scan")
async def start_scan(request: ScanRequest):
    job = ScanJob(id=uuid.uuid4().hex)
    with jobs_lock:
        jobs[job.id] = job
    asyncio.get_running_loop().run_in_executor(executor, run_scan, job, request)
    return {"job_id": job.id}


@app.get("/api/jobs/latest")
async def latest_job():
    with jobs_lock:
        if not jobs:
            raise HTTPException(status_code=404, detail="暂无扫描任务")
        job = max(jobs.values(), key=lambda item: item.started_at)
    return job_snapshot(job)


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在")
    return job_snapshot(job)


@app.post("/api/jobs/{job_id}/stop")
async def stop_job(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在")
        if job.status in {"done", "failed", "cancelled"}:
            pass
        else:
            job.cancel_requested = True
            job.message = "正在停止扫描任务"
            job.logs.append(f"{time.strftime('%H:%M:%S')} 收到停止请求")
            if len(job.logs) > 500:
                del job.logs[:-500]
    return job_snapshot(job)


@app.post("/api/import")
async def import_source_file(request: ImportRequest):
    source_path = validate_source_path(request.path)
    if not source_path.exists():
        raise HTTPException(status_code=404, detail=f"源文件不存在: {source_path}")
    if source_path.is_dir():
        job = ScanJob(id=uuid.uuid4().hex, message="等待开始导入")
        worker_count = request.workers or DEFAULT_WORKERS
        with jobs_lock:
            jobs[job.id] = job
        asyncio.get_running_loop().run_in_executor(executor, run_import_directory, job, source_path, worker_count)
        return {"status": "queued", "job_id": job.id}
    try:
        return import_one_source_path(source_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IsADirectoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise HTTPException(status_code=500, detail=f"转换 JXL 失败: {detail[-500:]}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"导入失败: {exc}") from exc


@app.get("/api/thumbnail")
async def thumbnail(background_tasks: BackgroundTasks, path: str = Query(...)):
    image_path = validate_user_path(path)
    if not image_path.exists() or image_path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise HTTPException(status_code=404, detail="图片不存在")

    try:
        image = load_vips_image(image_path)
        thumb = image.thumbnail_image(360)
        with tempfile.NamedTemporaryFile(suffix=".webp", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        thumb.webpsave(str(tmp_path), Q=80, effort=4)
        background_tasks.add_task(tmp_path.unlink, missing_ok=True)
        return FileResponse(tmp_path, media_type="image/webp", filename="thumbnail.webp")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/preview")
async def preview(background_tasks: BackgroundTasks, path: str = Query(...)):
    image_path = validate_user_path(path)
    if not image_path.exists() or image_path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise HTTPException(status_code=404, detail="图片不存在")

    try:
        image = load_vips_image(image_path)
        preview_image = image.thumbnail_image(1600)
        with tempfile.NamedTemporaryFile(suffix=".webp", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        preview_image.webpsave(str(tmp_path), Q=86, effort=4)
        background_tasks.add_task(tmp_path.unlink, missing_ok=True)
        return FileResponse(tmp_path, media_type="image/webp", filename="preview.webp")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/api/files")
async def delete_files(request: DeleteRequest):
    deleted = []
    skipped = []
    for raw_path in request.paths:
        path = validate_user_path(raw_path)
        if not path.exists() or not path.is_file():
            skipped.append(str(path))
            continue
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"不允许删除非图片文件: {path}")
        path.unlink()
        deleted.append(str(path))
    cache_delete_paths(deleted)
    snapshots = remove_deleted_records_from_jobs(deleted)
    for snapshot in snapshots:
        persist_job_snapshot(snapshot)
    return {"deleted": deleted, "skipped": skipped}


@app.get("/api/health")
async def health():
    tools = {
        "cjxl": shutil.which("cjxl") is not None,
        "djxl": shutil.which("djxl") is not None,
        "vips": shutil.which("vips") is not None,
    }
    status_code = 200 if all(tools.values()) else 503
    return Response(
        content=json.dumps({"ok": all(tools.values()), "tools": tools}),
        media_type="application/json",
        status_code=status_code,
    )
