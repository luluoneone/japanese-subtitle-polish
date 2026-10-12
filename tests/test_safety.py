"""モデル比較（2026-10-08）で見つかった、安全チェックの穴のテスト。"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import local_ai  # noqa: E402
from jptext import guard_problems  # noqa: E402

SRT = ("1\n00:00:01,000 --> 00:00:04,000\nヤマダさんに教えてもろたんです\n\n"
       "2\n00:00:04,000 --> 00:00:08,000\nそれでもええねん\n\n"
       "3\n00:00:08,000 --> 00:00:11,000\n次はプレミアプロで書き出します\n")
REPLY = {"candidates": [
    {"index": 1, "type": "固有名詞", "original": "ヤマダさん", "proposed": "山田さん", "reason": "人名", "evidence": "推測", "status": "apply"},
    {"index": 2, "type": "誤変換", "original": "ええねん", "proposed": "いいねん", "reason": "r", "evidence": "e", "status": "apply"},
    {"index": 3, "type": "固有名詞", "original": "プレミアプロ", "proposed": "Premiere Pro", "reason": "辞書", "evidence": "辞書", "status": "apply"},
]}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        b = json.dumps({"message": {"content": json.dumps(REPLY, ensure_ascii=False)}}, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


class SafetyTest(unittest.TestCase):
    def test_dialect_change_is_stopped(self):
        self.assertIn("方言・話し方が変わる", guard_problems("それでもええねん", "それでもいいねん"))
        self.assertIn("方言・話し方が変わる", guard_problems("上手くいかへんかった", "上手くいかなかった"))
        self.assertEqual(guard_problems("結講時間がかかるねん", "結構時間がかかるねん"), [])  # 方言はそのまま

    def test_ai_proper_noun_outside_glossary_goes_to_review(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        d = Path(tempfile.mkdtemp(prefix="jsp-safe-"))
        try:
            (d / "in.srt").write_bytes(SRT.encode("utf-8"))
            (d / "c.json").write_text(json.dumps({"schema": "japanese-subtitle-polish/candidates-v1", "candidates": []}), encoding="utf-8")
            local_ai.suggest(str(d / "in.srt"), str(d / "c.json"), "fake", [{"correct": "Premiere Pro", "variants": ["プレミアプロ"]}],
                             host=f"http://127.0.0.1:{srv.server_address[1]}")
            got = {c["original"]: c for c in json.loads((d / "c.json").read_text(encoding="utf-8"))["candidates"]}
            self.assertEqual(got["ヤマダさん"]["status"], "review")
            self.assertIn("辞書にない固有名詞", got["ヤマダさん"]["reason"])
            self.assertNotIn("辞書にない固有名詞", got["プレミアプロ"]["reason"])  # 辞書にある表記は対象外
        finally:
            srv.shutdown(); srv.server_close()
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
