"""見やすさの目安（1秒に読む字数）のテスト。目安は選べて、「知らせない」（0）も選べる。"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from app import App  # noqa: E402

EX = ROOT / "examples" / "01_whisper_talk"


class CpsGuidelineTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="jsp-cps-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def apply(self, cps: str, out: str):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "apply_candidates.py"), str(EX / "input.srt"),
                            str(EX / "candidates.json"), "--glossary", str(EX / "glossary.json"),
                            "--max-cps", cps, "--out-dir", str(self.dir / out)],
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        log = json.loads((self.dir / out / "input.apply-log.json").read_text(encoding="utf-8"))
        review = (self.dir / out / "input.review.md").read_text(encoding="utf-8")
        polished = (self.dir / out / "input.polished.srt").read_bytes()
        return log, review, polished

    def test_guideline_changes_only_the_report(self):
        _, slow, srt4 = self.apply("4", "c4")
        _, off, srt0 = self.apply("0", "c0")
        self.assertIn("見やすさの目安より速い字幕（1秒4字を超える）", slow)
        self.assertIn("| #", slow.split("## 3.")[1].split("## 4.")[0])
        self.assertIn("「知らせない」", off)
        self.assertNotIn("| #", off.split("## 3.")[1].split("## 4.")[0])
        self.assertEqual(srt4, srt0)  # 目安は印を付けるだけで、修正版は同じ

    def test_app_accepts_off_and_rejects_out_of_range(self):
        self.assertEqual(App.check_layout({"max_cps": 0})["max_cps"], 0)
        self.assertEqual(App.check_layout({"max_cps": 8})["max_cps"], 8)
        with self.assertRaises(ValueError):
            App.check_layout({"max_cps": 1})


if __name__ == "__main__":
    unittest.main()
