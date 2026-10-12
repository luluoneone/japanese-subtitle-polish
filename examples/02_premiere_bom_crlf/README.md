# 編集ソフトから書き出した字幕（BOM付き・CRLF・複数行）

BOMとWindowsの改行コード（CRLF）を保ったまま直します。「Premiere／Pro」「ルックアップテー／ブル」「100／%」のような語の途中の改行、数字だけの字幕（#3〜#5）、話者ごとの行（#12）を含みます。辞書はTSV形式です。

字幕・辞書・人名は架空です。`candidates.json` は、このSkillの手順に沿ってAI（Claude）が作った修正候補です。

## 修正前後

| 番号 | 時刻 | 修正前 | 修正後（`output`） |
|---|---|---|---|
| 1 | 00:00:01,000 | LUTを使った<br>カラーグレーディングの基本です | （変更なし） |
| 2 | 00:00:04,500 | 今回はPremiere<br>ProのLumetriカラーで説明します | 今回はPremiere Proの<br>Lumetriカラーで説明します |
| 3 | 00:00:09,000 | 3 | （変更なし） |
| 4 | 00:00:10,000 | 2 | （変更なし） |
| 5 | 00:00:11,000 | 1 | （変更なし） |
| 6 | 00:00:12,200 | まずルックアップテー<br>ブルを読み混みます | まずルックアップテーブルを<br>読み込みます |
| 7 | 00:00:16,000 | after effectsでも同じLUTが使えます | After Effectsでも<br>同じLUTが使えます |
| 8 | 00:00:20,200 | 適用量は100<br>%ではなく70%くらいに下げます | 適用量は100%ではなく<br>70%くらいに下げます |
| 9 | 00:00:25,100 | ログ素材には変換用のLUTを先に当てます | ログ素材には<br>変換用のLUTを先に当てます |
| 10 | 00:00:29,600 | 色が転ぶ場合は<br>ホワイトバランスを先に直してください | 色が転ぶ場合はホワイトバランスを<br>先に直してください |
| 11 | 00:00:35,000 | 書き出しは4K 60fpsで確認しました | （変更なし） |
| 12 | 00:00:39,000 | －このLUTは無料ですか？<br>－はい、無料で使えます | （変更なし） |
| 13 | 00:00:43,000 | ルミトリカラーのプリセットも<br>概要欄から配布しています | Lumetriカラーのプリセットも<br>概要欄から配布しています |
| 14 | 00:00:47,800 | 以上です | （変更なし） |

## ファイル

| ファイル | 中身 |
|---|---|
| `input.srt` | 修正前（文字コード utf-8、BOM あり、改行コード CRLF） |
| `inspect.txt` | `inspect_srt.py` の出力（AIが候補を作るときに読む一覧） |
| `candidates.json` | 修正候補 |
| `output/input.polished.srt` | 修正版 |
| `output/input.changes.md` | 変更一覧（末尾に検査結果） |
| `output/input.review.md` | 要確認一覧 |
| `output/input.apply-log.json` | 適用ログ |
| `output/input.review.html` | 確認画面（ダブルクリックで開く） |

## 再現するコマンド

このフォルダで実行します（出力がすでにある場合は `--overwrite` を付けます）。

```bash
python3 ../../scripts/inspect_srt.py input.srt --glossary glossary.tsv --template candidates.json
python3 ../../scripts/apply_candidates.py input.srt candidates.json --glossary glossary.tsv --out-dir output
python3 ../../scripts/verify_srt.py input.srt output/input.polished.srt --log output/input.apply-log.json
```

機械検査（件数・番号・順番・時刻・BOM・改行コード）は すべての出力で一致しています。日本語の意味・読みやすさ・映像との同期は、この検査の対象外です。
