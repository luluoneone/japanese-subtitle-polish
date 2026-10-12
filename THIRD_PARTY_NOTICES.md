# 参考にしたもの・出典

## Srt-Enhancer（zzh-editor）

- リポジトリ: https://github.com/zzh-editor/Srt-Enhancer
- 確認した版: commit `2088b8fa7057d34d3c374fbeaef962ba28afffa0`（2026-09-30）
- ライセンス: MIT License（Copyright (c) 2026 zzh-editor）

中国語のSRTを整えるAgent Skillです。設計の参考にしましたが、**ソースコード・辞書・文章は流用していません**。
このリポジトリのスクリプト、判断基準、サンプルはすべて新しく書いたものです。

参考にした考え方:

- AIが判断の材料を用意し、決まった処理はスクリプトに任せる分担
- 修正前後の比較を利用者に見せてから確定する流れ
- SRTを比べるときに、番号・時刻の行ではなく本文を比べる点

日本語向けに新しく作った部分と、設計を変えた部分は README の「Srt-Enhancer との違い」にまとめています。

Srt-Enhancer の MIT License 全文:

```text
MIT License

Copyright (c) 2026 zzh-editor

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 実行時に取得するもの（同梱していません）

- **Ollama**（https://github.com/ollama/ollama 、MIT License）：「AIエンジンを準備」を押したときに、公式 GitHub Releases の v0.32.6 を取得し、配布元の SHA-256 と照合してから使います。
- **AIモデル**（qwen2.5 など）：「モデルを入れる」を押したときに、Ollama を通じて取得します。利用条件は各モデルの提供元に従います。

## LULU Apps の自社製品から移したもの

- AIエンジンの準備・モデルの判定（scripts/local_engine.py）：Cutline Studio の方式をPythonへ移したもの
- 完了通知のアプリ（assets/notifier）と完了音（scripts/chime.py）：ChronoFlow の仕組みを元にしたもの

## サンプルの字幕

`examples/` の字幕・辞書・人名は、このリポジトリのために作った架空のものです。実在の動画や人物の発言ではありません。

## 紹介動画（docs/subtitle-polish-pv.mp4）の音

紹介動画は MIT License の対象外です。動画に含まれる音楽・効果音・ナレーションを取り出して、別の作品に使うことはできません。

- BGM：MotionElements「Uplifting Indie Rock」（素材番号 12032250）。作者が購入したロイヤリティフリー素材で、この動画の中でだけ使っています
- タイムコードの場面：MotionElements「Eva Timer Countdown」（素材番号 16211124、After Effects テンプレート）。作者が購入したロイヤリティフリー素材で、表示の文字を字幕ととのえ用に差し替えて書き出したもの
- タイムコードの場面の和文：フォントワークス「FOT-マティス Pro EB」。作者のライセンスで映像に書き出したもので、フォントのファイルはこのリポジトリに含まれていません
- ナレーション：Koe Studio（Google Gemini TTS）で作成
- そのほかの効果音：プログラムで合成したもの
