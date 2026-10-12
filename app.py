#!/usr/bin/env python3
"""字幕ととのえ（Japanese Subtitle Polish）のアプリ。

SRTをブラウザの画面にドロップして整え、確認画面で採用・見送りを選び、修正版を書き出す。
パソコンの中（127.0.0.1）だけで動き、起動ごとに作る合言葉（トークン）がないと操作できない。

- ドロップしたSRTはコピーして `出力/` の作業フォルダで扱う。元のファイルには触れない。
- 修正版を作るのは scripts/apply_candidates.py だけ。毎回、番号・時刻の一致を検査する。
- 「AIで整える」は、このパソコンの中で動くローカルAI（Ollama）を使う。字幕の文字はパソコンの外へ出ない。

起動: python3 app.py   （ブラウザを開かない場合は --no-browser、ポートは --port）
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, Optional  # noqa: F401
from urllib.parse import quote, urlparse, parse_qs

if sys.version_info < (3, 9):  # Windows の起動バッチは、入っている py -3 をそのまま使うため、古い版だとここで分かるように止める
    print(f"Python {sys.version.split()[0]} では動きません。Python 3.9 以上を入れるか、uv（https://docs.astral.sh/uv/）を入れてから"
          "もう一度起動してください。")
    sys.exit(1)

ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / "scripts"
UI = ROOT / "ui"
sys.path.insert(0, str(SCRIPTS))
import local_ai  # noqa: E402
import local_engine  # noqa: E402
from common import atomic_write, chars_for_font, load_glossary, parse_video_size, setup_console  # noqa: E402
from jptext import TELOP_KEEP_DEFAULT, Settings, best_breaks, flatten, strip_telop_punct  # noqa: E402
from srt_io import SrtError, parse_bytes  # noqa: E402
from verify_srt import verify  # noqa: E402

MAX_UPLOAD = 8 * 1024 * 1024  # SRTと辞書の上限（8MB）
PRESETS = {"standard", "safe", "vertical"}


class LocalAI:
    """ローカルAI（Ollama）のエンジンとモデル。用意・起動・モデル取得はアプリが引き受ける。"""

    def __init__(self, enabled: bool = True, host: str = local_ai.DEFAULT_HOST):
        self.enabled = enabled
        self.host = host
        self.install_state = {"phase": "", "status": "", "done": 0, "total": 0, "error": ""}
        self.pull = {"model": None, "status": "", "done": 0, "total": 0, "error": ""}
        self.cancel_install = False
        self.on_done: Optional[Callable[[str, str], None]] = None

    def status(self) -> Dict:
        if not self.enabled:
            return {"enabled": False}
        st = local_engine.status(self.host)
        st["models"] = [{**model, "version_issue": local_engine.model_version_issue(model["name"], st.get("engine_version"))}
                        for model in st.get("models", [])]
        m = local_engine.machine()
        return {"enabled": True, **st, "machine": m, "catalog": local_engine.catalog_for(m, st.get("engine_version")),
                "install": self.install_state, "pull": self.pull}

    def ensure_running(self) -> None:
        try:
            local_engine.start(self.host)
        except local_engine.EngineError as e:
            raise ValueError(str(e))

    def start_install(self) -> None:
        if self.install_state.get("phase") in ("download", "verify", "extract", "start"):
            raise ValueError("いまAIエンジンを準備しています")
        self.install_state = {"phase": "download", "status": "準備を始めます", "done": 0, "total": 0, "error": ""}
        self.cancel_install = False

        def work():
            try:
                local_engine.install(self.host, progress=lambda ph, st, d, t: self.install_state.update(
                    phase=ph, status=st, done=d, total=t), cancelled=lambda: self.cancel_install)
                if self.on_done:
                    self.on_done("AIエンジンの準備ができました", "続けてモデルを入れてください。")
            except Exception as e:  # 理由を画面に出す
                self.install_state.update(phase="error", status="中断しました", error=str(e))
        threading.Thread(target=work, daemon=True).start()

    def start_pull(self, model: str) -> None:
        if not local_engine.is_catalog_model(model):
            raise ValueError("一覧にあるモデルだけを入れられます")
        if self.pull.get("status") == "pulling":
            raise ValueError("いまモデルを入れています")
        spec = next(c for c in local_engine.CATALOG if c["id"] == model)
        free = local_engine.machine().get("disk_free_gb")
        if free is not None and free < spec["download_gb"] * 1.15:
            raise ValueError(f"空きディスクが足りません（空き{free}GB、必要な目安{spec['download_gb'] * 1.15:.0f}GB）")
        self.ensure_running()
        self.pull = {"model": model, "status": "pulling", "done": 0, "total": 0, "error": ""}
        threading.Thread(target=self._pull, args=(model,), daemon=True).start()

    def _pull_once(self, model: str) -> None:
        import urllib.request
        layers: Dict[str, list] = {}
        req = urllib.request.Request(self.host + "/api/pull", data=json.dumps({"model": model, "stream": True}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            for line in r:
                if not line.strip():
                    continue
                ev = json.loads(line)
                if ev.get("error"):
                    raise RuntimeError(ev["error"])
                if ev.get("digest") and ev.get("total"):  # 層ごとの進み具合を合計する
                    layers[ev["digest"]] = [ev.get("completed", 0), ev["total"]]
                    self.pull.update(done=sum(v[0] for v in layers.values()), total=sum(v[1] for v in layers.values()))

    def _pull(self, model: str) -> None:
        # 取得したデータの照合が合わない（digest mismatch）ことがある。Windows 実機の qwen2.5:14b で1回起き、取り直すと入った。
        # 通信の途中で壊れたものなので、1回だけ自動で取り直す。
        for attempt in range(2):
            try:
                self._pull_once(model)
                self.pull.update(status="done", error="")
                if self.on_done:
                    self.on_done("モデルを入れました", f"{model} を使えるようになりました。")
                return
            except Exception as e:  # 失敗の理由を画面に出す
                text = str(e)
                if "digest mismatch" in text and attempt == 0:
                    self.pull.update(done=0, total=0, error="")
                    continue
                if "digest mismatch" in text:
                    text = ("取得したデータが照合値と合いませんでした（通信の途中で壊れた可能性があります）。"
                            "もう一度「入れる」を押してください。詳細：" + text)
                self.pull.update(status="error", error=text)
                return

    def stop(self) -> None:
        """エンジンは止めない（ほかのアプリも使う。Cutline Studio / ChronoFlow と同じ方針）。"""


class App:
    def __init__(self, out_dir: Path, ai: Optional[LocalAI] = None, token: Optional[str] = None):
        self.out = out_dir
        self.token = token or secrets.token_urlsafe(18)
        self.ai = ai or LocalAI(enabled=False)
        self.ai.on_done = lambda sub, body: self.notify and native_notify("字幕ととのえ", sub, body)
        self.cancel_flags: Dict[str, bool] = {}
        self.notify = True  # 画面の「通知」の切り替え（Macの通知センター）
        self.lock = threading.Lock()
        self.result_lock = threading.RLock()  # 反映・改行・書き出しの途中の組を読ませない
        self.out.mkdir(parents=True, exist_ok=True)

    # ── 作業フォルダ ─────────────────────────────────────────
    def job_dir(self, job_id: str) -> Path:
        if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{4}", job_id or ""):
            raise KeyError("作業が見つかりません")
        d = self.out / job_id
        if not (d / "job.json").exists():
            raise KeyError("作業が見つかりません")
        return d

    @staticmethod
    def _retry_io(fn, tries: int = 20, wait: float = 0.025):
        """Windows では、置き換えの瞬間に job.json を読む・置き換えると一瞬だけ拒否される（Errno 13）ことがあるため、短く待って繰り返す。"""
        for i in range(tries):
            try:
                return fn()
            except OSError:
                if i == tries - 1:
                    raise
                time.sleep(wait)

    def load(self, job_id: str) -> Dict:
        d = self.job_dir(job_id)
        return json.loads(self._retry_io(lambda: (d / "job.json").read_text(encoding="utf-8")))

    def save(self, job: Dict) -> None:
        d = self.out / job["id"]
        tmp = d / ".job.json.tmp"
        tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        self._retry_io(lambda: os.replace(tmp, d / "job.json"))

    def update(self, job_id: str, **kw) -> Dict:
        with self.lock:
            job = self.load(job_id)
            job.update(kw)
            job["updated"] = datetime.now().isoformat(timespec="seconds")
            self.save(job)
            return job

    def list_jobs(self):
        items = []
        for d in sorted(self.out.glob("*/job.json"), reverse=True)[:20]:
            try:
                j = json.loads(d.read_text(encoding="utf-8"))
                items.append({k: j.get(k) for k in ("id", "name", "status", "message", "created", "cues", "mode", "model", "has_result")})
            except (OSError, ValueError):
                continue
        return items

    # ── 新しい作業 ───────────────────────────────────────────
    def create(self, body: Dict) -> Dict:
        name = Path(str(body.get("filename") or "")).name
        if not name.lower().endswith(".srt"):
            raise ValueError("SRTファイル（.srt）をドロップしてください")
        try:
            data = base64.b64decode(body.get("data") or "", validate=True)
        except ValueError:
            raise ValueError("ファイルを受け取れませんでした")
        if not data:
            raise ValueError("ファイルが空です")
        if len(data) > MAX_UPLOAD:
            raise ValueError("SRTファイルが大きすぎます（8MBまで）")
        encoding, src, last = None, None, None
        for enc in ("utf-8", "cp932"):
            try:
                src = parse_bytes(data, enc)
                encoding = enc
                break
            except SrtError as e:
                last = e
                if "として読めません" not in str(e):
                    raise ValueError(f"SRTとして読めません：{e}")
        if encoding is None:
            raise ValueError(f"文字コードを判別できません（UTF-8・Shift_JISのどちらでも読めません）：{last}")
        gname, gdata = None, None
        g = body.get("glossary")
        if g:
            gname = Path(str(g.get("filename") or "")).name
            if Path(gname).suffix.lower() not in (".json", ".tsv"):
                raise ValueError("辞書は .json か .tsv にしてください")
            try:
                gdata = base64.b64decode(g.get("data") or "", validate=True)
            except ValueError:
                raise ValueError("辞書ファイルを受け取れませんでした")
            if len(gdata) > MAX_UPLOAD:
                raise ValueError("辞書ファイルが大きすぎます（8MBまで）")

        settings = body.get("settings") or {}
        preset = settings.get("preset", "standard")
        if preset not in PRESETS:
            raise ValueError("用途の指定が正しくありません")
        punct = "telop" if settings.get("punctuation") == "telop" else "keep"
        fillers = bool(settings.get("fillers"))
        layout = self.check_layout(settings)

        stem = re.sub(r"[^\w\-ぁ-んァ-ヶ一-龠ー]+", "_", Path(name).stem).strip("_")[:60] or "subtitle"
        job_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        d = self.out / job_id
        d.mkdir(parents=True)
        (d / f"{stem}.srt").write_bytes(data)
        glossary = None
        if gdata is not None:
            glossary = "glossary" + Path(gname).suffix.lower()
            (d / glossary).write_bytes(gdata)
        job = {"id": job_id, "name": name, "stem": stem, "encoding": encoding, "bom": src.has_bom,
               "cues": len(src.cues), "preset": preset, "punctuation": punct, "fillers": fillers, **layout,
               "glossary": glossary, "status": "ready", "message": "", "mode": None,
               "created": datetime.now().isoformat(timespec="seconds")}
        self.save(job)
        r = self.run_script(job, "inspect_srt.py", [f"{stem}.srt", "--template", f"{stem}.candidates.json"]
                            + self.common_args(job, inspect=True))
        if r.returncode != 0:
            self.update(job_id, status="error", message=r.stderr.strip() or r.stdout.strip())
            raise ValueError(r.stderr.strip() or "SRTを読み込めませんでした")
        (d / "inspect.txt").write_text(r.stdout, encoding="utf-8")
        return self.load(job_id)

    @staticmethod
    def check_layout(settings: Dict) -> Dict:
        """動画のサイズ・文字の大きさ・行数・読む速さ。指定がなければプリセットの字数を使う。"""
        out = {}
        if settings.get("video_size"):
            w, h = parse_video_size(settings["video_size"])
            out["video_size"] = f"{w}x{h}"
            if settings.get("font_px") is not None:
                font = float(settings["font_px"])
                if not 8 <= font <= w / 4:
                    raise ValueError(f"文字の大きさは 8px 〜 {int(w / 4)}px の範囲で指定してください")
                out["font_px"] = font
        if settings.get("max_lines") is not None:
            n = int(settings["max_lines"])
            if not 1 <= n <= 4:
                raise ValueError("行数は1〜4行で指定してください")
            out["max_lines"] = n
        if settings.get("max_cps") is not None:
            c = float(settings["max_cps"])
            if c != 0 and not 2 <= c <= 20:  # 0 は「知らせない」
                raise ValueError("見やすさの目安は1秒2〜20字（知らせない場合は0）で指定してください")
            out["max_cps"] = c
        return out

    @staticmethod
    def to_utf8(job: Dict, data: bytes) -> bytes:
        """Shift_JIS の修正版を UTF-8（BOMなし）に変える。YouTube のヘルプでは、SRTは書式なしの UTF-8 である必要があるため。

        文字コードだけを変え、改行コード・番号・時刻・本文はそのまま。変換後に読み直して、すべての字幕が同じかを確かめる。
        """
        enc = job.get("encoding") or "utf-8"
        if enc == "utf-8":
            return data
        before = parse_bytes(data, enc)
        out = data.decode(enc).encode("utf-8")
        after = parse_bytes(out, "utf-8")
        if [(c.index_raw, c.timing_raw, c.text_raw) for c in before.cues] != [(c.index_raw, c.timing_raw, c.text_raw) for c in after.cues]:
            raise ValueError("UTF-8に変えたあとの番号・時刻・本文が一致しませんでした。元の文字コードのまま書き出してください")
        return out

    @classmethod
    def preview(cls, body: Dict) -> Dict:
        """文字の大きさの見本。整えるときと同じ改行の決め方で、1つの字幕を並べてみる。"""
        layout = cls.check_layout(body)
        w = int(layout.get("video_size", "1920x1080").split("x")[0])
        per = max(4, chars_for_font(w, layout.get("font_px", 115)))
        lines = [str(x) for x in (body.get("lines") or [])][:8]
        text = flatten(lines).text[:400]
        if body.get("punctuation") == "telop":
            text = strip_telop_punct(text, TELOP_KEEP_DEFAULT)[0]
        n = layout.get("max_lines", 2)
        br = best_breaks(text, Settings(max_lines=n, max_chars_per_line=per))
        if br.fits:
            return {"lines": br.lines, "fits": True, "per_line": per, "needed_lines": len(br.lines)}
        # 収まらないときも、語の区切りで行を分けて見せる（画面の折り返しで語の途中が切れないように）
        for more in range(n + 1, n + 6):
            wide = best_breaks(text, Settings(max_lines=more, max_chars_per_line=per))
            if wide.fits:
                return {"lines": wide.lines, "fits": False, "per_line": per, "needed_lines": more}
        return {"lines": br.lines, "fits": False, "per_line": per, "needed_lines": None}

    def relayout(self, job_id: str, body: Dict) -> Dict:
        """文字の大きさ・行数を変えて、今の候補のまま改行を作り直す。"""
        with self.result_lock:
            return self._relayout(job_id, body)

    def _relayout(self, job_id: str, body: Dict) -> Dict:
        job = self.load(job_id)
        if not self.result_ready(job):
            raise ValueError("修正版ができてから変えてください")
        layout = self.check_layout({"video_size": job.get("video_size", "1920x1080"), **body})
        r = self.apply({**job, **layout}, f"{job['stem']}.candidates.json")
        if r.returncode != 0:
            raise ValueError("改行を作り直せませんでした：" + (r.stderr or r.stdout).strip()[-600:])
        self.update(job_id, **layout, status="done", has_result=True, verify_ok=True,
                    message=" / ".join(r.stdout.strip().splitlines()[:2]))
        return {"ok": True, "verify_ok": True}

    @staticmethod
    def result_ready(job: Dict) -> bool:
        return job["status"] not in ("ai", "applying") and (job["status"] == "done" or job.get("has_result", False))

    def common_args(self, job: Dict, inspect: bool = False):
        args = ["--preset", job["preset"], "--punct", job["punctuation"]]
        if job.get("video_size"):
            args += ["--video-size", job["video_size"]]
        if job.get("font_px"):
            args += ["--font-size", f"{job['font_px']:g}"]
        if job.get("max_lines"):
            args += ["--max-lines", str(job["max_lines"])]
        if job.get("max_cps") is not None:
            args += ["--max-cps", f"{job['max_cps']:g}"]
        if job.get("encoding") and job["encoding"] != "utf-8":
            args += ["--encoding", job["encoding"]]
        if job.get("glossary"):
            args += ["--glossary", job["glossary"]]
        if not inspect and job.get("fillers") and job.get("mode") == "ai":
            args += ["--allow", "フィラー削除"]
        return args

    def run_script(self, job: Dict, script: str, args, timeout: int = 300) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPTS / script)] + list(args), cwd=str(self.out / job["id"]),
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)

    def apply(self, job: Dict, candidates: str, cancellable: bool = False) -> subprocess.CompletedProcess:
        """一時領域で全出力を作って検査し、成功した組だけを反映する。"""
        with self.result_lock:
            d, stem = self.job_dir(job["id"]), job["stem"]
            names = [f"{stem}.{suffix}" for suffix in
                     ("polished.srt", "changes.md", "review.md", "apply-log.json", "review.html")]
            with tempfile.TemporaryDirectory(prefix=".apply-", dir=str(d)) as pending:
                stage = Path(pending)
                r = self.run_script(job, "apply_candidates.py",
                                    [f"{stem}.srt", candidates, "--out-dir", str(stage), "--overwrite"] + self.common_args(job))
                if r.returncode != 0:
                    return r
                if not all((stage / name).is_file() for name in names):
                    raise ValueError("修正版の出力がそろいませんでした。前の結果を保っています。")
                checked = verify(str(d / f"{stem}.srt"), str(stage / names[0]),
                                 str(stage / f"{stem}.apply-log.json"), job.get("encoding", "utf-8"))
                if not checked["ok"]:
                    raise ValueError("機械検査が一致しませんでした。前の結果を保っています。")
                if cancellable and self.cancel_flags.get(job["id"]):
                    raise local_ai.OllamaError("中止しました。前の結果は変更していません。")
                previous = {name: (d / name).read_bytes() if (d / name).exists() else None for name in names}
                try:
                    for name in names:
                        os.replace(stage / name, d / name)
                except OSError:
                    for name, data in previous.items():
                        if data is None:
                            (d / name).unlink(missing_ok=True)
                        else:
                            atomic_write(d / name, data)
                    raise
                return r

    # ── 整える（AIあり・なし） ───────────────────────────────
    def start(self, job_id: str, mode: str, model: Optional[str] = None) -> Dict:
        with self.result_lock:
            return self._start(job_id, mode, model)

    def _start(self, job_id: str, mode: str, model: Optional[str] = None) -> Dict:
        job = self.load(job_id)
        if job["status"] in ("ai", "applying"):
            raise ValueError("すでに処理中です")
        if mode == "ai":
            if not self.ai.enabled:
                raise ValueError("ローカルAIは使わない設定で起動しています")
            self.ai.ensure_running()
            names = [m["name"] for m in local_engine.probe(self.ai.host) or []]
            if model not in names:
                raise ValueError("選んだモデルが Ollama に入っていません")
            issue = local_engine.model_version_issue(model, local_engine.probe_version(self.ai.host))
            if issue:
                raise ValueError(issue)
        self.cancel_flags[job_id] = False
        job = self.update(job_id, status="applying" if mode == "basic" else "ai", mode=mode, model=model if mode == "ai" else None,
                          has_result=job.get("has_result", job["status"] == "done"),
                          message="", started=time.time(), progress=0.05, progress_text="SRTを読み込みました")
        threading.Thread(target=self._work, args=(job_id, mode, model), daemon=True).start()
        return job

    def _work(self, job_id: str, mode: str, model: Optional[str]) -> None:
        cpath = self.job_dir(job_id) / f"{self.load(job_id)['stem']}.candidates.json"
        previous_candidates = cpath.read_bytes()
        published = False
        try:
            job = self.load(job_id)
            d = self.out / job_id
            cfile = f"{job['stem']}.candidates.json"
            if mode == "ai":
                def progress(i, n):
                    text = (f"ローカルAIが字幕を読んでいます（{min(job['cues'], i * 20)} / {job['cues']}件）"
                            if i < n else "ローカルAIの確認が終わりました")
                    if i == 0 and n == 1:  # 1回で全部読むときは件数が途中で進まないので、止まって見えない言い方にする
                        text = (f"ローカルAIが字幕{job['cues']}件をまとめて読んでいます"
                                "（最初はモデルの読み込みに時間がかかります。数分かかることもあります）")
                    elif i == 0:
                        text = f"ローカルAIが字幕を読み始めました（0 / {job['cues']}件。最初はモデルの読み込みに時間がかかります）"
                    self.update(job_id, progress=round(0.05 + 0.85 * (i / max(n, 1)), 3), progress_text=text)
                r = local_ai.suggest(str(d / f"{job['stem']}.srt"), str(d / cfile), model,
                                     load_glossary(str(d / job["glossary"])) if job.get("glossary") else None,
                                     fillers=job.get("fillers", False), telop=job["punctuation"] == "telop",
                                     encoding=job["encoding"], host=self.ai.host, progress=progress,
                                     cancelled=lambda: self.cancel_flags.get(job_id, False))
                self.update(job_id, ai_result=r)
            if self.cancel_flags.get(job_id):
                raise local_ai.OllamaError("中止しました。前の結果は変更していません。")
            self.update(job_id, status="applying", progress=0.92, progress_text="修正版を作って、番号・時刻を検査しています")
            r = self.apply(job, cfile, cancellable=True)
            if r.returncode != 0:
                raise ValueError((r.stderr or r.stdout).strip()[-1500:] or "修正版を作れませんでした")
            published = True
            first = r.stdout.strip().splitlines()[:2]
            self.update(job_id, status="done", has_result=True, verify_ok=True, message=" / ".join(first),
                        progress=1.0, progress_text="できあがり")
            if self.notify:
                native_notify("字幕ととのえ", "できあがりました", f"{job['name']}（字幕{job['cues']}件）。確認画面で確かめてください。")
        except local_ai.OllamaError as e:
            cancelled = self.cancel_flags.get(job_id)
            with self.result_lock:
                if not published:
                    atomic_write(cpath, previous_candidates)
                self.update(job_id, status="cancelled" if cancelled else "error", message=str(e))
            if self.notify and not cancelled:
                native_notify("字幕ととのえ", "止まりました", str(e))
        except Exception as e:  # 想定外の失敗も利用者に原因を返す
            with self.result_lock:
                if not published:
                    atomic_write(cpath, previous_candidates)
                self.update(job_id, status="error", message=f"{type(e).__name__}: {e}")

    def cancel(self, job_id: str) -> Dict:
        self.cancel_flags[job_id] = True
        return self.update(job_id, message="中止しています（いま読んでいる字幕のまとまりが終わると止まります）")

    def decisions(self, job_id: str, doc: Dict) -> Dict:
        with self.result_lock:
            return self._decisions(job_id, doc)

    def _decisions(self, job_id: str, doc: Dict) -> Dict:
        job = self.load(job_id)
        if not self.result_ready(job):
            raise ValueError("修正版ができてから反映してください")
        d = self.out / job_id
        cfile = d / f"{job['stem']}.candidates.json"
        if not isinstance(doc, dict) or not isinstance(doc.get("candidates"), list):
            raise ValueError("決定の形式が正しくありません")
        try:
            old = json.loads(cfile.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = {}
        for k in ("schema", "source_file", "source_sha256"):  # 画面以外から送られた決定でも、原本の照合に使う情報は残す
            if k not in doc and isinstance(old, dict) and k in old:
                doc[k] = old[k]
        n = len(list(d.glob(f"{job['stem']}.candidates.v*.json"))) + 1
        shutil.copy2(cfile, d / f"{job['stem']}.candidates.v{n}.json")  # 反映前の候補を残す
        previous = cfile.read_bytes()
        try:
            atomic_write(cfile, (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            r = self.apply(job, cfile.name)
            if r.returncode != 0:
                raise ValueError("反映できませんでした：" + (r.stderr or r.stdout).strip()[-600:])
        except Exception:
            atomic_write(cfile, previous)
            raise
        self.update(job_id, status="done", has_result=True, verify_ok=True,
                    message=" / ".join(r.stdout.strip().splitlines()[:2]))
        return {"ok": True, "verify_ok": True}


# ── HTTP ──────────────────────────────────────────────────────

def make_handler(app: App, port: int):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class H(BaseHTTPRequestHandler):
        server_version = "SubtitlePolish/1"

        def log_message(self, fmt, *args):  # 端末には失敗だけを出す
            if args and str(args[1]).startswith(("4", "5")):
                sys.stderr.write("%s %s\n" % (self.command, fmt % args))

        def _send(self, code: int, body: bytes, ctype: str, extra: Optional[Dict] = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if not (extra or {}).get("Cache-Control"):
                self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                                        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in hosts

        def _cookie_ok(self) -> bool:
            return f"jsp={app.token}" in (self.headers.get("Cookie") or "")

        def _page(self, html: str, set_cookie: bool = False):
            extra = {"Set-Cookie": f"jsp={app.token}; Path=/; HttpOnly; SameSite=Strict"} if set_cookie else None
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", extra)

        def _result(self, job_id: str, action: str, query: Dict):
            with app.result_lock:
                job = app.load(job_id)
                d = app.job_dir(job["id"])
                if action == "review":
                    f = d / f"{job['stem']}.review.html"
                    if not f.exists():
                        return self._json(404, {"error": "確認画面がまだありません"})
                    inject = "<script>window.JSP_APP=" + json.dumps({"job": job["id"], "token": app.token}) + "</script>"
                    return self._page(f.read_text(encoding="utf-8").replace("<head>", "<head>" + inject, 1))
                f = d / f"{job['stem']}.polished.srt"
                if not f.exists():
                    return self._json(404, {"error": "修正版がまだありません"})
                checked = verify(str(d / f"{job['stem']}.srt"), str(f),
                                 str(d / f"{job['stem']}.apply-log.json"), job.get("encoding", "utf-8"))
                if not checked["ok"]:
                    raise ValueError("修正版の機械検査が一致しません。書き出しを止めました。")
                data, suffix = f.read_bytes(), ""
                if query.get("utf8") == ["1"]:
                    data, suffix = app.to_utf8(job, data), ".utf8"
                out_name = Path(job["name"]).stem + suffix + ".polished.srt"
                disp = f"attachment; filename=\"polished{suffix}.srt\"; filename*=UTF-8''{quote(out_name)}"
                return self._send(200, data, "application/x-subrip", {"Content-Disposition": disp})

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path in STATIC:  # アイコンと完了音（秘密の情報は含まない）
                f, ctype = STATIC[u.path]
                if f.exists():
                    return self._send(200, f.read_bytes(), ctype, {"Cache-Control": "max-age=3600"})
                return self._send(404, b"not found", "text/plain")
            if u.path == "/":
                fresh = q.get("t", [""])[0] == app.token
                if not (fresh or self._cookie_ok()):
                    return self._page(LOCKED)
                html = (UI / "app.html").read_text(encoding="utf-8")
                cfg = {"token": app.token, "native_notify": sys.platform == "darwin"}
                html = html.replace("/*__APP__*/null", json.dumps(cfg, ensure_ascii=False).replace("<", "\\u003c"))
                if fresh:  # アドレス欄に合言葉を残さない
                    html = html.replace("<head>", "<head><script>history.replaceState(null,'','/')</script>", 1)
                return self._page(html, set_cookie=fresh)
            if not self._cookie_ok():
                return self._page(LOCKED)
            m = re.fullmatch(r"/jobs/([\w-]+)/(review|download)", u.path)
            if u.path == "/api/jobs":
                return self._json(200, {"jobs": app.list_jobs()})
            if u.path == "/api/ai":
                return self._json(200, app.ai.status())
            m2 = re.fullmatch(r"/api/jobs/([\w-]+)", u.path)
            try:
                if m2:
                    return self._json(200, app.load(m2.group(1)))
                if m:
                    return self._result(m.group(1), m.group(2), q)
            except KeyError as e:
                return self._json(404, {"error": str(e).strip("'")})
            except (ValueError, OSError, SrtError) as e:
                return self._json(400, {"error": str(e)})
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-Token") != app.token or not self._cookie_ok():
                return self._json(403, {"error": "この画面からは操作できません。アプリを起動し直してください。"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length < 0:
                    raise ValueError()
            except ValueError:
                return self._json(400, {"error": "送られた内容の長さが正しくありません"})
            if length > MAX_UPLOAD * 2:
                return self._json(413, {"error": "ファイルが大きすぎます（8MBまで）"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json(400, {"error": "送られた内容を読めません"})
            if not isinstance(body, dict):
                return self._json(400, {"error": "送られた内容の形式が正しくありません"})
            u = urlparse(self.path).path
            try:
                if u == "/api/jobs":
                    return self._json(200, app.create(body))
                if u == "/api/preview":
                    return self._json(200, app.preview(body))
                if u == "/api/prefs":
                    app.notify = bool(body.get("notify", True))
                    return self._json(200, {"ok": True})
                if u == "/api/notify-test":
                    return self._json(200, {"method": native_notify("字幕ととのえ", "通知のテスト", "できあがると、このように知らせます。")})
                if u == "/api/open-output":
                    open_folder(app.out)
                    return self._json(200, {"ok": True})
                if u == "/api/ai/start":
                    app.ai.ensure_running()
                    return self._json(200, app.ai.status())
                if u == "/api/ai/install":
                    app.ai.start_install()
                    return self._json(200, app.ai.status())
                if u == "/api/ai/install/cancel":
                    app.ai.cancel_install = True
                    return self._json(200, {"ok": True})
                if u == "/api/ai/pull":
                    app.ai.start_pull(str(body.get("model") or ""))
                    return self._json(200, app.ai.status())
                m = re.fullmatch(r"/api/jobs/([\w-]+)/(run|cancel|decisions|open|layout)", u)
                if not m:
                    return self._json(404, {"error": "not found"})
                job_id, action = m.groups()
                app.job_dir(job_id)
                if action == "run":
                    return self._json(200, app.start(job_id, "ai" if body.get("mode") == "ai" else "basic", body.get("model")))
                if action == "cancel":
                    return self._json(200, app.cancel(job_id))
                if action == "decisions":
                    return self._json(200, app.decisions(job_id, body))
                if action == "layout":
                    return self._json(200, app.relayout(job_id, body))
                if action == "open":
                    open_folder(app.job_dir(job_id))
                    return self._json(200, {"ok": True})
            except KeyError as e:
                return self._json(404, {"error": str(e).strip("'")})
            except (ValueError, OSError, subprocess.SubprocessError) as e:
                return self._json(400, {"error": str(e)})
            return self._json(404, {"error": "not found"})

    return H


# ── 完了通知（Mac）──────────────────────────────────────────────
NOTIFIER_APP = ROOT / "assets" / "notifier" / "SubtitlePolishNotifier.app"
NOTIFIER_BUILD = ROOT / "assets" / "notifier" / "build_notifier.command"


def ensure_notifier() -> bool:
    """Mac で通知アプリが無ければ組み立てる（初回だけ。clang が無ければ通知なしで動く）。"""
    if sys.platform != "darwin":
        return False
    if NOTIFIER_APP.exists():
        return True
    try:
        r = subprocess.run(["/bin/bash", str(NOTIFIER_BUILD)], capture_output=True, text=True, timeout=120)
        return r.returncode == 0 and NOTIFIER_APP.exists()
    except (OSError, subprocess.SubprocessError):
        return False


def native_notify(title: str, subtitle: str, body: str) -> str:
    """Mac の通知センターへ出す。このアプリのアイコンで出すため、専用の通知アプリを使う（ChronoFlowと同じ方式）。"""
    if sys.platform != "darwin":
        return "unsupported"
    clean = lambda t: str(t).replace("\n", " ").strip()[:200]
    if NOTIFIER_APP.exists():
        try:
            d = local_engine.app_support_dir()
            d.mkdir(parents=True, exist_ok=True)
            (d / "notification.txt").write_text(f"{clean(body)}\n{clean(title)}\n{clean(subtitle)}\n", encoding="utf-8")
            r = subprocess.run(["/usr/bin/open", "-n", "-g", str(NOTIFIER_APP)], capture_output=True, timeout=10)
            if r.returncode == 0:
                return "notifier"
        except (OSError, subprocess.SubprocessError):
            pass
    try:  # 通知アプリが使えないとき（アイコンはスクリプトエディタのものになる）
        subprocess.run(["/usr/bin/osascript", "-e", "on run argv", "-e",
                        "display notification (item 1 of argv) with title (item 2 of argv) subtitle (item 3 of argv)",
                        "-e", "end run", clean(body), clean(title), clean(subtitle)], capture_output=True, timeout=10)
        return "osascript"
    except (OSError, subprocess.SubprocessError):
        return "failed"


STATIC = {f"/assets/icon/icon_{n}.png": (ROOT / "assets" / "icon" / f"icon_{n}.png", "image/png") for n in (16, 32, 64, 128, 256, 512)}
STATIC["/assets/sound/completion_chime.wav"] = (ROOT / "assets" / "sound" / "completion_chime.wav", "audio/wav")
STATIC["/favicon.ico"] = (ROOT / "assets" / "icon" / "icon.ico", "image/x-icon")


LOCKED = """<!doctype html><meta charset="utf-8"><title>字幕ととのえ</title>
<body style="font:15px/1.7 -apple-system,'Hiragino Sans',sans-serif;padding:48px;color:#16172e">
<h1 style="font-size:20px">起動したときに開いた画面から使ってください</h1>
<p>このアプリは、起動するたびに作る合言葉で守られています。起動ファイルをもう一度ダブルクリックしてください。</p></body>"""


def open_folder(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    elif os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def main(argv=None) -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="字幕ととのえ：SRTをドロップして整えるアプリ")
    ap.add_argument("--port", type=int, default=0, help="待ち受けるポート（既定は空いているポート）")
    ap.add_argument("--out", default=str(ROOT / "出力"), help="作業結果を保存するフォルダ")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--token", help="合言葉を固定する（自動テスト用。通常は指定しない）")
    ap.add_argument("--no-ai", action="store_true", help="ローカルAI（Ollama）を使わない")
    args = ap.parse_args(argv)
    ai = LocalAI(enabled=not args.no_ai)
    app = App(Path(args.out).resolve(), ai, args.token)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), None)  # type: ignore[arg-type]
    port = server.server_address[1]
    server.RequestHandlerClass = make_handler(app, port)
    url = f"http://127.0.0.1:{port}/?t={app.token}"
    print("字幕ととのえ を起動しました。")
    print(f"  画面: {url}")
    print(f"  作業結果の保存先: {app.out}")
    st = ai.status()
    print("  ローカルAI: " + ("使わない設定" if not st["enabled"] else st["message"]))
    print("止めるときは、このウィンドウで Ctrl + C を押します。", flush=True)
    threading.Thread(target=ensure_notifier, daemon=True).start()  # 初回だけ通知アプリを組み立てる
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました。")
    finally:
        server.server_close()
        ai.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
