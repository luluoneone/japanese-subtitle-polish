"""SRTの読み書き。本文以外のバイト列を一切作り直さないことを最優先にする。

番号行・時刻行・字幕どうしの空行は、読み込んだ文字列（改行コード込み）をそのまま保持し、
書き出し時も同じ文字列を返す。変更できるのは各字幕の本文行だけ。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Union

BOM = "﻿"
_EOL_RE = re.compile(r"\r\n|\r|\n")
TIMING_RE = re.compile(
    r"^\s*(\d{1,3}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,3}):(\d{2}):(\d{2})[,.](\d{1,3})(.*)$"
)
INDEX_RE = re.compile(r"^\s*(\d+)\s*$")


class SrtError(Exception):
    """利用者に原因を伝えるための読み込みエラー。"""


@dataclass
class Gap:
    raw: str  # 空行（空白だけの行を含む）の並び。改行コード込み


@dataclass
class Cue:
    index_raw: str            # 番号行（改行コード込み）
    timing_raw: str           # 時刻行（改行コード込み）
    text_raw: List[str]       # 本文行（それぞれ改行コード込み）
    line_no: int              # 番号行のファイル内行番号（1始まり）
    new_lines: Optional[List[str]] = None  # 書き換え後の本文行（改行コードなし）

    @property
    def index(self) -> int:
        return int(INDEX_RE.match(_strip_eol(self.index_raw)).group(1))

    @property
    def timing(self) -> str:
        return _strip_eol(self.timing_raw).strip()

    @property
    def start_ms(self) -> int:
        return _timing_ms(self.timing)[0]

    @property
    def end_ms(self) -> int:
        return _timing_ms(self.timing)[1]

    @property
    def start(self) -> str:
        return self.timing.split("-->")[0].strip()

    @property
    def end(self) -> str:
        return self.timing.split("-->")[1].strip().split()[0]

    @property
    def lines(self) -> List[str]:
        """現在の本文行（改行コードなし）。書き換え済みなら新しい行。"""
        if self.new_lines is not None:
            return list(self.new_lines)
        return [_strip_eol(x) for x in self.text_raw]

    @property
    def original_lines(self) -> List[str]:
        return [_strip_eol(x) for x in self.text_raw]

    def eol(self) -> str:
        """この字幕で使われている改行コード。"""
        for raw in self.text_raw + [self.timing_raw, self.index_raw]:
            m = _EOL_RE.search(raw)
            if m:
                return m.group(0)
        return "\n"

    def render(self) -> str:
        if self.new_lines is None:
            return self.index_raw + self.timing_raw + "".join(self.text_raw)
        eol = self.eol()
        # 最後の本文行の改行コード（ファイル末尾で改行がない場合は空）を引き継ぐ
        if self.text_raw:
            m = _EOL_RE.search(self.text_raw[-1])
            last_eol = m.group(0) if m else ""
        else:
            last_eol = eol
        timing_raw = self.timing_raw
        if not _EOL_RE.search(timing_raw):
            # 本文なし・改行なしでファイルが終わっていた字幕に本文を足す場合
            timing_raw += eol
        body = eol.join(self.new_lines) + last_eol
        return self.index_raw + timing_raw + body


Item = Union[Gap, Cue]


@dataclass
class SrtFile:
    path: Optional[Path]
    encoding: str
    has_bom: bool
    items: List[Item]
    sha256: str
    warnings: List[str] = field(default_factory=list)

    @property
    def cues(self) -> List[Cue]:
        return [x for x in self.items if isinstance(x, Cue)]

    def newline_style(self) -> str:
        text = "".join(i.raw if isinstance(i, Gap) else i.render() for i in self.items)
        crlf = text.count("\r\n")
        lf = text.count("\n") - crlf
        cr = text.count("\r") - crlf
        kinds = [name for name, n in (("CRLF", crlf), ("LF", lf), ("CR", cr)) if n]
        if not kinds:
            return "なし"
        return "+".join(kinds) if len(kinds) > 1 else kinds[0]

    def render(self) -> str:
        out = [BOM] if self.has_bom else []
        for item in self.items:
            out.append(item.raw if isinstance(item, Gap) else item.render())
        return "".join(out)

    def to_bytes(self) -> bytes:
        return self.render().encode(self.encoding)


def _strip_eol(s: str) -> str:
    return _EOL_RE.sub("", s)


def _timing_ms(timing: str):
    m = TIMING_RE.match(timing)
    if not m:
        raise SrtError(f"時刻を読めません: {timing}")
    g = m.groups()

    def ms(h, mi, s, f):
        return ((int(h) * 60 + int(mi)) * 60 + int(s)) * 1000 + int(f.ljust(3, "0"))

    return ms(*g[0:4]), ms(*g[4:8])


def _split_keep_eol(text: str) -> List[str]:
    lines, pos = [], 0
    for m in _EOL_RE.finditer(text):
        lines.append(text[pos:m.end()])
        pos = m.end()
    if pos < len(text):
        lines.append(text[pos:])
    return lines


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Union[str, Path]) -> str:
    return sha256_bytes(Path(path).read_bytes())


def parse_bytes(data: bytes, encoding: str = "utf-8", path: Optional[Path] = None) -> SrtFile:
    has_bom = data.startswith(b"\xef\xbb\xbf") and encoding.lower().replace("_", "-") in ("utf-8", "utf8", "utf-8-sig")
    body = data[3:] if has_bom else data
    enc = "utf-8" if encoding.lower().replace("_", "-") in ("utf-8", "utf8", "utf-8-sig") else encoding
    try:
        text = body.decode(enc)
    except UnicodeDecodeError as e:
        raise SrtError(
            f"{enc} として読めません（{e.start}バイト目付近）。"
            "Shift_JISで書き出したファイルなら --encoding cp932 を指定してください。"
        ) from e
    if text.startswith(BOM):  # UTF-8以外で読んだ場合や二重BOM
        raise SrtError("ファイル先頭に余分なBOMがあります。書き出し設定を確認してください。")

    lines = _split_keep_eol(text)
    items: List[Item] = []
    warnings: List[str] = []
    gap: List[str] = []
    i, n = 0, len(lines)

    def is_blank(k: int) -> bool:
        return _strip_eol(lines[k]).strip() == ""

    def starts_cue(k: int) -> bool:
        return (k + 1 < n and INDEX_RE.match(_strip_eol(lines[k]))
                and TIMING_RE.match(_strip_eol(lines[k + 1]).strip()) is not None)

    while i < n:
        if is_blank(i):
            gap.append(lines[i])
            i += 1
            continue
        if not starts_cue(i):
            content = _strip_eol(lines[i])
            if INDEX_RE.match(content):
                raise SrtError(f"{i + 2}行目: 番号 {content.strip()} の次に時刻の行がありません。")
            raise SrtError(f"{i + 1}行目: 字幕番号の行が必要な位置に「{content[:30]}」があります。")
        if gap:
            items.append(Gap("".join(gap)))
            gap = []
        start_line = i
        index_raw, timing_raw = lines[i], lines[i + 1]
        j = i + 2
        text_raw: List[str] = []
        while j < n and not is_blank(j):
            if starts_cue(j):
                warnings.append(f"{j + 1}行目: 字幕の間に空行がありません（番号 {_strip_eol(lines[j]).strip()}）。")
                break
            text_raw.append(lines[j])
            j += 1
        items.append(Cue(index_raw, timing_raw, text_raw, start_line + 1))
        i = j
    if gap:
        items.append(Gap("".join(gap)))

    if not any(isinstance(x, Cue) for x in items):
        raise SrtError("字幕が1件も見つかりません。SRT形式か確認してください。")
    if any(not _EOL_RE.search(c.text_raw[-1] if c.text_raw else c.timing_raw)
           for c in items[:-1] if isinstance(c, Cue)):
        warnings.append("改行コードのない行が途中にあります。")
    return SrtFile(path, enc, has_bom, items, sha256_bytes(data), warnings)


def read_srt(path: Union[str, Path], encoding: str = "utf-8") -> SrtFile:
    p = Path(path)
    if not p.exists():
        raise SrtError(f"ファイルが見つかりません: {p}")
    if not p.is_file():
        raise SrtError(f"ファイルではありません: {p}")
    try:
        data = p.read_bytes()
    except PermissionError as e:
        raise SrtError(f"読み込み権限がありません: {p}") from e
    return parse_bytes(data, encoding, p)


def format_ms(ms: int) -> str:
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, f = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{f:03d}"
