#!/usr/bin/env python3
"""ローカルAI（Ollama）に修正候補を作らせ、候補ファイルへ追記する。

字幕をいくつかずつ区切って渡し、決まった形（JSON）で候補を返させる。
返ってきた候補のうち、原文がその字幕に1回だけ現れるものだけを候補ファイルへ入れる。
実際に適用するか・安全かの判断は apply_candidates.py が行う（このスクリプトは字幕を書き換えない）。

字幕の文字はこのパソコンの Ollama（既定 http://127.0.0.1:11434）にだけ送られる。

使い方:
  python3 scripts/local_ai.py 元.srt 元.candidates.json --model qwen3.6:latest [--glossary 辞書.tsv]
      [--fillers] [--punct telop] [--batch 20]
終了コード: 0=完了 / 2=入力の問題 / 4=Ollamaに接続できない・応答が不正
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_glossary, setup_console  # noqa: E402
from jptext import flatten  # noqa: E402
from srt_io import SrtError, read_srt  # noqa: E402

DEFAULT_HOST = "http://127.0.0.1:11434"
TYPES_APPLY = ["誤字", "誤変換", "表記ゆれ", "固有名詞", "フィラー削除"]

RECOMMENDED = "qwen3.5:27b"  # 2026-10-08 の比較でいちばん正確だったモデル（約17GB。メモリ32GB以上）

RULES = """あなたは日本語字幕の校正者です。音声認識（Whisper等）で作った字幕の、文字の誤りだけを見つけます。
字幕の本文を書き直してはいけません。見つけた箇所を「修正候補」として返します。

直してよいもの（status: "apply"）:
- 誤字：存在しない語・打ち間違い（例：読み混む→読み込む、結講→結構）
- 誤変換：同音異義語のうち、文脈で一方しか成り立たないもの（例：以外と大事→意外と大事）
- 固有名詞：与えられた辞書にある語の誤表記
- 表記ゆれ：同じ字幕群の中で多い方の表記にそろえる場合だけ

要確認にするもの（status: "review"。迷ったら必ずこちら）:
- 文脈でどちらも成り立つ同音異義語（機能／昨日、制作／製作）
- 辞書にない人名・社名・商品名の表記
- 数字の聞き間違いの疑い
- 意味が通らないが、正解が推測になるもの

絶対に変えないもの:
- 数字・単位・否定（ない・ません・へん）・推量や断定（たぶん・かも・くらい・だけ）
- 話し言葉・方言・語尾（です／だ、ね、よ、やん、ねん）・言い回し
- より自然・短い表現への言い換え、句読点の流儀、改行
- 問題のない字幕（候補を作らない。候補が0件でもよい）

