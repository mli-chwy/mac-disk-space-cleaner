#!/usr/bin/env python3
"""Start one local service, verify its identity, then open the classic UI."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

VERSION = "0.3.0"
SERVICE = "disk-space-analyzer"
ROOT = Path(__file__).resolve().parent
DEFAULT_STATE = Path.home() / "Library" / "Application Support" / "磁盘空间管理"


def health(port: int) -> dict | None:
    # Never send loopback requests to an environment-configured HTTP proxy.
    try:
        with build_opener(ProxyHandler({})).open(
            f"http://127.0.0.1:{port}/api/health", timeout=1.0
        ) as response:
            value = json.loads(response.read(65536))
            return value if isinstance(value, dict) else None
    except (OSError, URLError, ValueError):
        return None


def matching(value: dict | None) -> bool:
    return bool(value and value.get("ok") is True
                and value.get("service") == SERVICE and value.get("version") == VERSION)


def port_busy(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def ensure_server(port: int, state_dir: Path, timeout: float = 25.0) -> None:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    logs = state_dir / "logs"
    logs.mkdir(mode=0o700, exist_ok=True)
    lock_path = state_dir / f"launcher-{port}.lock"
    with lock_path.open("a+") as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("另一个启动过程仍在运行，请稍后重试。")
                time.sleep(0.15)
        current = health(port)
        if matching(current):
            return
        if current or port_busy(port):
            if current and current.get("service") == SERVICE:
                detail = f"磁盘空间管理 {current.get('version', '旧版本')}"
            else:
                detail = "其他程序或尚未就绪的服务"
            raise RuntimeError(
                f"端口 {port} 已被{detail}占用。请先退出旧服务，或用 --port 指定其他端口。"
                "启动器没有结束任何进程。")
        env = dict(os.environ, ATLAS_STATE_DIR=str(state_dir), PYTHONUNBUFFERED="1")
        log_path = logs / "server.log"
        with log_path.open("ab", buffering=0) as log:
            process = subprocess.Popen(
                [sys.executable, str(ROOT / "server.py"), "--host", "127.0.0.1", "--port", str(port)],
                cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"服务启动失败（退出码 {process.returncode}）。日志：{log_path}")
            if matching(health(port)):
                (state_dir / f"server-{port}.json").write_text(json.dumps({
                    "pid": process.pid, "version": VERSION, "port": port,
                    "source": str(ROOT), "started_at": time.time(),
                }, ensure_ascii=False), encoding="utf-8")
                return
            time.sleep(0.2)
        raise RuntimeError(f"服务未在 {int(timeout)} 秒内就绪。请检查日志后重试：{log_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="启动磁盘空间管理本地服务")
    parser.add_argument("--port", type=int, default=int(os.environ.get("DISK_ANALYZER_PORT", "8799")))
    parser.add_argument("--no-open", action="store_true", help="验证服务后不打开浏览器")
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        raise RuntimeError("需要 Python 3.10 或更新版本：https://www.python.org/downloads/macos/")
    if not 1024 <= args.port <= 65535:
        raise RuntimeError("端口必须在 1024 到 65535 之间。")
    state_dir = Path(os.environ.get("ATLAS_STATE_DIR", str(DEFAULT_STATE))).expanduser().resolve()
    ensure_server(args.port, state_dir)
    url = f"http://127.0.0.1:{args.port}/classic.html"
    if not args.no_open:
        if sys.platform != "darwin":
            raise RuntimeError("桌面启动入口仅支持 macOS；调试服务可用 --no-open。")
        subprocess.run(["/usr/bin/open", url], check=True)
    print(url)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"磁盘空间管理：{exc}", file=sys.stderr)
        raise SystemExit(1)
