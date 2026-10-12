#!/usr/bin/env python3
"""SRTを読み、AIが修正候補を作るための一覧と、構造・表示時間・改行の検査結果を出す。

ファイルは読むだけで、何も書き換えない。

使い方:
  python3 scripts/inspect_srt.py 元.srt [--glossary 辞書.json] [--preset standard]
      [--template 候補.json] [--json]

--template を付けると、ファイルのSHA-256と辞書から作った候補を入れた候補ファイルの
下書きを書き出す（既存のファイルは上書きしない）。
終了コード: 0=読み込めた / 2=読み込めない
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (glossary_candidates, load_glossary, load_settings, protect_terms,  # noqa: E402
                    settings_summary, setup_console, to_settings)
from jptext import existing_break_problems, flatten, is_dialogue, reading_chars, variant_hints  # noqa: E402
from srt_io import SrtError, read_srt  # noqa: E402


def inspect(path: str, cfg: dict, glossary: list, encoding: str = "utf-8") -> dict:
    src = read_srt(path, encoding)
    st = to_settings(cfg)
    protect = protect_terms(glossary, cfg)
    cues = src.cues
    rows, structure = [], list(src.warnings)
    seen = {}
    for k, c in enumerate(cues):
        dur = (c.end_ms - c.start_ms) / 1000
        text = flatten(c.original_lines).text
        chars = reading_chars("\n".join(c.original_lines), st.halfwidth_weight)
        flags = []
        if dur <= 0:
            structure.append(f"#{c.index}: 終了時刻が開始時刻より前か同じ（{c.timing}）")
        elif st.max_cps and chars / dur > st.max_cps:
            flags.append(f"目安より速い（1秒{chars / dur:.1f}字）")
        if is_dialogue(c.original_lines):
            flags.append("話者ごとの行（改行は変えない）")
        else:
            flags += [f"改行: {p}" for p in existing_break_problems(c.original_lines, st, protect)]
        if not text:
            flags.append("本文なし")
        if k + 1 < len(cues) and cues[k + 1].start_ms < c.end_ms:
            structure.append(f"#{c.index}: 次の字幕 #{cues[k + 1].index} と表示時間が重なる")
        if k > 0 and c.index != cues[k - 1].index + 1:
            structure.append(f"#{c.index}: 番号が連続していない（前は #{cues[k - 1].index}）")
        if c.index in seen:
            structure.append(f"#{c.index}: 同じ番号が2回以上ある（この番号への候補は適用されません）")
        seen[c.index] = True
        rows.append({"index": c.index, "start": c.start, "end": c.end, "duration": round(dur, 3),
                     "lines": c.original_lines, "text": text, "cps": round(chars / dur, 2) if dur > 0 else None,
                     "flags": flags})
    return {
        "file": Path(path).name, "source_sha256": src.sha256, "encoding": src.encoding,
        "bom": src.has_bom, "newline": src.newline_style(), "cue_count": len(cues),
        "settings": cfg, "structure": structure, "cues": rows,
        "glossary_candidates": glossary_candidates(cues, glossary),
        "variant_hints": variant_hints([(c.index, flatten(c.original_lines).text) for c in cues]),
    }


def format_text(r: dict) -> str:
    out = [f"# {Path(r['file']).name}",
           f"source_sha256: {r['source_sha256']}",
           f"文字コード {r['encoding']}・BOM {'あり' if r['bom'] else 'なし'}・改行コード {r['newline']}・字幕 {r['cue_count']}件",
           f"設定: {settings_summary(r['settings'])}", "",
           "## 構造", *([f"- {x}" for x in r["structure"]] or ["- 問題なし"]), "",
           "## 字幕（本文は改行を外した形。候補の original はこの文字列から写す。改行位置は「⏎」）",
           "（「⚠ 改行」は適用時にスクリプトが整えるため、候補にしなくてよい）"]
    if r["settings"].get("punctuation") == "telop":
        out.insert(4, "句読点：適用時にスクリプトが外す（テロップ表記）。句読点の候補は作らない")
    for c in r["cues"]:
        flags = ("  ⚠ " + " / ".join(c["flags"])) if c["flags"] else ""
        cps = f"{c['cps']}字/秒" if c["cps"] is not None else "—"
        out.append(f"[{c['index']}] {c['start']} --> {c['end']} ({c['duration']:.2f}秒, {cps}){flags}")
        out.append("    " + "⏎".join(c["lines"]) if len(c["lines"]) > 1 else "    " + c["text"])
    out += ["", "## 辞書と一致した箇所（候補ファイルの下書きに入ります）"]
    out += [f"- #{g['index']}: 「{g['original']}」→「{g['proposed']}」（{g['evidence']}）"
            for g in r["glossary_candidates"]] or ["- なし"]
    out += ["", "## 表記ゆれの手がかり（統一するかは文脈で判断）"]
    out += [f"- {h}" for h in r["variant_hints"]] or ["- なし"]
    return "\n".join(out)


def main(argv=None) -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="SRTを読み、修正候補づくりのための一覧と検査結果を出します。")
    ap.add_argument("srt")
    ap.add_argument("--glossary")
    ap.add_argument("--preset", default="standard")
    ap.add_argument("--config")
    ap.add_argument("--max-lines", type=int)
    ap.add_argument("--max-chars", type=float, dest="max_chars_per_line")
    ap.add_argument("--max-cps", type=float)
    ap.add_argument("--video-size", help="動画のサイズ（例: 1920x1080、縦型は 1080x1920）")
    ap.add_argument("--font-size", type=float, dest="font_px",
                    help="字幕の文字の大きさ（動画のピクセル）。指定すると1行の字数をここから計算する")
    ap.add_argument("--punct", choices=["keep", "telop"], dest="punctuation",
                    help="telop: 適用時に句読点を外す予定であることを表示に含める")
    ap.add_argument("--encoding", default="utf-8")
    ap.add_argument("--template", help="候補ファイルの下書きを書き出すパス")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        cfg = load_settings(args.preset, args.config, {
            "max_lines": args.max_lines, "max_chars_per_line": args.max_chars_per_line, "max_cps": args.max_cps, "video_size": args.video_size, "font_px": args.font_px,
            "punctuation": args.punctuation})
        glossary = load_glossary(args.glossary)
        r = inspect(args.srt, cfg, glossary, args.encoding)
    except (SrtError, ValueError, OSError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2
    print(json.dumps(r, ensure_ascii=False, indent=2) if args.json else format_text(r))
    if args.template:
        tp = Path(args.template)
        if tp.exists():
            print(f"\nエラー: {tp} はすでにあるため、下書きを書き出しませんでした。", file=sys.stderr)
            return 2
        draft = {"schema": "japanese-subtitle-polish/candidates-v1", "source_file": Path(args.srt).name,
                 "source_sha256": r["source_sha256"], "candidates": r["glossary_candidates"]}
        tp.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n候補ファイルの下書き: {tp}（辞書から {len(r['glossary_candidates'])}件）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
