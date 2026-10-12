"""完了音（2音のチャイム、約0.48秒）を作る。

ChronoFlow（LULU Apps）の sounds.py と同じ作り方。外部の音源を使わず、コードで波形を作るので、
配布しても権利の問題がない。E5 → B5、短いアタックと指数的な減衰。

使い方: python3 scripts/chime.py  → assets/sound/completion_chime.wav を作り直す
"""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

SAMPLE_RATE = 44100
AMPLITUDE = 0.32


def write_chime(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    notes = [(659.26, 0.0, 0.22), (987.77, 0.16, 0.30)]  # E5 -> B5
    total = int(SAMPLE_RATE * 0.48)
    samples = [0.0] * total
    for freq, start_s, length_s in notes:
        start, count = int(start_s * SAMPLE_RATE), int(length_s * SAMPLE_RATE)
        for i in range(count):
            pos = start + i
            if pos >= total:
                break
            t = i / SAMPLE_RATE
            attack = min(1.0, i / (0.012 * SAMPLE_RATE))
            decay = math.exp(-4.5 * t / length_s)
            samples[pos] += AMPLITUDE * attack * decay * math.sin(2 * math.pi * freq * t)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(struct.pack("<" + "h" * total, *[max(-32768, min(32767, int(v * 32767))) for v in samples]))
    return path


if __name__ == "__main__":
    print(write_chime(Path(__file__).resolve().parent.parent / "assets" / "sound" / "completion_chime.wav"))
