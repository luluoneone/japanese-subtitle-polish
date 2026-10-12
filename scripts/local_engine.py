"""ローカルAIのエンジン（Ollama）とモデルを、アプリが用意・管理する。

LULU の Cutline Studio で実機検証した方式をPythonへ移したもの:
- 「公式サイトから入れてください」と案内するのではなく、アプリが取得して設定する
- 取得する版は固定し（最新を追わない）、配布元の SHA-256 と照合してから展開する
- すでにPCに Ollama があれば、それをそのまま使う（二重に入れない）
- エンジンは detached で起動し、このアプリを閉じても止めない（ほかのアプリも使うため）
- モデルはPCの搭載メモリと空き容量から「合うかどうか」を判定して勧める

字幕の文字は、このPCのエンジン（127.0.0.1）にだけ送られる。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

ENGINE_VERSION = "v0.32.6"
RELEASE_BASE = f"https://github.com/ollama/ollama/releases/download/{ENGINE_VERSION}"
# 配布元 sha256sum.txt（v0.32.6）の値。取得時に配布元の値とも照合し、両方一致したときだけ展開する
PINNED_SHA256 = {
    "ollama-darwin.tgz": "c256147703b0b24a9871ec9f94fc108f18cf87ff043aebd6f7e4a95fcfb4f042",
    "ollama-windows-amd64.zip": "756dd7e41fbde34b8869bd7106d626d913a566cce4d0a95a61aa0579f455ddbc",
    "ollama-windows-arm64.zip": "14e1d052bf7e94fc1d84126e57a7fc3a9d0eeccad3aca43c1bdcb7d07e1087fb",
}
DEFAULT_HOST = "http://127.0.0.1:11434"
APP_DIR_NAME = "Japanese Subtitle Polish"

# 数値は Cutline Studio のカタログ（2026-08-09 実測。ダウンロード量はOllama公式レジストリの値、
# 実行時のメモリは「ダウンロード量×1.25」の見積もり）。note は字幕ととのえでの比較（2026-10-08、M4 Max、
# 字幕4本・正解8件。docs/verification.md）。min_engine は、JSONで返す指定が効くことを確かめたエンジンの版
CATALOG = [
    {"id": "qwen2.5:3b", "download_gb": 1.93, "runtime_gb": 2.4, "recommended_memory_gb": 8, "recommended_vram_gb": 4,
     "note": "軽い。見落としや誤った提案が増える"},
    {"id": "qwen2.5:7b", "download_gb": 4.68, "runtime_gb": 5.9, "recommended_memory_gb": 16, "recommended_vram_gb": 8,
     "note": "速い。語尾まで変える提案が混じることがある（適用前に止める）"},
    {"id": "qwen2.5:14b", "download_gb": 8.99, "runtime_gb": 11.2, "recommended_memory_gb": 24, "recommended_vram_gb": 12,
     "note": "速さと正しさの釣り合いが良い。誤った提案は要確認に回ることが多い"},
    {"id": "qwen3.5:27b", "download_gb": 17.48, "runtime_gb": 21.9, "recommended_memory_gb": 32, "recommended_vram_gb": 24,
     "min_engine": "0.32.6", "note": "いちばん正確（4本で誤った提案なし）。時間は 14b の約2倍"},
]
# qwen2.5:32b は外した（方言を標準語に、人名を推測で漢字に変える提案が混じった）。qwen3.5:9b は見落としが多かった
RECOMMEND_UP_TO = "qwen3.5:27b"


def version_tuple(v: Optional[str]):
    nums = re.findall(r"\d+", v or "")
    return tuple(int(x) for x in nums[:3]) if nums else None


def probe_version(host: str = DEFAULT_HOST, timeout: float = 1.8) -> Optional[str]:
    """動いているエンジンの版（例: 0.32.6）。応答しなければ None。"""
    try:
        with urllib.request.urlopen(host + "/api/version", timeout=timeout) as r:
            return json.loads(r.read()).get("version")
    except (OSError, ValueError, urllib.error.URLError):
        return None


def model_version_issue(model: str, engine_version: Optional[str]) -> Optional[str]:
    """一覧外の同系列モデルにも、確認済みの最低エンジン版を適用する。"""
    spec = next((c for c in CATALOG if c["id"] == model), {})
    minimum = spec.get("min_engine") or ("0.32.6" if re.match(r"^qwen3\.5(?::|$)", model) else None)
    have = version_tuple(engine_version)
    if minimum and have and have < version_tuple(minimum):
        return f"{model} は Ollama {minimum} 以上で確かめています。現在は {engine_version} です。AIエンジンを更新してください。"
    return None


class EngineError(Exception):
    pass


# ── 場所 ───────────────────────────────────────────────────────────

def app_support_dir() -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP_DIR_NAME
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / APP_DIR_NAME
    return home / ".japanese-subtitle-polish"


def engine_dir() -> Path:
    return app_support_dir() / "engine"


def models_dir() -> Path:
    configured = (os.environ.get("OLLAMA_MODELS") or "").strip()
    return Path(configured) if configured else Path.home() / ".ollama" / "models"


def display_path(p: Path) -> str:
    """画面に出す形。ユーザー名を出さない（Macは~、Windowsは%USERPROFILE%）。"""
    s, home = str(p), str(Path.home())
    if s.startswith(home):
        return ("%USERPROFILE%" if os.name == "nt" else "~") + s[len(home):]
    return s


def _binary_name() -> str:
    return "ollama.exe" if os.name == "nt" else "ollama"


def archive_name() -> Optional[str]:
    if sys.platform == "darwin":
        return "ollama-darwin.tgz"
    if os.name == "nt":
        return "ollama-windows-arm64.zip" if platform.machine().lower() in ("arm64", "aarch64") else "ollama-windows-amd64.zip"
    return None


def candidate_paths() -> List[Path]:
    out = [Path(p) for p in [(os.environ.get("OLLAMA_PATH") or "").strip()] if p]
    which = shutil.which("ollama")
    if which:
        out.append(Path(which))
    if sys.platform == "darwin":
        out += [Path("/usr/local/bin/ollama"), Path("/opt/homebrew/bin/ollama"),
                Path("/Applications/Ollama.app/Contents/Resources/ollama"),
                Path.home() / "Applications/Ollama.app/Contents/Resources/ollama"]
    elif os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        out += [local / "Programs" / "Ollama" / "ollama.exe",
                Path(os.environ.get("ProgramFiles") or r"C:\Program Files") / "Ollama" / "ollama.exe"]
    else:
        out += [Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama")]
    return out


def _managed_binary() -> Path:
    """アプリが入れたエンジンの本体。engine.json に記録した場所を使う。"""
    try:
        rel = json.loads((engine_dir() / "engine.json").read_text(encoding="utf-8")).get("binary")
        if rel:
            return engine_dir() / rel
    except (OSError, ValueError):
        pass
    return engine_dir() / _binary_name()


def resolve_binary() -> Optional[Dict]:
    managed = _managed_binary()
    for c in [managed] + candidate_paths():
        if c.is_file():
            return {"path": str(c), "managed": c == managed}
    return None


# ── 状態 ───────────────────────────────────────────────────────────

def probe(host: str = DEFAULT_HOST, timeout: float = 1.8) -> Optional[List[Dict]]:
    """エンジンが応答すればモデル一覧。応答しなければ None。"""
    try:
        with urllib.request.urlopen(host + "/api/tags", timeout=timeout) as r:
            data = json.loads(r.read())
        return [{"name": m.get("name") or m.get("model"), "size_gb": round((m.get("size") or 0) / 1e9, 2),
                 "params": (m.get("details") or {}).get("parameter_size", "")} for m in data.get("models", [])]
    except (OSError, ValueError, urllib.error.URLError):
        return None


def download_size() -> Optional[int]:
    """実際に取得する容量（配布元の Content-Length）。推測しない。"""
    name = archive_name()
    if not name:
        return None
    try:
        req = urllib.request.Request(f"{RELEASE_BASE}/{name}", method="HEAD")
        with urllib.request.urlopen(req, timeout=8) as r:
            n = int(r.headers.get("Content-Length") or 0)
            return n or None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def status(host: str = DEFAULT_HOST) -> Dict:
    base = {"target_version": ENGINE_VERSION, "engine_dir": display_path(engine_dir()),
            "models_dir": display_path(models_dir())}
    models = probe(host)
    found = resolve_binary()
    if models is not None:
        return {**base, "code": "ready" if models else "no-models", "installed": True, "running": True,
                "engine_version": probe_version(host),
                "managed": bool(found and found["managed"]), "models": models,
                "message": "AIエンジンは動作中です。字幕はこのパソコンの中で処理します。" if models
                else "AIエンジンは動作中ですが、モデルがまだありません。下の一覧から1つ入れてください。"}
    if found:
        return {**base, "code": "installed-not-running", "installed": True, "running": False,
                "managed": found["managed"], "models": [],
                "message": "AIエンジンは用意できていますが、起動していません。"}
    if not archive_name():
        return {**base, "code": "unsupported-platform", "installed": False, "running": False, "managed": False,
                "models": [], "message": "このOSでは、AIエンジンを自動で用意できません。AIなしで整えることはできます。"}
    return {**base, "code": "not-installed", "installed": False, "running": False, "managed": False,
            "models": [], "download_bytes": download_size(),
            "message": "AIエンジンがまだこのパソコンにありません。「AIエンジンを準備」を押すと、アプリが取得して設定します。"}


_START_LOCK = threading.Lock()


def _port_open(host: str) -> bool:
    """エンジンの番号（既定 11434）で、何かが接続を受け付けているか。"""
    u = urllib.parse.urlparse(host)
    try:
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 11434), timeout=0.5):
            return True
    except OSError:
        return False


def start(host: str = DEFAULT_HOST, wait_s: float = 90.0) -> None:
    """エンジンを起動して応答を待つ。動いていれば何もしない（二重起動しない）。
    Windows の初回は、GPU の確認やセキュリティソフトの検査で 30 秒以上かかることがある（実機で約36秒）。"""
    with _START_LOCK:  # 準備と「AIエンジンを起動」が重なっても、起動は1回だけ
        if probe(host) is not None:
            return
        found = resolve_binary()
        if not found:
            raise EngineError("AIエンジンがこのパソコンにありません。先に「AIエンジンを準備」を押してください。")
        env = dict(os.environ, OLLAMA_HOST=host.replace("http://", "").replace("https://", ""))
        kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "env": env}
        if os.name == "nt":
            kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
        else:
            kw["start_new_session"] = True
        proc = subprocess.Popen([found["path"], "serve"], **kw)
        deadline = time.time() + wait_s
        while time.time() < deadline:
            time.sleep(0.5)
            if probe(host) is not None:
                return
            # すぐ終わった：止められたか、起動に失敗した。ただし先に起動したエンジンが番号を使っていれば待ち続ける
            if proc.poll() is not None and not _port_open(host):
                raise EngineError("AIエンジンを起動できませんでした。セキュリティソフトが止めていないか確認し、"
                                  "パソコンを再起動してからもう一度試してください。")
        raise EngineError("AIエンジンの起動に時間がかかっています。少し待ってから「AIエンジンを起動」を押してください。")


# ── 準備（取得 → 照合 → 展開 → 起動） ───────────────────────────────

_WARM_ROOTS = os.name == "nt"  # Windows だけ。テストで切り替える


def _urlopen(url: str, timeout: float):
    """配布元へ接続する。

    Windows は最小限のルート証明書しか持たず、足りない分は OS の通信（schannel）が必要になったときに取りに行く。
    Python（OpenSSL）の通信ではその仕組みが働かないため、初めてのPCでは github.com の証明書を確かめられずに失敗する
    （Parallels の Windows 11 で再現）。検証に失敗したら、OS の仕組みで同じ配布元へ一度つないで証明書をそろえ、もう一度だけ試す。
    """
    try:
        return urllib.request.urlopen(url, timeout=timeout)
    except urllib.error.URLError as e:
        if not _WARM_ROOTS or not isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
            raise
        origin = "{0.scheme}://{0.netloc}/".format(urllib.parse.urlsplit(url))
        try:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            f"[void](Invoke-WebRequest -UseBasicParsing -Method Head '{origin}')"],
                           capture_output=True, timeout=40, creationflags=0x08000000 if os.name == "nt" else 0)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            return urllib.request.urlopen(url, timeout=timeout)
        except urllib.error.URLError as e2:
            if isinstance(getattr(e2, "reason", None), ssl.SSLCertVerificationError):
                raise EngineError("配布元の証明書を確かめられませんでした。セキュリティソフトや会社のネットワークが通信を"
                                  "書き換えていないか確認してください。") from e2
            raise


def _fetch_checksums() -> Dict[str, str]:
    with _urlopen(f"{RELEASE_BASE}/sha256sum.txt", timeout=30) as r:
        text = r.read().decode("utf-8", "replace")
    out = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and len(parts[0]) == 64:
            out[os.path.basename(parts[1].lstrip("*"))] = parts[0].lower()
    return out


def _safe_extract(archive: Path, dest: Path) -> None:
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            for n in z.namelist():
                target = (dest / n).resolve()
                if not str(target).startswith(str(dest.resolve())):
                    raise EngineError("取得したファイルの中身が正しくありません（展開先の外を指しています）。")
            z.extractall(dest)
        return
    r = subprocess.run(["/usr/bin/tar", "-xzf", str(archive), "-C", str(dest)], capture_output=True, text=True)
    if r.returncode != 0:
        raise EngineError(f"展開に失敗しました（{r.stderr.strip()[:200]}）。空きディスク容量を確認してください。")


def _locate_binary(root: Path) -> Optional[Path]:
    direct = root / _binary_name()
    if direct.is_file():
        return direct
    for p in sorted(root.rglob(_binary_name())):
        if p.is_file():
            return p
    return None


def install(host: str = DEFAULT_HOST, progress: Optional[Callable[[str, str, int, int], None]] = None,
            cancelled: Optional[Callable[[], bool]] = None, base_url: str = RELEASE_BASE) -> None:
    """AIエンジンを取得して照合し、アプリの管理フォルダへ置いて起動する。失敗したら途中の物を残さない。"""
    name = archive_name()
    if not name:
        raise EngineError("このOS向けのAIエンジンは自動で用意できません。")
    report = progress or (lambda *a: None)
    work = Path(tempfile.mkdtemp(prefix="jsp-engine-"))
    try:
        report("download", "配布元へ接続しています", 0, 0)
        expected = PINNED_SHA256.get(name)
        published = _fetch_checksums().get(name) if base_url == RELEASE_BASE else expected
        if not expected or published != expected:
            raise EngineError("配布元の照合値が、このアプリに記録した値と一致しません。取得を中止しました。")
        archive = work / name
        h = hashlib.sha256()
        with _urlopen(f"{base_url}/{name}", timeout=60) as r, open(archive, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done, last = 0, 0
            while True:
                if cancelled and cancelled():
                    raise EngineError("中止しました。")
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if done - last > max(total / 200, 1_000_000):
                    last = done
                    report("download", "AIエンジンを取得しています", done, total)
        report("verify", "配布元の照合値と照らし合わせています", done, total)
        if h.hexdigest() != expected:
            raise EngineError("取得したファイルが照合値と一致しませんでした。通信が途中で壊れた可能性があります。もう一度試してください。")
        report("extract", "展開しています", done, total)
        staging = work / "unpacked"
        staging.mkdir()
        _safe_extract(archive, staging)
        if not _locate_binary(staging):
            raise EngineError("展開したあとに、AIエンジンの本体が見つかりませんでした。")
        target = engine_dir()
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(target))  # 本体と関連ファイルをまとめて移す（別ドライブでもコピーして移る）
        exe = _locate_binary(target)
        if os.name != "nt":
            exe.chmod(0o755)
        (target / "engine.json").write_text(json.dumps({
            "version": ENGINE_VERSION, "source": f"{base_url}/{name}", "sha256": expected,
            "binary": str(exe.relative_to(target)),
            "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}, indent=2), encoding="utf-8")
        report("start", "AIエンジンを起動しています", done, total)
        start(host)
        report("done", "AIエンジンの準備ができました", done, total)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ── このPCに合うモデル ─────────────────────────────────────────────

def _memory_gb() -> Optional[float]:
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["/usr/sbin/sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=3).stdout
            return int(out.strip()) / 1024 ** 3
        if os.name == "nt":
            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))  # type: ignore[attr-defined]
            return m.ullTotalPhys / 1024 ** 3
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 1024 ** 2
    except Exception:
        return None
    return None


_machine_cache: Dict = {}


def _run(cmd: List[str], timeout: float = 6) -> Optional[str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _mac_gpus(apple_silicon: bool, notes: List[str]) -> List[Dict]:
    out = _run(["/usr/sbin/system_profiler", "SPDisplaysDataType", "-json"], 12)  # 初回は数秒かかる
    if not out:
        notes.append("GPU情報を取得できませんでした。モデルの判定は搭載メモリだけで行っています。")
        return []
    gpus = []
    try:
        for item in json.loads(out).get("SPDisplaysDataType", []):
            name = str(item.get("sppci_model") or item.get("_name") or "").strip()
            if not name:
                continue
            raw = str(item.get("spdisplays_vram") or item.get("spdisplays_vram_shared") or "")
            import re as _re
            m = _re.search(r"(\d+)\s*(MB|GB)", raw, _re.I)
            vram = round((int(m.group(1)) if m.group(2).upper() == "GB" else int(m.group(1)) / 1024), 1) if m else None
            gpus.append({"name": name, "vram_gb": vram, "kind": "unified" if apple_silicon else "discrete" if vram else "unknown"})
    except ValueError:
        notes.append("GPU情報の読み取りに失敗しました。モデルの判定は搭載メモリだけで行っています。")
    return gpus


def _windows_gpus(notes: List[str]) -> List[Dict]:
    # AdapterRAM は 4GB を超えると正しい値を返さないので、レジストリの qwMemorySize を先に読む（Cutline Studio と同じ）
    script = ("$ErrorActionPreference='SilentlyContinue';$out=@();"
              "$key='HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Class\\{4d36e968-e325-11ce-bfc1-08002be10318}';"
              "foreach($c in Get-CimInstance Win32_VideoController){$bytes=$null;"
              "foreach($sub in Get-ChildItem $key){$p=Get-ItemProperty $sub.PSPath;"
              "if($p.DriverDesc -eq $c.Name -and $p.'HardwareInformation.qwMemorySize'){$bytes=[int64]$p.'HardwareInformation.qwMemorySize'}}"
              "if(-not $bytes -and $c.AdapterRAM -gt 0){$bytes=[int64]$c.AdapterRAM};"
              "$out+=[pscustomobject]@{name=$c.Name;bytes=$bytes}};"
              "@($out) | ConvertTo-Json -Depth 3 -Compress")
    out = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], 15)
    if not out:
        notes.append("GPU情報を取得できませんでした。モデルの判定は搭載メモリだけで行っています。")
        return []
    try:
        data = json.loads(out)
        items = data if isinstance(data, list) else [data]
        gpus = []
        import re as _re
        for it in items:
            if not it or not it.get("name"):
                continue
            vram = round(it["bytes"] / 1024 ** 3, 1) if it.get("bytes") else None
            integrated = bool(_re.search(r"intel|uhd|iris|radeon\(tm\) graphics|vega \d+ graphics", it["name"], _re.I))
            gpus.append({"name": it["name"], "vram_gb": vram,
                         "kind": "integrated" if integrated else "discrete" if vram else "unknown"})
        return gpus
    except ValueError:
        notes.append("GPU情報の読み取りに失敗しました。モデルの判定は搭載メモリだけで行っています。")
        return []


def _disk_free_gb(target: Path, notes: List[str]) -> Optional[float]:
    """モデルの保存先はまだ無いことがあるので、親フォルダへ遡って同じドライブの空きを測る。"""
    cur = target
    for _ in range(60):
        try:
            return round(shutil.disk_usage(cur).free / 1024 ** 3, 1)
        except OSError:
            if cur.parent == cur:
                break
            cur = cur.parent
    notes.append("空きディスク容量を取得できませんでした。")
    return None


def machine(force: bool = False) -> Dict:
    """このPCの性能を実測する（Cutline Studio の system-profile と同じ考え方）。

    取れなかった項目は None のまま返す。0や適当な既定値で埋めない（動かないモデルを勧めてしまうため）。
    """
    if not force and _machine_cache.get("at", 0) > time.time() - 60:
        return _machine_cache["profile"]
    notes: List[str] = []
    mem = _memory_gb()
    apple = None
    if sys.platform == "darwin":
        brand = (_run(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"]) or "").strip()
        apple = brand if brand.startswith("Apple ") else None
        gpus = _mac_gpus(bool(apple), notes)
    elif os.name == "nt":
        gpus = _windows_gpus(notes)
    else:
        gpus = []
        notes.append("このOSではGPU情報を調べていません。モデルの判定は搭載メモリだけで行っています。")
    dedicated = [g["vram_gb"] for g in gpus if g.get("vram_gb") and g["kind"] == "discrete"]
    kind = "unified" if apple else "dedicated" if dedicated else "unknown"
    graphics = round(mem, 1) if apple and mem else (max(dedicated) if dedicated else None)
    if mem is None:
        notes.append("搭載メモリを取得できませんでした。")
    profile = {"memory_gb": round(mem, 1) if mem else None, "apple_silicon": apple, "gpus": gpus,
               "graphics_memory_gb": graphics, "graphics_memory_kind": kind,
               "disk_free_gb": _disk_free_gb(models_dir(), notes), "notes": notes}
    _machine_cache.update(profile=profile, at=time.time())
    return profile


def fit(model: Dict, m: Dict, engine_version: Optional[str] = None) -> Dict:
    """このPCでそのモデルがどうなるか（Cutline Studio の fitForMachine と同じ判定）。

    判定に使うのは「実際に載せられるメモリ」：統合メモリ機は搭載メモリ、専用GPU機はVRAM。
    VRAMを超えた分はCPU側へこぼれて極端に遅くなるので、専用GPU機ではVRAMを主に見る。
    """
    need = version_tuple(model.get("min_engine"))
    have = version_tuple(engine_version)
    if need and have and have < need:
        return {"level": "unsupported", "label": "AIエンジンの更新が必要", "disk": None,
                "reason": f"動いている Ollama は {engine_version} です。このモデルは {model['min_engine']} 以上で確かめています"
                          "（古い版では返答が崩れます）。Ollama を更新してください。"}
    need_disk = model["download_gb"] * 1.15
    disk = None
    if m.get("disk_free_gb") is not None and m["disk_free_gb"] < need_disk:
        disk = f"空きディスクが{m['disk_free_gb']:g}GBです。このモデルには{int(need_disk + 0.99)}GBほどの空きが必要です。"
    mem = m.get("memory_gb")
    if mem is None:
        return {"level": "unknown", "label": "このPCのメモリを調べられませんでした", "disk": disk}
    dedicated = m.get("graphics_memory_kind") == "dedicated" and m.get("graphics_memory_gb") is not None
    usable = m["graphics_memory_gb"] if dedicated else mem
    if mem < model["runtime_gb"] + 1:
        return {"level": "unsupported", "label": "このPCでは動きません", "disk": disk,
                "reason": f"実行中に約{model['runtime_gb']}GBのメモリを使います（このPCは{mem:g}GB）。起動できないか、途中で止まる可能性が高いです。"}
    if usable < model["runtime_gb"] + 2:
        return {"level": "tight", "label": "動作が遅くなる可能性", "disk": disk,
                "reason": "メモリ／VRAMを多く使います。遅くなるか、ほかのアプリを閉じないと起動できない可能性があります。"}
    if mem >= model["recommended_memory_gb"] and (not dedicated or m["graphics_memory_gb"] >= model["recommended_vram_gb"]):
        return {"level": "recommended", "label": "このPCに適しています", "disk": disk,
                "reason": f"推奨の目安（メモリ{model['recommended_memory_gb']}GB" + (f"・VRAM{model['recommended_vram_gb']}GB" if dedicated else "") + "）を満たしています。"}
    return {"level": "usable", "label": "動きますが余裕は少なめ", "disk": disk,
            "reason": f"動作しますが、推奨の目安（メモリ{model['recommended_memory_gb']}GB）を下回ります。長い字幕では待ち時間が伸びます。"}


def catalog_for(m: Dict, engine_version: Optional[str] = None) -> List[Dict]:
    out = [dict(model, fit=fit(model, m, engine_version)) for model in CATALOG]
    limit = [c["id"] for c in CATALOG].index(RECOMMEND_UP_TO)
    allowed = out[:limit + 1]
    pick = ([x for x in allowed if x["fit"]["level"] == "recommended" and not x["fit"]["disk"]]
            or [x for x in allowed if x["fit"]["level"] == "usable" and not x["fit"]["disk"]]
            or allowed[:1])
    rec = pick[-1]["id"]
    for x in out:
        x["recommended"] = x["id"] == rec
    return out


def is_catalog_model(name: str) -> bool:
    return any(c["id"] == name for c in CATALOG)
