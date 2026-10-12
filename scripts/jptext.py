"""日本語字幕の文字数計算・改行位置の評価・修正候補の安全チェック。

形態素解析器を使わず、文字の種類・禁則・数字と単位・辞書語の保護から改行位置を選ぶ。
外部ライブラリを使わないため、どのOSでもPython標準ライブラリだけで動く。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

INF = float("inf")

# ── 文字の種類 ─────────────────────────────────────────────────────

SMALL_KANA = set("ぁぃぅぇぉっゃゅょゎゕゖァィゥェォッャュョヮヵヶㇰㇱㇲㇳㇴㇵㇶㇷㇸㇹㇺㇻㇼㇽㇾㇿ")
# 行頭に置かない文字（行頭禁則）。W3C「日本語組版処理の要件」（JLREQ）3.1.7 の厳しい方の規則に合わせ、
# 終わり括弧類・ハイフン類・区切り約物・中点類・句点類・読点類・繰返し記号・長音記号・小書きの仮名を含める。
# 「％」「℃」などの後置の単位記号も、数字から離さないために行頭に置かない（JLREQ 3.1.10）
NO_LINE_START = set("、。，．,.!！?？‼⁇⁈⁉…‥:：;；)）]］}｝」』】〉》〕〙〗’”〟»⦆・ー〜～‐゠–々〻ゝゞヽヾ%％‰℃°′″¢") | SMALL_KANA
# 行末に置かない文字（行末禁則）。始め括弧類（JLREQ 3.1.8）と、数字の前に付く記号（JLREQ 3.1.10）
NO_LINE_END = set("(（[［{｛「『【〈《〔〘〖‘“〝«⦅¥￥$＄#＃£€№")
# 2つ続けて使い、間で分けない記号（JLREQ の分離禁止文字。「……」「――」など）
INSEPARABLE = set("—―…‥")
SENTENCE_PUNCT = set("、。，．！？!?…‥")
CLOSE_BRACKETS = set(")）]］}｝」』】〉》〕〙〗’”")
# 数字の直後で切らない単位・助数詞（数字と単位を分けない）
UNIT_CHARS = set("円%％個人件回倍本枚台度歳才年月日時分秒週冊杯匹頭羽着足点位番階号kKmMgGTBbpxs℃°万億兆千百")
KANJI_NUMERALS = set("〇一二三四五六七八九十百千万億兆零")
TAG_RE = re.compile(r"<[^>]*>|\{\\[^}]*\}")


def char_class(ch: str) -> str:
    if ch.isspace():
        return "S"
    o = ord(ch)
    if 0x3041 <= o <= 0x309F:
        return "H"
    if 0x30A0 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF or 0xFF66 <= o <= 0xFF9F:
        return "T"
    if ch in "々〆ヶ" or 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF:
        return "K"
    n = unicodedata.normalize("NFKC", ch)
    if n.isdigit():
        return "D"
    if n.isascii() and n.isalpha():
        return "L"
    return "O"


def is_alnum_class(ch: str) -> bool:
    return char_class(ch) in ("L", "D")


# ── 文字数 ─────────────────────────────────────────────────────────

def display_width(text: str, halfwidth_weight: float = 0.5) -> float:
    """表示上の文字数の目安。全角=1、半角=halfwidth_weight。タグは数えない。

    フォントの実際の幅（ピクセル）を測るものではない。
    """
    total = 0.0
    for ch in TAG_RE.sub("", text):
        if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            continue  # 結合文字・ゼロ幅文字
        eaw = unicodedata.east_asian_width(ch)
        total += 1.0 if eaw in ("F", "W", "A") else halfwidth_weight
    return total


def reading_chars(text: str, halfwidth_weight: float = 0.5) -> float:
    """表示時間の判定に使う文字数（空白・改行・タグを除く）。"""
    return display_width(re.sub(r"\s+", "", TAG_RE.sub("", text)), halfwidth_weight)


# ── 本文の平坦化（改行を外した本文）と復元 ─────────────────────────

@dataclass
class Flat:
    text: str
    breaks: List[Tuple[int, bool]]  # (位置, その位置の空白1文字を改行で消費したか)


def flatten(lines: Sequence[str]) -> Flat:
    """改行を外した本文を作る。英数字どうしの改行は空白1文字として扱う。"""
    parts = [ln.strip() for ln in lines]
    parts = [p for p in parts if p != ""]
    text, breaks = "", []
    for k, p in enumerate(parts):
        if k > 0:
            if text and p and is_alnum_class(text[-1]) and is_alnum_class(p[0]):
                breaks.append((len(text), True))
                text += " "
            else:
                breaks.append((len(text), False))
        text += p
    return Flat(text, breaks)


def split_at(text: str, breaks: Iterable[Tuple[int, bool]]) -> List[str]:
    lines, prev = [], 0
    for pos, consumed in sorted(breaks):
        lines.append(text[prev:pos])
        prev = pos + 1 if consumed else pos
    lines.append(text[prev:])
    return [ln.strip() for ln in lines]


def map_breaks(breaks: List[Tuple[int, bool]], edits: List[Tuple[int, int, int]]):
    """編集（開始, 終了, 新しい長さ）のあとに、元の改行位置を移す。

    改行が編集範囲の内側にある場合は None（元の改行を保てない）を返す。
    """
    out = []
    for pos, consumed in breaks:
        shift = 0
        for s, e, newlen in edits:
            delta = newlen - (e - s)
            if consumed:
                if s <= pos < e:
                    return None
                if pos >= e:
                    shift += delta
            else:
                if s < pos < e:
                    return None
                if pos >= e and pos != s:
                    shift += delta
        out.append((pos + shift, consumed))
    return out


# ── 改行位置の評価 ─────────────────────────────────────────────────

# この語尾で終わる位置は、ひらがな同士でも文節の切れ目になりやすい
PHRASE_END = ("ます", "です", "ました", "でした", "ません", "けど", "けれど", "から", "ので", "のに",
              "たら", "なら", "ても", "でも", "ながら", "って", "ては", "には", "とは", "では",
              "まで", "より", "など", "ので")
SINGLE_PARTICLES = set("はがをにへもとや")
# 漢字の直後の1字で、助詞・接続として切れ目になりやすいもの
PARTICLE_LIKE = set("はがをにへもとやでのかてば")
# 直後の名詞にかかるため、行末に置くと読みにくい語
PRENOMINAL = ("この", "その", "あの", "どの", "こんな", "そんな", "あんな", "どんな", "こういう", "そういう",
              "ああいう", "どういう", "いわゆる", "ある")
# 前の語に付くため、行頭に来ると読みにくい語
HEAD_ATTACH = ("けど", "けれど", "ので", "のに", "って", "ね", "よ", "か")
# 前の語と一体の助動詞。行頭に置かない
HEAD_AUX = ("ない", "なく", "なかっ", "ません", "ます", "まし", "ませ", "です", "でし", "たい", "たく",
            "られ", "れる", "せる", "させ", "ている", "てる", "ず")
# て形に続く補助動詞（〜てもらう・〜てしまう等）
TE_AUX = ("もら", "らっ", "らう", "らえ", "くれ", "くだ", "あげ", "おく", "おい", "おき", "しま", "しまっ",
          "ちゃ", "いる", "いた", "いく", "いっ", "いき", "くる", "きた", "きま", "みる", "みた", "みま", "ほし", "る", "た")
# 切り離すと読みにくい、ひらがな2文字の組（です・ます・ない等）
TIGHT_BIGRAMS = {"もら", "です", "ます", "ませ", "まし", "でし", "った", "って", "てる", "てい", "ない", "なく",
                 "たい", "れる", "られ", "せる", "させ", "んで", "んだ", "んじ", "ちゃ", "じゃ", "よう"}


def protected_ranges(text: str, terms: Iterable[str]) -> List[Tuple[int, int]]:
    ranges = []
    for t in terms:
        if not t or len(t) < 2:
            continue
        start = 0
        while True:
            k = text.find(t, start)
            if k < 0:
                break
            ranges.append((k, k + len(t)))
            start = k + 1
    return ranges


def break_cost(text: str, i: int, protected: Sequence[Tuple[int, int]] = ()) -> float:
    """text[:i] と text[i:] の間で改行するときの不自然さ（小さいほど自然）。"""
    if i <= 0 or i >= len(text):
        return INF
    a, b = text[i - 1], text[i]
    for s, e in protected:
        if s < i < e:
            return INF
    # 空白の位置で切る（空白は改行で消える）
    if b.isspace():
        left = text[:i].rstrip()
        right = text[i:].lstrip()
        if not left or not right:
            return INF
        if is_alnum_class(left[-1]) and is_alnum_class(right[0]):
            return 3.0  # 英単語どうしの間
        return 0.5
    if a.isspace():
        return INF  # 空白の直後ではなく空白の位置で切る
    if b in NO_LINE_START or a in NO_LINE_END:
        return INF
    if a == b and a in INSEPARABLE:
        return INF  # 「――」「……」の間
    ca, cb = char_class(a), char_class(b)
    if ca in ("L", "D") and cb in ("L", "D"):
        return INF  # 英単語・数字の途中
    if ca == "D" and (b in UNIT_CHARS or cb == "L"):
        return INF  # 数字と単位
    if a in "第約¥￥$＄" and cb == "D":
        return INF
    if a in ".,:：．，" and i >= 2 and char_class(text[i - 2]) == "D" and cb == "D":
        return INF  # 3.5 / 1,000 / 10:30
    if a in KANJI_NUMERALS and b in UNIT_CHARS:
        return INF
    if a in SENTENCE_PUNCT or a in CLOSE_BRACKETS:
        return 0.0
    if ca == "H" and cb in ("K", "T", "L", "D", "O"):
        tail = text[:i]
        if tail.endswith(PRENOMINAL):
            return 7.0  # 「この／設定」のように、次の名詞にかかる語を切り離す
        if i >= 2 and char_class(text[i - 2]) == "K" and a not in PARTICLE_LIKE:
            return 7.0  # 「書き／出し」「取り／扱い」：漢字＋送り仮名1字＋漢字の複合語
        if a in "のな":
            return 3.0  # 「素材の／場合」：名詞どうしをつなぐ「の」「な」のあと
        return 1.0  # 助詞・語尾のあと、次の語の前
    if ca in ("K", "T", "L", "D") and cb == "H":
        return 8.0  # 送り仮名・助詞を切り離す
    if ca == "H" and cb == "H":
        if a + b in TIGHT_BIGRAMS:
            return 9.0
        tail = text[:i]
        if text.startswith(HEAD_AUX, i):
            return 9.0  # 「なら／ない」：助動詞を行頭に置かない
        if tail.endswith(("て", "で", "ても", "でも")) and text.startswith(TE_AUX, i):
            return 9.0  # 「教えても／らった」「して／しまう」：て形と補助動詞
        if text.startswith(HEAD_ATTACH, i):
            return 6.0
        if tail.endswith(PHRASE_END):
            return 1.0  # 「〜なら／」「〜ので／」：節の切れ目
        if a in SINGLE_PARTICLES:
            return 4.5
        return 6.0
    if ca == cb and ca in ("K", "T"):
        return 9.0  # 漢字・カタカナの連続（複合語）の途中
    if ca in ("K", "T", "L", "D", "O") and cb in ("K", "T", "L", "D", "O"):
        return 2.5  # 文字種が変わる位置
    return 5.0


BAD_BREAK_COST = 8.0

DIALOGUE_RE = re.compile(r"^\s*([-－‐—–]|[（(][^）)]{1,12}[）)]|[^\s:：0-9０-９]{1,10}[:：])")


def is_dialogue(lines: Sequence[str]) -> bool:
    """「－」や「（話者）」で始まる行が2行以上ある字幕。行の区切りが話者の区切りなので改行を変えない。"""
    return sum(1 for ln in lines if DIALOGUE_RE.match(ln)) >= 2  # これ以上の位置にある既存の改行は「語の途中」とみなす


@dataclass
class Settings:
    max_lines: int = 2
    max_chars_per_line: float = 16
    halfwidth_weight: float = 0.5
    max_cps: float = 6.0
    min_line_chars: float = 2
    linebreak_mode: str = "auto"  # auto / keep / all


@dataclass
class BreakResult:
    lines: List[str]
    fits: bool          # すべての行が上限内か
    cost: float


def _line(text: str, s: int, e: int) -> str:
    return text[s:e].strip()


def best_breaks(text: str, st: Settings, protect: Iterable[str] = ()) -> BreakResult:
    """設定の範囲で最も自然な改行を選ぶ。

    探す順番:
      1. 上限内に収まり、語の途中で切らない案
      2. 語の途中で切らず、上限を超える案（fits=False。要確認に回す）
      3. それでも無ければ、語の途中を含む案（fits=False）
    """
    text = text.strip()
    w = lambda s: display_width(s, st.halfwidth_weight)
    if w(text) <= st.max_chars_per_line or st.max_lines <= 1:
        return BreakResult([text], w(text) <= st.max_chars_per_line, 0.0)
    prot = protected_ranges(text, protect)
    n = len(text)
    costs = {i: break_cost(text, i, prot) for i in range(1, n)}

    def next_start(i: int) -> int:
        return i + 1 if text[i].isspace() else i

    for allow_over, allow_bad in ((False, False), (True, False), (True, True)):
        cand = [i for i, c in costs.items() if c < INF and (allow_bad or c < BAD_BREAK_COST)]
        frontier = {0: (0.0, [])}  # 行の開始位置 → (コスト, 改行位置の列)
        results = []
        for k in range(1, st.max_lines + 1):
            new_frontier = {}
            for s, (c, path) in frontier.items():
                lw = w(_line(text, s, n))
                if lw <= st.max_chars_per_line or allow_over:
                    results.append((c + max(0.0, lw - st.max_chars_per_line) * 20, path))
                if k == st.max_lines:
                    continue
                for i in cand:
                    if i <= s:
                        continue
                    sw = w(_line(text, s, i))
                    if sw > st.max_chars_per_line and not allow_over:
                        break  # これより後ろはさらに長くなる
                    nc = c + costs[i] + max(0.0, sw - st.max_chars_per_line) * 20 + 0.8
                    key = next_start(i)
                    if key not in new_frontier or new_frontier[key][0] > nc:
                        new_frontier[key] = (nc, path + [i])
            frontier = new_frontier
        scored = []
        for c, path in results:
            lines = split_at(text, [(i, text[i].isspace()) for i in path])
            if any(x == "" for x in lines):
                continue
            widths = [w(x) for x in lines]
            balance = (max(widths) - min(widths)) * 0.25 if len(widths) > 1 else 0.0
            short = sum(max(0.0, st.min_line_chars - x) * 4 for x in widths)
            scored.append((c + balance + short, lines))
        if scored:
            c, lines = min(scored, key=lambda x: x[0])
            fits = all(w(x) <= st.max_chars_per_line for x in lines)
            return BreakResult(lines, fits, c)
    return BreakResult([text], False, INF)


def existing_break_problems(lines: Sequence[str], st: Settings, protect: Iterable[str] = ()) -> List[str]:
    """今の改行の問題点（語の途中、行数・文字数の超過）を返す。"""
    probs = []
    flat = flatten(lines)
    if len(lines) > st.max_lines:
        probs.append(f"{len(lines)}行（上限{st.max_lines}行）")
    for ln in lines:
        lw = display_width(ln.strip(), st.halfwidth_weight)
        if lw > st.max_chars_per_line:
            probs.append(f"1行{lw:g}字（上限{st.max_chars_per_line:g}字）")
            break
    prot = protected_ranges(flat.text, protect)
    for pos, _consumed in flat.breaks:
        c = break_cost(flat.text, pos, prot)
        if c >= BAD_BREAK_COST:
            left = flat.text[max(0, pos - 6):pos].strip()
            right = flat.text[pos:pos + 6].strip()
            probs.append(f"語の途中で改行（{left}／{right}）")
    return probs


# ── 修正候補の安全チェック ─────────────────────────────────────────

FILLERS = ("えーっと", "えーと", "ええと", "えっと", "えー", "あのー", "あの", "そのー", "まあ", "まぁ",
           "うーん", "うん", "あー", "んー", "なんか", "ほら")
NEGATION = ("ない", "なく", "なかっ", "ません", "ずに", "ず、", "ず。", "じゃな", "ではな", "ないで",
            "不", "非", "未", "無", "否")
HEDGE_ASSERT = ("かもしれ", "かも", "でしょう", "だろう", "と思", "はず", "らしい", "みたい", "ようだ",
                "ようです", "っぽい", "絶対", "必ず", "きっと", "たぶん", "多分", "おそらく", "恐らく",
                "確実", "間違いなく", "べき", "しか", "だけ", "ほぼ", "約", "くらい", "ぐらい", "程度", "以上",
                "以下", "未満", "前後")
TONE_RE = re.compile(
    r"(です|ます|でした|ました|でしょ|じゃん|っす|やん|やで|ねん|だよ|だね|よね|かな|わ|ぞ|ぜ|[ねよなさ])"
    r"(?=[。！？!?、…\s」』]|$)"
)
NUM_UNIT_RE = re.compile(
    r"([0-9]+(?:[.,][0-9]+)*|[〇一二三四五六七八九十百千万億兆零]+)\s*"
    r"(円|ドル|%|パーセント|個|件|人|回|倍|本|枚|台|度|歳|年|月|日|時間|時|分|秒|週|割|[A-Za-z]+)?"
)


# 方言の目印。数が変わる修正は、話し方を標準語に寄せている可能性が高い（「ええねん」→「いいねん」など）。
# 標準語にも出る語（「たい」「とる」「けん」など）は、誤って止めすぎるので入れない
DIALECT = ("へん", "ねん", "やん", "やで", "やろ", "やけど", "ええ", "あかん", "ほんま", "めっちゃ", "はる", "ちゃう",
           "せや", "おおきに", "さかい", "しとる", "じゃけ", "だべ", "っぺ", "ずら", "ばってん")


def _count(text: str, words: Iterable[str]) -> dict:
    return {wd: text.count(wd) for wd in words if text.count(wd)}


def _numbers_units(text: str) -> List[Tuple[str, str]]:
    """数字と、その直後の単位の組。全角・半角と桁区切りの違いは同じとみなす。"""
    n = unicodedata.normalize("NFKC", text)
    return [(m.group(1).replace(",", ""), (m.group(2) or "").lower()) for m in NUM_UNIT_RE.finditer(n)]


def guard_problems(original: str, proposed: str, allowed_deletion: bool = False) -> List[str]:
    """意味を変えやすい変更を検出する。検出されなければ安全という意味ではない。"""
    probs = []
    no, np_ = _numbers_units(original), _numbers_units(proposed)
    if [x[0] for x in no] != [x[0] for x in np_]:
        fmt = lambda xs: "・".join(x[0] for x in xs) or "なし"
        probs.append(f"数字が変わる（{fmt(no)} → {fmt(np_)}）")
    elif no != np_:
        probs.append("数字の単位が変わる")
    if _count(original, NEGATION) != _count(proposed, NEGATION):
        probs.append("否定の表現が変わる")
    if _count(original, HEDGE_ASSERT) != _count(proposed, HEDGE_ASSERT):
        probs.append("断定・推量・限定の強さが変わる")
    if TONE_RE.findall(original) != TONE_RE.findall(proposed):
        probs.append("語尾・口調が変わる")
    if _count(original, DIALECT) != _count(proposed, DIALECT):
        probs.append("方言・話し方が変わる")
    if not allowed_deletion:
        o_f = _count(original, FILLERS)
        p_f = _count(proposed, FILLERS)
        if any(p_f.get(k, 0) < v for k, v in o_f.items()):
            probs.append("フィラー（えー・あの等）が消える")
        ow = reading_chars(original, 1.0)
        pw = reading_chars(proposed, 1.0)
        if ow - pw >= 3 and pw < ow * 0.7:
            probs.append("本文が大きく減る（削除・要約に当たる）")
    return probs


# ── 表記ゆれの手がかり ─────────────────────────────────────────────

VARIANT_PAIRS = [
    ("子供", "子ども"), ("一つ", "1つ"), ("二つ", "2つ"), ("一人", "1人"), ("出来る", "できる"),
    ("出来な", "できな"), ("行なう", "行う"), ("下さい", "ください"), ("全て", "すべて"),
    ("様々", "さまざま"), ("色々", "いろいろ"), ("沢山", "たくさん"), ("是非", "ぜひ"),
    ("頂く", "いただく"), ("頂き", "いただき"), ("致し", "いたし"), ("事が", "ことが"),
    ("時に", "ときに"), ("分かる", "わかる"), ("分から", "わから"), ("良い", "いい"),
    ("お勧め", "おすすめ"), ("お薦め", "おすすめ"),
]


def variant_hints(texts: Sequence[Tuple[int, str]]) -> List[str]:
    """同じファイルに混在している表記の組を挙げる（どちらに統一するかは人が決める）。"""
    hints = []
    for a, b in VARIANT_PAIRS:
        ia = [i for i, t in texts if a in t]
        ib = [i for i, t in texts if b in t]
        if ia and ib:
            hints.append(f"「{a}」（{_fmt_idx(ia)}）と「{b}」（{_fmt_idx(ib)}）が混在")
    # 全角・半角の混在（ＡＩ と AI、１０ と 10 など）
    tokens = {}
    for i, t in texts:
        for m in re.finditer(r"[0-9A-Za-z０-９Ａ-Ｚａ-ｚ]+", t):
            tokens.setdefault(unicodedata.normalize("NFKC", m.group(0)), {}).setdefault(m.group(0), []).append(i)
    for norm, raws in tokens.items():
        if len(raws) > 1:
            parts = [f"「{r}」（{_fmt_idx(ix)}）" for r, ix in raws.items()]
            hints.append("全角・半角の混在: " + "と".join(parts))
    # 英字の大文字・小文字の混在（iPhone と iphone など）
    latin = {}
    for i, t in texts:
        for m in re.finditer(r"[A-Za-z][A-Za-z0-9]+", t):
            latin.setdefault(m.group(0).lower(), {}).setdefault(m.group(0), []).append(i)
    for low, raws in latin.items():
        if len(raws) > 1:
            parts = [f"「{r}」（{_fmt_idx(ix)}）" for r, ix in raws.items()]
            hints.append("大文字・小文字の混在: " + "と".join(parts))
    return hints


def _fmt_idx(ix: List[int]) -> str:
    uniq = sorted(set(ix))
    s = ", ".join(f"#{i}" for i in uniq[:5])
    return s + (f" ほか{len(uniq) - 5}件" if len(uniq) > 5 else "")


# ── テロップ表記：句読点を外す ───────────────────────────────────

# 初期設定で残す記号（ダッシュ・長音・三点リーダー・感嘆符・疑問符・波ダッシュ）
TELOP_KEEP_DEFAULT = "ー－−—―‐-…‥！!？?〜～"
# 外す候補になる記号。文の途中の句読点・区切りは全角スペースにして間を残す
PAUSE_PUNCT = set("、。，．,.：:；;！!？?")
DROP_PUNCT = set("・「」『』（）()［］[]【】〈〉《》〔〕“”‘’\"'…‥〜～")
# ！？…〜 は初期設定では残す（keep に含まれる）。keep から外したときだけ外す


def strip_telop_punct(text: str, keep: str = TELOP_KEEP_DEFAULT):
    """句読点などを外した本文と、編集（開始, 終了, 新しい長さ）の一覧を返す。

    数字・英字の中の記号（29.97、1,000、10:30、H.264、Don't）と、
    カタカナ・英字にはさまれた「・」（ジョン・スミス）は残す。
    """
    keep_set = set(keep)
    out, edits = [], []
    n = len(text)
    for i, ch in enumerate(text):
        if ch in keep_set or (ch not in PAUSE_PUNCT and ch not in DROP_PUNCT):
            out.append(ch)
            continue
        prev = text[i - 1] if i > 0 else ""
        nxt = text[i + 1] if i + 1 < n else ""
        if ch in ".,:'’" and prev and nxt and is_alnum_class(prev) and is_alnum_class(nxt):
            out.append(ch)
            continue
        if ch == "・" and prev and nxt and char_class(prev) in ("T", "L") and char_class(nxt) in ("T", "L"):
            out.append(ch)
            continue
        rest = text[i + 1:].strip()
        if ch in PAUSE_PUNCT and rest and not all(c in PAUSE_PUNCT or c in DROP_PUNCT for c in rest) \
                and not nxt.isspace() and out and not out[-1].isspace():
            rep = " " if ch in ",.:;!?" else "\u3000"
        else:
            rep = ""
        out.append(rep)
        edits.append((i, i + 1, len(rep)))
    new = "".join(out)
    if not new.strip():
        return text, []
    return new, edits
