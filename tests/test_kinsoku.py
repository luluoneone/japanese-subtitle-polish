"""禁則（W3C「日本語組版処理の要件」JLREQ 3.1.7〜3.1.10）のテスト。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from jptext import NO_LINE_END, NO_LINE_START, Settings, best_breaks, break_cost  # noqa: E402

INF = float("inf")


class KinsokuTest(unittest.TestCase):
    def test_jlreq_classes_never_start_a_line(self):
        # 終わり括弧類・ハイフン類・区切り約物・繰返し記号・後置の単位記号など
        for ch in "〟»‐゠–‼⁇⁈⁉〻‰℃°′″":
            with self.subTest(ch):
                self.assertIn(ch, NO_LINE_START)
                self.assertEqual(break_cost("あいう" + ch + "えお", 3), INF)

    def test_jlreq_opening_and_prefix_never_end_a_line(self):
        for ch in "〝«£€№":
            with self.subTest(ch):
                self.assertIn(ch, NO_LINE_END)
                self.assertEqual(break_cost("あいう" + ch + "えお", 4), INF)

    def test_inseparable_pairs_are_kept(self):
        for text in ["それは――つまり大事なことなんです", "ええと……どこから話せばいいのか"]:
            with self.subTest(text):
                lines = best_breaks(text, Settings(max_lines=2, max_chars_per_line=8)).lines
                self.assertFalse(any(a.endswith(c) and b.startswith(c) for a, b in zip(lines, lines[1:]) for c in "―…"), lines)

    def test_degree_stays_with_number(self):
        lines = best_breaks("今日の最高気温は30℃を超える見込みです", Settings(max_lines=2, max_chars_per_line=10)).lines
        self.assertTrue(any("30℃" in ln for ln in lines), lines)


if __name__ == "__main__":
    unittest.main()
