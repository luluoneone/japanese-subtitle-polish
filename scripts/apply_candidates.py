#!/usr/bin/env python3
"""修正候補（JSON）のうち、照合と安全チェックを通ったものだけをSRTの本文へ適用する。

元のSRTは読むだけで、修正版は別名で保存する。番号行・時刻行・字幕の間の空行は
読み込んだバイト列をそのまま書き戻し、最後に verify_srt.py と同じ検査を行う。

使い方:
  python3 scripts/apply_candidates.py 元.srt 候補.json [--glossary 辞書.json] [--preset standard]
      [--allow フィラー削除,要約] [--linebreak auto|keep|all] [--out-dir 出力先] [--dry-run]

出力（既定は元のSRTと同じフォルダ）:
  元.polished.srt      修正版
  元.changes.md        変更一覧（修正前後の比較）
  元.review.md         要確認一覧
  元.apply-log.json    適用ログ（verify_srt.py の照合に使う）
終了コード: 0=完了 / 1=検査で不一致 / 2=入力の問題 / 3=候補が別の版のSRT向け
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CANDIDATE_TYPES, MANUAL_TYPE, OPT_IN_TYPES, atomic_write, load_glossary,  # noqa: E402
                    load_settings, md_cell, protect_terms, settings_summary, setup_console,
                    to_settings)
from jptext import (BAD_BREAK_COST, Settings, best_breaks, break_cost, display_width,  # noqa: E402
                    TELOP_KEEP_DEFAULT, existing_break_problems, flatten, guard_problems, is_dialogue,
                    map_breaks, strip_telop_punct,
                    protected_ranges, reading_chars, split_at)
from srt_io import Cue, SrtError, format_ms, parse_bytes, read_srt, sha256_bytes, sha256_file  # noqa: E402
import review_ui  # noqa: E402
import verify_srt  # noqa: E402


@dataclass
class Planned:
    cand: Dict
    cue: Cue
    start: int
    end: int
    proposed: str
    explicit_lines: Optional[List[str]] = None


@dataclass
class CueChange:
    cue: Cue
    before: List[str]
    after: List[str]
    applied: List[Dict] = field(default_factory=list)
    linebreak_note: str = ""
    script_text: bool = False  # スクリプトが本文の文字を変えた（句読点を外した）


def _refuse(refused: List[Dict], cand: Dict, reason: str) -> None:
    refused.append({**cand, "refused_reason": reason})


def _badness(lines: List[str], st: Settings, protect) -> float:
    """改行の悪さ。語の途中の改行・行数超過・文字数超過を数える。"""
    flat = flatten(lines)
    prot = protected_ranges(flat.text, protect)
    bad = 0.0
    for pos, _ in flat.breaks:
        if break_cost(flat.text, pos, prot) >= BAD_BREAK_COST:
            bad += 100
    bad += max(0, len(lines) - st.max_lines) * 50
    for ln in lines:
        bad += max(0.0, display_width(ln, st.halfwidth_weight) - st.max_chars_per_line)
    return bad


def plan_candidates(src, cands: List[Dict], cfg: Dict) -> Tuple[List[Planned], List[Dict], List[Dict]]:
    """候補を照合し、適用予定・要確認（AIが判断保留）・不採用に分ける。"""
    by_index: Dict[int, List[Cue]] = {}
    for c in src.cues:
        by_index.setdefault(c.index, []).append(c)
    allowed_opt = set(cfg.get("allow_types", []))
    planned: List[Planned] = []
    review: List[Dict] = []
    refused: List[Dict] = []
    seen_ids = set()

    for cand in cands:
        cid = cand.get("id")
        if cid in seen_ids:
            _refuse(refused, cand, f"候補ID {cid} が重複しています")
            continue
        seen_ids.add(cid)
        status = cand.get("status", "apply")
        if status not in ("apply", "review"):
            _refuse(refused, cand, "status は apply か review にしてください")
            continue
        try:
            idx = int(cand["index"])
            original = cand["original"]
            proposed = cand.get("proposed", "")
        except (KeyError, TypeError, ValueError):
            _refuse(refused, cand, "index・original がない、または形式が違います")
            continue
        if status == "review":
            review.append(cand)
            continue
        ctype = cand.get("type", "")
        if ctype not in CANDIDATE_TYPES:
            _refuse(refused, cand, f"種類「{ctype}」は使えません（{'・'.join(CANDIDATE_TYPES)}）")
            continue
        if ctype == MANUAL_TYPE and not cand.get("approved"):
            _refuse(refused, cand, "「手動修正」は確認画面で利用者が直したもの（approved: true）だけ適用します")
            continue
        if ctype in OPT_IN_TYPES and ctype not in allowed_opt:
            _refuse(refused, cand, f"「{ctype}」は標準では行いません（利用者が選んだときだけ --allow {ctype}）")
            continue
        if not str(cand.get("reason", "")).strip() or not str(cand.get("evidence", "")).strip():
            _refuse(refused, cand, "理由（reason）と確認根拠（evidence）の両方が必要です")
            continue
        targets = by_index.get(idx, [])
        if not targets:
            _refuse(refused, cand, f"番号 {idx} の字幕がありません")
            continue
        if len(targets) > 1:
            _refuse(refused, cand, f"番号 {idx} の字幕が{len(targets)}件あり、対象を決められません")
            continue
        cue = targets[0]
        if cand.get("start") and cand["start"] != cue.start:
            _refuse(refused, cand, f"開始時刻が違います（候補 {cand['start']} / ファイル {cue.start}）")
            continue
        if "-->" in proposed:
            _refuse(refused, cand, "修正案に「-->」が含まれています")
            continue
        flat = flatten(cue.original_lines)
        scope = cand.get("scope", "span")
        explicit = None
        if scope == "cue":
            if flatten(original.split("\n")).text != flat.text:
                _refuse(refused, cand, "原文不一致：字幕全体の原文がファイルの本文と一致しません")
                continue
            s, e = 0, len(flat.text)
            if "\n" in proposed or ctype == "改行":
                # 「改行」の候補は、1行のままにする指定も含めて行の形をそのまま使う
                explicit = [x.strip() for x in proposed.split("\n") if x.strip()]
            elif is_dialogue(cue.original_lines):
                _refuse(refused, cand, "話者ごとに分かれた字幕です。字幕全体の候補では行の区切りも指定してください")
                continue
            new_text = flatten(proposed.split("\n")).text
            orig_text = flat.text
        elif scope == "span":
            if "\n" in proposed or "\n" in original:
                _refuse(refused, cand, "改行を含む候補は scope: \"cue\" で字幕全体を指定してください")
                continue
            if original == "":
                _refuse(refused, cand, "原文（original）が空です")
                continue
            count = flat.text.count(original)
            if count == 0:
                squeezed = flat.text.replace(" ", "").replace("　", "")
                hint = "（空白・改行を除くと見つかります）" if original.replace(" ", "") in squeezed else ""
                _refuse(refused, cand, "原文不一致：この字幕の本文に原文が見つかりません" + hint)
                continue
            if count > 1:
                _refuse(refused, cand, f"対象が曖昧：同じ字幕に原文が{count}回あります。前後の文字を含めて一意にしてください")
                continue
            s = flat.text.index(original)
            e = s + len(original)
            new_text = proposed
            orig_text = original
            if is_dialogue(cue.original_lines) and any(s < pos < e or (cons and s <= pos < e)
                                                       for pos, cons in flat.breaks):
                _refuse(refused, cand, "話者ごとの行をまたいでいます。行ごとに分けて候補を作ってください")
                continue
        else:
            _refuse(refused, cand, "scope は span か cue にしてください")
            continue
        if ctype == "改行" and new_text != orig_text:
            _refuse(refused, cand, "種類が「改行」なのに本文の文字が変わっています")
            continue
        if new_text == orig_text and explicit is None:
            _refuse(refused, cand, "修正前と修正後が同じです")
            continue
        full_after = flat.text[:s] + new_text + flat.text[e:]
        if not full_after.strip():
            _refuse(refused, cand, "本文が空になります（字幕の削除は件数の変更に当たるため行いません）")
            continue
        probs = guard_problems(orig_text, new_text, allowed_deletion=ctype in allowed_opt)
        if probs and not cand.get("approved"):
            _refuse(refused, cand, "安全チェック：" + "／".join(probs)
                    + "。利用者が確認して承認した場合だけ \"approved\": true で適用できます")
            continue
        planned.append(Planned(cand, cue, s, e, new_text, explicit))

    # 同じ字幕の中で範囲が重なる候補は、どちらも適用しない
    by_cue: Dict[int, List[Planned]] = {}
    for p in planned:
        by_cue.setdefault(id(p.cue), []).append(p)
    final: List[Planned] = []
    for plist in by_cue.values():
        plist.sort(key=lambda p: (p.start, p.end))
        bad = set()
        for a_i in range(len(plist)):
            for b_i in range(a_i + 1, len(plist)):
                a, b = plist[a_i], plist[b_i]
                if a.start < b.end and b.start < a.end or a.explicit_lines is not None or b.explicit_lines is not None \
                        or (a.start == a.end == b.start == b.end):
                    bad.update((a_i, b_i))
        for k, p in enumerate(plist):
            if k in bad:
                others = [q.cand.get("id") for j, q in enumerate(plist) if j != k and j in bad]
                _refuse(refused, p.cand, f"同じ箇所に別の候補があります（{', '.join(map(str, others))}）")
            else:
                final.append(p)
    return final, review, refused


def layout(cue: Cue, plist: List[Planned], st: Settings, protect, mode: str,
           punct_keep: Optional[str] = None) -> Tuple[List[str], str, bool]:
    """適用後の本文行を決める。

    戻り値: (行, 改行についての説明, 句読点を外したか)。punct_keep が None なら句読点は外さない。
    """
    flat = flatten(cue.original_lines)
    orig_lines = [x.strip() for x in cue.original_lines if x.strip()]
    if plist and plist[0].explicit_lines is not None:
        return plist[0].explicit_lines, "候補で指定された改行", False
    dialogue = is_dialogue(cue.original_lines)
    if dialogue:
        mode = "keep"  # 話者ごとの行は改行を変えない
    text = flat.text
    edits = []
    for p in sorted(plist, key=lambda p: -p.start):
        text = text[:p.start] + p.proposed + text[p.end:]
    for p in sorted(plist, key=lambda p: p.start):
        edits.append((p.start, p.end, len(p.proposed)))
    mapped = map_breaks(flat.breaks, edits) if plist else list(flat.breaks)
    punct = False
    if punct_keep is not None:
        # 話者の行の「（名前）」は外さない
        text2, pedits = strip_telop_punct(text, punct_keep + ("（）()" if dialogue else ""))
        if pedits:
            punct = True
            mapped = map_breaks(mapped, pedits) if mapped is not None else None
            text = text2
    kept = split_at(text, mapped) if mapped is not None else None
    if kept is not None and any(x == "" for x in kept):
        kept = None

    if mode == "keep":
        if kept is not None:
            return kept, "", punct
        n = max(1, len(orig_lines))
        st2 = Settings(**{**st.__dict__, "max_lines": n})
        return best_breaks(text, st2, protect).lines, "修正箇所に改行があったため、元の行数で改行し直した", punct

    br = best_breaks(text, st, protect)
    if mode == "all":
        return br.lines, "設定に合わせて改行し直した", punct
    # auto: 元の改行（修正後の位置）を優先し、問題がある場合だけ改行し直す
    if kept is not None:
        if _badness(kept, st, protect) <= _badness(br.lines, st, protect):
            return kept, "", punct
        probs = existing_break_problems(kept, st, protect)
        return br.lines, ("改行を整えた：" + "、".join(probs) if probs else "改行を整えた"), punct
    return br.lines, "修正箇所に改行があったため改行し直した", punct


def build(src, cands: List[Dict], cfg: Dict, glossary: List[Dict]):
    st = to_settings(cfg)
    protect = protect_terms(glossary, cfg)
    planned, review, refused = plan_candidates(src, cands, cfg)
    by_cue: Dict[int, List[Planned]] = {}
    for p in planned:
        by_cue.setdefault(id(p.cue), []).append(p)
    punct_keep = cfg.get("punct_keep", TELOP_KEEP_DEFAULT) if cfg.get("punctuation") == "telop" else None

    changes: List[CueChange] = []
    for cue in src.cues:
        plist = by_cue.get(id(cue), [])
        before = cue.original_lines
        if not plist and cfg["linebreak_mode"] == "keep" and punct_keep is None:
            continue
        lines, note, punct = layout(cue, plist, st, protect, cfg["linebreak_mode"], punct_keep)
        if lines == before:
            continue
        if not plist and not punct and cfg["linebreak_mode"] == "auto" and \
                _badness(lines, st, protect) >= _badness([x.strip() for x in before if x.strip()], st, protect):
            continue  # 改行し直しても良くならない字幕は触らない
        cue.new_lines = lines
        if not plist:
            probs = existing_break_problems(before, st, protect)
            if probs:
                note = "改行を整えた：" + "、".join(probs)
            elif not note and not punct:
                note = "改行を整えた"
        if punct:
            note = "句読点を外した（テロップ表記）" + ("。" + note if note else "")
        changes.append(CueChange(cue, before, lines, [p.cand for p in plist], note, punct))

    # 適用後の字幕を検査し、要確認へ回す
    auto_review: List[Dict] = []
    cues = src.cues
    for k, cue in enumerate(cues):
        lines = cue.lines
        dur = (cue.end_ms - cue.start_ms) / 1000
        chars = reading_chars("\n".join(lines), st.halfwidth_weight)
        if dur <= 0:
            auto_review.append({"kind": "構造", "index": cue.index, "cue": cue,
                                "detail": f"終了時刻が開始時刻より前か同じです（{cue.timing}）"})
        elif st.max_cps and chars / dur > st.max_cps:  # 0 は「知らせない」
            auto_review.append({"kind": "長い", "index": cue.index, "cue": cue,
                                "detail": f"{chars:g}字 / {dur:.2f}秒 = 1秒あたり{chars / dur:.1f}字（目安 {st.max_cps:g}字）",
                                "cps": chars / dur})
        over = [display_width(x, st.halfwidth_weight) for x in lines]
        if len(lines) > st.max_lines or any(x > st.max_chars_per_line for x in over):
            auto_review.append({"kind": "収まらない", "index": cue.index, "cue": cue,
                                "detail": f"{len(lines)}行・最長{max(over):g}字（上限 {st.max_lines}行・{st.max_chars_per_line:g}字）"})
        if k + 1 < len(cues) and cues[k + 1].start_ms < cue.end_ms:
            auto_review.append({"kind": "構造", "index": cue.index, "cue": cue,
                                "detail": f"次の字幕 #{cues[k + 1].index} と表示時間が重なっています"})
        if k > 0 and cue.index != cues[k - 1].index + 1:
            auto_review.append({"kind": "構造", "index": cue.index, "cue": cue,
                                "detail": f"番号が連続していません（前は #{cues[k - 1].index}）"})
    for w in src.warnings:
        auto_review.append({"kind": "構造", "index": None, "cue": None, "detail": w})
    return changes, review, refused, auto_review, planned


# ── レポート ───────────────────────────────────────────────────────

def _time(cue: Cue) -> str:
    return f"{cue.start} → {cue.end}"


def changes_md(name: str, out_name: str, changes: List[CueChange], cfg: Dict) -> str:
    n_cand = sum(len(c.applied) for c in changes)
    n_lb = sum(1 for c in changes if not c.applied)
    n_p = sum(1 for c in changes if c.script_text)
    out = [f"# 変更一覧 — {name}", "",
           f"- 修正版: `{out_name}`",
           f"- 変更した字幕: {len(changes)}件（修正候補の適用 {n_cand}件、スクリプトだけの調整 {n_lb}件）"
           + (f"。句読点を外した字幕 {n_p}件" if n_p else ""),
           f"- 設定: {settings_summary(cfg)}",
           "- 字幕の件数・番号・順番・時刻は変えていません（検査結果は末尾）。", ""]
    if not changes:
        out.append("変更はありません。")
        return "\n".join(out)
    out += ["## 一覧", "", "| 番号 | 種類 | 修正前 | 修正後 | 理由 |", "|---|---|---|---|---|"]
    for ch in changes:
        if ch.applied:
            for c in ch.applied:
                out.append(f"| #{ch.cue.index} | {c.get('type')} | {md_cell(c['original'])} | "
                           f"{md_cell(c.get('proposed', ''))} | {md_cell(c.get('reason', ''))} |")
        else:
            kind = "句読点・改行" if ch.script_text else "改行"
            out.append(f"| #{ch.cue.index} | {kind} | {md_cell(chr(10).join(ch.before))} | "
                       f"{md_cell(chr(10).join(ch.after))} | {md_cell(ch.linebreak_note)} |")
    out += ["", "## 字幕ごとの修正前後", ""]
    for ch in changes:
        out.append(f"### #{ch.cue.index}　{_time(ch.cue)}")
        out.append("")
        out.append("```text")
        out.append("修正前:")
        out.extend("  " + x for x in ch.before)
        out.append("修正後:")
        out.extend("  " + x for x in ch.after)
        out.append("```")
        for c in ch.applied:
            appr = "（利用者が承認）" if c.get("approved") else ""
            out.append(f"- {c.get('id', '')} {c.get('type')}{appr}：「{c['original']}」→「{c.get('proposed', '')}」"
                       f"　理由: {c.get('reason', '')}　確認根拠: {c.get('evidence', '')}")
        if ch.linebreak_note:
            out.append(f"- スクリプト：{ch.linebreak_note}")
        out.append("")
    return "\n".join(out)


def review_md(name: str, review: List[Dict], refused: List[Dict], auto_review: List[Dict],
              src, cfg: Dict) -> str:
    by_idx = {}
    for c in src.cues:
        by_idx.setdefault(c.index, c)

    def t(idx):
        c = by_idx.get(idx)
        return _time(c) if c else "—"

    out = [f"# 要確認一覧 — {name}", "",
           "スクリプトもAIも、ここにある箇所は自動では直していません。音声・台本・資料で確かめてから判断してください。", ""]
    out += ["## 1. 音声や資料で確かめてほしい箇所", ""]
    if review:
        out += ["| 番号 | 時刻 | 原文 | 候補 | 理由 | 確かめ方 |", "|---|---|---|---|---|---|"]
        for c in review:
            out.append(f"| #{c.get('index')} | {t(c.get('index'))} | {md_cell(c.get('original', ''))} | "
                       f"{md_cell(c.get('proposed', '') or '—')} | {md_cell(c.get('reason', ''))} | "
                       f"{md_cell(c.get('evidence', '') or c.get('check', ''))} |")
    else:
        out.append("なし")
    out += ["", "## 2. 適用しなかった修正候補（照合・安全チェック）", ""]
    if refused:
        out += ["| 候補 | 番号 | 原文 | 修正案 | 適用しなかった理由 |", "|---|---|---|---|---|"]
        for c in refused:
            out.append(f"| {c.get('id', '—')} | #{c.get('index', '—')} | {md_cell(str(c.get('original', '')))} | "
                       f"{md_cell(str(c.get('proposed', '')))} | {md_cell(c['refused_reason'])} |")
    else:
        out.append("なし")
    long_ = [r for r in auto_review if r["kind"] == "長い"]
    if not cfg.get("max_cps"):
        out += ["", "## 3. 見やすさの目安より速い字幕", "", "読む速さの目安は「知らせない」に設定されています。"]
    else:
        out += ["", f"## 3. 見やすさの目安より速い字幕（1秒{cfg['max_cps']:g}字を超える）", "",
                "目安なので、超えても誤りではありません。時刻は変更していません。読み切れるか再生して確かめ、"
                "必要なら編集ソフトで表示時間か文言を調整してください。", ""]
        if long_:
            out += ["| 番号 | 時刻 | 本文 | 文字数と表示時間 |", "|---|---|---|---|"]
            for r in sorted(long_, key=lambda r: -r["cps"]):
                out.append(f"| #{r['index']} | {_time(r['cue'])} | {md_cell(chr(10).join(r['cue'].lines))} | {r['detail']} |")
        else:
            out.append("なし")
    fit = [r for r in auto_review if r["kind"] == "収まらない"]
    out += ["", "## 4. 行数・文字数の上限に収まらない字幕", "",
            "語の途中で切らずに改行できる範囲で整えています。字幕の分割は時刻の変更になるため行いません。", ""]
    if fit:
        out += ["| 番号 | 時刻 | 本文 | 状態 |", "|---|---|---|---|"]
        for r in fit:
            out.append(f"| #{r['index']} | {_time(r['cue'])} | {md_cell(chr(10).join(r['cue'].lines))} | {r['detail']} |")
    else:
        out.append("なし")
    struct = [r for r in auto_review if r["kind"] == "構造"]
    out += ["", "## 5. 構造の注意（変更はしていません）", ""]
    out += [f"- {('#' + str(r['index']) + ' ') if r['index'] is not None else ''}{r['detail']}" for r in struct] or ["なし"]
    out += ["", "## この一覧と機械検査で確認していないこと", "",
            "- 日本語として意味・ニュアンスが保たれているか（修正版を通して読んで確認）",
            "- 映像の上で読みやすいか（実際の画面サイズで再生して確認）",
            "- 音声・映像との同期（時刻は原本のままなので、原本のずれもそのまま残ります）"]
    return "\n".join(out)


def main(argv=None) -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="修正候補をSRTの本文へ適用し、修正版を別名で保存します。")
    ap.add_argument("srt")
    ap.add_argument("candidates", help="修正候補のJSON")
    ap.add_argument("--glossary", help="辞書（.json または .tsv）。改行で辞書語を分けないために使う")
    ap.add_argument("--preset", default="standard", help="standard / safe / vertical")
    ap.add_argument("--config", help="設定ファイル（プリセットを上書き）")
    ap.add_argument("--max-lines", type=int)
    ap.add_argument("--max-chars", type=float, dest="max_chars_per_line")
    ap.add_argument("--max-cps", type=float, help="見やすさの目安（1秒あたりの字数）。超えた字幕を報告する。0 で報告しない")
    ap.add_argument("--video-size", help="動画のサイズ（例: 1920x1080、縦型は 1080x1920）")
    ap.add_argument("--font-size", type=float, dest="font_px",
                    help="字幕の文字の大きさ（動画のピクセル）。指定すると1行の字数をここから計算する")
    ap.add_argument("--linebreak", choices=["auto", "keep", "all"], dest="linebreak_mode")
    ap.add_argument("--allow", default="", help="標準では行わない種類を許可（フィラー削除,要約）")
    ap.add_argument("--punct", choices=["keep", "telop"], dest="punctuation",
                    help="telop: 句読点などを外す（ダッシュ・…・！・？は残す）")
    ap.add_argument("--punct-keep", dest="punct_keep", help="telop のときに残す記号（例: \"ー－−…！\"）")
    ap.add_argument("--encoding", default="utf-8")
    ap.add_argument("--out-dir", help="出力先（既定は元のSRTと同じフォルダ）")
    ap.add_argument("--overwrite", action="store_true", help="既存の出力ファイルを上書きする")
    ap.add_argument("--dry-run", action="store_true", help="ファイルを書かずに結果だけ表示")
    args = ap.parse_args(argv)

    try:
        allow = [x.strip() for x in args.allow.split(",") if x.strip()]
        cfg = load_settings(args.preset, args.config, {
            "max_lines": args.max_lines, "max_chars_per_line": args.max_chars_per_line,
            "max_cps": args.max_cps, "video_size": args.video_size, "font_px": args.font_px, "linebreak_mode": args.linebreak_mode,
            "allow_types": allow or None, "punctuation": args.punctuation, "punct_keep": args.punct_keep})
        glossary = load_glossary(args.glossary)
        src_path = Path(args.srt).resolve()
        src = read_srt(src_path, args.encoding)
        cpath = Path(args.candidates)
        if not cpath.exists():
            raise ValueError(f"候補ファイルが見つかりません: {cpath}")
        cdata = json.loads(cpath.read_text(encoding="utf-8-sig"))
    except (SrtError, ValueError, OSError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"エラー: 候補ファイルのJSONが読めません（{e.lineno}行目 {e.colno}文字目: {e.msg}）", file=sys.stderr)
        return 2

    if isinstance(cdata, list):
        cands, expected_sha = cdata, None
    else:
        cands, expected_sha = cdata.get("candidates", []), cdata.get("source_sha256")
    if expected_sha and expected_sha != src.sha256:
        print("エラー: 候補ファイルは別の版のSRTに対して作られています（source_sha256 が一致しません）。\n"
              f"  候補: {expected_sha}\n  SRT : {src.sha256}\n"
              "SRTを書き出し直した場合は、inspect_srt.py からやり直してください。", file=sys.stderr)
        return 3
    notes = [] if expected_sha else ["候補ファイルに source_sha256 がないため、ファイル全体の照合は省略しました。"]

    changes, review, refused, auto_review, planned = build(src, cands, cfg, glossary)

    out_dir = Path(args.out_dir).resolve() if args.out_dir else src_path.parent
    stem = src_path.stem
    out_srt = out_dir / f"{stem}.polished.srt"
    out_changes = out_dir / f"{stem}.changes.md"
    out_review = out_dir / f"{stem}.review.md"
    out_log = out_dir / f"{stem}.apply-log.json"
    out_html = out_dir / f"{stem}.review.html"
    outputs = [out_srt, out_changes, out_review, out_log, out_html]

    data = src.to_bytes()
    n_applied = sum(len(c.applied) for c in changes)
    summary = [f"字幕 {len(src.cues)}件 / 変更 {len(changes)}件（候補の適用 {n_applied}件・スクリプトだけ "
               f"{sum(1 for c in changes if not c.applied)}件）",
               f"要確認 {len(review)}件 / 適用しなかった候補 {len(refused)}件 / "
               f"目安より速い字幕 {sum(1 for r in auto_review if r['kind'] == '長い')}件 / "
               f"上限に収まらない字幕 {sum(1 for r in auto_review if r['kind'] == '収まらない')}件"]
    if args.dry_run:
        print("\n".join(notes + summary))
        for r in refused:
            print(f"  不採用 {r.get('id')}: {r['refused_reason']}")
        return 0

    if any(p.resolve() == src_path for p in outputs):
        print("エラー: 出力先が元のファイルと同じです。", file=sys.stderr)
        return 2
    existing = [p for p in outputs if p.exists()]
    if existing and not args.overwrite:
        print("エラー: 出力ファイルがすでにあります（上書きする場合は --overwrite）:\n  "
              + "\n  ".join(str(p) for p in existing), file=sys.stderr)
        return 2

    before_sha = src.sha256
    if sha256_file(src_path) != before_sha:
        print("エラー: 読み込み後に元のファイルが変更されました。保存を中止しました。", file=sys.stderr)
        return 3
    # 書き出す前に、メモリ上で構造が保たれているか確かめる
    reparsed = parse_bytes(data, src.encoding)
    if [c.timing_raw for c in reparsed.cues] != [c.timing_raw for c in src.cues] or \
            [c.index_raw for c in reparsed.cues] != [c.index_raw for c in src.cues]:
        print("エラー: 書き出し前の検査で構造の不一致が見つかったため、保存を中止しました。", file=sys.stderr)
        return 1

    atomic_write(out_srt, data)
    log = {
        "tool": "japanese-subtitle-polish",
        "source": src_path.name, "source_sha256": before_sha,
        "output": out_srt.name, "output_sha256": sha256_bytes(data),
        "encoding": src.encoding, "bom": src.has_bom, "newline": src.newline_style(),
        "settings": cfg,
        "changed_cues": [c.cue.index for c in changes],
        "text_changed_cues": [c.cue.index for c in changes if c.applied or c.script_text],
        "applied": [{**c, "applied_to": ch.cue.index} for ch in changes for c in ch.applied],
        "script_only": [{"index": ch.cue.index, "before": ch.before, "after": ch.after, "note": ch.linebreak_note,
                         "punctuation": ch.script_text}
                           for ch in changes if not ch.applied],
        "review": review, "refused": refused,
        "auto_review": [{k: v for k, v in r.items() if k != "cue"} for r in auto_review],
        "notes": notes,
        "glossary": Path(args.glossary).name if args.glossary else None,
        "preset": args.preset,
        "candidates_file": cpath.name,
    }
    atomic_write(out_log, json.dumps(log, ensure_ascii=False, indent=2).encode("utf-8"))
    vr = verify_srt.verify(str(src_path), str(out_srt), str(out_log), args.encoding)
    report = verify_srt.format_report(vr)
    atomic_write(out_changes, (changes_md(src_path.name, out_srt.name, changes, cfg)
                               + "\n\n---\n\n" + report + "\n").encode("utf-8"))
    atomic_write(out_review, (review_md(src_path.name, review, refused, auto_review, src, cfg) + "\n").encode("utf-8"))
    atomic_write(out_html, review_ui.render(src, cdata if isinstance(cdata, dict) else {"candidates": cands},
                                            changes, review, refused, auto_review, cfg, log, vr).encode("utf-8"))

    if sha256_file(src_path) != before_sha:
        print("エラー: 処理中に元のファイルが変更されました。結果を使わないでください。", file=sys.stderr)
        return 1
    print("\n".join(notes + summary))
    print(f"\n修正版   : {out_srt}\n変更一覧 : {out_changes}\n要確認   : {out_review}\n確認画面 : {out_html}\n"
          f"適用ログ : {out_log}\n")
    print(report)
    return 0 if vr["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
