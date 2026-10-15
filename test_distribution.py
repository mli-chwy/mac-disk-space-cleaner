"""Regression coverage for local startup and the public source distribution."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

import launcher

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

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
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
        payload = {"ok": True, "service": "disk-space-analyzer", "version": "0.2.0"}
        with http_service(payload) as port, tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "no_proxy": ""}), \
                    patch.object(launcher.subprocess, "Popen") as spawn:
                launcher.ensure_server(port, Path(tmp))
                spawn.assert_not_called()

    def test_simultaneous_launches_start_only_one_detached_service(self):
        # A tiny process fixture exercises locking, readiness, and a copied app
        # runtime without reading or scanning any user files.
        fixture = '''import json, os, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import argparse
p=argparse.ArgumentParser(); p.add_argument('--host'); p.add_argument('--port', type=int); a=p.parse_args()
state=Path(os.environ['ATLAS_STATE_DIR'])
with (state/'test-starts').open('a') as f: f.write(str(os.getpid())+'\\n')
time.sleep(0.3)
class Handler(BaseHTTPRequestHandler):
 def log_message(self, *a): pass
 def do_GET(self):
  self.send_response(200); self.end_headers()
  self.wfile.write(json.dumps({'ok':True,'service':'disk-space-analyzer','version':'0.2.0'}).encode())
HTTPServer((a.host,a.port), Handler).serve_forever()
'''
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime, state = base / "app resources", base / "state"
            runtime.mkdir()
            (runtime / "server.py").write_text(fixture, encoding="utf-8")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            processes = []
            real_popen = launcher.subprocess.Popen

            def remember_process(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                processes.append(process)
                return process

            try:
                with patch.object(launcher, "ROOT", runtime), \
                        patch.object(launcher.subprocess, "Popen", side_effect=remember_process), \
                        ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(launcher.ensure_server, port, state, 8.0) for _ in range(2)]
                    for future in futures:
                        future.result(timeout=12)
                starts = (state / "test-starts").read_text().splitlines()
                self.assertEqual(len(starts), 1)
                info = json.loads((state / f"server-{port}.json").read_text())
                self.assertEqual(info["pid"], int(starts[0]))
                self.assertEqual(info["source"], str(runtime))
                self.assertTrue(launcher.matching(launcher.health(port)))
            finally:
                # Only process objects created by this isolated test are ended.
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=5)


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
