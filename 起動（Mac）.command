#!/bin/zsh
# 字幕ととのえ を起動する（ダブルクリック用）。止めるときはこのウィンドウで Ctrl+C。
cd "$(dirname "$0")" || exit 1
# ダブルクリックで起動したときも uv / python3 を見つけられるようにする
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
UV="$HOME/.local/bin/uv"
[ -x "$UV" ] || UV="$(command -v uv 2>/dev/null)"
if [ -n "$UV" ]; then
  "$UV" run -q --no-project app.py
elif python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  python3 app.py
else
  echo "動かすのに必要な「uv」（Python の実行ツール）が入っていません。"
  echo "公式の手順（https://docs.astral.sh/uv/）で今すぐ入れますか？ [y/N]"
  read -r ans
  if [ "$ans" != "y" ] && [ "$ans" != "Y" ]; then echo "中止しました。"; read -k 1; exit 1; fi
  curl -LsSf https://astral.sh/uv/install.sh | sh || { echo "uv を入れられませんでした。"; read -k 1; exit 1; }
  "$HOME/.local/bin/uv" run -q --no-project app.py
fi
code=$?
# Ctrl+C で止めたとき（130）は正常な終了として扱う
if [ $code -ne 0 ] && [ $code -ne 130 ]; then echo "終了コード $code で止まりました。上のメッセージを確認してください。"; read -k 1; fi
