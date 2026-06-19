import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { Check, CheckSquare, Eye, FolderSearch, Loader2, Square, Trash2, X } from "lucide-react";
import "./styles.css";

const API = "/api";
const LAST_JOB_KEY = "imageScanner.lastJobId";
const SCAN_SETTINGS_KEY = "imageScanner.scanSettings";
const IMPORT_SETTINGS_KEY = "imageScanner.importSettings";

function loadScanSettings() {
  try {
    return JSON.parse(window.localStorage.getItem(SCAN_SETTINGS_KEY) || "{}");
  } catch {
    return {};
  }
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatDate(seconds) {
  if (!seconds) return "-";
  return new Date(seconds * 1000).toLocaleString();
}

function getDisplaySize(file) {
  return file?.total_size || file?.size || 0;
}

function isLivePhoto(file) {
  return Boolean(file?.live_photo_video_path);
}

function getFailedPaths(job) {
  const paths = new Set();
  (job?.failed_records || []).forEach((record) => {
    if (record?.path) paths.add(record.path);
  });
  (job?.errors || []).forEach((error) => {
    const match = String(error).match(/^扫描失败 (.*?): /);
    if (match?.[1]) paths.add(match[1]);
  });
  return Array.from(paths);
}

function App() {
  const initialSettings = useMemo(loadScanSettings, []);
  const initialImportSettings = useMemo(() => {
    try {
      return JSON.parse(window.localStorage.getItem(IMPORT_SETTINGS_KEY) || "{}");
    } catch {
      return {};
    }
  }, []);
  const [directories, setDirectories] = useState(initialSettings.directories || "");
  const [convert, setConvert] = useState(Boolean(initialSettings.convert));
  const [fullScan, setFullScan] = useState(Boolean(initialSettings.fullScan));
  const [workers, setWorkers] = useState(initialSettings.workers || "");
  const [sourceFile, setSourceFile] = useState(initialImportSettings.sourceFile || "");
  const [serverSettings, setServerSettings] = useState({
    scan_roots: [],
    source_roots: [],
    supported_extensions: [],
    supports_live_photo: false,
  });
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState(null);
  const [jobId, setJobId] = useState(null);
  const [job, setJob] = useState(null);
  const [selected, setSelected] = useState(() => new Set());
  const [error, setError] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [previewFile, setPreviewFile] = useState(null);

  const directoryList = useMemo(
    () => directories.split("\n").map((item) => item.trim()).filter(Boolean),
    [directories]
  );

  useEffect(() => {
    let stopped = false;
    const loadSettings = async () => {
      try {
        const response = await fetch(`${API}/settings`);
        if (!response.ok) return;
        const data = await response.json();
        if (stopped) return;
        setServerSettings(data);
        if (!initialSettings.directories && data.scan_roots?.length) {
          setDirectories(data.scan_roots.join("\n"));
        }
        if (!initialImportSettings.sourceFile) {
          const firstSource = data.source_roots?.[0] || data.scan_roots?.[0] || "";
          setSourceFile(firstSource);
        }
      } catch {
        // Keep local defaults if settings API is unavailable.
      }
    };

    loadSettings();
    return () => {
      stopped = true;
    };
  }, [initialImportSettings.sourceFile, initialSettings.directories]);

  useEffect(() => {
    let stopped = false;
    const restoreJob = async () => {
      const storedJobId = window.localStorage.getItem(LAST_JOB_KEY);
      if (storedJobId) {
        try {
          const response = await fetch(`${API}/jobs/${storedJobId}`);
          if (response.ok) {
            const data = await response.json();
            if (!stopped) {
              setJobId(data.id);
              setJob(data);
            }
            return;
          }
        } catch {
          window.localStorage.removeItem(LAST_JOB_KEY);
        }
      }

      try {
        const response = await fetch(`${API}/jobs/latest`);
        if (!response.ok) return;
        const data = await response.json();
        if (!stopped) {
          window.localStorage.setItem(LAST_JOB_KEY, data.id);
          setJobId(data.id);
          setJob(data);
        }
      } catch {
        // No previous in-memory job exists.
      }
    };

    restoreJob();
    return () => {
      stopped = true;
    };
  }, []);

  useEffect(() => {
    window.localStorage.setItem(
      SCAN_SETTINGS_KEY,
      JSON.stringify({ directories, convert, fullScan, workers })
    );
  }, [directories, convert, fullScan, workers]);

  useEffect(() => {
    window.localStorage.setItem(IMPORT_SETTINGS_KEY, JSON.stringify({ sourceFile }));
  }, [sourceFile]);

  useEffect(() => {
    if (!jobId) return;
    window.localStorage.setItem(LAST_JOB_KEY, jobId);
    let stopped = false;
    const load = async () => {
      try {
        const response = await fetch(`${API}/jobs/${jobId}`);
        if (!response.ok) throw new Error(await response.text());
        const data = await response.json();
        if (!stopped) setJob(data);
      } catch (err) {
        if (!stopped) setError(String(err.message || err));
      }
    };
    load();
    const timer = setInterval(load, 1000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [jobId]);

  const startScan = async () => {
    setError("");
    setSelected(new Set());
    setJob(null);
    const payload = {
      directories: directoryList,
      convert,
      full_scan: fullScan,
      workers: workers ? Number(workers) : null,
    };
    try {
      const response = await fetch(`${API}/scan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) throw new Error(await response.text());
      const data = await response.json();
      window.localStorage.setItem(LAST_JOB_KEY, data.job_id);
      setJobId(data.job_id);
    } catch (err) {
      setError(String(err.message || err));
    }
  };

  const importSourceFile = async () => {
    if (!sourceFile.trim() || importing) return;
    setError("");
    setImportResult(null);
    setImporting(true);
    try {
      const response = await fetch(`${API}/import`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: sourceFile.trim(),
          workers: workers ? Number(workers) : null,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || JSON.stringify(data));
      if (data.job_id) {
        window.localStorage.setItem(LAST_JOB_KEY, data.job_id);
        setJobId(data.job_id);
        setJob(null);
      }
      setImportResult(data);
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setImporting(false);
    }
  };

  const togglePath = (path) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const getSmallDuplicatePaths = (inputGroups) => {
    const paths = new Set();
    inputGroups.forEach((group) => {
      const ordered = [...group.files].sort((left, right) => {
        const rightSize = getDisplaySize(right);
        const leftSize = getDisplaySize(left);
        if (rightSize !== leftSize) return rightSize - leftSize;
        return left.path.localeCompare(right.path);
      });
      ordered.slice(1).forEach((file) => paths.add(file.path));
    });
    return paths;
  };

  const selectSmallDuplicates = () => {
    setSelected(getSmallDuplicatePaths(groups));
  };

  const selectSmallDuplicatesInGroup = (group) => {
    const paths = getSmallDuplicatePaths([group]);
    setSelected((current) => {
      const next = new Set(current);
      paths.forEach((path) => next.add(path));
      return next;
    });
  };

  const deleteSelected = async () => {
    if (selected.size === 0 || deleting) return;
    const selectedFiles = Array.from(selected).map((path) => fileIndex.get(path)).filter(Boolean);
    const livePhotoCount = selectedFiles.filter((file) => isLivePhoto(file)).length;
    const confirmMessage = livePhotoCount > 0
      ? `确认删除 ${selected.size} 个图片文件？其中 ${livePhotoCount} 个是苹果实况照片，会额外删除 ${livePhotoCount} 个配对的 MOV 视频。该操作不可撤销。`
      : `确认删除 ${selected.size} 个文件？该操作不可撤销。`;
    const confirmed = window.confirm(confirmMessage);
    if (!confirmed) return;
    setDeleting(true);
    setError("");
    try {
      const response = await fetch(`${API}/files`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths: Array.from(selected) }),
      });
      if (!response.ok) throw new Error(await response.text());
      const result = await response.json();
      setSelected(new Set());
      if (jobId) {
        const refreshed = await fetch(`${API}/jobs/${jobId}`);
        if (refreshed.ok) setJob(await refreshed.json());
      }
      if (result.deleted?.length === 0) setError("没有删除任何文件，可能文件已经不存在。");
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setDeleting(false);
    }
  };

  const stopJob = async () => {
    if (!jobId || stopping) return;
    setStopping(true);
    setError("");
    try {
      const response = await fetch(`${API}/jobs/${jobId}/stop`, { method: "POST" });
      if (!response.ok) throw new Error(await response.text());
      setJob(await response.json());
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setStopping(false);
    }
  };

  const isRunning = job && !["done", "failed", "cancelled"].includes(job.status);
  const groups = job?.duplicates || [];
  const failedPaths = useMemo(() => getFailedPaths(job), [job]);
  const fileIndex = useMemo(() => {
    const map = new Map();
    groups.forEach((group) => {
      group.files.forEach((file) => {
        map.set(file.path, file);
      });
    });
    return map;
  }, [groups]);

  const deleteFailedFiles = async () => {
    if (failedPaths.length === 0 || deleting || isRunning) return;
    const confirmed = window.confirm(`确认删除 ${failedPaths.length} 个异常文件？该操作不可撤销。`);
    if (!confirmed) return;
    setDeleting(true);
    setError("");
    try {
      const response = await fetch(`${API}/files`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths: failedPaths }),
      });
      if (!response.ok) throw new Error(await response.text());
      const result = await response.json();
      if (jobId) {
        const refreshed = await fetch(`${API}/jobs/${jobId}`);
        if (refreshed.ok) setJob(await refreshed.json());
      }
      if (result.deleted?.length === 0) setError("没有删除任何异常文件，可能文件已经不存在或挂载为只读。");
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setDeleting(false);
    }
  };

  return (
    <main className="app-shell">
      <section className="control-band">
        <div className="title-row">
          <div>
            <h1>图片重复扫描</h1>
            <p>扫描重复图片，支持 JPG、PNG、WEBP、JXL、HEIC、HEIF，也支持苹果实况照片。</p>
          </div>
          <div className="action-row">
            <button className="primary" onClick={startScan} disabled={directoryList.length === 0 || isRunning}>
              {isRunning ? <Loader2 className="spin" size={18} /> : <FolderSearch size={18} />}
              开始扫描
            </button>
            <button className="secondary" onClick={stopJob} disabled={!isRunning || stopping}>
              {stopping ? <Loader2 className="spin" size={18} /> : <Square size={18} />}
              停止
            </button>
          </div>
        </div>

        <div className="form-grid">
          <label className="path-input">
            <span>扫描目录列表</span>
            <textarea
              value={directories}
              onChange={(event) => setDirectories(event.target.value)}
              placeholder="/vol1/1000/图片库"
              spellCheck="false"
            />
          </label>
          <div className="settings">
            <label className="toggle">
              <input type="checkbox" checked={convert} onChange={(event) => setConvert(event.target.checked)} />
              <span>转换 JPG/PNG/HEIC 到 JXL</span>
            </label>
            <label className="toggle">
              <input type="checkbox" checked={fullScan} onChange={(event) => setFullScan(event.target.checked)} />
              <span>全量扫描（忽略缓存）</span>
            </label>
            <label>
              <span>工作线程</span>
              <input
                type="number"
                min="1"
                max="64"
                placeholder={job?.default_workers ? `默认 ${job.default_workers}` : "默认 4"}
                value={workers}
                onChange={(event) => setWorkers(event.target.value)}
              />
            </label>
          </div>
        </div>
        <div className="helper-card">
          {serverSettings.scan_roots?.length > 0 ? (
            <>
              <p>
                已授权目录：
                <code>{serverSettings.scan_roots.join(" / ")}</code>
              </p>
              <p>
                支持格式：
                <code>{serverSettings.supported_extensions?.join(" ") || ".jpg .jpeg .png .webp .jxl .heic .heif"}</code>
              </p>
              {serverSettings.supports_live_photo && (
                <p>如果是苹果实况照片，应用会把同名的 <code>.mov</code> 一起识别；删图片时，也会一起删掉配对视频。</p>
              )}
            </>
          ) : (
            <p>
              还没有可用授权目录。请先到飞牛「应用设置」里的「授权目录」添加图片目录，再输入像
              <code>/vol1/1000/图片库</code> 这样的完整路径。
            </p>
          )}
        </div>
      </section>

      <section className="control-band import-band">
        <div className="title-row">
          <div>
            <h2>导入源文件</h2>
            <p>可导入整个已授权目录，或导入某个具体图片文件路径；苹果实况照片会连同配对视频一起带入。</p>
          </div>
          <button className="primary" onClick={importSourceFile} disabled={!sourceFile.trim() || importing}>
            {importing ? <Loader2 className="spin" size={18} /> : <FolderSearch size={18} />}
            导入
          </button>
        </div>
        <div className="import-row">
          <label>
            <span>源文件路径</span>
            <input
              type="text"
              placeholder="/vol1/1000/待整理/008c203cb0ce5cf56005d114db47990b.jpg"
              value={sourceFile}
              onChange={(event) => setSourceFile(event.target.value)}
              spellCheck="false"
            />
          </label>
        </div>
        {importResult && (
          <div className={`import-result ${importResult.status}`}>
            {importResult.status === "queued" ? (
              <p>目录导入任务已开始，任务 ID <code>{importResult.job_id}</code></p>
            ) : importResult.status === "imported" ? (
              <p>已导入到 <code>{importResult.target}</code></p>
            ) : (
              <p>发现重复文件，未导入。匹配数量 {importResult.duplicates?.length || 0}</p>
            )}
          </div>
        )}
      </section>

      {error && <div className="error">{error}</div>}

      {job && (
        <section className="status-band">
          <div className="status-top">
            <div>
              <strong>{job.message}</strong>
              <span>{job.status}</span>
            </div>
            <div className="action-row">
              <button className="secondary" onClick={selectSmallDuplicates} disabled={groups.length === 0 || isRunning}>
                <CheckSquare size={17} />
                一键选择小文件
              </button>
              <button className="secondary" onClick={() => setSelected(new Set())} disabled={selected.size === 0}>
                <X size={17} />
                清空选择
              </button>
              <button className="secondary" onClick={stopJob} disabled={!isRunning || stopping}>
                {stopping ? <Loader2 className="spin" size={17} /> : <Square size={17} />}
                停止
              </button>
              <button className="danger" onClick={deleteSelected} disabled={selected.size === 0 || deleting}>
                {deleting ? <Loader2 className="spin" size={17} /> : <Trash2 size={17} />}
                删除已选 {selected.size}
              </button>
            </div>
          </div>
          <div className="progress-grid">
            <Progress label={job.discovery_done ? "发现完成" : "发现中"} value={job.discovered_files} total={job.discovery_done ? job.total_files : job.discovered_files} />
            <Progress label="已处理" value={job.processed_files || 0} total={job.discovery_done ? job.total_files : job.discovered_files} />
            <Progress label="已转换" value={job.converted_files} total={job.discovered_files} />
          </div>
          <div className="stats">
            <span>文件 {job.discovery_done ? job.total_files : `${job.discovered_files}+`}</span>
            <span>已扫描 {job.scanned_files}</span>
            <span>缓存命中 {job.cache_hit_files || 0}</span>
            <span>跳过 pHash {job.phash_skipped_files || 0}</span>
            <span>队列 {job.queue_size || 0}</span>
            <span>活跃线程 {job.active_workers || 0}</span>
            <span>失败 {job.failed_files}</span>
            <span>重复组 {groups.length}</span>
            <span>线程上限 {job.max_workers}</span>
          </div>
          {job.timings && (
            <div className="stats timing-stats">
              <span>SHA256读取+哈希 {job.timings.sha_seconds_total}s</span>
              <span>图片打开(懒加载) {job.timings.decode_seconds_total}s</span>
              <span>pHash物化/缩放 {job.timings.phash_prepare_seconds_total}s</span>
              <span>pHash DCT {job.timings.phash_dct_seconds_total}s</span>
              <span>pHash总计 {job.timings.phash_seconds_total}s</span>
              <span>转换累计 {job.timings.convert_seconds_total}s</span>
            </div>
          )}
        </section>
      )}

      {job?.logs?.length > 0 && (
        <section className="log-band">
          <h2>运行日志</h2>
          <div className="log-list">
            {job.logs.slice(-80).map((item, index) => (
              <p key={`${item}-${index}`}>{item}</p>
            ))}
          </div>
        </section>
      )}

      <section className="results">
        {groups.length === 0 && job?.status === "done" && (
          <div className="empty">
            <Check size={22} />
            没有发现 SHA256 或 pHash 完全相同的图片。
          </div>
        )}
        {groups.map((group, index) => (
          <DuplicateGroup
            key={`${group.key_type}-${group.key}`}
            group={group}
            index={index}
            selected={selected}
            onToggle={togglePath}
            onPreview={setPreviewFile}
            onSelectSmall={selectSmallDuplicatesInGroup}
          />
        ))}
      </section>

      {previewFile && (
        <div className="preview-backdrop" onClick={() => setPreviewFile(null)}>
          <div className="preview-panel" onClick={(event) => event.stopPropagation()}>
            <button className="preview-close" onClick={() => setPreviewFile(null)} title="关闭预览">
              <X size={20} />
            </button>
            <img src={`${API}/preview?path=${encodeURIComponent(previewFile.path)}`} alt="" />
            <div className="preview-meta">
              <strong>{previewFile.path.split("/").pop()}</strong>
              {isLivePhoto(previewFile) ? (
                <>
                  <span>{previewFile.width || "-"} × {previewFile.height || "-"} · 总占用 {formatBytes(getDisplaySize(previewFile))}</span>
                  <span>照片 {formatBytes(previewFile.size)} + 视频 {formatBytes(previewFile.live_photo_video_size)}</span>
                  <code>{previewFile.live_photo_video_path}</code>
                </>
              ) : (
                <span>{previewFile.width || "-"} × {previewFile.height || "-"} · {formatBytes(previewFile.size)}</span>
              )}
              <code>{previewFile.path}</code>
            </div>
          </div>
        </div>
      )}

      {job?.errors?.length > 0 && (
        <section className="log-band error-log">
          <div className="log-header">
            <div>
              <h2>错误日志</h2>
              <p>{failedPaths.length} 个异常文件</p>
            </div>
            <button className="danger" onClick={deleteFailedFiles} disabled={failedPaths.length === 0 || deleting || isRunning}>
              {deleting ? <Loader2 className="spin" size={17} /> : <Trash2 size={17} />}
              删除所有异常文件
            </button>
          </div>
          <div className="log-list">
            {job.errors.slice(-50).map((item, index) => (
              <p key={`${item}-${index}`}>{item}</p>
            ))}
          </div>
        </section>
      )}
    </main>
  );
}

function Progress({ label, value, total }) {
  const percent = total > 0 ? Math.round((value / total) * 100) : 0;
  return (
    <div className="progress-item">
      <div>
        <span>{label}</span>
        <strong>{value}/{total}</strong>
      </div>
      <div className="bar"><i style={{ width: `${Math.min(100, percent)}%` }} /></div>
    </div>
  );
}

function DuplicateGroup({ group, index, selected, onToggle, onPreview, onSelectSmall }) {
  return (
    <article className="group">
      <header>
        <div>
          <h2>重复组 {index + 1}</h2>
          <p>{group.key_type.toUpperCase()} · {group.files.length} 个文件 · {group.key}</p>
        </div>
        <button className="secondary compact" onClick={() => onSelectSmall(group)}>
          <CheckSquare size={16} />
          选择小文件
        </button>
      </header>
      <div className="image-grid">
        {group.files.map((file) => {
          const checked = selected.has(file.path);
          return (
            <div className={`image-item ${checked ? "selected" : ""}`} key={file.path}>
              <button className="thumb-button" onClick={() => onPreview(file)} title="放大预览">
                <img src={`${API}/thumbnail?path=${encodeURIComponent(file.path)}`} alt="" loading="lazy" />
                <span className="preview-indicator"><Eye size={16} /></span>
              </button>
              <button className="select-indicator" onClick={() => onToggle(file.path)} title="选择删除">
                {checked ? <X size={16} /> : <Trash2 size={16} />}
              </button>
              <div className="meta">
                <div className="meta-title">
                  <strong>{file.path.split("/").pop()}</strong>
                  {isLivePhoto(file) && <span className="live-badge">实况照片</span>}
                </div>
                {isLivePhoto(file) ? (
                  <>
                    <span>{file.width || "-"} × {file.height || "-"} · 总占用 {formatBytes(getDisplaySize(file))}</span>
                    <span>照片 {formatBytes(file.size)} + 视频 {formatBytes(file.live_photo_video_size)}</span>
                    <code title={file.live_photo_video_path}>{file.live_photo_video_path}</code>
                  </>
                ) : (
                  <span>{file.width || "-"} × {file.height || "-"} · {formatBytes(file.size)}</span>
                )}
                <span>{formatDate(file.modified_at)}</span>
                <code title={file.path}>{file.path}</code>
                {file.converted_from && <em>转换自 {file.converted_from}</em>}
              </div>
            </div>
          );
        })}
      </div>
    </article>
  );
}

createRoot(document.getElementById("root")).render(<App />);
