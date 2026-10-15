#!/usr/bin/env python3
"""Local, loopback-only macOS disk space analyzer.

The service is intentionally dependency-free. It uses Spotlight for a bounded
fast first view and a cancellable exact filesystem walk for directory totals.
All mutating calls require an origin check, JSON, and a per-process token.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import heapq
import stat
import ipaddress
import mimetypes
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from collections import defaultdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
UI_ROOT = ROOT if (ROOT / "index.html").is_file() else ROOT / "static"
STATIC_ROOT = ROOT / "static"
HOME = Path.home().resolve()
DEFAULT_SCAN_ROOT = HOME
STATE_DIR = Path(os.environ.get("ATLAS_STATE_DIR", str(HOME / "Library" / "Application Support" / "磁盘空间管理"))).expanduser()
SNAPSHOT_PATH = STATE_DIR / "scan.json"
MAX_BODY = 2 * 1024 * 1024
MAX_TREE_RESULTS = 5000
MAX_SEARCH_RESULTS = 500
TOP_FILES = 1000
SPOTLIGHT_TTL = 300.0
CONFIRM_TTL = 300.0
TOKEN = secrets.token_urlsafe(32)


def sys_is_macos() -> bool:
    return hasattr(os, "uname") and os.uname().sysname == "Darwin"


def iso_time(value: float | int | None) -> str | None:
    if not value:
        return None
    try:
        return dt.datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return None


def human_location(path: Path) -> str:
    try:
        rel = path.relative_to(HOME)
        return "Home" if str(rel) == "." else f"~/{rel}"
    except ValueError:
        return str(path)


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def stat_record(path: Path, *, follow: bool = False) -> dict | None:
    try:
        st = path.stat() if follow else path.lstat()
    except (OSError, ValueError):
        return None
    return {
        "logical_size": int(st.st_size),
        "allocated_size": int(getattr(st, "st_blocks", 0)) * 512,
        "mtime_ns": int(st.st_mtime_ns),
        "ctime_ns": int(st.st_ctime_ns),
        "inode": int(st.st_ino),
        "device": int(st.st_dev),
        "mode": int(st.st_mode),
        "modified": iso_time(st.st_mtime),
    }


def extension_for(path: Path) -> str:
    suffix = path.suffix.lower()
    return suffix[1:] if suffix.startswith(".") else ("(no extension)" if path.is_file() else "")


def command_output(args: list[str], timeout: float = 8.0) -> bytes | None:
    try:
        return subprocess.check_output(args, stderr=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def run_partial(args: list[str], timeout: float) -> tuple[bytes, bool, str | None]:
    """Return stdout even if a subprocess times out."""
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        return b"", False, str(exc)
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        out = exc.output or b""
        try:
            proc.kill()
            more, _ = proc.communicate(timeout=2)
            out = more or out
        except (OSError, subprocess.SubprocessError):
            pass
    return out or b"", timed_out, ("command failed" if not timed_out and proc.returncode else None)


def normalize_existing(raw: str | None, *, allow_file: bool = False) -> Path:
    if not raw:
        return DEFAULT_SCAN_ROOT
    value = str(raw)
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ValueError("path must be absolute")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("path does not exist")
    if allow_file:
        if not (resolved.is_file() or resolved.is_dir()):
            raise ValueError("path must be a file or directory")
    elif not resolved.is_dir():
        raise ValueError("path must be a directory")
    return resolved


def path_has_symlink_ancestor(path: Path) -> bool:
    """Check every component without resolving through links."""
    if not path.is_absolute():
        return True
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


# Protected locations are deliberately conservative. A user can still remove
# ordinary files/folders inside Downloads, Documents, Desktop, etc. by previewing
# those exact children; container roots and app/runtime data are never candidates.
HOME_STANDARD_ROOTS = {HOME / n for n in ("Desktop", "Documents", "Downloads", "Movies", "Music", "Pictures", "Public")}
HIDDEN_PROTECTED = {".codex", ".claude", ".ssh", ".config", ".local", ".cache"}
PROTECTED_WORDS = ("codex", "chatgpt", "shadowrocket", "wireguard", "tailscale", "clash", "proxy", "vpn", "chrome", "safari", "firefox", "vivaldi", "browser", "network", "openclaw")


def trash_reasons(path: Path) -> list[str]:
    reasons: list[str] = []
    if not path.is_absolute():
        reasons.append("path must be absolute")
    try:
        absolute = Path(os.fspath(path))
    except (OSError, TypeError):
        return ["invalid path"]
    if not absolute.is_absolute():
        reasons.append("path must be absolute")
    if path_has_symlink_ancestor(absolute):
        reasons.append("symbolic-link path is protected")
    try:
        resolved = absolute.resolve(strict=True)
    except (OSError, RuntimeError):
        return reasons + ["path does not exist"]
    if resolved in (Path("/"), HOME, Path("/Applications"), HOME / "Library"):
        reasons.append("protected root")
    if any(is_within(resolved, p) or is_within(p, resolved) for p in (ROOT,)):
        reasons.append("this project or its ancestor is protected")
    if any(is_within(resolved, p) for p in HOME_STANDARD_ROOTS):
        if resolved in HOME_STANDARD_ROOTS:
            reasons.append("standard home folder is protected")
    if is_within(resolved, HOME / "Library"):
        reasons.append("Library and application data are protected")
    try:
        rel_parts = resolved.relative_to(HOME).parts
    except ValueError:
        rel_parts = resolved.parts
    lower_parts = [p.lower() for p in rel_parts]
    if any(p in HIDDEN_PROTECTED or p.startswith(".") for p in lower_parts):
        reasons.append("hidden configuration is protected")
    if any(any(word in part for word in PROTECTED_WORDS) for part in lower_parts):
        reasons.append("browser, proxy, VPN, network, or Codex data is protected")
    # Paths inside an app bundle are runtime files; only the bundle itself may
    # be reviewed as an uninstall candidate.
    app_ancestor = next((p for p in (resolved, *resolved.parents) if p.suffix.lower() == ".app"), None)
    if app_ancestor and resolved != app_ancestor:
        reasons.append("files inside an app bundle are protected")
    if app_ancestor and resolved == app_ancestor and running_app_bundle(app_ancestor):
        reasons.append("application is currently running")
    # A path that contains the running service project or an ancestor of it is
    # unsafe even if it otherwise sits under HOME.
    if resolved == ROOT or is_within(ROOT, resolved):
        reasons.append("this project or its ancestor is protected")
    allowed = is_within(resolved, HOME) or is_within(resolved, Path("/Applications"))
    if not allowed:
        reasons.append("only home or Applications paths are manageable")
    return list(dict.fromkeys(reasons))


def can_trash(path: Path) -> bool:
    return not trash_reasons(path)


def running_app_bundle(app_path: Path) -> bool:
    if not sys_is_macos():
        return False
    out = command_output(["/bin/ps", "-axo", "command="], timeout=2)
    if not out:
        # If status cannot be checked, err on the side of protecting the app.
        return True
    prefix = str(app_path) + "/Contents/"
    return any(line.startswith(prefix) for line in out.decode("utf-8", "replace").splitlines())


def trash_fields(item: dict) -> dict:
    reasons = trash_reasons(Path(item.get("path", "")))
    item = dict(item)
    item["can_trash"] = not reasons
    item["trash_reason"] = reasons[0] if reasons else None
    return item


def empty_aggregate() -> dict:
    return {"logical_bytes": 0, "allocated_bytes": 0, "files": 0, "folders": 0,
            "items": 0, "error_count": 0, "complete": False}


class ScanState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.cancel_event = threading.Event()
        self.running = False
        self.scan_id = None
        self.path = None
        self.started_at = self.finished_at = None
        self.files_scanned = self.dirs_scanned = 0
        self.logical_bytes = self.allocated_bytes = 0
        self.error_count = 0
        self.errors = []
        self.excluded = []
        self.complete = self.cancelled = False
        self.source = "none"
        self.root_device = None
        self.dir_sizes = {}
        self.extensions_by_dir = {}
        self.top_heap = []
        self._seq = 0
        self.hardlinks = {}

    def reset(self, root: Path, scan_id: str) -> None:
        self.path, self.scan_id = root, scan_id
        self.started_at, self.finished_at = time.time(), None
        self.files_scanned = self.dirs_scanned = self.logical_bytes = self.allocated_bytes = 0
        self.error_count = 0
        self.errors, self.excluded = [], []
        self.complete = self.cancelled = False
        self.source = "filesystem-exact"
        self.dir_sizes, self.extensions_by_dir, self.top_heap = {}, {}, []
        self.hardlinks, self._seq = {}, 0
        self.root_device = root.stat().st_dev
        self.cancel_event = threading.Event()
        self.running = True

    def top_file(self, entry: dict) -> None:
        self._seq += 1
        item = (entry["size"], self._seq, entry)
        if len(self.top_heap) < TOP_FILES:
            heapq.heappush(self.top_heap, item)
        elif item[0] > self.top_heap[0][0]:
            heapq.heapreplace(self.top_heap, item)

    def record_error(self, path: Path, exc: Exception) -> None:
        self.error_count += 1
        if len(self.errors) < 100:
            self.errors.append(f"{path}: {getattr(exc, 'strerror', None) or type(exc).__name__}")

    def snapshot(self) -> dict:
        with self.lock:
            return {"running": self.running, "scan_id": self.scan_id,
                    "path": str(self.path) if self.path else None,
                    "files_scanned": self.files_scanned, "dirs_scanned": self.dirs_scanned,
                    "logical_bytes": self.logical_bytes, "allocated_bytes": self.allocated_bytes,
                    "bytes_scanned": self.allocated_bytes, "errors": list(self.errors),
                    "error_count": self.error_count, "complete": self.complete,
                    "partial": bool(self.path) and not self.complete, "cancelled": self.cancelled,
                    "finished_at": iso_time(self.finished_at), "started_at": iso_time(self.started_at),
                    "source": self.source, "exact": self.complete,
                    "excluded_paths": list(self.excluded),
                    "allocation_note": "Allocated blocks deduplicate hardlinks; APFS clones and snapshots may differ from volume usage."}

    def has_coverage(self, path: Path) -> bool:
        with self.lock:
            return bool(self.path and not self.running and str(path) in self.dir_sizes)


STATE = ScanState()
SPOTLIGHT_CACHE = {}
APP_CACHE = None
CONFIRMATIONS = {}
CACHE_LOCK = threading.RLock()


def persist_snapshot() -> None:
    with STATE.lock:
        payload = STATE.snapshot() | {"version": 3, "dir_sizes": STATE.dir_sizes,
                "extensions_by_dir": STATE.extensions_by_dir,
                "hardlink_owners": {f"{k[0]}:{k[1]}": v for k, v in STATE.hardlinks.items()},
                "top_files": [x[2] for x in STATE.top_heap]}
        data = json.dumps(payload, ensure_ascii=False)
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="scan-", suffix=".tmp", dir=STATE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
        os.replace(name, SNAPSHOT_PATH)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_snapshot() -> None:
    try:
        payload = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        if payload.get("version") != 3 or payload.get("running") or not payload.get("finished_at"):
            return
        with STATE.lock:
            STATE.path = Path(payload["path"])
            STATE.scan_id = payload.get("scan_id")
            for key in ("started_at", "finished_at"):
                setattr(STATE, key, dt.datetime.fromisoformat(payload[key]).timestamp())
            for key in ("files_scanned", "dirs_scanned", "logical_bytes", "allocated_bytes", "error_count"):
                setattr(STATE, key, int(payload.get(key, 0)))
            STATE.errors = payload.get("errors", [])[:100]
            STATE.excluded = payload.get("excluded_paths", [])[:100]
            STATE.complete = bool(payload.get("complete"))
            STATE.cancelled = bool(payload.get("cancelled"))
            STATE.source = "filesystem-exact-persisted"
            STATE.dir_sizes = payload.get("dir_sizes", {})
            STATE.extensions_by_dir = payload.get("extensions_by_dir", {})
            STATE.hardlinks = {tuple(map(int, key.split(":"))): value for key, value in payload.get("hardlink_owners", {}).items()}
            STATE.top_heap = [(x["size"], n, x) for n, x in enumerate(payload.get("top_files", [])[:TOP_FILES])]
            heapq.heapify(STATE.top_heap)
    except (OSError, ValueError, TypeError, KeyError):
        return


def scan_policy(root: Path, path: Path, device: int, allowed_devices: set[int]) -> bool:
    # macOS exposes Data via firmlinks beneath /. Avoid also traversing the
    # physical Data volume under System/Volumes or mounted volumes twice.
    excluded = (Path("/System/Volumes"), Path("/Volumes")) if root == Path("/") else (root / "Volumes",)
    if sys_is_macos() and root in (Path("/"), Path("/System/Volumes/Data")) and path in excluded:
        return False
    return device in allowed_devices


def scan_worker(root: Path, scan_id: str) -> None:
    # reset happens before the thread starts; a cancel cannot be cleared here.
    with STATE.lock:
        if STATE.scan_id != scan_id:
            return
        cancel = STATE.cancel_event
        devices = {STATE.root_device}
    if sys_is_macos() and root == Path("/"):
        try:
            devices.add(Path("/System/Volumes/Data").stat().st_dev)
        except OSError:
            pass
    frames = []

    def enter(path: Path) -> None:
        aggregate = empty_aggregate()
        with STATE.lock:
            STATE.dir_sizes[str(path)] = aggregate
            STATE.extensions_by_dir[str(path)] = {}
        try:
            iterator = os.scandir(path)
        except OSError as exc:
            aggregate["error_count"] = 1
            with STATE.lock:
                STATE.record_error(path, exc)
            frames.append([path, None, aggregate])
        else:
            with STATE.lock:
                STATE.dirs_scanned += 1
            frames.append([path, iterator, aggregate])

    def leave(finished: bool) -> None:
        path, iterator, aggregate = frames.pop()
        if iterator is not None:
            iterator.close()
        aggregate["items"] = aggregate["files"] + aggregate["folders"]
        aggregate["complete"] = finished and not aggregate["error_count"]
        if frames:
            parent = frames[-1][2]
            for key in ("logical_bytes", "allocated_bytes", "files", "folders", "error_count"):
                parent[key] += aggregate[key]
            parent["folders"] += 1

    try:
        enter(root)
        while frames and not cancel.is_set():
            directory, iterator, aggregate = frames[-1]
            if iterator is None:
                leave(True)
                continue
            try:
                entry = next(iterator)
            except StopIteration:
                leave(True)
                continue
            except OSError as exc:
                aggregate["error_count"] += 1
                with STATE.lock:
                    STATE.record_error(directory, exc)
                leave(False)
                continue
            path = Path(entry.path)
            try:
                st = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(st.st_mode):
                    continue
                if not scan_policy(root, path, st.st_dev, devices):
                    if len(STATE.excluded) < 100:
                        STATE.excluded.append(str(path))
                    continue
                if stat.S_ISDIR(st.st_mode):
                    enter(path)
                elif stat.S_ISREG(st.st_mode):
                    inode = (st.st_dev, st.st_ino)
                    logical = st.st_size
                    allocated = getattr(st, "st_blocks", 0) * 512
                    unique_allocated = allocated
                    if st.st_nlink > 1:
                        if inode in STATE.hardlinks:
                            unique_allocated = 0
                        else:
                            STATE.hardlinks[inode] = str(path)
                    aggregate["files"] += 1
                    aggregate["logical_bytes"] += logical
                    aggregate["allocated_bytes"] += unique_allocated
                    ext = path.suffix.lower().lstrip(".") or "(no extension)"
                    counts = STATE.extensions_by_dir[str(directory)].setdefault(ext, [0, 0, 0])
                    counts[0] += 1; counts[1] += logical; counts[2] += unique_allocated
                    item = {"path": str(path), "name": path.name, "size": unique_allocated,
                            "logical_size": logical, "allocated_size": unique_allocated,
                            "physical_allocated_size": allocated, "modified": iso_time(st.st_mtime),
                            "kind": "file", "extension": ext, "inode": st.st_ino, "device": st.st_dev,
                            "files": 1, "folders": 0, "items": 1}
                    with STATE.lock:
                        STATE.files_scanned += 1
                        STATE.logical_bytes += logical
                        STATE.allocated_bytes += unique_allocated
                        STATE.top_file(item)
            except OSError as exc:
                aggregate["error_count"] += 1
                with STATE.lock:
                    STATE.record_error(path, exc)
    except Exception as exc:
        with STATE.lock:
            STATE.record_error(root, exc)
        if frames:
            frames[-1][2]["error_count"] += 1
    finally:
        while frames:
            leave(False)
        with STATE.lock:
            STATE.cancelled = cancel.is_set()
            root_info = STATE.dir_sizes.get(str(root), empty_aggregate())
            STATE.complete = bool(root_info["complete"] and not STATE.cancelled and not STATE.error_count)
            STATE.finished_at = time.time()
            # Keep new scans blocked until the snapshot is safely captured.
            STATE.running = False
            try:
                persist_snapshot()
            except OSError:
                pass


def start_scan(raw_path: str | None) -> dict:
    root = normalize_existing(raw_path)
    with STATE.lock:
        if STATE.running:
            return {"scan_id": STATE.scan_id, "path": str(STATE.path), "already_running": True}
        scan_id = uuid.uuid4().hex
        STATE.reset(root, scan_id)
        threading.Thread(target=scan_worker, args=(root, scan_id), daemon=True, name="disk-scan").start()
    return {"scan_id": scan_id, "path": str(root), "already_running": False, "source": "filesystem-exact"}


def cancel_scan() -> dict:
    with STATE.lock:
        if not STATE.running:
            return {"cancelled": STATE.cancelled, "running": False, "scan_id": STATE.scan_id}
        STATE.cancel_event.set()
        return {"cancelled": True, "running": True, "scan_id": STATE.scan_id}


def spotlight_paths(root: Path, query: str, limit: int, timeout: float = 10.0) -> tuple[list[Path], bool, str | None]:
    if not sys_is_macos() or not root.is_dir():
        return [], False, "spotlight unavailable"
    output, timed_out, error = run_partial(["/usr/bin/mdfind", "-onlyin", str(root), "-0", query], timeout)
    paths: list[Path] = []
    records = output.split(b"\0")
    if timed_out and not output.endswith(b"\0"):
        records = records[:-1]
    for raw in records:
        if not raw: continue
        try: p = Path(os.fsdecode(raw))
        except (OSError, ValueError): continue
        if not p.is_absolute() or path_has_symlink_ancestor(p) or not is_within(p, root) or not p.exists(): continue
        paths.append(p)
        if len(paths) >= limit:
            timed_out = True  # Result cap is also an explicitly partial query.
            break
    return paths, timed_out, error


def escape_mdfind_name(query: str) -> str:
    # mdfind metadata query string escape; cap length to keep query bounded.
    escaped = ""
    for char in query[:200]:
        if char in "\r\n\x00":
            escaped += " "
        else:
            escaped += ("\\" if char in "\\\'\"*?[]" else "") + char
    return escaped


def file_info(path: Path) -> dict | None:
    st = stat_record(path)
    if not st or not path.is_file(): return None
    reasons = trash_reasons(path)
    return {"path": str(path), "name": path.name, "size": st["allocated_size"], "logical_size": st["logical_size"], "allocated_size": st["allocated_size"], "modified": st["modified"], "kind": "file", "extension": extension_for(path), "can_trash": not reasons, "trash_reason": reasons[0] if reasons else None}


def spotlight_largest(root: Path, limit: int) -> tuple[list[dict], dict]:
    key = (str(root), limit)
    with CACHE_LOCK:
        cached = SPOTLIGHT_CACHE.get(key)
        if cached and time.time() - cached[0] < SPOTLIGHT_TTL: return cached[1]["files"], cached[1]
    paths, partial, error = spotlight_paths(root, "kMDItemFSSize >= 100000000", max(limit * 20, 2000), timeout=12)
    values = [v for p in paths if (v := file_info(p))]
    values.sort(key=lambda x: (-x["allocated_size"], -x["logical_size"], x["path"]))
    payload = {"files": values[:limit], "source": "spotlight", "partial": True, "complete": False, "query_complete": not partial and not error, "error": error, "indexed_at": iso_time(time.time()), "scanned_path": str(root)}
    with CACHE_LOCK: SPOTLIGHT_CACHE[key] = (time.time(), payload)
    return payload["files"], payload


def scope_metadata(path: Path) -> dict:
    with STATE.lock:
        aggregate = STATE.dir_sizes.get(str(path)) if not STATE.running else None
        covered = aggregate is not None
        complete = bool(covered and aggregate.get("complete"))
        return {"path": str(path), "source": STATE.source if covered else "filesystem-shallow",
                "complete": complete, "partial": not complete,
                "error_count": aggregate.get("error_count", 0) if covered else 0,
                "requires_scan": not covered, "snapshot_at": iso_time(STATE.finished_at),
                "running": STATE.running}


def extension_stats(path: Path) -> dict:
    metadata = scope_metadata(path)
    counts = defaultdict(lambda: [0, 0, 0])
    if not metadata["requires_scan"]:
        with STATE.lock:
            for directory, extensions in STATE.extensions_by_dir.items():
                if is_within(Path(directory), path):
                    for ext, values in extensions.items():
                        for index in range(3):
                            counts[ext][index] += values[index]
    total = sum(x[2] for x in counts.values())
    values = [{"extension": ext, "count": n, "size": allocated,
               "logical_size": logical, "allocated_size": allocated,
               "percent": allocated * 100 / total if total else 0}
              for ext, (n, logical, allocated) in counts.items()]
    values.sort(key=lambda item: (-item["size"], item["extension"]))
    return metadata | {"extensions": values, "size": total,
                       "count": sum(x[0] for x in counts.values())}


def app_size(path: Path) -> tuple[int | None, None, bool]:
    args = ["/usr/bin/du", "-sk", str(path)] if sys_is_macos() else ["du", "-sk", str(path)]
    out, partial, error = run_partial(args, 15)
    allocated = None
    if out:
        try: allocated = int(out.decode("utf-8", "replace").split()[0]) * 1024
        except (ValueError, IndexError): pass
    return allocated, None, bool(partial or error or allocated is None)


def list_apps() -> list[dict]:
    global APP_CACHE
    with CACHE_LOCK:
        if APP_CACHE and time.time() - APP_CACHE[0] < SPOTLIGHT_TTL: return APP_CACHE[1]
    paths: set[Path] = set()
    for base in (Path("/Applications"), HOME / "Applications"):
        try: paths.update(p for p in base.iterdir() if p.suffix.lower() == ".app" and not p.is_symlink())
        except OSError: pass
    result = []
    for p in paths:
        allocated, logical, partial = app_size(p); st = stat_record(p)
        reasons = trash_reasons(p)
        result.append({"name": p.stem, "path": str(p), "full_path": str(p), "kind": "application", "size": allocated, "allocated_size": allocated, "logical_size": logical, "modified": st["modified"] if st else None, "location": human_location(p), "can_trash": not reasons, "trash_reasons": reasons, "trash_reason": reasons[0] if reasons else None, "size_partial": partial, "source": "du+filesystem"})
    result.sort(key=lambda x: (-(x["size"] or 0), x["name"].lower()))
    with CACHE_LOCK: APP_CACHE = (time.time(), result)
    return result


def aggregate_row(path: Path, aggregate: dict, source: str) -> dict:
    return {"path": str(path), "name": path.name or str(path), "kind": "directory",
            "size": aggregate["allocated_bytes"], "allocated_size": aggregate["allocated_bytes"],
            "logical_size": aggregate["logical_bytes"], "files": aggregate.get("files", 0),
            "folders": aggregate.get("folders", 0), "items": aggregate.get("items", 0),
            "complete": aggregate.get("complete", False), "partial": not aggregate.get("complete", False),
            "error_count": aggregate.get("error_count", 0), "source": source,
            "can_reveal": True}


def tree_for(path: Path, limit: int) -> dict:
    st = stat_record(path)
    if not st:
        raise ValueError("path does not exist")
    if not stat.S_ISDIR(st["mode"]):
        kind = "symlink" if stat.S_ISLNK(st["mode"]) else "file"
        return trash_fields({"path": str(path), "name": path.name, "kind": kind,
                "size": st["allocated_size"], "logical_size": st["logical_size"],
                "allocated_size": st["allocated_size"], "children": [], "has_more": False,
                "files": 1, "folders": 0, "items": 1, "source": "filesystem", "complete": True})
    metadata = scope_metadata(path)
    children = []
    with STATE.lock:
        sizes = dict(STATE.dir_sizes) if not STATE.running else {}
        counted_files = {x[2]["path"]: x[2] for x in STATE.top_heap} if not STATE.running else {}
        hardlink_owners = dict(STATE.hardlinks) if not STATE.running else {}
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                child = Path(entry.path)
                info = stat_record(child)
                if info is None:
                    continue
                if stat.S_ISLNK(info["mode"]):
                    kind = "symlink"
                elif stat.S_ISDIR(info["mode"]):
                    kind = "directory"
                elif stat.S_ISREG(info["mode"]):
                    kind = "file"
                else:
                    continue
                aggregate = sizes.get(str(child)) if kind == "directory" else None
                if aggregate is not None:
                    row = aggregate_row(child, aggregate, metadata["source"])
                elif kind == "directory":
                    row = {"path": str(child), "name": child.name, "kind": kind, "size": None,
                           "allocated_size": None, "logical_size": None, "files": None,
                           "folders": None, "items": None, "complete": False, "partial": True,
                           "error_count": 0, "can_reveal": True}
                else:
                    allocated = counted_files.get(str(child), {}).get("size", info["allocated_size"])
                    owner = hardlink_owners.get((info["device"], info["inode"]))
                    if owner is not None and owner != str(child):
                        allocated = 0
                    row = {"path": str(child), "name": child.name, "kind": kind,
                           "size": allocated, "allocated_size": allocated,
                           "logical_size": info["logical_size"], "files": 1, "folders": 0,
                           "items": 1, "complete": True, "partial": False, "error_count": 0,
                           "can_reveal": True}
                row["modified"] = info["modified"]
                row["extension"] = child.suffix.lower().lstrip(".") if kind == "file" else ""
                children.append(trash_fields(row))
    except OSError as exc:
        raise ValueError(f"cannot read directory: {getattr(exc, 'strerror', 'permission denied')}")
    aggregate = sizes.get(str(path))
    if aggregate is not None:
        result = aggregate_row(path, aggregate, metadata["source"])
    else:
        result = {"path": str(path), "name": path.name or str(path), "kind": "directory",
                  "size": None, "allocated_size": None, "logical_size": None,
                  "files": None, "folders": None, "items": None}
    total = result.get("size") or 0
    for row in children:
        row["percent_parent"] = (row["size"] * 100 / total) if total and row["size"] is not None else None
    children.sort(key=lambda row: (-(row.get("size") or 0), row["name"].lower()))
    return trash_fields(result | metadata | {"children": children[:limit],
                 "has_more": len(children) > limit, "child_count": len(children)})


def treemap_for(path: Path, depth: int = 3, maxnodes: int = 800) -> dict:
    depth = max(1, min(4, depth)); maxnodes = max(2, min(1500, maxnodes))
    metadata = scope_metadata(path)
    with STATE.lock:
        sizes = dict(STATE.dir_sizes) if not STATE.running else {}
        top = [dict(x[2]) for x in STATE.top_heap] if not STATE.running else []
    root_aggregate = sizes.get(str(path))
    if root_aggregate is None:
        return metadata | {"root": {"name": path.name or str(path), "path": str(path),
                "kind": "directory", "size": None, "logical_size": None, "children": [],
                "can_reveal": True}, "node_count": 1, "truncated": False}
    directories = defaultdict(list)
    leaves = defaultdict(list)
    for name, aggregate in sizes.items():
        child = Path(name)
        if child != path and is_within(child, path):
            directories[str(child.parent)].append(aggregate_row(child, aggregate, metadata["source"]))
    for item in top:
        if is_within(Path(item["path"]), path) and item["size"] > 0:
            leaves[str(Path(item["path"]).parent)].append(item | {"can_reveal": True, "complete": True})
    node_count = 0
    truncated = False

    def build(row: dict, level: int, budget: int) -> dict:
        nonlocal node_count, truncated
        node_count += 1
        row = dict(row); row["children"] = []
        if row["kind"] != "directory" or level >= depth or budget <= 1:
            return row
        choices = directories.get(row["path"], []) + leaves.get(row["path"], [])
        choices.sort(key=lambda child: (-(child.get("size") or 0), child["name"]))
        remaining_budget = budget - 1
        remaining_size = row["size"]
        remaining_logical = row["logical_size"]
        for child in choices:
            if (child.get("size") or 0) <= 0:
                continue
            # Reserve one node for unexpanded entries / files outside the top set.
            if remaining_budget <= 1:
                truncated = True
                break
            child_budget = max(1, min(remaining_budget - 1, max(1, budget // max(1, len(choices)))))
            before = node_count
            child_node = build(child, level + 1, child_budget)
            child_node["percent_parent"] = child["size"] * 100 / row["size"] if row["size"] else 0
            row["children"].append(child_node)
            remaining_budget -= node_count - before
            remaining_size -= child["size"]
            remaining_logical -= child.get("logical_size", 0)
        if remaining_size > 0 and remaining_budget > 0:
            node_count += 1
            row["children"].append({"name": "Other files / folders", "path": None,
                "kind": "summary", "size": remaining_size, "allocated_size": remaining_size,
                "logical_size": max(0, remaining_logical), "children": [],
                "percent_parent": remaining_size * 100 / row["size"] if row["size"] else 0,
                "can_reveal": False, "complete": row.get("complete", False)})
            truncated = True
        return row

    root = build(aggregate_row(path, root_aggregate, metadata["source"]), 0, maxnodes)
    return metadata | {"root": root, "node_count": node_count, "truncated": truncated,
                       "depth": depth, "maxnodes": maxnodes}


def mounted_volumes() -> list[dict]:
    result = []
    candidates = [Path("/")]
    try: candidates.extend(p for p in Path("/Volumes").iterdir() if p.is_dir())
    except OSError: pass
    for p in candidates:
        try:
            u = shutil.disk_usage(p); result.append({"path": str(p), "name": "Macintosh HD" if p == Path("/") else p.name, "total_bytes": u.total, "free_bytes": u.free, "used_bytes": max(0, u.total - u.free), "internal": p == Path("/"), "apfs": sys_is_macos(), "source": "statvfs"})
        except OSError: pass
    return result


def spotlight_search(query: str, limit: int, root: Path = HOME) -> dict:
    query = query.strip()
    if not query: return {"results": [], "source": "spotlight", "partial": False, "complete": True}
    escaped = escape_mdfind_name(query)
    paths, partial, error = spotlight_paths(root, f"kMDItemFSName == '*{escaped}*'cd", limit, timeout=8)
    results = []
    for p in paths:
        st = stat_record(p)
        if st:
            reasons = trash_reasons(p)
            results.append({"path": str(p), "name": p.name, "kind": "directory" if p.is_dir() else "file", "size": st["allocated_size"] if p.is_file() else None, "logical_size": st["logical_size"] if p.is_file() else None, "allocated_size": st["allocated_size"], "modified": st["modified"], "location": human_location(p), "can_trash": not reasons, "trash_reason": reasons[0] if reasons else None})
    return {"results": results, "source": "spotlight", "partial": partial, "complete": not partial and not error, "error": error}


def compile_native_trash() -> Path | None:
    """Compatibility name: locate a prebuilt helper, never invoke a compiler.

    Macs without a shipped helper can use Reveal in Finder and native manual
    Trash. Merely opening the app must not prompt installation of Xcode tools.
    """
    if not sys_is_macos():
        return None
    architecture = os.uname().machine
    for candidate in (ROOT / "bin" / f"trash-helper-{architecture}", STATE_DIR / "trash_helper"):
        if candidate.is_file() and not candidate.is_symlink() and os.access(candidate, os.X_OK):
            return candidate
    return None


def preview_trash(paths: list[str]) -> dict:
    if not isinstance(paths, list) or not paths or len(paths) > 100:
        raise ValueError("paths must contain 1-100 items")
    items = []
    seen: set[str] = set()
    for raw in paths:
        if not isinstance(raw, str): raise ValueError("each path must be text")
        p = Path(raw).expanduser()
        if not p.is_absolute() or ".." in p.parts: raise ValueError("paths must be absolute without parent traversal")
        if str(p) in seen: continue
        seen.add(str(p))
        reasons = trash_reasons(p)
        st = stat_record(p)
        item = {"path": str(p), "name": p.name, "ctime_ns": st["ctime_ns"] if st else None, "mode": st["mode"] if st else None, "size": st["allocated_size"] if st else None, "exists": bool(st), "can_trash": bool(st and not reasons), "reasons": reasons, "logical_size": st["logical_size"] if st else 0, "allocated_size": st["allocated_size"] if st else 0, "mtime_ns": st["mtime_ns"] if st else None, "inode": st["inode"] if st else None, "device": st["device"] if st else None}
        items.append(item)
    if not all(i["can_trash"] for i in items): raise PermissionError("one or more paths are protected")
    if any(a != b and is_within(Path(a["path"]), Path(b["path"])) for a in items for b in items):
        raise ValueError("select either a folder or its descendants, not both")
    token = secrets.token_urlsafe(18)
    with CACHE_LOCK:
        expired = [key for key, value in CONFIRMATIONS.items() if time.time() - value["created"] > CONFIRM_TTL]
        for key in expired: CONFIRMATIONS.pop(key, None)
        if len(CONFIRMATIONS) >= 100: CONFIRMATIONS.pop(next(iter(CONFIRMATIONS)))
        CONFIRMATIONS[token] = {"created": time.time(), "items": items}
    return {"confirmation_token": token, "expires_in": CONFIRM_TTL, "items": items, "source": "review"}


def native_trash(items: list[dict]) -> dict:
    helper = compile_native_trash()
    if helper is None: raise RuntimeError("Native Trash is unavailable. Use Reveal in Finder, then move to Trash manually.")
    paths = [i["path"] for i in items]
    proc = subprocess.run([str(helper), *paths], capture_output=True, timeout=30)
    if proc.returncode != 0: raise RuntimeError("macOS Trash operation failed")
    return {"ok": True, "trashed": paths}


def confirm_trash(token: str) -> dict:
    if not isinstance(token, str): raise ValueError("confirmation_token is required")
    with CACHE_LOCK: review = CONFIRMATIONS.pop(token, None)
    if not review or time.time() - review["created"] > CONFIRM_TTL: raise PermissionError("confirmation token expired")
    for item in review["items"]:
        p = Path(item["path"]); st = stat_record(p)
        if not st or not can_trash(p) or st["inode"] != item["inode"] or st["device"] != item["device"] or any(st[k] != item[k] for k in ("mtime_ns", "ctime_ns", "mode", "logical_size", "allocated_size")):
            raise PermissionError("reviewed path changed or is protected")
    return native_trash(review["items"])


class LoopbackHTTPServer(ThreadingHTTPServer):
    """Bind locally without HTTPServer's blocking reverse-DNS lookup."""

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


