#!/usr/bin/env python3
"""原本と修正版のSRTを照合し、本文以外が変わっていないことを機械的に検査する。

検査すること:
  - 原本が適用前から変わっていないか（適用ログのSHA-256と照合）
  - 字幕の件数・番号・順番・開始時刻・終了時刻（時刻行は1バイトも違わないこと）
  - 字幕の間の空行、BOM、改行コード、文字コード
  - 本文が変わった字幕が、適用ログに記録された字幕だけか
  - 修正版の本文に空行や時刻行の紛れ込みがないか

検査しないこと（人が確認する）:
  日本語として意味が保たれているか、読みやすいか、音声・映像と同期しているか。

使い方:
  python3 scripts/verify_srt.py 元.srt 元.polished.srt [--log 元.apply-log.json] [--json]
終了コード: 0=すべて一致 / 1=不一致あり / 2=読み込めない
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import setup_console  # noqa: E402
from srt_io import Cue, Gap, SrtError, read_srt, sha256_file  # noqa: E402

OUT_OF_SCOPE = [
    "日本語として意味・ニュアンスが保たれているか",
    "読みやすさ（改行位置・1行の長さが映像の上で自然か）",
    "音声・映像との同期（話し始め・話し終わりと表示のずれ）",
    "要確認一覧に残した箇所の正解",
]


def _compact(lines: List[str]) -> str:
    return re.sub(r"\s+", "", "".join(lines))


def verify(original: str, polished: str, log_path: Optional[str] = None,
           encoding: str = "utf-8") -> Dict:
    checks: List[Dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    log = None
    if log_path:
        log = json.loads(Path(log_path).read_text(encoding="utf-8"))
        cur = sha256_file(original)
        add("原本が変わっていない（SHA-256）", cur == log["source_sha256"],
            "" if cur == log["source_sha256"] else f"適用時 {log['source_sha256'][:12]}… / 現在 {cur[:12]}…")

    a = read_srt(original, encoding)
    b = read_srt(polished, encoding)
    ca, cb = a.cues, b.cues

    add("字幕の件数", len(ca) == len(cb), f"原本 {len(ca)}件 / 修正版 {len(cb)}件")

    idx_diff = [f"{k + 1}件目: {x.index_raw.strip()} → {y.index_raw.strip()}"
                for k, (x, y) in enumerate(zip(ca, cb)) if x.index_raw != y.index_raw]
    add("字幕番号と順番", not idx_diff and len(ca) == len(cb), "; ".join(idx_diff[:5]))

    t_diff = []
    for x, y in zip(ca, cb):
        if x.timing_raw != y.timing_raw:
            t_diff.append(f"#{x.index}: {x.timing} → {y.timing}")
    add("開始時刻・終了時刻（時刻行の完全一致）", not t_diff and len(ca) == len(cb), "; ".join(t_diff[:5]))

    gaps_a = [i.raw for i in a.items if isinstance(i, Gap)]
    gaps_b = [i.raw for i in b.items if isinstance(i, Gap)]
    order_a = ["G" if isinstance(i, Gap) else "C" for i in a.items]
    order_b = ["G" if isinstance(i, Gap) else "C" for i in b.items]
    add("字幕の間の空行", gaps_a == gaps_b and order_a == order_b)
    add("BOM", a.has_bom == b.has_bom, f"原本 {'あり' if a.has_bom else 'なし'} / 修正版 {'あり' if b.has_bom else 'なし'}")
    add("改行コード", a.newline_style() == b.newline_style(), f"原本 {a.newline_style()} / 修正版 {b.newline_style()}")

    bad_lines = []
    for y in cb:
        for ln in y.original_lines:
            if not ln.strip():
                bad_lines.append(f"#{y.index}: 空の本文行")
            if "-->" in ln:
                bad_lines.append(f"#{y.index}: 本文に「-->」")
    add("修正版の本文行が正しい形", not bad_lines, "; ".join(bad_lines[:5]))

    changed = [x.index for x, y in zip(ca, cb) if x.original_lines != y.original_lines]
    text_changed = [x.index for x, y in zip(ca, cb) if _compact(x.original_lines) != _compact(y.original_lines)]
    result = {"ok": False, "checks": checks, "changed_cues": changed,
              "text_changed_cues": text_changed, "out_of_scope": OUT_OF_SCOPE,
              "original": str(original), "polished": str(polished)}
    if log is not None:
        allowed_text = set(log.get("text_changed_cues", []))
        allowed_any = set(log.get("changed_cues", []))
        unexpected = [i for i in text_changed if i not in allowed_text]
        unexpected_lb = [i for i in changed if i not in allowed_any]
        add("本文の変更が適用ログの字幕だけ", not unexpected and not unexpected_lb,
            ("記録にない本文変更: " + ", ".join(f"#{i}" for i in unexpected) if unexpected else "")
            + ("記録にない改行変更: " + ", ".join(f"#{i}" for i in unexpected_lb) if unexpected_lb else ""))
        out_sha = sha256_file(polished)
        add("修正版が適用直後のまま（SHA-256）", out_sha == log.get("output_sha256"),
            "" if out_sha == log.get("output_sha256") else "適用後に修正版が編集されています")
    result["ok"] = all(c["ok"] for c in checks)
    return result


def format_report(r: Dict) -> str:
    out = ["## 機械検査（構造・時刻）", ""]
    for c in r["checks"]:
        mark = "OK " if c["ok"] else "NG "
        out.append(f"- [{mark}] {c['name']}" + (f" — {c['detail']}" if c["detail"] else ""))
    out.append("")
    out.append(f"判定: {'すべて一致' if r['ok'] else '不一致あり（修正版を使わないでください）'}")
    out.append(f"本文または改行が変わった字幕: {len(r['changed_cues'])}件"
               + (f"（うち文字が変わった字幕 {len(r['text_changed_cues'])}件）" if r["changed_cues"] else ""))
    out.append("")
    out.append("## この検査で確認していないこと（人が確認する）")
    out.extend(f"- {x}" for x in r["out_of_scope"])
    return "\n".join(out)


def main(argv=None) -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="原本と修正版SRTの件数・番号・順番・時刻の一致を検査します。")
    ap.add_argument("original")
    ap.add_argument("polished")
    ap.add_argument("--log", help="apply_candidates.py が書き出した .apply-log.json")
    ap.add_argument("--encoding", default="utf-8")
    ap.add_argument("--json", action="store_true", help="結果をJSONで出力")
    args = ap.parse_args(argv)
    try:
        r = verify(args.original, args.polished, args.log, args.encoding)
    except (SrtError, OSError, ValueError, KeyError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2
    print(json.dumps(r, ensure_ascii=False, indent=2) if args.json else format_report(r))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
