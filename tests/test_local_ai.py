"""ローカルAI（Ollama）連携のテスト。本物のモデルの代わりに、決まった返答をする偽のOllamaを立てる。"""
from __future__ import annotations

import base64
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import app as appmod  # noqa: E402
import local_ai  # noqa: E402
from srt_io import sha256_bytes  # noqa: E402

SRT = ("1\n00:00:01,000 --> 00:00:04,000\n書き出しの設定は以外と大事です\n\n"
       "2\n00:00:04,000 --> 00:00:08,000\nこれが結講時間がかかるんですよね\n\n"
       "3\n00:00:08,000 --> 00:00:11,000\n次はプレミアプロで書き出します\n")

REPLY = {"candidates": [
    {"index": 1, "type": "誤変換", "original": "以外と大事", "proposed": "意外と大事", "reason": "同音異義語",
     "evidence": "文脈", "status": "apply"},
    {"index": 2, "type": "誤字", "original": "結講時間", "proposed": "結構な時間", "reason": "誤字",
     "evidence": "文脈", "status": "apply"},
    {"index": 3, "type": "固有名詞", "original": "プレミアプロ", "proposed": "Premiere Pro", "reason": "辞書",
     "evidence": "辞書", "status": "apply"},
    {"index": 3, "type": "誤字", "original": "存在しない原文", "proposed": "x", "reason": "r", "evidence": "e",
     "status": "apply"},
    {"index": 9, "type": "誤字", "original": "a", "proposed": "b", "reason": "r", "evidence": "e", "status": "apply"},
    {"index": 2, "type": "誤字", "original": "これが結講時間がかかる", "proposed": "結構", "reason": "r",
     "evidence": "e", "status": "apply"},
]}


class FakeOllama(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def _json(self, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._json({"models": [{"name": "fake:1b", "size": 1, "details": {"parameter_size": "1B"}}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.calls.append(body)
        self._json({"message": {"content": json.dumps(REPLY, ensure_ascii=False)}})


class LocalAITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ollama = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=cls.ollama.serve_forever, daemon=True).start()
        cls.host = f"http://127.0.0.1:{cls.ollama.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.ollama.shutdown()
        cls.ollama.server_close()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="jsp-ai-"))
        FakeOllama.calls.clear()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_suggest_filters_and_marks(self):
        srt = self.dir / "a.srt"
        srt.write_text(SRT, encoding="utf-8")
        cand = self.dir / "a.candidates.json"
        glossary_cand = {"id": "G001", "index": 3, "type": "固有名詞", "original": "プレミアプロ",
                         "proposed": "Premiere Pro", "reason": "辞書", "evidence": "辞書", "status": "apply"}
        cand.write_text(json.dumps({"source_sha256": sha256_bytes(SRT.encode()), "candidates": [glossary_cand]}),
                        encoding="utf-8")
        seen = []
        r = local_ai.suggest(str(srt), str(cand), "fake:1b", host=self.host, progress=lambda i, n: seen.append((i, n)))
        doc = json.loads(cand.read_text(encoding="utf-8"))
        ai = {c["original"]: c for c in doc["candidates"] if c.get("source") == "local-ai"}
        self.assertEqual(ai["以外と大事"]["status"], "apply")
        self.assertEqual(ai["結講時間"]["status"], "review")            # 原文にない「な」が加わる
        self.assertIn("原文にない文字", ai["結講時間"]["reason"])
        self.assertNotIn("プレミアプロ", ai)                           # 辞書候補と重なる提案は捨てる
        self.assertNotIn("存在しない原文", ai)
        self.assertNotIn("これが結講時間がかかる", ai)                 # 上の候補と重なる
        self.assertEqual(r["added"], 2)
        self.assertEqual(seen[-1], (1, 1))
        self.assertEqual(FakeOllama.calls[0]["format"], local_ai.SCHEMA)
        self.assertEqual(FakeOllama.calls[0]["options"]["temperature"], 0)

    def test_app_ai_flow_with_progress(self):
        app = appmod.App(self.dir / "出力", appmod.LocalAI(host=self.host), "tok")
        app.notify = False  # テスト中にMacの通知を出さない
        server = ThreadingHTTPServer(("127.0.0.1", 0), None)
        port = server.server_address[1]
        server.RequestHandlerClass = appmod.make_handler(app, port)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            def call(path, body=None):
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                             data=None if body is None else json.dumps(body).encode(),
                                             headers={"X-Token": "tok", "Cookie": "jsp=tok",
                                                      "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as res:
                    return json.loads(res.read())
            st = call("/api/ai")
            self.assertTrue(st["running"])
            self.assertEqual(st["models"][0]["name"], "fake:1b")
            job = call("/api/jobs", {"filename": "a.srt", "data": base64.b64encode(SRT.encode()).decode(),
                                     "settings": {"preset": "standard"}})
            call(f"/api/jobs/{job['id']}/run", {"mode": "ai", "model": "fake:1b"})
            progress = []
            for _ in range(100):
                j = call(f"/api/jobs/{job['id']}")
                progress.append(j.get("progress"))
                if j["status"] in ("done", "error"):
                    break
                time.sleep(0.1)
            self.assertEqual(j["status"], "done", j.get("message"))
            self.assertEqual(j["progress"], 1.0)
            self.assertEqual(j["model"], "fake:1b")
            out = (app.out / job["id"] / "a.polished.srt").read_text(encoding="utf-8")
            self.assertIn("意外と大事", out)
            self.assertIn("結講", out)          # 要確認に回った候補は適用されない
            with self.assertRaises(urllib.error.HTTPError):
                call(f"/api/jobs/{job['id']}/run", {"mode": "ai", "model": "not-installed"})
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
