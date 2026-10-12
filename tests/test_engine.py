"""AIエンジン（Ollama）の準備のテスト。本物は取得せず、偽の配布元からの取得で確かめる。"""
from __future__ import annotations

import hashlib
import io
import shutil
import sys
import tarfile
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import local_engine  # noqa: E402


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@unittest.skipUnless(sys.platform == "darwin", "Mac用の取得（tgz）を試す")
class EngineInstallTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="jsp-eng-"))
        self.www = self.dir / "www"
        self.www.mkdir()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for name, body, mode in [("ollama", b"#!/bin/sh\necho fake\n", 0o755), ("lib/ollama/libx.dylib", b"x", 0o644)]:
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(body), mode
                t.addfile(info, io.BytesIO(body))
        self.archive = buf.getvalue()
        (self.www / "ollama-darwin.tgz").write_bytes(self.archive)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(self.www)))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.patches = [mock.patch.object(local_engine, "app_support_dir", return_value=self.dir / "support"),
                        mock.patch.object(local_engine, "start", return_value=None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_install_verifies_and_places_engine(self):
        sha = hashlib.sha256(self.archive).hexdigest()
        phases = []
        with mock.patch.dict(local_engine.PINNED_SHA256, {"ollama-darwin.tgz": sha}):
            local_engine.install(progress=lambda ph, st, d, t: phases.append(ph), base_url=self.base)
        eng = local_engine.engine_dir()
        self.assertTrue((eng / "ollama").is_file())
        self.assertTrue((eng / "lib" / "ollama" / "libx.dylib").is_file())
        self.assertIn('"sha256"', (eng / "engine.json").read_text(encoding="utf-8"))
        self.assertEqual(local_engine.resolve_binary()["managed"], True)
        self.assertEqual(phases[-1], "done")
        self.assertIn("verify", phases)

    def test_checksum_mismatch_leaves_nothing(self):
        with mock.patch.dict(local_engine.PINNED_SHA256, {"ollama-darwin.tgz": "0" * 64}):
            with self.assertRaises(local_engine.EngineError):
                local_engine.install(base_url=self.base)
        self.assertFalse(local_engine.engine_dir().exists())

    def test_cancel(self):
        sha = hashlib.sha256(self.archive).hexdigest()
        with mock.patch.dict(local_engine.PINNED_SHA256, {"ollama-darwin.tgz": sha}):
            with self.assertRaises(local_engine.EngineError):
                local_engine.install(base_url=self.base, cancelled=lambda: True)
        self.assertFalse(local_engine.engine_dir().exists())


class CatalogTest(unittest.TestCase):
    def test_recommendation_follows_memory(self):
        rec = lambda mem: [c["id"] for c in local_engine.catalog_for({"memory_gb": mem, "disk_free_gb": 500}) if c["recommended"]]
        self.assertEqual(rec(8), ["qwen2.5:3b"])
        self.assertEqual(rec(16), ["qwen2.5:7b"])
        self.assertEqual(rec(24), ["qwen2.5:14b"])
        self.assertEqual(rec(32), ["qwen3.5:27b"])
        self.assertEqual(rec(128), ["qwen3.5:27b"])
        fits = {c["id"]: c["fit"]["level"] for c in local_engine.catalog_for({"memory_gb": 8, "disk_free_gb": 500})}
        self.assertEqual(fits["qwen2.5:14b"], "unsupported")

    def test_old_engine_blocks_newer_models(self):
        # 古い Ollama（0.30.11）では qwen3.5 の返答が崩れたため、使えないと示し、おすすめも 14b に下げる
        c = local_engine.catalog_for({"memory_gb": 128, "disk_free_gb": 500}, "0.30.11")
        q = next(x for x in c if x["id"] == "qwen3.5:27b")
        self.assertEqual(q["fit"]["level"], "unsupported")
        self.assertIn("0.30.11", q["fit"]["reason"])
        self.assertEqual([x["id"] for x in c if x["recommended"]], ["qwen2.5:14b"])
        ok = local_engine.catalog_for({"memory_gb": 128, "disk_free_gb": 500}, "0.32.6")
        self.assertEqual(next(x for x in ok if x["id"] == "qwen3.5:27b")["fit"]["level"], "recommended")

    def test_disk_shortage_is_reported(self):
        c = local_engine.catalog_for({"memory_gb": 64, "disk_free_gb": 5})
        self.assertTrue(next(x for x in c if x["id"] == "qwen2.5:14b")["fit"]["disk"])

    def test_display_path_hides_user_name(self):
        self.assertTrue(local_engine.display_path(Path.home() / "x").startswith(("~", "%USERPROFILE%")))


class EngineStartTest(unittest.TestCase):
    """起動の待ち方。Windows の初回は応答まで30秒以上かかることがあり、20秒で諦めると動いているのに失敗と出た。"""

    def setUp(self):
        self.patches = [mock.patch.object(local_engine, "resolve_binary", return_value={"path": "ollama", "managed": True}),
                        mock.patch.object(local_engine.time, "sleep")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _proc(self, exited=False):
        return mock.Mock(poll=mock.Mock(return_value=1 if exited else None))

    def test_waits_for_slow_engine(self):
        answers = [None] + [None] * 60 + [[]]  # 最初の確認・起動後30秒ぶん応答なし・そのあと応答
        with mock.patch.object(local_engine, "probe", side_effect=answers), \
                mock.patch.object(local_engine.subprocess, "Popen", return_value=self._proc()):
            local_engine.start()

    def test_exited_engine_fails_at_once(self):
        with mock.patch.object(local_engine, "probe", return_value=None), \
                mock.patch.object(local_engine, "_port_open", return_value=False), \
                mock.patch.object(local_engine.subprocess, "Popen", return_value=self._proc(exited=True)) as popen:
            with self.assertRaisesRegex(local_engine.EngineError, "セキュリティソフト"):
                local_engine.start()
        popen.assert_called_once()

    def test_exited_but_earlier_engine_still_starting(self):
        # 先に起動したエンジンが番号を使っていて二つ目がすぐ終わっても、失敗にせず応答を待つ
        with mock.patch.object(local_engine, "probe", side_effect=[None, None, None, []]), \
                mock.patch.object(local_engine, "_port_open", return_value=True), \
                mock.patch.object(local_engine.subprocess, "Popen", return_value=self._proc(exited=True)):
            local_engine.start()

    def test_timeout_says_it_is_slow(self):
        with mock.patch.object(local_engine, "probe", return_value=None), \
                mock.patch.object(local_engine.subprocess, "Popen", return_value=self._proc()):
            with self.assertRaisesRegex(local_engine.EngineError, "時間がかかっています"):
                local_engine.start(wait_s=0.05)


class WindowsRootStoreTest(unittest.TestCase):
    """Windows で初めて配布元へつなぐとき、足りないルート証明書を OS の通信でそろえてから再試行する。"""

    def _ssl_error(self):
        import ssl
        import urllib.error
        return urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))

    def test_retries_once_after_warming_the_store(self):
        calls = []
        with mock.patch.object(local_engine, "_WARM_ROOTS", True), \
                mock.patch.object(local_engine.subprocess, "run", side_effect=lambda cmd, **kw: calls.append(cmd)), \
                mock.patch.object(local_engine.urllib.request, "urlopen", side_effect=[self._ssl_error(), "response"]) as uo:
            self.assertEqual(local_engine._urlopen("https://github.com/ollama/x/sha256sum.txt", timeout=5), "response")
        self.assertEqual(uo.call_count, 2)
        self.assertEqual(len(calls), 1)
        self.assertIn("https://github.com/", " ".join(calls[0]))  # 同じ配布元へ OS の通信で一度つなぐ

    def test_readable_error_when_still_failing(self):
        with mock.patch.object(local_engine, "_WARM_ROOTS", True), \
                mock.patch.object(local_engine.subprocess, "run"), \
                mock.patch.object(local_engine.urllib.request, "urlopen", side_effect=[self._ssl_error(), self._ssl_error()]):
            with self.assertRaisesRegex(local_engine.EngineError, "証明書"):
                local_engine._urlopen("https://github.com/x", timeout=5)

    def test_not_on_mac(self):
        with mock.patch.object(local_engine, "_WARM_ROOTS", False), \
                mock.patch.object(local_engine.subprocess, "run") as run, \
                mock.patch.object(local_engine.urllib.request, "urlopen", side_effect=[self._ssl_error()]):
            with self.assertRaises(local_engine.urllib.error.URLError):
                local_engine._urlopen("https://github.com/x", timeout=5)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
