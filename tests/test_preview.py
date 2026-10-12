"""文字の大きさの見本（/api/preview）のテスト。整えるときと同じ改行の決め方で並ぶかを確かめる。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from app import App  # noqa: E402


class PreviewTest(unittest.TestCase):
    def test_keeps_english_space_and_avoids_mid_word(self):
        r = App.preview({"lines": ["今回はPremiere", "ProのLumetriカラーで説明します"],
                         "video_size": "1920x1080", "font_px": 115, "max_lines": 2})
        self.assertEqual(r["per_line"], 15)
        self.assertTrue(r["fits"])
        self.assertEqual(r["lines"], ["今回はPremiere Proの", "Lumetriカラーで説明します"])

    def test_reports_overflow(self):
        r = App.preview({"lines": ["書き出しの設定を間違えると画質が大きく変わってしまうので意外と大事なんですよね"],
                         "video_size": "1920x1080", "font_px": 115, "max_lines": 2})
        self.assertFalse(r["fits"])
        # 収まらないときは、語の区切りで行を増やして見せ、必要な行数を返す
        self.assertEqual(r["needed_lines"], len(r["lines"]))
        self.assertGreater(len(r["lines"]), 2)
        self.assertTrue(all(len(x) <= 15 for x in r["lines"]), r["lines"])
        self.assertTrue(any("画質" in x for x in r["lines"]), r["lines"])  # 語の途中で切らない

    def test_telop_strips_punctuation(self):
        r = App.preview({"lines": ["はい、無料で使えます。"], "video_size": "1920x1080", "font_px": 115,
                         "max_lines": 2, "punctuation": "telop"})
        self.assertNotIn("、", "".join(r["lines"]))
        self.assertNotIn("。", "".join(r["lines"]))

    def test_vertical_font_size(self):
        r = App.preview({"lines": ["縦型の動画は一行が短くなります"], "video_size": "1080x1920", "font_px": 80, "max_lines": 2})
        self.assertEqual(r["per_line"], 12)
        self.assertTrue(all(len(x) <= 12 for x in r["lines"]))

    def test_rejects_bad_font(self):
        with self.assertRaises(ValueError):
            App.preview({"lines": ["あ"], "video_size": "1920x1080", "font_px": 2000})


if __name__ == "__main__":
    unittest.main()
