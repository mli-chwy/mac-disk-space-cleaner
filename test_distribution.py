"""Regression coverage for local startup and the public source distribution."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

import launcher
from server import LoopbackHTTPServer

_spec = importlib.util.spec_from_file_location("build_release", Path(__file__).parent / "scripts/build_release.py")
packaging = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(packaging)


@contextmanager
def http_service(payload):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

    server = LoopbackHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class LauncherTests(unittest.TestCase):
    def test_occupied_unrelated_and_old_services_remain_available(self):
        for payload in ({"service": "another-app"},
                        {"ok": True, "service": "disk-space-analyzer", "version": "0.1.0"}):
            with self.subTest(payload=payload), http_service(payload) as port, tempfile.TemporaryDirectory() as tmp:
                with patch.object(launcher.subprocess, "Popen") as spawn:
                    with self.assertRaisesRegex(RuntimeError, "已被"):
                        launcher.ensure_server(port, Path(tmp))
                    spawn.assert_not_called()
                self.assertEqual(launcher.health(port), payload)

    def test_current_service_is_reused_even_with_proxy_environment(self):
        payload = {"ok": True, "service": "disk-space-analyzer", "version": launcher.VERSION}
        with http_service(payload) as port, tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "no_proxy": ""}), \
                    patch.object(launcher.subprocess, "Popen") as spawn:
                launcher.ensure_server(port, Path(tmp))
                spawn.assert_not_called()

    def test_simultaneous_launches_start_only_one_detached_service(self):
        # Double-clicks create separate launcher processes. Avoid making this a
        # test of fork-from-worker-thread behavior on macOS or mocking Popen.
        # Stage logs and timed stack dumps make a slow bind/DNS lookup visible.
        fixture = '''import faulthandler, json, os, time
faulthandler.dump_traceback_later(4, repeat=True)
print('fixture: boot', flush=True)
from http.server import BaseHTTPRequestHandler
from backend import LoopbackHTTPServer
from pathlib import Path
import argparse
p=argparse.ArgumentParser(); p.add_argument('--host'); p.add_argument('--port', type=int); a=p.parse_args()
state=Path(os.environ['ATLAS_STATE_DIR'])
with (state/'test-starts').open('a') as f: f.write(str(os.getpid())+'\\n')
time.sleep(0.3)
print('fixture: before production LoopbackHTTPServer bind', flush=True)
class Handler(BaseHTTPRequestHandler):
 def log_message(self, *a): pass
 def do_GET(self):
  self.send_response(200); self.end_headers()
  self.wfile.write(json.dumps({'ok':True,'service':'disk-space-analyzer','version':__SERVICE_VERSION__}).encode())
server=LoopbackHTTPServer((a.host,a.port), Handler)
print('fixture: bound; entering serve_forever', flush=True)
server.serve_forever()
'''.replace("__SERVICE_VERSION__", json.dumps(launcher.VERSION))
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            runtime, state = base / "app resources", base / "state"
            runtime.mkdir()
            shutil.copyfile(Path(launcher.__file__), runtime / "launcher.py")
            shutil.copyfile(Path(__file__).with_name("server.py"), runtime / "backend.py")
            (runtime / "server.py").write_text(fixture, encoding="utf-8")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            processes = []
            output_paths = [base / f"launcher-{n}.log" for n in range(2)]
            driver = (
                "import faulthandler; faulthandler.dump_traceback_later(4, repeat=True); "
                "import launcher, pathlib, sys; "
                "print('launcher: entering ensure_server', flush=True); "
                "launcher.ensure_server(int(sys.argv[1]), pathlib.Path(sys.argv[2]), 8.0); "
                "print('launcher: ready', flush=True)"
            )
            try:
                for output_path in output_paths:
                    with output_path.open("wb") as output:
                        processes.append(launcher.subprocess.Popen(
                            [sys.executable, "-c", driver, str(port), str(state)], cwd=runtime,
                            stdout=output, stderr=output, stdin=launcher.subprocess.DEVNULL,
                        ))
                for process in processes:
                    self.assertEqual(process.wait(timeout=12), 0)
                starts = (state / "test-starts").read_text().splitlines()
                self.assertEqual(len(starts), 1)
                info = json.loads((state / f"server-{port}.json").read_text())
                self.assertEqual(info["pid"], int(starts[0]))
                self.assertEqual(info["source"], str(runtime))
                self.assertTrue(launcher.matching(launcher.health(port)))
            except Exception as exc:
                sections = []
                # GitHub annotations truncate large payloads. Keep the server
                # first and retain every process's tail within one short error.
                for path in [state / "logs/server.log"] + output_paths:
                    content = path.read_text(errors="replace") if path.exists() else "(not created)"
                    if path.name == "server.log":
                        stages = "\n".join(line for line in content.splitlines() if line.startswith("fixture:"))[:150]
                        content = stages + "\n[tail]\n" + content[-750:]
                    else:
                        content = content[-900:]
                    sections.append(f"{path.name}:\n{content}"[:1000])
                message = f"Concurrent launcher regression: {str(exc)[:120]}\n" + "\n".join(sections)
                raise AssertionError(message[:3000]) from None
            finally:
                # Only launchers and fixture PIDs created by this test are ended.
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=5)
                marker = state / "test-starts"
                for pid in marker.read_text().splitlines() if marker.exists() else []:
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                    except ProcessLookupError:
                        pass


class SourceDistributionTests(unittest.TestCase):
    def source_fixture(self, root):
        for name in packaging.REQUIRED:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("public source\n", encoding="utf-8")

    def test_release_contains_runnable_tests_and_omits_local_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "src"
            source.mkdir()
            self.source_fixture(source)
            for name in (".state/scan.json", "logs/server.log", "__pycache__/secret.pyc", ".env", "private.txt"):
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("LOCAL_PRIVATE_DATA", encoding="utf-8")
            archive, checksums = packaging.build(source, base / "dist", "0.2.0")
            with zipfile.ZipFile(archive) as zipped:
                names = {name.split("/", 1)[1] for name in zipped.namelist()}
                self.assertIn("test_server.py", names)
                self.assertIn("test_distribution.py", names)
                self.assertIn("scripts/install_macos.sh", names)
                self.assertTrue(names.isdisjoint({".env", "private.txt", ".state/scan.json", "logs/server.log", "__pycache__/secret.pyc"}))
                for info in zipped.infolist():
                    self.assertNotIn(b"LOCAL_PRIVATE_DATA", zipped.read(info))
                    if info.filename.endswith(".command"):
                        self.assertEqual((info.external_attr >> 16) & 0o777, 0o755)
            expected = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertEqual(checksums.read_text(), f"{expected}  {archive.name}\n")
            with self.assertRaises(FileExistsError):
                packaging.build(source, base / "dist", "0.2.0")

    def test_symlink_cannot_smuggle_private_file_into_source_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.source_fixture(root)
            private = root / "private.txt"
            private.write_text("PRIVATE")
            (root / "server.py").unlink()
            (root / "server.py").symlink_to(private)
            with self.assertRaisesRegex(ValueError, "unsafe"):
                packaging.build(root, root / "dist", "0.2.0")
            self.assertFalse((root / "dist").exists())


if __name__ == "__main__":
    unittest.main()
