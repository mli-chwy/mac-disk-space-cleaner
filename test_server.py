"""Backend regression tests. All writes stay in disposable synthetic fixtures."""
import errno
import http.client
import importlib.util
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

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
                        mock.patch.object(server, "QUICK_CACHE", {}),
                        mock.patch.object(server, "QUICK_INFLIGHT", {}),
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
        httpd = server.LoopbackHTTPServer(("127.0.0.1", 0), server.Handler)
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

    def test_loopback_server_binds_without_reverse_dns(self):
        original_family = server.ThreadingHTTPServer.address_family
        with mock.patch.object(socket, "getfqdn", side_effect=AssertionError("reverse DNS must not run")) as lookup:
            with server.LoopbackHTTPServer(("127.0.0.1", 0), server.Handler) as httpd:
                self.assertEqual(httpd.server_name, "localhost")
                self.assertEqual(httpd.server_port, httpd.server_address[1])
                self.assertGreater(httpd.server_port, 0)
            if socket.has_ipv6:
                try:
                    ipv6 = server.LoopbackHTTPServerV6(("::1", 0), server.Handler)
                except OSError as exc:
                    if exc.errno not in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL):
                        raise
                else:
                    with ipv6:
                        self.assertEqual(ipv6.server_name, "localhost")
                        self.assertEqual(ipv6.server_port, ipv6.server_address[1])
                        self.assertEqual(ipv6.address_family, socket.AF_INET6)
            lookup.assert_not_called()
        self.assertEqual(server.ThreadingHTTPServer.address_family, original_family)

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

    def test_quick_snapshot_shared_scoped_sparse_hardlinks_and_expired_consumers(self):
        child = self.root / "child"; child.mkdir()
        one = child / "one.txt"; one.write_bytes(b"a" * 9000)
        hard = child / "two.txt"; os.link(one, hard)
        sparse = child / "sparse.bin"
        with sparse.open("wb") as stream: stream.truncate(4 * 1024 * 1024)
        other = self.root / "other.log"; other.write_bytes(b"b" * 5000)
        outside = self.base / "outside.txt"; outside.write_bytes(b"outside")
        alias = self.root / "alias"; alias.symlink_to(child, target_is_directory=True)
        link = self.root / "link.txt"; link.symlink_to(one)
        hidden = self.root / "not-indexed.txt"; hidden.write_bytes(b"not indexed")
        candidates = [other, one, hard, sparse, outside, alias / "one.txt", link, child]
        query_metadata = {"available": True, "query_complete": True, "error": None}
        old_state = server.STATE.snapshot()
        with mock.patch.object(server, "QUICK_THRESHOLD", 1), mock.patch.object(server, "quick_spotlight_candidates", return_value=(candidates, query_metadata)) as query, mock.patch.object(server.os, "scandir", side_effect=AssertionError("quick mode must not walk")):
            snapshot, hit = server.quick_snapshot(self.root)
            self.assertFalse(hit)
            snapshot["created_at"] -= server.SPOTLIGHT_TTL + 1
            tree = server.quick_tree_for(child, self.root, 100)
            extensions = server.quick_extension_stats(child, self.root)
            treemap = server.quick_treemap_for(child, self.root)
            largest = server.quick_largest(child, self.root, 100)
            self.assertEqual(query.call_count, 1)
            self.assertEqual({x["snapshot_id"] for x in [tree, extensions, treemap, largest]}, {snapshot["metadata"]["snapshot_id"]})
            allocated = one.stat().st_blocks * 512 + sparse.stat().st_blocks * 512
            self.assertEqual(tree["size"], allocated)
            self.assertEqual(tree["files"], 3)
            self.assertEqual(sum(x["size"] for x in tree["children"]), allocated)
            self.assertEqual(extensions["size"], allocated)
            self.assertEqual(extensions["count"], 3)
            self.assertEqual(treemap["root"]["size"], allocated)
            self.assertEqual(snapshot["metadata"]["indexed_file_count"], 4)
            self.assertEqual(tree["source"], "spotlight")
            self.assertFalse(tree["complete"])
            self.assertTrue(tree["partial"])
            self.assertFalse(tree["requires_scan"])
            with self.assertRaises(ValueError): server.quick_tree_for(self.base, self.root, 100)
            new, hit = server.quick_snapshot(self.root)
            self.assertEqual(query.call_count, 2)  # /quick alone applies TTL.
            self.assertFalse(hit)
            self.assertNotEqual(snapshot["metadata"]["id"], new["metadata"]["id"])
            server.quick_snapshot(self.root, refresh=True)
            self.assertEqual(query.call_count, 3)
        self.assertEqual(server.STATE.snapshot(), old_state)
        self.assertFalse(server.STATE.cancel_event.is_set())

    def test_running_scan_publishes_only_completed_subtrees(self):
        ready = self.root / "a"; ready.mkdir(); (ready / "ok.txt").write_bytes(b"x" * 5000)
        busy = self.root / "b"; busy.mkdir(); (busy / "later.bin").write_bytes(b"y" * 5000)
        real_scandir = os.scandir
        blocked = threading.Event(); release = threading.Event()
        class SortedEntries:
            def __init__(self, path):
                with real_scandir(path) as entries:
                    self.entries = iter(sorted(entries, key=lambda entry: entry.name))
            def __next__(self): return next(self.entries)
            def __iter__(self): return self
            def close(self): pass
        def scan_entries(path):
            if Path(path) == self.root:
                return SortedEntries(path)
            if Path(path) == busy:
                blocked.set(); release.wait(3)
            return real_scandir(path)
        with mock.patch.object(server.os, "scandir", side_effect=scan_entries):
            server.start_scan(str(self.root))
            try:
                self.assertTrue(blocked.wait(1))
                status = server.STATE.snapshot()
                self.assertTrue(status["running"])
                self.assertEqual(status["current_directory"], str(busy))
                self.assertEqual(status["dirs_completed"], 1)
                for result in [server.tree_for(ready, 100), server.extension_stats(ready), server.treemap_for(ready)]:
                    self.assertTrue(result["complete"])
                    self.assertFalse(result["requires_scan"])
                self.assertEqual(server.extension_stats(ready)["count"], 1)
                self.assertTrue(server.scope_metadata(self.root)["requires_scan"])
                self.assertTrue(server.scope_metadata(busy)["requires_scan"])
            finally:
                release.set(); self.wait_scan()
        self.assertEqual(server.STATE.snapshot()["dirs_completed"], 3)
        self.assertIsNone(server.STATE.snapshot()["current_directory"])

    def test_completed_tree_reads_direct_children_and_treemap_limits_scope_depth(self):
        child = self.root / "a"; child.mkdir()
        deep = child / "deep"; deep.mkdir(); (deep / "file.bin").write_bytes(b"x" * 5000)
        self.scan()
        class DirectLookupOnly(dict):
            def items(self): raise AssertionError("directory tree must not enumerate the whole snapshot")
            def __iter__(self): raise AssertionError("directory tree must not copy the whole snapshot")
        with mock.patch.object(server.STATE, "dir_sizes", DirectLookupOnly(server.STATE.dir_sizes)):
            result = server.tree_for(self.root, 100)
            self.assertEqual(result["files"], 1)
            self.assertEqual(len(result["children"]), 1)
            self.assertEqual(result["children"][0]["size"], result["size"])
        original = server.render_treemap
        server.STATE.dir_sizes["/unrelated/path"] = server.empty_aggregate()
        with mock.patch.object(server, "render_treemap", wraps=original) as render:
            server.treemap_for(self.root, depth=1)
            self.assertEqual(set(render.call_args.args[1]), {str(self.root), str(child)})

    def test_quick_empty_unavailable_cap_and_query_failure_are_partial(self):
        for metadata in [
            {"available": True, "query_complete": True, "error": None},
            {"available": False, "query_complete": False, "error": "Spotlight unavailable"},
            {"available": True, "query_complete": False, "timed_out": True, "error": None},
        ]:
            with self.subTest(metadata=metadata), mock.patch.object(server, "quick_spotlight_candidates", return_value=([], metadata)), mock.patch.object(server.os, "scandir", side_effect=AssertionError("no fallback scan")):
                snapshot, _ = server.quick_snapshot(self.root, refresh=True)
                result = server.quick_tree_for(self.root, self.root, 100)
                self.assertEqual(result["children"], [])
                self.assertEqual(result["available"], metadata["available"])
                self.assertEqual(result["query_complete"], metadata["query_complete"])
                self.assertFalse(result["complete"])
                self.assertTrue(result["partial"])
                self.assertEqual(result["error_count"], int(bool(metadata["error"])))
                self.assertEqual(result["indexed_file_count"], 0)
                self.assertEqual(server.quick_treemap_for(self.root, self.root)["root"]["children"], [])
        files = []
        for n in range(4):
            path = self.root / f"{n}.bin"; path.write_bytes(b"fixture"); files.append(path)
        with mock.patch.object(server, "QUICK_THRESHOLD", 1), mock.patch.object(server, "QUICK_MAX_CANDIDATES", 2), mock.patch.object(server, "quick_spotlight_candidates", return_value=(files, {"available": True, "query_complete": True, "error": None})):
            snapshot = server.build_quick_snapshot(self.root)
        self.assertEqual(snapshot["metadata"]["candidate_count"], 2)
        self.assertEqual(snapshot["metadata"]["indexed_file_count"], 2)
        self.assertTrue(snapshot["metadata"]["limit_reached"])
        self.assertFalse(snapshot["metadata"]["query_complete"])

    def test_quick_concurrent_panels_coalesce_one_query(self):
        entered = threading.Event(); release = threading.Event()
        results = []; errors = []
        def query(root):
            entered.set(); release.wait(2)
            return [], {"available": True, "query_complete": True, "error": None}
        def consume():
            try: results.append(server.quick_scope(self.root, self.root)[1])
            except Exception as exc: errors.append(exc)
        with mock.patch.object(server, "quick_spotlight_candidates", side_effect=query) as call:
            threads = [threading.Thread(target=consume) for _ in range(5)]
            for thread in threads: thread.start()
            self.assertTrue(entered.wait(1))
            release.set()
            for thread in threads: thread.join(2)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertFalse(errors)
            self.assertEqual(call.call_count, 1)
            self.assertEqual(len(results), 5)
            self.assertEqual(len({row["snapshot_id"] for row in results}), 1)

    def test_quick_stream_timeout_and_limit_keep_only_complete_records(self):
        real_popen = subprocess.Popen
        paths = [self.root / f"{n}.bin" for n in range(5)]
        payload = b"\0".join(os.fsencode(path) for path in paths) + b"\0"
        def delayed_query(args, **kwargs):
            self.assertEqual(args[-1], f"kMDItemFSSize >= {server.QUICK_THRESHOLD}")
            script = f"import os,time; os.write(1,{payload!r}+b'/unfinished'); time.sleep(3)"
            return real_popen([sys.executable, "-c", script], **kwargs)
        with mock.patch.object(server, "sys_is_macos", return_value=True), mock.patch.object(server.subprocess, "Popen", side_effect=delayed_query), mock.patch.object(server, "QUICK_TIMEOUT", .2):
            started = time.monotonic()
            found, metadata = server.quick_spotlight_candidates(self.root)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(found, paths)
            self.assertTrue(metadata["timed_out"])
            self.assertFalse(metadata["query_complete"])
            with mock.patch.object(server, "QUICK_MAX_CANDIDATES", 2):
                found, metadata = server.quick_spotlight_candidates(self.root)
                self.assertEqual(found, paths[:2])
                self.assertTrue(metadata["limit_reached"])
                self.assertFalse(metadata["query_complete"])

    def test_quick_http_routes_share_snapshot_and_validate_scope(self):
        httpd = server.LoopbackHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
        def request(endpoint, **query):
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=2)
            conn.request("GET", endpoint + "?" + urlencode(query))
            response = conn.getresponse(); body = json.loads(response.read()); conn.close()
            return response.status, body
        try:
            with mock.patch.object(server, "quick_spotlight_candidates", return_value=([], {"available": True, "query_complete": True, "error": None})) as query:
                status, first = request("/api/quick", path=str(self.root))
                self.assertEqual(status, 200)
                for endpoint in ("tree", "extensions", "treemap", "largest"):
                    with self.subTest(endpoint=endpoint):
                        status, result = request("/api/" + endpoint, path=str(self.root), mode="quick", scope=str(self.root))
                        self.assertEqual(status, 200)
                        self.assertEqual(result["snapshot_id"], first["snapshot_id"])
                self.assertEqual(request("/api/tree", path=str(self.root), mode="quick", root=str(self.root))[0], 200)
                self.assertEqual(request("/api/tree", path=str(self.base), mode="quick", scope=str(self.root))[0], 400)
                self.assertEqual(query.call_count, 1)
                self.assertEqual(request("/api/quick", path=str(self.root), refresh=1)[0], 200)
                self.assertEqual(query.call_count, 2)
        finally:
            httpd.shutdown(); httpd.server_close(); thread.join(2)


if __name__ == "__main__":
    unittest.main()
