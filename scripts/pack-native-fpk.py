#!/usr/bin/env python3
from __future__ import annotations

import sys
import tarfile
import tempfile
from pathlib import Path


DIR_MODE = 0o755
FILE_MODE = 0o644
EXEC_MODE = 0o755


def normalize(info: tarfile.TarInfo, mode: int) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mode = mode
    return info


def add_file(tar: tarfile.TarFile, source: Path, arcname: str, mode: int) -> None:
    info = tar.gettarinfo(str(source), arcname)
    normalize(info, mode)
    with source.open("rb") as handle:
        tar.addfile(info, handle)


def add_directory(tar: tarfile.TarFile, source: Path, arcname: str) -> None:
    info = tar.gettarinfo(str(source), arcname)
    normalize(info, DIR_MODE)
    tar.addfile(info)


def add_tree(tar: tarfile.TarFile, source: Path, arcname: str, executable: bool = False) -> None:
    add_directory(tar, source, arcname)
    for child in sorted(source.iterdir(), key=lambda item: item.name):
        child_arcname = f"{arcname}/{child.name}"
        if child.is_dir():
            add_tree(tar, child, child_arcname, executable=executable)
        else:
            add_file(tar, child, child_arcname, EXEC_MODE if executable else FILE_MODE)


def build_app_archive(pack_dir: Path, app_tgz: Path) -> None:
    with tarfile.open(app_tgz, "w:gz", format=tarfile.GNU_FORMAT) as archive:
        add_tree(archive, pack_dir / "app" / "server", "server")
        add_tree(archive, pack_dir / "app" / "ui", "ui")
        add_tree(archive, pack_dir / "config", "config")


def build_outer_archive(pack_dir: Path, app_tgz: Path, output_file: Path) -> None:
    with tarfile.open(output_file, "w:gz", format=tarfile.GNU_FORMAT) as archive:
        add_file(archive, app_tgz, "app.tgz", FILE_MODE)
        add_tree(archive, pack_dir / "cmd", "cmd", executable=True)
        add_tree(archive, pack_dir / "config", "config")
        add_file(archive, pack_dir / "ICON.PNG", "ICON.PNG", FILE_MODE)
        add_file(archive, pack_dir / "ICON_256.PNG", "ICON_256.PNG", FILE_MODE)
        add_file(archive, pack_dir / "manifest", "manifest", FILE_MODE)
        add_tree(archive, pack_dir / "wizard", "wizard")


def main() -> int:
    root_dir = Path(__file__).resolve().parent.parent
    pack_dir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else root_dir / "fnnas.imageduplicatescanner"
    output_file = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else root_dir / "fnnas.imageduplicatescanner.fpk"

    if not pack_dir.exists():
        raise SystemExit(f"Package directory does not exist: {pack_dir}")

    output_file.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="fnos-pack-") as temp_dir:
        app_tgz = Path(temp_dir) / "app.tgz"
        build_app_archive(pack_dir, app_tgz)
        build_outer_archive(pack_dir, app_tgz, output_file)

    print(f"Built {output_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
