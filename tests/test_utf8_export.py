"""Shift_JIS のSRTを、修正版だけ UTF-8 で書き出すテスト（YouTube のヘルプでは SRT は UTF-8 である必要がある）。"""
from __future__ import annotations

import base64
import http.cookiejar
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import app as appmod  # noqa: E402
from srt_io import parse_bytes  # noqa: E402

SRT = ("1\r\n00:00:01,000 --> 00:00:03,500\r\nプレミアプロで書き出します\r\n\r\n"
       "2\r\n00:00:03,500 --> 00:00:06,000\r\n①の設定は山﨑さんに聞きました\r\n").encode("cp932")


class Utf8ExportTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="jsp-u8-"))
        self.app = appmod.App(self.dir, appmod.LocalAI(enabled=False), "t0k")
        self.app.notify = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), None)
        self.port = self.server.server_address[1]
        self.server.RequestHandlerClass = appmod.make_handler(self.app, self.port)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.port}"
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.opener.open(self.base + "/?t=t0k").read()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Token": "t0k"})
        return json.loads(self.opener.open(req).read())

    def test_shift_jis_job_can_be_exported_as_utf8(self):
        glossary = base64.b64encode("Premiere Pro\tプレミアプロ\n".encode("utf-8")).decode()
        job = self.post("/api/jobs", {"filename": "talk.srt", "data": base64.b64encode(SRT).decode(),
                                      "glossary": {"filename": "dict.tsv", "data": glossary},
                                      "settings": {"preset": "standard"}})
        self.assertEqual(job["encoding"], "cp932")
        self.post(f"/api/jobs/{job['id']}/run", {"mode": "basic"})
        for _ in range(100):
            if self.app.load(job["id"])["status"] in ("done", "error"):
                break
            time.sleep(0.1)
        self.assertEqual(self.app.load(job["id"])["status"], "done")

        sjis = self.opener.open(f"{self.base}/jobs/{job['id']}/download").read()
        res = self.opener.open(f"{self.base}/jobs/{job['id']}/download?utf8=1")
        u8 = res.read()
        self.assertIn("utf8.polished.srt", res.headers["Content-Disposition"])
        self.assertIn("Premiere Pro".encode("cp932"), sjis)  # 修正は反映済み
        self.assertEqual(u8, sjis.decode("cp932").encode("utf-8"))  # 文字コードだけが違う
        self.assertFalse(u8.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", u8)
        a, b = parse_bytes(sjis, "cp932"), parse_bytes(u8, "utf-8")
        self.assertEqual([(c.index_raw, c.timing_raw) for c in a.cues], [(c.index_raw, c.timing_raw) for c in b.cues])
        self.assertIn("①の設定は山﨑さん", u8.decode("utf-8"))  # 機種依存文字（①・﨑）も化けない

    def test_utf8_job_is_returned_unchanged(self):
        data = "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n".encode("utf-8")
        self.assertEqual(appmod.App.to_utf8({"encoding": "utf-8"}, data), data)


if __name__ == "__main__":
    unittest.main()
