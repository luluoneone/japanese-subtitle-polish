"""候補を作っている間に原本が変わった場合、出力しない。"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import apply_candidates


class SourceRaceTest(unittest.TestCase):
    def test_source_changes_during_build_are_detected_before_any_output(self):
        with tempfile.TemporaryDirectory(prefix="jsp-source-race-") as tmp:
            d = Path(tmp)
            source = d / "talk.srt"
            source.write_bytes("1\n00:00:01,000 --> 00:00:04,000\n読み混みます\n".encode())
            candidates = d / "candidates.json"
            candidates.write_text(json.dumps({"candidates": [{"id": "C1", "index": 1, "type": "誤字",
                "original": "読み混み", "proposed": "読み込み", "reason": "誤変換", "evidence": "表記", "status": "apply"}]}), encoding="utf-8")
            real_build = apply_candidates.build

            def change_source(*args):
                result = real_build(*args)
                source.write_bytes("1\n00:00:01,000 --> 00:00:04,000\n別の発言です\n".encode())
                return result

            with patch.object(apply_candidates, "build", side_effect=change_source), contextlib.redirect_stderr(io.StringIO()):
                code = apply_candidates.main([str(source), str(candidates), "--out-dir", str(d / "out")])
            self.assertNotEqual(code, 0)
            self.assertFalse((d / "out").exists())


if __name__ == "__main__":
    unittest.main()
