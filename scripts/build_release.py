#!/usr/bin/env python3
"""Build a deterministic, explicitly whitelisted macOS source distribution."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "README.md", "LICENSE", "server.py", "launcher.py", "trash_helper.swift",
    "index.html", "app.js", "styles.css", "classic.html", "classic.js", "classic.css",
    "static/index.html", "static/app.js", "static/styles.css",
    "安装到应用程序.command", "启动磁盘空间管理.command", "scripts/install_macos.sh",
    "scripts/build_release.py", ".github/workflows/ci.yml",
    "test_server.py", "test_distribution.py",
)
OPTIONAL = (".gitignore", "docs/classic-preview.jpg", "CHANGELOG.md", "bin/trash-helper-arm64", "bin/trash-helper-x86_64")


def release_files(root: Path) -> list[Path]:
    result = []
    for name in REQUIRED + OPTIONAL:
        path = root / name
        if name in OPTIONAL and not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing or unsafe release file: {name}")
        result.append(path)
    return sorted(result, key=lambda p: p.relative_to(root).as_posix())


def build(root: Path, output: Path, version: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?", version):
        raise ValueError("Version must be a semantic version, for example 0.2.1")
    files = release_files(root)
    output.mkdir(parents=True, exist_ok=True)
    stem = f"disk-space-analyzer-v{version}-macos-source"
    archive = output / (stem + ".zip")
    checksums = output / (stem + ".sha256")
    for existing in (archive, checksums):
        if existing.exists() or existing.is_symlink():
            raise FileExistsError(f"Refusing to overwrite {existing}")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            relative = path.relative_to(root).as_posix()
            entry = zipfile.ZipInfo(f"{stem}/{relative}", date_time=(2026, 1, 1, 0, 0, 0))
            entry.create_system = 3
            executable = relative.endswith((".command", ".sh")) or relative.startswith("bin/")
            entry.external_attr = (0o100755 if executable else 0o100644) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(entry, path.read_bytes())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    with checksums.open("x", encoding="utf-8") as handle:
        handle.write(f"{digest}  {archive.name}\n")
    return archive, checksums


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="0.2.1")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    for path in build(ROOT, args.output.expanduser().resolve(), args.version):
        print(path)


if __name__ == "__main__":
    main()
