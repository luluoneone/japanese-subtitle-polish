"""japanese-subtitle-polish のテスト。標準ライブラリの unittest だけで動く。

実行: python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import apply_candidates  # noqa: E402
import inspect_srt  # noqa: E402
import verify_srt  # noqa: E402
from common import glossary_hits  # noqa: E402
from jptext import Settings, best_breaks, guard_problems, strip_telop_punct  # noqa: E402
from srt_io import SrtError, parse_bytes, sha256_bytes  # noqa: E402

BASIC = (
    "1\n00:00:01,000 --> 00:00:04,000\n今日は以外と簡単な方法を紹介します\n\n"
    "2\n00:00:04,000 --> 00:00:06,000\n100\n\n"
    "3\n00:00:06,500 --> 00:00:09,000\nafter effectsで\nLUTを使います\n\n"
    "4\n00:00:09,000 --> 00:00:12,000\n書き出しは\n4Kでできないわけじゃないです\n"
)


def run(fn, *args) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(list(args))
    return code, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="jsp-test-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name: str, content, raw: bool = False) -> Path:
        p = self.dir / name
        if raw:
            p.write_bytes(content)
        else:
            p.write_bytes((content).encode("utf-8"))
        return p

    def cands(self, items, srt: Path = None, name: str = "c.json") -> Path:
        data = {"candidates": items}
        if srt is not None:
            data["source_sha256"] = sha256_bytes(srt.read_bytes())
        return self.write(name, json.dumps(data, ensure_ascii=False))

    def apply(self, srt: Path, cand: Path, *extra) -> tuple:
        return run(apply_candidates.main, str(srt), str(cand), "--out-dir", str(self.dir / "out"), *extra)

    def out(self, stem: str = "in") -> dict:
        d = self.dir / "out"
        return {
            "srt": d / f"{stem}.polished.srt",
            "log": json.loads((d / f"{stem}.apply-log.json").read_text(encoding="utf-8")),
            "changes": (d / f"{stem}.changes.md").read_text(encoding="utf-8"),
            "review": (d / f"{stem}.review.md").read_text(encoding="utf-8"),
        }


def cand(**kw):
    base = {"id": "C1", "type": "誤変換", "reason": "テスト", "evidence": "テスト", "status": "apply"}
    base.update(kw)
    return base


# ── 読み書き ───────────────────────────────────────────────────────

class TestRoundTrip(unittest.TestCase):
    CASES = {
        "LF": BASIC.encode(),
        "BOM+CRLF": ("﻿" + BASIC.replace("\n", "\r\n")).encode(),
        "末尾改行なし": BASIC.rstrip("\n").encode(),
        "余分な空行と空白だけの行": ("\n\n" + BASIC.replace("\n\n", "\n \n\n") + "\n\n").encode(),
        "本文が空の字幕": b"1\n00:00:01,000 --> 00:00:02,000\n\n2\n00:00:02,000 --> 00:00:03,000\nabc\n",
        "座標付き・ピリオド区切り": b"1\n00:00:01.000 --> 00:00:02.500 X1:10 X2:20\ntext\n",
        "CRだけの改行": BASIC.replace("\n", "\r").encode(),
    }

    def test_unchanged_roundtrip_is_byte_identical(self):
        for name, data in self.CASES.items():
            with self.subTest(name):
                self.assertEqual(parse_bytes(data).to_bytes(), data)

    def test_cp932(self):
        data = BASIC.encode("cp932")
        f = parse_bytes(data, "cp932")
        self.assertEqual(f.to_bytes(), data)
        with self.assertRaises(SrtError):
            parse_bytes(data)  # UTF-8として読むと、原因を示すエラーになる

    def test_malformed_is_rejected(self):
        with self.assertRaises(SrtError):
            parse_bytes(b"1\nnot a timing\ntext\n")
        with self.assertRaises(SrtError):
            parse_bytes(b"hello\n")

    def test_missing_blank_line_is_warned(self):
        f = parse_bytes(b"1\n00:00:01,000 --> 00:00:02,000\na\n2\n00:00:02,000 --> 00:00:03,000\nb\n")
        self.assertEqual(len(f.cues), 2)
        self.assertTrue(f.warnings)


# ── 適用 ───────────────────────────────────────────────────────────

class TestApply(Base):
    def test_normal_japanese_typo(self):
        srt = self.write("in.srt", BASIC)
        before = srt.read_bytes()
        c = self.cands([cand(index=1, start="00:00:01,000", original="以外と", proposed="意外と")], srt)
        code, out, err = self.apply(srt, c)
        self.assertEqual(code, 0, err)
        self.assertEqual(srt.read_bytes(), before, "元ファイルが変わってはいけない")
        o = self.out()
        text = o["srt"].read_text(encoding="utf-8")
        self.assertIn("意外と簡単な方法", text.replace("\n", ""))
        self.assertIn("[OK ] 開始時刻・終了時刻", out)
        self.assertEqual(o["log"]["text_changed_cues"], [1])
        self.assertIn("以外と", o["changes"])

    def test_numbers_only_cue_is_untouched_and_protected(self):
        srt = self.write("in.srt", BASIC)
        c = self.cands([cand(index=2, original="100", proposed="百", type="表記ゆれ")], srt)
        code, _, _ = self.apply(srt, c)
        self.assertEqual(code, 0)
        o = self.out()
        self.assertIn("数字が変わる", o["review"])
        self.assertIn("\n100\n", o["srt"].read_text(encoding="utf-8"))

    def test_latin_proper_noun_from_glossary(self):
        srt = self.write("in.srt", BASIC)
        g = self.write("g.tsv", "After Effects\tafter effects\n")
        code, _, _ = run(inspect_srt.main, str(srt), "--glossary", str(g), "--template", str(self.dir / "draft.json"))
        self.assertEqual(code, 0)
        code, _, err = self.apply(srt, self.dir / "draft.json", "--glossary", str(g))
        self.assertEqual(code, 0, err)
        text = self.out()["srt"].read_text(encoding="utf-8")
        self.assertIn("After Effectsで", text)
        self.assertNotIn("after effects", text)

    def test_glossary_does_not_match_inside_words(self):
        g = [{"correct": "LUT", "variants": ["ラット"], "note": ""},
             {"correct": "AE", "variants": ["ae"], "note": ""}]
        self.assertEqual(glossary_hits("フラットな画でaerialを撮る", g), [])
        self.assertEqual(len(glossary_hits("ラットを当てる", g)), 1)

    def test_multiline_keeps_good_breaks(self):
        srt = self.write("in.srt", BASIC)
        c = self.cands([cand(index=4, original="4Kで", proposed="4Kで", type="誤字")], srt)
        self.apply(srt, c)
        # 同じ文字列への置換は不採用。ほかの字幕の良い改行は変えない
        text = self.out()["srt"].read_text(encoding="utf-8")
        self.assertIn("書き出しは\n4Kでできないわけじゃないです\n", text)
        self.assertIn("修正前と修正後が同じ", self.out()["review"])

    def test_edit_inside_line_keeps_original_break(self):
        srt = self.write("in.srt", "1\n00:00:01,000 --> 00:00:05,000\n今日は撮影の\n基本を説名します\n")
        c = self.cands([cand(index=1, original="説名", proposed="説明", type="誤字")], srt)
        self.assertEqual(self.apply(srt, c)[0], 0)
        self.assertIn("今日は撮影の\n基本を説明します\n", self.out()["srt"].read_text(encoding="utf-8"))

    def test_bom_crlf_preserved(self):
        data = ("﻿" + BASIC.replace("\n", "\r\n")).encode()
        srt = self.write("in.srt", data, raw=True)
        c = self.cands([cand(index=1, original="以外と", proposed="意外と")], srt)
        code, out, err = self.apply(srt, c)
        self.assertEqual(code, 0, err)
        b = self.out()["srt"].read_bytes()
        self.assertTrue(b.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\n", b.replace(b"\r\n", b""))
        self.assertIn("[OK ] 改行コード — 原本 CRLF / 修正版 CRLF", out)

    def test_original_mismatch_is_refused(self):
        srt = self.write("in.srt", BASIC)
        c = self.cands([cand(index=1, original="存在しない文", proposed="x"),
                        cand(id="C2", index=1, original="以外 と", proposed="意外と"),
                        cand(id="C3", index=99, original="a", proposed="b"),
                        cand(id="C4", index=1, start="00:00:02,000", original="以外と", proposed="意外と")], srt)
        code, _, _ = self.apply(srt, c)
        self.assertEqual(code, 0)
        o = self.out()
        self.assertEqual(o["log"]["text_changed_cues"], [])
        self.assertEqual(len(o["log"]["refused"]), 4)
        self.assertIn("原文不一致", o["review"])
        self.assertIn("番号 99 の字幕がありません", o["review"])
        self.assertIn("開始時刻が違います", o["review"])

    def test_candidates_for_other_version_stop_everything(self):
        srt = self.write("in.srt", BASIC)
        c = self.write("c.json", json.dumps({"source_sha256": "0" * 64, "candidates": []}))
        code, _, err = self.apply(srt, c)
        self.assertEqual(code, 3)
        self.assertIn("別の版", err)
        self.assertFalse((self.dir / "out").exists())

    def test_ambiguous_target_is_refused(self):
        srt = self.write("in.srt", "1\n00:00:01,000 --> 00:00:05,000\nテストのテスト\n\n"
                         "1\n00:00:05,000 --> 00:00:06,000\n重複番号\n")
        c = self.cands([cand(index=1, original="テスト", proposed="試験")], srt)
        self.apply(srt, c)
        self.assertIn("2件あり", self.out()["review"])

        srt = self.write("in.srt", "1\n00:00:01,000 --> 00:00:05,000\nテストのテスト\n")
        c = self.cands([cand(index=1, original="テスト", proposed="試験")], srt)
        self.apply(srt, c, "--overwrite")
        self.assertIn("2回あります", self.out()["review"])

    def test_overlapping_candidates_are_refused(self):
        srt = self.write("in.srt", BASIC)
        c = self.cands([cand(index=1, original="以外と簡単", proposed="意外と簡単"),
                        cand(id="C2", index=1, original="以外と", proposed="意外と")], srt)
        self.apply(srt, c)
        self.assertEqual(len(self.out()["log"]["refused"]), 2)

    def test_guards(self):
        cases = [
            ("できないわけじゃない", "できるわけじゃない", "否定"),
            ("たぶん大丈夫です", "大丈夫です", "断定"),
            ("3000円です", "300円です", "数字"),
            ("10分です", "10秒です", "単位"),
            ("そうなんだよ", "そうなんです", "口調"),
            ("えー今日は", "今日は", "フィラー"),
            ("今日は長い前置きの話をします", "今日は話します", "大きく減る"),
        ]
        for o, p, word in cases:
            with self.subTest(o):
                self.assertTrue(any(word in x for x in guard_problems(o, p)), guard_problems(o, p))
        for o, p in [("以外と", "意外と"), ("チャットGPT", "ChatGPT"), ("４Ｋ", "4K"), ("3,000円", "3000円"),
                     ("下さい", "ください")]:
            with self.subTest(o):
                self.assertEqual(guard_problems(o, p), [])

    def test_guard_can_be_overridden_by_user_approval(self):
        srt = self.write("in.srt", BASIC)
        c = self.cands([cand(index=2, original="100", proposed="百", type="表記ゆれ", approved=True)], srt)
        self.apply(srt, c)
        o = self.out()
        self.assertIn("\n百\n", o["srt"].read_text(encoding="utf-8"))
        self.assertIn("利用者が承認", o["changes"])

    def test_filler_and_summary_are_opt_in(self):
        srt = self.write("in.srt", "1\n00:00:01,000 --> 00:00:05,000\nえーと今日は撮影の話です\n")
        items = [cand(type="フィラー削除", index=1, original="えーと", proposed="")]
        self.apply(srt, self.cands(items, srt))
        self.assertIn("標準では行いません", self.out()["review"])
        self.assertEqual(self.out()["log"]["text_changed_cues"], [])
        self.apply(srt, self.cands(items, srt), "--allow", "フィラー削除", "--overwrite")
        self.assertIn("今日は撮影の話です", self.out()["srt"].read_text(encoding="utf-8"))
        # 「誤字」と名乗ってフィラーを消す候補も止める
        items = [cand(type="誤字", index=1, original="えーと今日", proposed="今日")]
        self.apply(srt, self.cands(items, srt), "--overwrite")
        self.assertIn("フィラー", self.out()["review"])

    def test_requires_reason_and_evidence(self):
        srt = self.write("in.srt", BASIC)
        c = self.cands([cand(index=1, original="以外と", proposed="意外と", evidence="")], srt)
        self.apply(srt, c)
        self.assertIn("確認根拠", self.out()["review"])

    def test_review_status_is_not_applied(self):
        srt = self.write("in.srt", BASIC)
        c = self.cands([cand(index=1, original="以外と", proposed="意外と", status="review",
                             reason="音声で確認が必要")], srt)
        self.apply(srt, c)
        o = self.out()
        self.assertEqual(o["log"]["text_changed_cues"], [])
        self.assertIn("音声で確認が必要", o["review"])

    def test_cannot_empty_a_cue_or_inject_timing(self):
        srt = self.write("in.srt", "1\n00:00:01,000 --> 00:00:02,000\nえー\n\n2\n00:00:02,000 --> 00:00:03,000\nabc\n")
        c = self.cands([cand(type="フィラー削除", index=1, original="えー", proposed=""),
                        cand(id="C2", type="誤字", index=2, original="abc", proposed="00:00:09,000 --> 00:00:10,000")], srt)
        self.apply(srt, c, "--allow", "フィラー削除")
        r = self.out()["review"]
        self.assertIn("本文が空になります", r)
        self.assertIn("-->", r)

    def test_existing_outputs_are_not_overwritten(self):
        srt = self.write("in.srt", BASIC)
        c = self.cands([], srt)
        self.assertEqual(self.apply(srt, c)[0], 0)
        code, _, err = self.apply(srt, c)
        self.assertEqual(code, 2)
        self.assertIn("--overwrite", err)

    def test_long_cue_is_reported_without_changing_time(self):
        srt = self.write("in.srt", "1\n00:00:01,000 --> 00:00:02,000\nこれは一秒では読み切れない長さの字幕です\n")
        self.apply(srt, self.cands([], srt))
        o = self.out()
        self.assertIn("1秒あたり", o["review"])
        self.assertIn("00:00:01,000 --> 00:00:02,000", o["srt"].read_text(encoding="utf-8"))

    def test_dialogue_lines_are_kept(self):
        srt = self.write("in.srt", "1\n00:00:01,000 --> 00:00:05,000\n－これは無料ですか？とても気になります\n－はい\n")
        c = self.cands([cand(index=1, original="とても", proposed="とっても", type="誤字")], srt)
        self.apply(srt, c)
        self.assertIn("－これは無料ですか？とっても気になります\n－はい\n", self.out()["srt"].read_text(encoding="utf-8"))

    def test_linebreak_candidate_can_keep_single_line(self):
        srt = self.write("in.srt", "1\n00:00:01,000 --> 00:00:09,000\nビットレートは10メガから16メガくらいが目安です\n")
        c = self.cands([cand(type="改行", scope="cue", index=1, original="ビットレートは10メガから16メガくらいが目安です",
                             proposed="ビットレートは10メガから16メガくらいが目安です", approved=True)], srt)
        self.apply(srt, c)
        self.assertIn("\nビットレートは10メガから16メガくらいが目安です\n", self.out()["srt"].read_text(encoding="utf-8"))
        self.assertEqual(self.out()["log"]["refused"], [])

    def test_manual_edit_requires_approval(self):
        srt = self.write("in.srt", BASIC)
        items = [cand(type="手動修正", scope="cue", index=1, original="今日は以外と簡単な方法を紹介します",
                      proposed="今日は意外と簡単な\n方法を紹介します")]
        self.apply(srt, self.cands(items, srt))
        self.assertIn("確認画面で利用者が直したもの", self.out()["review"])
        items[0]["approved"] = True
        self.apply(srt, self.cands(items, srt), "--overwrite")
        self.assertIn("今日は意外と簡単な\n方法を紹介します\n", self.out()["srt"].read_text(encoding="utf-8"))

    def test_review_html_is_written_and_safe(self):
        srt = self.write("in.srt", "1\n00:00:01,000 --> 00:00:04,000\n</script><b>タグ</b>以外と\n")
        c = self.cands([cand(index=1, original="以外と", proposed="意外と")], srt)
        self.assertEqual(self.apply(srt, c)[0], 0)
        html = (self.dir / "out" / "in.review.html").read_text(encoding="utf-8")
        payload = html.split('<script id="data" type="application/json">')[1].split("</script>")[0]
        self.assertNotIn("<", payload)
        data = json.loads(payload)
        self.assertEqual(data["cues"][0]["after"], ["</script><b>タグ</b>意外と"])
        self.assertEqual(data["candidates"][0]["state"], "applied")

    def test_telop_punctuation_is_opt_in(self):
        text = ("1\n00:00:01,000 --> 00:00:06,000\nどうも、こんにちは。今日は「LUT」の話です！\n\n"
                "2\n00:00:06,000 --> 00:00:09,000\n解像度は1920×1080、29.97fpsです\n\n"
                "3\n00:00:09,000 --> 00:00:12,000\n（田中）本当ですか？\n（佐藤）はい、本当です…\n")
        srt = self.write("in.srt", text)
        c = self.cands([], srt)
        self.apply(srt, c)
        self.assertEqual(self.out()["srt"].read_text(encoding="utf-8").count("、"), 3, "標準では句読点を残す")
        code, out, _ = self.apply(srt, c, "--punct", "telop", "--overwrite")
        self.assertEqual(code, 0, out)
        t = self.out()["srt"].read_text(encoding="utf-8")
        self.assertIn("どうも　こんにちは", t)
        self.assertIn("LUT", t)
        self.assertNotIn("「", t)
        self.assertNotIn("。", t)
        self.assertIn("今日はLUTの話です！", t.replace("\n", ""))
        self.assertIn("29.97fps", t)
        self.assertIn("（田中）本当ですか？\n（佐藤）はい　本当です…\n", t)
        self.assertIn("00:00:06,000 --> 00:00:09,000", t)
        log = self.out()["log"]
        self.assertEqual(sorted(log["text_changed_cues"]), [1, 2, 3])
        self.assertIn("句読点を外した", self.out()["changes"])
        self.assertIn("[OK ] 本文の変更が適用ログの字幕だけ", out)

    def test_telop_punct_function(self):
        cases = {
            "H.264とH.265、1,000円、10:30から": "H.264とH.265　1,000円　10:30から",
            "ジョン・スミスです": "ジョン・スミスです",
            "Hello, world.": "Hello world",
            "いや、、そうですね。": "いや　そうですね",
            "－有料ですか？": "－有料ですか？",
            "。": "。",
        }
        for src, want in cases.items():
            with self.subTest(src):
                self.assertEqual(strip_telop_punct(src)[0], want)
        self.assertEqual(strip_telop_punct("本当？", keep="")[0], "本当")

    def test_font_size_sets_chars_per_line(self):
        from common import chars_for_font, load_settings
        self.assertEqual(chars_for_font(1920, 80), 21)
        self.assertEqual(chars_for_font(1080, 80), 12)
        cfg = load_settings("standard", None, {"video_size": "1080x1920", "font_px": 90})
        self.assertEqual(cfg["max_chars_per_line"], 10)
        with self.assertRaises(ValueError):
            load_settings("standard", None, {"video_size": "1920x1080", "font_px": 2000})
        text = "1\n00:00:01,000 --> 00:00:09,000\n今日はカラーグレーディングの基本を説明します\n"
        srt = self.write("in.srt", text)
        self.apply(srt, self.cands([], srt), "--video-size", "1920x1080", "--font-size", "60")
        big = self.out()["srt"].read_text(encoding="utf-8")
        self.assertIn("今日はカラーグレーディングの基本を説明します\n", big)  # 1行28字まで入るので改行しない
        self.apply(srt, self.cands([], srt), "--video-size", "1920x1080", "--font-size", "120", "--overwrite")
        small = self.out()["srt"].read_text(encoding="utf-8").strip().split("\n")[2:]
        self.assertEqual(len(small), 2)  # 1行14字までなので2行になる
        self.assertIn("文字120px", self.out()["changes"])

    def test_settings_change_line_limits(self):
        srt = self.write("in.srt", "1\n00:00:01,000 --> 00:00:09,000\n今日はカラーグレーディングの基本を説明します\n")
        self.apply(srt, self.cands([], srt), "--max-chars", "12", "--max-lines", "3")
        lines = self.out()["srt"].read_text(encoding="utf-8").strip().split("\n")[2:]
        self.assertGreaterEqual(len(lines), 2)
        self.assertLessEqual(len(lines), 3)


# ── 改行 ───────────────────────────────────────────────────────────

class TestLineBreak(unittest.TestCase):
    st = Settings()

    def lines(self, text, protect=()):
        return best_breaks(text, self.st, protect).lines

    def test_does_not_split_words(self):
        cases = [
            ("今回はPremiere ProのLumetriカラーで説明します", "Premiere Pro", ["Premiere Pro"]),
            ("適用量は100%ではなく70%くらいに下げます", "100%", []),
            ("書き出しは4K 60fpsで確認しました", "60fps", []),
            ("まずルックアップテーブルを読み込みます", "ルックアップテーブル", []),
            ("次回はDaVinci Resolveでの書き出しを紹介します", "書き出し", ["DaVinci Resolve"]),
            ("書き出し時間は短くならないので注意してください", "ならない", []),
            ("ここでH.264とH.265の違いを説明しておきます", "H.265", []),
            ("あの人に二、三回教えてもらっただけで世界が変わりました", "もらった", []),
        ]
        for text, word, prot in cases:
            with self.subTest(text):
                joined = "\n".join(self.lines(text, prot))
                self.assertIn(word, joined, joined)

    def test_fits_when_possible(self):
        for text in ["えー今日はPremiere Proで動画を書き出す時の設定を解説します",
                     "ビットレートは10メガから16メガくらいが目安です",
                     "あのYouTubeに上げるならこの設定で十分です"]:
            with self.subTest(text):
                r = best_breaks(text, self.st, ["Premiere Pro"])
                self.assertTrue(r.fits, r.lines)
                self.assertLessEqual(len(r.lines), 2)

    def test_no_line_starts_with_forbidden_chars(self):
        for text in ["それは、とても大事なことです。だから確認してください",
                     "「字幕」と「テロップ」の違いを説明していきますね"]:
            with self.subTest(text):
                for ln in self.lines(text)[1:]:
                    self.assertNotIn(ln[0], "、。」』ーっ）")


# ── 検査（改ざんの検出） ───────────────────────────────────────────

class TestVerify(Base):
    def make(self):
        srt = self.write("in.srt", BASIC)
        c = self.cands([cand(index=1, original="以外と", proposed="意外と")], srt)
        self.assertEqual(self.apply(srt, c)[0], 0)
        o = self.out()
        return srt, o["srt"], self.dir / "out" / "in.apply-log.json"

    def check(self, srt, polished, log=None):
        args = [str(srt), str(polished)] + (["--log", str(log)] if log else [])
        return run(verify_srt.main, *args)

    def tamper(self, polished: Path, old: str, new: str, count: int = 1):
        t = polished.read_text(encoding="utf-8")
        self.assertIn(old, t)
        polished.write_bytes((t.replace(old, new, count)).encode("utf-8"))

    def test_clean_output_passes(self):
        srt, pol, log = self.make()
        code, out, _ = self.check(srt, pol, log)
        self.assertEqual(code, 0, out)
        self.assertIn("この検査で確認していないこと", out)

    def test_changed_time_is_detected(self):
        srt, pol, log = self.make()
        self.tamper(pol, "00:00:04,000 --> 00:00:06,000", "00:00:04,001 --> 00:00:06,000")
        code, out, _ = self.check(srt, pol)
        self.assertEqual(code, 1)
        self.assertIn("[NG ] 開始時刻・終了時刻", out)

    def test_time_format_change_is_detected(self):
        srt, pol, _ = self.make()
        self.tamper(pol, "00:00:04,000 --> 00:00:06,000", "00:00:04.000 --> 00:00:06.000")
        self.assertEqual(self.check(srt, pol)[0], 1)

    def test_renumbering_is_detected(self):
        srt, pol, _ = self.make()
        self.tamper(pol, "\n3\n00:00:06,500", "\n5\n00:00:06,500")
        code, out, _ = self.check(srt, pol)
        self.assertEqual(code, 1)
        self.assertIn("[NG ] 字幕番号と順番", out)

    def test_deleted_cue_is_detected(self):
        srt, pol, _ = self.make()
        t = pol.read_text(encoding="utf-8")
        pol.write_bytes((t.split("\n\n2\n")[0] + "\n\n3" + t.split("\n\n3", 1)[1]).encode("utf-8"))
        code, out, _ = self.check(srt, pol)
        self.assertEqual(code, 1)
        self.assertIn("[NG ] 字幕の件数", out)

    def test_swapped_order_is_detected(self):
        srt, pol, _ = self.make()
        t = pol.read_text(encoding="utf-8")
        blocks = t.split("\n\n")
        blocks[1], blocks[2] = blocks[2], blocks[1]
        pol.write_bytes(("\n\n".join(blocks)).encode("utf-8"))
        self.assertEqual(self.check(srt, pol)[0], 1)

    def test_unlogged_text_change_is_detected(self):
        srt, pol, log = self.make()
        self.tamper(pol, "LUTを使います", "LUTを使いません")
        code, out, _ = self.check(srt, pol, log)
        self.assertEqual(code, 1)
        self.assertIn("記録にない本文変更: #3", out)

    def test_modified_original_is_detected(self):
        srt, pol, log = self.make()
        srt.write_bytes((BASIC.replace("100", "200")).encode("utf-8"))
        code, out, _ = self.check(srt, pol, log)
        self.assertEqual(code, 1)
        self.assertIn("[NG ] 原本が変わっていない", out)

    def test_bom_and_newline_changes_are_detected(self):
        srt, pol, _ = self.make()
        data = pol.read_bytes()
        pol.write_bytes(b"\xef\xbb\xbf" + data)
        self.assertIn("[NG ] BOM", self.check(srt, pol)[1])
        pol.write_bytes(data.replace(b"\n", b"\r\n"))
        self.assertIn("[NG ] 改行コード", self.check(srt, pol)[1])


class TestCli(unittest.TestCase):
    def test_scripts_run_as_commands(self):
        for script in ["inspect_srt.py", "apply_candidates.py", "verify_srt.py"]:
            r = subprocess.run([sys.executable, str(ROOT / "scripts" / script), "--help"],
                               capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_missing_file_gives_readable_error(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "inspect_srt.py"), "nai.srt"],
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 2)
        self.assertIn("見つかりません", r.stderr)

    def test_examples_verify(self):
        for ex in sorted((ROOT / "examples").glob("0*")):
            for out in sorted(ex.glob("output*")):
                with self.subTest(str(out.relative_to(ROOT))):
                    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "verify_srt.py"),
                                        str(ex / "input.srt"), str(out / "input.polished.srt"),
                                        "--log", str(out / "input.apply-log.json")],
                                       capture_output=True, text=True, encoding="utf-8")
                    self.assertEqual(r.returncode, 0, r.stdout)


if __name__ == "__main__":
    unittest.main()