class LoopbackHTTPServerV6(LoopbackHTTPServer):
    address_family = socket.AF_INET6


class Handler(BaseHTTPRequestHandler):
    server_version = "DiskSpaceAnalyzer/2.0"

    def log_message(self, fmt: str, *args) -> None:
        if self.path.startswith("/api/"): super().log_message(fmt, *args)

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store"); self.send_header("X-Content-Type-Options", "nosniff"); self.end_headers(); self.wfile.write(body)

    def error(self, status: int, message: str) -> None: self.send_json({"error": message, "status": status}, status)

    def read_json(self) -> dict:
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json": raise ValueError("Content-Type must be application/json")
        try: length = int(self.headers.get("Content-Length", "0"))
        except ValueError: raise ValueError("invalid Content-Length")
        if length < 0 or length > MAX_BODY: raise ValueError("invalid request body length")
        try: data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError): raise ValueError("invalid JSON")
        if not isinstance(data, dict): raise ValueError("JSON object required")
        return data

    def request_guard(self) -> None:
        if len(self.headers.get_all("Host", [])) != 1:
            raise PermissionError("one loopback Host header required")
        raw_host = self.headers.get("Host", "")
        try:
            parsed = urlparse("http://" + raw_host)
            host = parsed.hostname
            port = parsed.port or 80
        except ValueError:
            raise PermissionError("invalid Host")
        if (host not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment):
            raise PermissionError("loopback host required")
        if port != int(self.server.server_address[1]):
            raise PermissionError("loopback port required")
        origin = self.headers.get("Origin")
        if origin is not None and origin != "http://" + raw_host:
            raise PermissionError("same-origin request required")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise PermissionError("cross-site request rejected")

    def mutation_guard(self) -> None:
        self.request_guard()
        if not secrets.compare_digest(self.headers.get("X-Atlas-Token", ""), TOKEN):
            raise PermissionError("invalid service token")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            self.request_guard()
            if parsed.path.startswith("/api/"):
                self.api_get(parsed.path, parse_qs(parsed.query))
            else:
                self.serve_static(parsed.path)
        except ValueError as exc:
            self.error(400, str(exc))
        except PermissionError as exc:
            self.error(403, str(exc))
        except Exception:
            self.error(500, "request failed")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"): return self.error(404, "not found")
        try:
            self.mutation_guard(); self.api_post(parsed.path, self.read_json())
        except ValueError as exc: self.error(400, str(exc))
        except PermissionError as exc: self.error(403, str(exc))
        except RuntimeError as exc: self.error(503, str(exc))
        except Exception: self.error(500, "request failed")

    def api_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/health": self.send_json({"ok": True, "service": "disk-space-analyzer", "version": "0.2.1", "platform": os.uname().sysname, "home": str(HOME), "token": TOKEN, "index": {"available": sys_is_macos() and Path("/usr/bin/mdfind").exists(), "provider": "Spotlight" if sys_is_macos() else "filesystem"}, "trash": {"available": compile_native_trash() is not None, "fallback": "Reveal in Finder, then move to Trash manually"}, "defaults": {"scan_path": str(DEFAULT_SCAN_ROOT), "data_path": "/System/Volumes/Data" if Path("/System/Volumes/Data").is_dir() else None, "port": self.server.server_address[1]}})
        elif path == "/api/volumes": self.send_json({"volumes": mounted_volumes()})
        elif path in ("/api/scan/status", "/api/status"): self.send_json(STATE.snapshot())
        elif path == "/api/summary": self.send_json(STATE.snapshot() | {"volumes": mounted_volumes()})
        elif path == "/api/tree":
            raw = query.get("path", [str(STATE.path or DEFAULT_SCAN_ROOT)])[0]; limit = min(max(int(query.get("limit", ["200"])[0]), 1), MAX_TREE_RESULTS); self.send_json(tree_for(normalize_existing(raw, allow_file=True), limit))
        elif path == "/api/largest":
            root = normalize_existing(query.get("path", [str(HOME)])[0]); limit = min(max(int(query.get("limit", ["100"])[0]), 1), MAX_SEARCH_RESULTS)
            with STATE.lock: ready = bool(STATE.path and not STATE.running and STATE.has_coverage(root))
            if ready:
                with STATE.lock:
                    vals = [trash_fields(x[2]) for x in sorted(STATE.top_heap, key=lambda x: (-x[0], x[2].get("path", ""))) if is_within(Path(x[2]["path"]), root)][:limit]
                    status = STATE.snapshot()
                self.send_json({"files": vals, "source": status["source"], "partial": not status["complete"], "complete": status["complete"], "error": status["errors"][:1] if status["errors"] else None, "scanned_path": status["path"], "top_limit": TOP_FILES, "scope_filtered_top": root != STATE.path})
            else:
                vals, payload = spotlight_largest(root, limit); self.send_json(payload | {"files": vals})
        elif path == "/api/extensions":
            self.send_json(extension_stats(normalize_existing(query.get("path", [str(HOME)])[0])))
        elif path == "/api/treemap":
            self.send_json(treemap_for(normalize_existing(query.get("path", [str(HOME)])[0]), int(query.get("depth", ["3"])[0]), int(query.get("maxnodes", ["800"])[0])))
        elif path == "/api/apps":
            apps = list_apps()
            self.send_json({"apps": apps, "source": "du+filesystem", "complete": not any(x["size_partial"] for x in apps)})
        elif path == "/api/search": self.send_json(spotlight_search(query.get("q", [""])[0], min(max(int(query.get("limit", ["100"])[0]), 1), MAX_SEARCH_RESULTS), normalize_existing(query.get("path", [str(HOME)])[0])))
        else: self.error(404, "unknown endpoint")

    def api_post(self, path: str, data: dict) -> None:
        if path == "/api/scan": self.send_json(start_scan(data.get("path")), 202)
        elif path == "/api/scan/cancel": self.send_json(cancel_scan())
        elif path == "/api/trash/preview": self.send_json(preview_trash(data.get("paths")))
        elif path == "/api/trash": self.send_json(confirm_trash(data.get("confirmation_token")))
        elif path == "/api/reveal":
            p = normalize_existing(data.get("path"), allow_file=True); subprocess.Popen(["/usr/bin/open", "-R", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); self.send_json({"ok": True, "path": str(p)})
        else: self.error(404, "unknown endpoint")

    def serve_static(self, raw_path: str) -> None:
        requested = unquote(raw_path)
        if requested in ("", "/"): requested = "/index.html"
        if Path(requested).name not in {"index.html", "app.js", "styles.css", "classic.html", "classic.js", "classic.css"} or "/" in requested.strip("/"):
            return self.error(404, "not found")
        for base in (UI_ROOT, STATIC_ROOT):
            p = (base / Path(requested).name).resolve()
            if p.is_file() and is_within(p, base):
                content = p.read_bytes(); ctype, _ = mimetypes.guess_type(str(p)); self.send_response(200); self.send_header("Content-Type", ctype or "application/octet-stream"); self.send_header("Content-Length", str(len(content))); self.end_headers(); self.wfile.write(content); return
        self.error(404, "not found")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local macOS disk analyzer")
    parser.add_argument("--host", default=os.environ.get("DISK_ANALYZER_HOST", "127.0.0.1")); parser.add_argument("--port", type=int, default=int(os.environ.get("DISK_ANALYZER_PORT", "8799"))); args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("--host must be a loopback address")
    server_class = LoopbackHTTPServerV6 if args.host == "::1" else LoopbackHTTPServer
    # Keep even the optional localhost spelling independent of DNS resolution.
    bind_host = "127.0.0.1" if args.host == "localhost" else args.host
    load_snapshot(); server = server_class((bind_host, args.port), Handler)
    print(f"Disk Space Analyzer listening on http://{args.host}:{args.port}", flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
