"""Backend regression tests. All writes stay in disposable synthetic fixtures."""
import errno
import http.client
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("atlas_server", Path(__file__).with_name("server.py"))
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="atlas-backend-test-")
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "fixture"
        self.root.mkdir()
        self.patches = [mock.patch.object(server, "STATE", server.ScanState()),
                        mock.patch.object(server, "STATE_DIR", self.base / "state"),
                        mock.patch.object(server, "SNAPSHOT_PATH", self.base / "state" / "scan.json")]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        if server.STATE.running:
            server.cancel_scan()
            self.wait_scan()
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def wait_scan(self):
        deadline = time.monotonic() + 5
        while server.STATE.running and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(server.STATE.running)

    def scan(self):
        server.start_scan(str(self.root))
        self.wait_scan()

    def test_scoped_extensions_counts_sparse_hardlinks_and_snapshot(self):
        a = self.root / "a"; a.mkdir()
        (a / "one.txt").write_bytes(b"a" * 9000)
        os.link(a / "one.txt", a / "hard.txt")
        with (a / "sparse.bin").open("wb") as stream:
            stream.truncate(8 * 1024 * 1024)
        (self.root / "two.log").write_bytes(b"b" * 5000)
        (self.root / "link").symlink_to(a, target_is_directory=True)
        self.scan()
        status = server.STATE.snapshot()
        self.assertTrue(status["complete"])
        allocated = sum((p.stat().st_blocks * 512) for p in [a / "one.txt", a / "sparse.bin", self.root / "two.log"])
        self.assertEqual(status["allocated_bytes"], allocated)
        tree = server.tree_for(self.root, 100)
        self.assertEqual((tree["files"], tree["folders"], tree["items"]), (4, 1, 5))
        self.assertEqual(tree["size"], allocated)
        subtree = server.tree_for(a, 100)
        self.assertEqual(sum(row["size"] for row in subtree["children"]), subtree["size"])
        ext = server.extension_stats(a)
        self.assertEqual(ext["count"], 3)
        self.assertEqual({x["extension"] for x in ext["extensions"]}, {"txt", "bin"})
        self.assertEqual(sum(x["size"] for x in ext["extensions"]), allocated - (self.root / "two.log").stat().st_blocks * 512)
        self.assertEqual(server.extension_stats(self.base)["extensions"], [])
        saved_time = status["finished_at"]
        server.STATE = server.ScanState()
        server.load_snapshot()
        self.assertEqual(server.STATE.snapshot()["finished_at"], saved_time)
        self.assertEqual(server.extension_stats(a)["count"], 3)

    def test_permission_errors_local_completeness_and_usable_partial(self):
        (self.root / "denied").mkdir()
        good = self.root / "good"; good.mkdir(); (good / "ok.txt").write_text("ok")
        real_scandir = os.scandir
        def fake_scandir(path):
            if Path(path).name == "denied":
                raise PermissionError(errno.EACCES, "Permission denied", str(path))
            return real_scandir(path)
        with mock.patch.object(server.os, "scandir", side_effect=fake_scandir):
            self.scan()
        self.assertEqual(server.STATE.snapshot()["error_count"], 1)
        tree = server.tree_for(self.root, 100)
        self.assertFalse(tree["complete"])
        self.assertTrue(tree["partial"])
        self.assertFalse(tree["requires_scan"])
        self.assertEqual(tree["folders"], 2)
        self.assertTrue(server.tree_for(good, 100)["complete"])
        self.assertTrue(server.extension_stats(good)["complete"])
        self.assertEqual(server.treemap_for(self.root)["error_count"], 1)

    def test_empty_scan_and_cancel_before_worker_reset(self):
        self.scan()
        self.assertTrue(server.tree_for(self.root, 100)["complete"])
        self.assertEqual(server.extension_stats(self.root)["count"], 0)
        original = server.scan_worker
        entered = threading.Event(); release = threading.Event()
        def delayed(*args):
            entered.set(); release.wait(2); original(*args)
        with mock.patch.object(server, "scan_worker", side_effect=delayed):
            result = server.start_scan(str(self.root))
            self.assertTrue(entered.wait(1))
            self.assertEqual(server.STATE.scan_id, result["scan_id"])
            self.assertEqual(server.start_scan(str(self.root))["already_running"], True)
            server.cancel_scan(); release.set(); self.wait_scan()
        self.assertTrue(server.STATE.cancelled)
        self.assertFalse(server.STATE.complete)

    def test_top_bound_full_extensions_and_treemap_budget(self):
        # More files than the top file bound: extension totals must still include all.
        with mock.patch.object(server, "TOP_FILES", 10):
            for n in range(30):
                folder = self.root / str(n % 5); folder.mkdir(exist_ok=True)
                (folder / f"file{n}.dat").write_bytes(b"x" * (n + 1) * 200)
            self.scan()
            self.assertEqual(len(server.STATE.top_heap), 10)
        self.assertEqual(server.extension_stats(self.root)["count"], 30)
        result = server.treemap_for(self.root, 99, 12)
        def check(node):
            if node["children"]:
                self.assertEqual(sum(x["size"] for x in node["children"]), node["size"])
            for child in node["children"]:
                if child["kind"] == "summary":
                    self.assertIsNone(child["path"])
                    self.assertFalse(child["can_reveal"])
                check(child)
            return 1 + sum(count(child) for child in node["children"])
        def count(node):
            return 1 + sum(count(child) for child in node["children"])
        check(result["root"])
        self.assertEqual(count(result["root"]), result["node_count"])
        self.assertLessEqual(result["node_count"], 12)
        self.assertLessEqual(result["depth"], 4)
        self.assertTrue(result["truncated"])

    def test_apfs_root_policy_and_no_symlink_follow(self):
        with mock.patch.object(server, "sys_is_macos", return_value=True):
            self.assertTrue(server.scan_policy(Path("/"), Path("/Users"), 22, {11, 22}))
            self.assertFalse(server.scan_policy(Path("/"), Path("/System/Volumes"), 22, {11, 22}))
            self.assertFalse(server.scan_policy(Path("/"), Path("/Volumes"), 22, {11, 22}))
            self.assertFalse(server.scan_policy(Path("/System/Volumes/Data"), Path("/System/Volumes/Data/Volumes"), 22, {22}))
            self.assertFalse(server.scan_policy(self.root, self.root / "mounted", 33, {22}))
        outside = self.base / "outside"; outside.mkdir(); (outside / "secret.txt").write_text("outside")
        (self.root / "alias").symlink_to(outside, target_is_directory=True)
        self.scan()
        self.assertEqual(server.STATE.files_scanned, 0)

    def test_rebinding_origin_and_token_guards(self):
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
        def request(path, headers=None, method="GET", data=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request(method, path, data, headers or {})
            response = conn.getresponse(); body = response.read(); conn.close()
            return response.status, json.loads(body)
        try:
            self.assertEqual(request("/api/health", {"Host": f"attacker.example:{port}"})[0], 403)
            self.assertEqual(request("/api/health", {"Origin": "http://attacker.example"})[0], 403)
            self.assertEqual(request("/api/health", {"Sec-Fetch-Site": "cross-site"})[0], 403)
            self.assertEqual(request("/api/health")[0], 200)
            self.assertEqual(request("/api/health", {"Host": "127.0.0.1:1"})[0], 403)
            self.assertEqual(request("/server.py")[0], 404)
            self.assertEqual(request("/api/scan/cancel", {"Content-Type": "application/json"}, "POST", "{}")[0], 403)
            headers = {"Content-Type": "application/json", "X-Atlas-Token": server.TOKEN}
            self.assertEqual(request("/api/scan/cancel", headers, "POST", "{}")[0], 200)
        finally:
            httpd.shutdown(); httpd.server_close(); thread.join(2)

    def test_trash_preview_protects_symlinks_unchanged_file_and_never_autocompiles(self):
        home = self.base / "home"; docs = home / "Documents"; docs.mkdir(parents=True)
        target = docs / "test.txt"; target.write_text("safe fixture")
        with mock.patch.object(server, "HOME", home), mock.patch.object(server, "HOME_STANDARD_ROOTS", {docs}), mock.patch.object(server, "sys_is_macos", return_value=False):
            self.assertFalse(server.can_trash(docs))
            alias = home / "alias"; alias.symlink_to(docs, target_is_directory=True)
            with self.assertRaises(PermissionError): server.preview_trash([str(alias / "test.txt")])
            with self.assertRaises(ValueError): server.preview_trash([str(docs / ".." / "Documents" / "test.txt")])
            review = server.preview_trash([str(target)])
            target.write_text("changed fixture")
            with mock.patch.object(server, "native_trash") as trash:
                with self.assertRaises(PermissionError): server.confirm_trash(review["confirmation_token"])
                trash.assert_not_called()
            review = server.preview_trash([str(target)])
            with mock.patch.object(server, "native_trash", return_value={"ok": True, "trashed": [str(target)]}) as trash:
                self.assertTrue(server.confirm_trash(review["confirmation_token"])["ok"])
                self.assertEqual(trash.call_count, 1)
            self.assertTrue(target.exists())  # No deletion is performed by this suite.
        with mock.patch.object(server.subprocess, "run") as run:
            server.compile_native_trash()
            run.assert_not_called()

    def test_nonloopback_cli_rejected(self):
        result = subprocess.run([sys.executable, str(Path(server.__file__)), "--host", "0.0.0.0"], capture_output=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"loopback", result.stderr)


if __name__ == "__main__":
    unittest.main()
