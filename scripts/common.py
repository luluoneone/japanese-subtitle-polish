"""設定・辞書・出力の共通処理。"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, fields
from pathlib import Path
from typing import Dict, List, Optional

from jptext import TELOP_KEEP_DEFAULT, Settings, char_class, flatten

SKILL_DIR = Path(__file__).resolve().parent.parent
PRESET_DIR = SKILL_DIR / "config" / "presets"

CANDIDATE_TYPES = ["誤字", "誤変換", "表記ゆれ", "固有名詞", "句読点", "改行", "フィラー削除", "要約", "手動修正"]
MANUAL_TYPE = "手動修正"  # 確認画面で利用者が直接直したもの（approved: true のときだけ適用）
OPT_IN_TYPES = {"フィラー削除", "要約"}  # 標準では適用しない（--allow で選んだときだけ）


def setup_console() -> None:
    """Windowsのコンソールでも日本語が化けないようにする。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def load_settings(preset: str = "standard", config_path: Optional[str] = None,
                  overrides: Optional[Dict] = None) -> Dict:
    """プリセット → 設定ファイル → コマンドの指定、の順に上書きする。"""
    pfile = PRESET_DIR / f"{preset}.json"
    if not pfile.exists():
        names = sorted(p.stem for p in PRESET_DIR.glob("*.json"))
        raise ValueError(f"プリセット「{preset}」がありません。使えるもの: {', '.join(names)}")
    cfg = json.loads(pfile.read_text(encoding="utf-8"))
    if config_path:
        cp = Path(config_path)
        if not cp.exists():
            raise ValueError(f"設定ファイルが見つかりません: {cp}")
        cfg.update(json.loads(cp.read_text(encoding="utf-8")))
    for k, v in (overrides or {}).items():
        if v is not None:
            cfg[k] = v
    cfg.setdefault("allow_types", [])
    bad = [t for t in cfg["allow_types"] if t not in OPT_IN_TYPES]
    if bad:
        raise ValueError(f"--allow に指定できるのは {', '.join(sorted(OPT_IN_TYPES))} だけです（指定: {', '.join(bad)}）")
    cfg.setdefault("punctuation", "keep")
    if cfg["punctuation"] not in ("keep", "telop"):
        raise ValueError("punctuation は keep（残す）か telop（句読点を外す）です")
    if cfg.get("punct_keep") is None:
        cfg["punct_keep"] = TELOP_KEEP_DEFAULT
    if cfg.get("linebreak_mode") not in ("auto", "keep", "all"):
        raise ValueError("linebreak_mode は auto / keep / all のどれかです")
    apply_font_size(cfg)
    if int(cfg.get("max_lines", 2)) < 1 or float(cfg.get("max_chars_per_line", 16)) < 4:
        raise ValueError("max_lines は1以上、max_chars_per_line は4以上にしてください")
    return cfg


def parse_video_size(text: str):
    """「1920x1080」→ (1920, 1080)"""
    import re as _re
    m = _re.fullmatch(r"\s*(\d{2,5})\s*[x×X*]\s*(\d{2,5})\s*", str(text))
    if not m:
        raise ValueError(f"動画のサイズは「1920x1080」の形で指定してください（指定: {text}）")
    return int(m.group(1)), int(m.group(2))


