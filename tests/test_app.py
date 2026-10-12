"""アプリ（app.py）のテスト。実際にローカルでサーバーを立てて、画面と同じ手順でAPIを呼ぶ。

「AIで整える」は Claude Code が必要なため、ここでは「AIなしで整える」だけを試す。
"""
from __future__ import annotations

import base64
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import app as appmod  # noqa: E402
from srt_io import parse_bytes  # noqa: E402

SRT = ("1\n00:00:01,000 --> 00:00:04,000\nどうも、こんにちは\n\n"
       "2\n00:00:04,000 --> 00:00:09,000\n今日はプレミアプロで書き出しの設定を解説していきます\n\n"
       "3\n00:00:09,500 --> 00:00:11,000\n100\n")


class AppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="jsp-app-"))
        cls.app = appmod.App(cls.dir / "出力", None, "testtoken")
        cls.app.notify = False  # テスト中にMacの通知を出さない
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), None)
        cls.port = cls.server.server_address[1]
        cls.server.RequestHandlerClass = appmod.make_handler(cls.app, cls.port)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def req(self, path, body=None, token="testtoken", cookie=True, host=None):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, method="POST" if body is not None else "GET")
        if token:
            r.add_header("X-Token", token)
        if cookie:
            r.add_header("Cookie", "jsp=testtoken")
        if body is not None:
            r.add_header("Content-Type", "application/json")
        if host:
            r.add_header("Host", host)
        try:
            with urllib.request.urlopen(r, timeout=30) as res:
                return res.status, res.read(), dict(res.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def upload(self, data: bytes, name="talk.srt", **settings):
        return self.req("/api/jobs", {"filename": name, "data": base64.b64encode(data).decode(),
                                      "settings": {"preset": "standard", "punctuation": "keep", **settings},
                                      "glossary": {"filename": "dict.tsv",
                                                   "data": base64.b64encode("Premiere Pro\tプレミアプロ\n".encode()).decode()}})

    def wait_done(self, job_id):
        for _ in range(100):
            code, body, _ = self.req(f"/api/jobs/{job_id}")
            job = json.loads(body)
            if job["status"] in ("done", "error", "cancelled"):
                return job
            time.sleep(0.2)
        self.fail("終わりませんでした")

    def test_requires_token_cookie_and_local_host(self):
        code, body, _ = self.req("/", token=None, cookie=False)
        self.assertIn("起動したときに開いた画面", body.decode())
        code, body, headers = self.req("/?t=testtoken", token=None, cookie=False)
        self.assertEqual(code, 200)
        self.assertIn("jsp=testtoken", headers.get("Set-Cookie", ""))
        self.assertIn("ドロップ", body.decode())
        self.assertEqual(self.upload(SRT.encode())[0], 200)
        self.assertEqual(self.req("/api/jobs", {"filename": "a.srt", "data": ""}, token="wrong")[0], 403)
        self.assertEqual(self.req("/api/jobs", {"filename": "a.srt", "data": ""}, cookie=False)[0], 403)
        self.assertEqual(self.req("/api/jobs", token="testtoken", host="evil.example:80")[0], 403)

    def test_basic_flow_review_decisions_download(self):
        code, body, _ = self.upload(SRT.encode(), punctuation="telop")
        self.assertEqual(code, 200, body)
        job = json.loads(body)
        self.assertEqual(job["cues"], 3)
        self.assertEqual(self.req(f"/api/jobs/{job['id']}/run", {"mode": "basic"})[0], 200)
        job = self.wait_done(job["id"])
        self.assertEqual(job["status"], "done", job.get("message"))
        self.assertTrue(job["verify_ok"])

        code, body, _ = self.req(f"/jobs/{job['id']}/review")
        html = body.decode()
        self.assertIn("window.JSP_APP=", html)
        data = json.loads(html.split('<script id="data" type="application/json">')[1].split("</script>")[0])
        cue2 = data["cues"][1]
        self.assertIn("Premiere Pro", "".join(cue2["after"]))

        # 確認画面で「改行（句読点）を見送り」を選んだときと同じ内容を送る
        doc = dict(data["candidates_doc"])
        doc["candidates"] = [c["cand"] for c in data["candidates"]] + [{
            "id": "L001", "index": 1, "start": "00:00:01,000", "scope": "cue", "type": "改行",
            "original": data["cues"][0]["flat_before"], "proposed": "\n".join(data["cues"][0]["before"]),
            "reason": "元に戻す", "evidence": "利用者の判断", "status": "apply", "approved": True}]
        code, body, _ = self.req(f"/api/jobs/{job['id']}/decisions", doc)
        self.assertEqual(code, 200, body)
        self.assertTrue(json.loads(body)["verify_ok"])

        code, body, headers = self.req(f"/jobs/{job['id']}/download")
        self.assertEqual(code, 200)
        self.assertIn("talk.polished.srt", urllib.request.unquote(headers["Content-Disposition"]))
        out = parse_bytes(body)
        src = parse_bytes(SRT.encode())
        self.assertEqual([c.timing_raw for c in out.cues], [c.timing_raw for c in src.cues])
        self.assertEqual(out.cues[0].original_lines, ["どうも、こんにちは"])  # 見送りで句読点が戻った
        self.assertIn("Premiere Pro", "".join(out.cues[1].original_lines))
        # 作業フォルダのSRTはドロップされた内容のまま
        d = self.app.out / job["id"]
        self.assertEqual((d / "talk.srt").read_bytes(), SRT.encode())
        self.assertTrue((d / "talk.candidates.v1.json").exists())

    def test_font_size_and_relayout(self):
        long = "1\n00:00:01,000 --> 00:00:09,000\n今日はカラーグレーディングの基本を説明していきます\n"
        code, body, _ = self.upload(long.encode(), video_size="1920x1080", font_px=60, max_lines=2, max_cps=6)
        job = json.loads(body)
        self.assertEqual(job["font_px"], 60)
        self.req(f"/api/jobs/{job['id']}/run", {"mode": "basic"})
        self.wait_done(job["id"])
        out = parse_bytes(self.req(f"/jobs/{job['id']}/download")[1])
        self.assertEqual(len(out.cues[0].original_lines), 1)       # 60pxなら1行28字まで入る
        code, body, _ = self.req(f"/api/jobs/{job['id']}/layout", {"font_px": 130})
        self.assertEqual(code, 200, body)
        out = parse_bytes(self.req(f"/jobs/{job['id']}/download")[1])
        self.assertEqual(len(out.cues[0].original_lines), 2)       # 130pxなら1行13字まで
        self.assertEqual(out.cues[0].timing_raw, parse_bytes(long.encode()).cues[0].timing_raw)
        code, body, _ = self.req(f"/api/jobs/{job['id']}/layout", {"font_px": 5000})
        self.assertEqual(code, 400)

    def test_shift_jis_is_accepted_and_kept(self):
        data = SRT.encode("cp932")
        code, body, _ = self.upload(data)
        self.assertEqual(code, 200, body)
        job = json.loads(body)
        self.assertEqual(job["encoding"], "cp932")
        self.req(f"/api/jobs/{job['id']}/run", {"mode": "basic"})
        job = self.wait_done(job["id"])
        self.assertEqual(job["status"], "done", job.get("message"))
        code, body, _ = self.req(f"/jobs/{job['id']}/download")
        parse_bytes(body, "cp932")  # Shift_JISのまま書き出される
        with self.assertRaises(Exception):
            body.decode("utf-8")

    def test_rejects_bad_input(self):
        code, body, _ = self.upload(b"hello", name="memo.txt")
        self.assertEqual(code, 400)
        self.assertIn(".srt", json.loads(body)["error"])
        code, body, _ = self.upload(b"not srt at all\n")
        self.assertEqual(code, 400)
        code, body, _ = self.req("/api/jobs/../../etc/run", {"mode": "basic"})
        self.assertIn(code, (403, 404))
        code, body, _ = self.req("/api/jobs/20260101-000000-abcd/run", {"mode": "ai"})
        self.assertEqual(code, 404)

    def test_ai_disabled_is_reported(self):
        code, body, _ = self.upload(SRT.encode())
        job = json.loads(body)
        code, body, _ = self.req(f"/api/jobs/{job['id']}/run", {"mode": "ai", "model": "x"})
        self.assertEqual(code, 400)
        self.assertIn("ローカルAI", json.loads(body)["error"])
        self.assertEqual(json.loads(self.req("/api/ai")[1]), {"enabled": False})


if __name__ == "__main__":
    unittest.main()