original は、その字幕の本文から一字一句そのまま写した短い文字列にする（前後の数文字を含めて一意にする）。
proposed は original を置き換える文字列。reason は直す理由、evidence は根拠（文脈のどこから分かるか）を日本語で短く書く。"""

SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "type": {"type": "string", "enum": TYPES_APPLY + ["その他"]},
                    "original": {"type": "string"},
                    "proposed": {"type": "string"},
                    "reason": {"type": "string"},
                    "evidence": {"type": "string"},
                    "status": {"type": "string", "enum": ["apply", "review"]},
                },
                "required": ["index", "type", "original", "proposed", "reason", "evidence", "status"],
            },
        }
    },
    "required": ["candidates"],
}


class OllamaError(Exception):
    pass


def _adds_text(orig: str, prop: str) -> bool:
    """共通の前後を除いた差分で、修正案のほうが長い（原文にない文字が加わる）か。"""
    a, b = orig, prop
    while a and b and a[0] == b[0]:
        a, b = a[1:], b[1:]
    while a and b and a[-1] == b[-1]:
        a, b = a[:-1], b[:-1]
    return len(b) > len(a)


def ollama_models(host: str = DEFAULT_HOST, timeout: float = 2.0) -> Optional[List[Dict]]:
    """インストール済みのモデル一覧。Ollamaが動いていなければ None。"""
    try:
        with urllib.request.urlopen(host + "/api/tags", timeout=timeout) as r:
            data = json.loads(r.read())
        return [{"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 1),
                 "params": (m.get("details") or {}).get("parameter_size", "")} for m in data.get("models", [])]
    except (OSError, ValueError, urllib.error.URLError):
        return None


FIRST_TOKEN_TIMEOUT = 600  # モデルの読み込みを含め、最初の応答まで待つ秒数
IDLE_TIMEOUT = 120         # 応答が始まったあと、無応答で打ち切る秒数
NUM_CTX = 8192             # 既定の4096では指示と出力が窓を取り合い、尻切れになる（Cutline Studioの実測）
NUM_PREDICT = 3000         # 同じ内容を繰り返し続ける暴走を止める上限
REPEAT_PENALTY = 1.1


def _repair_json(text: str) -> Optional[Dict]:
    """途中で切れたJSONから、閉じている候補だけを取り出す。"""
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("[")
    if start < 0:
        return None
    end = text.rfind("}")
    while end > start:
        try:
            items = json.loads(text[start:end + 1] + "]")
            return {"candidates": items}
        except ValueError:
            end = text.rfind("}", start, end)
    return None


def _chat_once(host: str, model: str, prompt: str, think: bool, temperature: float,
               cancelled: Optional[Callable[[], bool]]) -> str:
    body = {"model": model, "stream": True, "format": SCHEMA, "think": think,
            "options": {"temperature": temperature, "num_ctx": NUM_CTX, "num_predict": NUM_PREDICT,
                        "repeat_penalty": REPEAT_PENALTY},
            "messages": [{"role": "system", "content": RULES}, {"role": "user", "content": prompt}]}
    req = urllib.request.Request(host + "/api/chat", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    parts: List[str] = []
    try:
        with urllib.request.urlopen(req, timeout=FIRST_TOKEN_TIMEOUT) as r:
            first = True
            for line in r:
                if cancelled and cancelled():
                    raise OllamaError("中止しました。")
                if first:
                    first = False
                    try:  # 応答が始まったら、無応答の打ち切りを短くする
                        r.fp.raw._sock.settimeout(IDLE_TIMEOUT)  # type: ignore[attr-defined]
                    except AttributeError:
                        pass
                if not line.strip():
                    continue
                ev = json.loads(line)
                if ev.get("error"):
                    raise OllamaError(f"ローカルAIがエラーを返しました：{ev['error']}")
                parts.append((ev.get("message") or {}).get("content", ""))
                if ev.get("done"):
                    break
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if e.code == 404 or "not found" in detail:
            raise OllamaError(f"モデル {model} が入っていません。")
        raise OllamaError(f"ローカルAIがエラーを返しました（{e.code}）：{detail}")
    except (TimeoutError, OSError, urllib.error.URLError) as e:
        if isinstance(e, OllamaError):
            raise
        raise OllamaError(f"ローカルAIの応答が止まりました（{e}）。AIエンジンが動いているか確認してください。")
    return "".join(parts)


def _chat(host: str, model: str, prompt: str, timeout: int = 0, think: bool = False,
          cancelled: Optional[Callable[[], bool]] = None) -> Dict:
    """候補をJSONで受け取る。読めなければ修復し、それでも駄目なら温度を変えて1回だけやり直す。"""
    for temperature in (0.0, 0.3):
        content = _chat_once(host, model, prompt, think, temperature, cancelled)
        data = _repair_json(content)
        if data is not None:
            return data
    raise OllamaError("ローカルAIの返答を読めませんでした（JSONになっていません）。別のモデルを選んでください。")


def suggest(srt_path: str, cand_path: str, model: str, glossary: Optional[List[Dict]] = None,
            fillers: bool = False, telop: bool = False, encoding: str = "utf-8", batch: int = 20,
            host: str = DEFAULT_HOST, timeout: int = 600, think: bool = False,
            progress: Optional[Callable[[int, int], None]] = None,
            cancelled: Optional[Callable[[], bool]] = None) -> Dict:
    src = read_srt(srt_path, encoding)
    cpath = Path(cand_path)
    doc = json.loads(cpath.read_text(encoding="utf-8"))
    cues = [(c.index, c.start, flatten(c.original_lines).text) for c in src.cues]
    texts = {i: t for i, _, t in cues}
    starts = {i: s for i, s, _ in cues}
    counts: Dict[int, int] = {}
    for c in src.cues:
        counts[c.index] = counts.get(c.index, 0) + 1
    gl = "\n".join(f"- {t['correct']}（誤表記: {'、'.join(t['variants']) or 'なし'}）" for t in (glossary or [])) or "なし"
    known = {t["correct"] for t in (glossary or [])}  # 固有名詞として直してよいのは、辞書にある正しい表記だけ
    notes = []
    if fillers:
        notes.append("利用者はフィラー（えー・えっと・あのー・前置きの「まあ」）の削除を選んでいます。"
                     "type「フィラー削除」で、消したあとに残る読点も含めて候補にしてください。感情を表す「いやー」や指示語の「あの人」は残します。")
    else:
        notes.append("フィラー（えー・あの等）は消しません。")
    if telop:
        notes.append("句読点はあとで機械的に外すので、句読点の候補は作りません。")
    used = {c.get("id") for c in doc.get("candidates", [])}
    spans: Dict[int, List] = {}
    for c in doc.get("candidates", []):
        t = texts.get(c.get("index"), "")
        o = c.get("original", "")
        if c.get("scope") == "cue":
            spans.setdefault(c["index"], []).append((0, len(t)))
        elif o and o in t:
            k0 = t.index(o)
            spans.setdefault(c["index"], []).append((k0, k0 + len(o)))
    n_new, n_drop = 0, 0
    batches = [cues[k:k + batch] for k in range(0, len(cues), batch)]
    for b_i, part in enumerate(batches):
        if cancelled and cancelled():
            raise OllamaError("中止しました。")
        if progress:
            progress(b_i, len(batches))
        before = cues[max(0, cues.index(part[0]) - 2):cues.index(part[0])]
        after = cues[cues.index(part[-1]) + 1:cues.index(part[-1]) + 3]
        prompt = ("辞書:\n" + gl + "\n\n" + "\n".join(notes) + "\n\n"
                  + ("前後の字幕（文脈の参考。候補は作らない）:\n"
                     + "\n".join(f"[{i}] {t}" for i, _, t in before + after) + "\n\n" if before or after else "")
                  + "校正する字幕（[番号] 本文）:\n" + "\n".join(f"[{i}] {t}" for i, _, t in part)
                  + "\n\n修正候補を JSON で返してください。")
        res = _chat(host, model, prompt, timeout, think, cancelled)
        allowed = {i for i, _, _ in part}
        for c in res.get("candidates", []):
            idx = c.get("index")
            orig, prop = str(c.get("original", "")), str(c.get("proposed", ""))
            # 原文がその字幕に1回だけ現れる候補だけを残す（照合できない提案は捨てる）
            if idx not in allowed or counts.get(idx) != 1 or not orig or orig == prop \
                    or texts[idx].count(orig) != 1 or "\n" in orig + prop:
                n_drop += 1
                continue
            # 辞書やほかの候補と同じ箇所に重なる提案は入れない（重なると両方とも適用されなくなる）
            s0 = texts[idx].index(orig)
            if any(o[0] < s0 + len(orig) and s0 < o[1] for o in spans.get(idx, [])):
                n_drop += 1
                continue
            status = c.get("status") if c.get("status") in ("apply", "review") else "review"
            ctype = c.get("type") if c.get("type") in TYPES_APPLY else "その他"
            note = ""
            if ctype == "その他" or (ctype == "フィラー削除" and not fillers):
                status = "review"
            elif ctype == "固有名詞" and prop not in known:
                status, note = "review", "（辞書にない固有名詞のため要確認。人名などは音声で確かめる）"
            elif ctype != "フィラー削除" and _adds_text(orig, prop):
                status, note = "review", "（原文にない文字が加わるため要確認）"
            elif ctype != "フィラー削除" and len(orig) - len(prop) >= 3 and len(prop) < len(orig) * 0.7:
                status, note = "review", "（本文が大きく減るため要確認）"
            k = 1
            while f"A{k:03d}" in used:
                k += 1
            cid = f"A{k:03d}"
            used.add(cid)
            doc.setdefault("candidates", []).append({
                "id": cid, "index": idx, "start": starts[idx], "type": ctype, "original": orig, "proposed": prop,
                "reason": str(c.get("reason") or "ローカルAIの指摘") + note,
                "evidence": str(c.get("evidence") or "文脈（ローカルAI）") + f"（{model}）",
                "status": status, "source": "local-ai"})
            spans.setdefault(idx, []).append((s0, s0 + len(orig)))
            n_new += 1
    if progress:
        progress(len(batches), len(batches))
    tmp = cpath.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(cpath)
    return {"added": n_new, "dropped": n_drop, "batches": len(batches)}


def main(argv=None) -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="ローカルAI（Ollama）で修正候補を作り、候補ファイルへ追記します。")
    ap.add_argument("srt")
    ap.add_argument("candidates")
    ap.add_argument("--model", required=True)
    ap.add_argument("--glossary")
    ap.add_argument("--fillers", action="store_true")
    ap.add_argument("--punct", choices=["keep", "telop"], default="keep")
    ap.add_argument("--encoding", default="utf-8")
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--think", action="store_true", help="考えるモード（対応モデルのみ。遅くなる）")
    args = ap.parse_args(argv)
    try:
        r = suggest(args.srt, args.candidates, args.model, load_glossary(args.glossary), args.fillers,
                    args.punct == "telop", args.encoding, args.batch, args.host, think=args.think,
                    progress=lambda i, n: print(f"  {i}/{n}", file=sys.stderr, flush=True))
    except (SrtError, ValueError, OSError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2
    except OllamaError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 4
    print(f"候補を {r['added']}件 追加しました（照合できずに捨てた提案 {r['dropped']}件）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
