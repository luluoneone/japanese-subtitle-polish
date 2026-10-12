"""確認画面（1つのHTMLファイル）を作る。

適用結果をHTMLに埋め込むので、利用者はファイルをダブルクリックするだけで開ける。
画面は外部のサイトやフォントを読み込まず、SRTも書き換えない。決定は候補ファイルとして保存し、
apply_candidates.py で適用し直す。
"""
from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
from typing import Dict, List

from jptext import flatten, reading_chars

TEMPLATE = Path(__file__).resolve().parent.parent / "ui" / "review.html"


def _state_of(cands: List[Dict], changes, review: List[Dict], refused: List[Dict]) -> List[Dict]:
    applied_ids = {id(c): ch.cue.index for ch in changes for c in ch.applied}
    review_ids = {id(c) for c in review}
    pending_refused = list(refused)
    out = []
    for k, c in enumerate(cands):
        item = {"key": k, "cand": c, "state": "unknown", "refused_reason": "", "approvable": False}
        if id(c) in applied_ids:
            item["state"] = "applied"
        elif id(c) in review_ids:
            item["state"] = "review"
        else:
            for r in pending_refused:
                if {kk: vv for kk, vv in r.items() if kk != "refused_reason"} == c:
                    item["state"] = "refused"
                    item["refused_reason"] = r["refused_reason"]
                    item["approvable"] = r["refused_reason"].startswith("安全チェック")
                    pending_refused.remove(r)
                    break
        out.append(item)
    return out


def build_data(src, cdoc: Dict, changes, review, refused, auto_review, cfg: Dict, log: Dict, vr: Dict) -> Dict:
    hw = float(cfg["halfwidth_weight"])
    ch_by_cue = {ch.cue.index: ch for ch in changes}
    flags: Dict[int, List[Dict]] = {}
    for r in auto_review:
        if r.get("index") is not None:
            flags.setdefault(r["index"], []).append({"kind": r["kind"], "detail": r["detail"]})
    cues = []
    for c in src.cues:
        ch = ch_by_cue.get(c.index)
        before = c.original_lines
        after = c.lines
        dur = (c.end_ms - c.start_ms) / 1000
        chars = reading_chars("\n".join(after), hw)
        cues.append({
            "index": c.index, "start": c.start, "end": c.end, "duration": round(dur, 3),
            "cps": round(chars / dur, 2) if dur > 0 else None,
            "before": before, "after": after, "flat_before": flatten(before).text,
            "changed": ch is not None, "linebreak_only": bool(ch and not ch.applied),
            "linebreak_note": ch.linebreak_note if ch else "",
            "script_label": ("句読点・改行" if ch and ch.script_text else "改行だけ"),
            "flags": flags.get(c.index, []),
        })
    cands = list(cdoc.get("candidates", []))
    doc_meta = {k: v for k, v in cdoc.items() if k != "candidates"}
    doc_meta.setdefault("source_sha256", src.sha256)
    return {
        "schema": "japanese-subtitle-polish/review-ui-v1",
        "file": log["source"], "stem": Path(log["source"]).stem, "output": log["output"],
        "source_sha256": src.sha256, "encoding": src.encoding, "bom": src.has_bom,
        "newline": src.newline_style(), "settings": cfg, "preset": log.get("preset"),
        "glossary": log.get("glossary"), "candidates_file": log.get("candidates_file"),
        "verify": {"ok": vr["ok"], "checks": vr["checks"]}, "out_of_scope": vr["out_of_scope"],
        "structure": [r["detail"] for r in auto_review if r["kind"] == "構造"],
        "cues": cues, "candidates": _state_of(cands, changes, review, refused), "candidates_doc": doc_meta,
        # 候補が変わるたびに変わる値。画面の決定の記憶をこれで区別する（適用し直したあとに古い決定を使わない）
        "revision": hashlib.sha256(json.dumps(cands, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16],
    }


def render(src, cdoc, changes, review, refused, auto_review, cfg, log, vr) -> str:
    data = build_data(src, cdoc, changes, review, refused, auto_review, cfg, log, vr)
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace("\u2028", "\\u2028")
    tpl = TEMPLATE.read_text(encoding="utf-8")
    return (tpl.replace("__TITLE__", html.escape(data["file"]))
               .replace("/*__DATA__*/null", payload))