def chars_for_font(video_width: int, font_px: float, safe_margin: float = 0.05) -> int:
    """動画の横幅のうち字幕を置ける幅（左右 safe_margin ずつ空ける）に、全角が何字入るか。"""
    return int((video_width * (1 - 2 * safe_margin)) // font_px)


def apply_font_size(cfg: Dict) -> None:
    """文字の大きさ（font_px）と動画のサイズが指定されていれば、1行の字数をそこから決める。"""
    if cfg.get("video_size"):
        cfg["video_width"], cfg["video_height"] = parse_video_size(cfg["video_size"])
    if cfg.get("font_px"):
        w = int(cfg.get("video_width") or 1920)
        cfg.setdefault("video_width", w)
        cfg.setdefault("video_height", 1080 if w >= 1080 * 16 / 9 - 1 else int(w * 16 / 9))
        font = float(cfg["font_px"])
        if not 8 <= font <= w / 4:
            raise ValueError(f"文字の大きさは 8px 〜 {int(w / 4)}px の範囲で指定してください（動画の横幅 {w}px）")
        cfg["max_chars_per_line"] = max(4, chars_for_font(w, font, float(cfg.get("safe_margin", 0.05))))


def to_settings(cfg: Dict) -> Settings:
    names = {f.name for f in fields(Settings)}
    st = Settings(**{k: v for k, v in cfg.items() if k in names})
    st.max_lines = int(st.max_lines)
    return st


def settings_summary(cfg: Dict) -> str:
    size = (f"文字{cfg['font_px']:g}px（{cfg['video_width']}×{cfg['video_height']}の動画）→ " if cfg.get("font_px") else "")
    return (size + f"最大{cfg['max_lines']}行・1行{cfg['max_chars_per_line']}字・"
            f"半角{cfg['halfwidth_weight']}字換算・"
            + (f"見やすさの目安 1秒{cfg['max_cps']:g}字" if cfg.get("max_cps") else "読む速さは知らせない")
            + f"・改行モード {cfg['linebreak_mode']}"
            + ("・句読点を外す（テロップ表記）" if cfg.get("punctuation") == "telop" else "")
            + (f"・追加で許可: {', '.join(cfg['allow_types'])}" if cfg.get("allow_types") else ""))


# ── 辞書 ───────────────────────────────────────────────────────────

def load_glossary(path: Optional[str]) -> List[Dict]:
    """JSON（{"terms":[{"correct","variants","note"}]}）または TSV（正式表記<TAB>誤表記|誤表記<TAB>メモ）。"""
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        raise ValueError(f"辞書ファイルが見つかりません: {p}")
    raw = p.read_text(encoding="utf-8-sig")
    terms: List[Dict] = []
    if p.suffix.lower() == ".json":
        data = json.loads(raw)
        for t in data.get("terms", []):
            if not t.get("correct"):
                raise ValueError(f"辞書の項目に correct がありません: {t}")
            terms.append({"correct": t["correct"], "variants": list(t.get("variants", [])),
                          "note": t.get("note", "")})
    else:
        for k, line in enumerate(raw.splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cols = line.split("\t")
            if not cols[0].strip():
                raise ValueError(f"辞書 {k}行目: 正式表記が空です")
            variants = [v.strip() for v in (cols[1] if len(cols) > 1 else "").split("|") if v.strip()]
            terms.append({"correct": cols[0].strip(), "variants": variants,
                          "note": cols[2].strip() if len(cols) > 2 else ""})
    return terms


def protect_terms(glossary: List[Dict], cfg: Dict) -> List[str]:
    return [t["correct"] for t in glossary] + list(cfg.get("protect_terms", []))


_KATA = "\u30a0-\u30ff\u31f0-\u31ff\uff66-\uff9f"


def _variant_regex(variant: str) -> re.Pattern:
    """英字は単語の途中、カタカナはカタカナ語の途中に一致させない（「ラット」と「フラット」等）。"""
    body = re.escape(variant).replace(r"\ ", r"\s*")
    if variant.isascii():
        return re.compile(r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])", re.IGNORECASE)
    if char_class(variant[0]) == "T":
        body = f"(?<![{_KATA}])" + body
    if char_class(variant[-1]) == "T":
        body = body + f"(?![{_KATA}])"
    return re.compile(body)


def glossary_hits(text: str, glossary: List[Dict]):
    """本文の中で、辞書の正式表記と違う書き方をしている箇所。"""
    hits = []
    for t in glossary:
        correct = t["correct"]
        inside_correct = [(m.start(), m.end()) for m in re.finditer(re.escape(correct), text)]
        pats = [_variant_regex(v) for v in t["variants"]]
        if correct.isascii():
            pats.append(_variant_regex(correct))  # 大文字・小文字だけ違う場合
        for pat in pats:
            for m in pat.finditer(text):
                if m.group(0) == correct:
                    continue
                if any(s <= m.start() and m.end() <= e for s, e in inside_correct):
                    continue
                hits.append({"start": m.start(), "end": m.end(), "found": m.group(0),
                             "correct": correct, "note": t.get("note", "")})
    hits.sort(key=lambda h: (h["start"], -(h["end"] - h["start"])))
    chosen, last_end = [], -1
    for h in hits:
        if h["start"] >= last_end:
            chosen.append(h)
            last_end = h["end"]
    return chosen


def glossary_candidates(cues, glossary: List[Dict]) -> List[Dict]:
    """辞書から機械的に作る候補。AIはこの候補も確認してから適用する。"""
    out = []
    for cue in cues:
        flat = flatten(cue.original_lines).text
        hits = glossary_hits(flat, glossary)
        if not hits:
            continue
        unique = all(flat.count(h["found"]) == 1 for h in hits)
        if unique:
            for h in hits:
                out.append(_gcand(len(out) + 1, cue, h["found"], h["correct"], "span",
                                  f"辞書: {h['correct']} ← {h['found']}" + (f"（{h['note']}）" if h["note"] else "")))
        else:
            new = flat
            for h in sorted(hits, key=lambda h: -h["start"]):
                new = new[:h["start"]] + h["correct"] + new[h["end"]:]
            ev = "、".join(f"{h['correct']} ← {h['found']}" for h in hits)
            out.append(_gcand(len(out) + 1, cue, flat, new, "cue", f"辞書: {ev}"))
    return out


def _gcand(n: int, cue, original: str, proposed: str, scope: str, evidence: str) -> Dict:
    c = {"id": f"G{n:03d}", "index": cue.index, "start": cue.start, "type": "固有名詞",
         "original": original, "proposed": proposed, "reason": "指定辞書の正式表記にそろえる",
         "evidence": evidence, "status": "apply", "source": "glossary"}
    if scope == "cue":
        c["scope"] = "cue"
    return c


# ── 出力 ───────────────────────────────────────────────────────────

def atomic_write(path: Path, data: bytes) -> None:
    """一時ファイルに書いてから置き換える（途中で止まっても壊れたファイルを残さない）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", "<br>") if text else "（空）"


def settings_dict(st: Settings) -> Dict:
    return asdict(st)
