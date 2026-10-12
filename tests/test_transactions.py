"""反映失敗・中止の後も、最後に検査を通った結果を使えることを確かめる。"""
from __future__ import annotations

import base64
import json
import http.cookiejar
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import app as appmod

SRT = b"1\n00:00:01,000 --> 00:00:04,000\nHello world\n"


class TransactionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jsp-transaction-")
        self.app = appmod.App(Path(self.tmp.name))
        self.app.notify = False
        job = self.app.create({"filename": "test.srt", "data": base64.b64encode(SRT).decode()})
        self.id = job["id"]
        self.app._work(self.id, "basic", None)
        self.assertEqual(self.app.load(self.id)["status"], "done")
        self.dir = self.app.job_dir(self.id)
        self.before = self.snapshot()

    def tearDown(self):
        self.tmp.cleanup()

    def snapshot(self):
        return {p.name: p.read_bytes() for p in self.dir.glob("test.*") if ".v" not in p.name}

    def decisions(self):
        doc = json.loads((self.dir / "test.candidates.json").read_text(encoding="utf-8"))
        doc["candidates"] = [{"id": "human-1", "index": 1, "scope": "cue", "type": "手動修正",
                              "original": "Hello world", "proposed": "Hello there", "reason": "利用者の確認",
                              "evidence": "利用者の入力", "status": "apply", "approved": True}]
        return doc

    def test_timeout_keeps_candidates_and_all_outputs(self):
        with patch.object(self.app, "run_script", side_effect=subprocess.TimeoutExpired("apply", 300)):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.app.decisions(self.id, self.decisions())
        self.assertEqual(self.snapshot(), self.before)
        self.assertEqual(self.app.load(self.id)["status"], "done")
        self.assertTrue(self.app.decisions(self.id, self.decisions())["verify_ok"])

    def test_nonzero_exit_never_reports_success_or_replaces_outputs(self):
        for code in (1, 2):
            with self.subTest(code=code):
                with patch.object(self.app, "run_script", return_value=subprocess.CompletedProcess([], code, "NG", "")):
                    with self.assertRaises(ValueError):
                        self.app.decisions(self.id, self.decisions())
                self.assertEqual(self.snapshot(), self.before)

    def test_failed_relayout_keeps_previous_settings_and_outputs(self):
        old = self.app.load(self.id)
        with patch.object(self.app, "run_script", side_effect=subprocess.TimeoutExpired("apply", 300)):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.app.relayout(self.id, {"font_px": 150})
        self.assertEqual(self.app.load(self.id).get("font_px"), old.get("font_px"))
        self.assertEqual(self.snapshot(), self.before)

    def test_incomplete_output_bundle_is_refused(self):
        with patch.object(self.app, "run_script", return_value=subprocess.CompletedProcess([], 0, "", "")):
            with self.assertRaises(ValueError):
                self.app.decisions(self.id, self.decisions())
        self.assertEqual(self.snapshot(), self.before)

    def test_cancel_during_apply_keeps_previous_result_and_can_resume_review(self):
        real_run = self.app.run_script

        def cancel_at_end(job, script, args, **kw):
            result = real_run(job, script, args, **kw)
            self.app.cancel(self.id)
            return result

        with patch.object(self.app, "run_script", side_effect=cancel_at_end):
            self.app._work(self.id, "basic", None)
        self.assertEqual(self.app.load(self.id)["status"], "cancelled")
        self.assertEqual(self.snapshot(), self.before)
        self.assertTrue(self.app.decisions(self.id, self.decisions())["verify_ok"])

    def test_failed_ai_rerun_restores_previous_candidates(self):
        def changed_then_failed(*args, **kwargs):
            (self.dir / "test.candidates.json").write_text(json.dumps(self.decisions()), encoding="utf-8")
            raise appmod.local_ai.OllamaError("AIの応答が止まりました")

        with patch.object(appmod.local_ai, "suggest", side_effect=changed_then_failed):
            self.app._work(self.id, "ai", "fake")
        self.assertEqual(self.app.load(self.id)["status"], "error")
        self.assertEqual(self.snapshot(), self.before)
        self.assertTrue(self.app.decisions(self.id, self.decisions())["verify_ok"])

    def test_two_simultaneous_decisions_keep_distinct_backups_and_matching_result(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.app.decisions(self.id, self.decisions()), range(2)))
        self.assertTrue(all(r["verify_ok"] for r in results))
        self.assertEqual(len(list(self.dir.glob("test.candidates.v*.json"))), 2)
        self.assertIn(b"Hello there", (self.dir / "test.polished.srt").read_bytes())
        self.assertTrue(appmod.verify(str(self.dir / "test.srt"), str(self.dir / "test.polished.srt"),
                                     str(self.dir / "test.apply-log.json"))["ok"])

    def test_partial_publish_failure_restores_the_whole_bundle(self):
        real_replace = appmod.os.replace
        failed = False

        def fail_once(src, dst):
            nonlocal failed
            if not failed and Path(src).parent.name.startswith(".apply-") and Path(src).name.endswith("review.md"):
                failed = True
                raise OSError("書き込み失敗の再現")
            return real_replace(src, dst)

        with patch.object(appmod.os, "replace", side_effect=fail_once):
            with self.assertRaises(OSError):
                self.app.decisions(self.id, self.decisions())
        self.assertTrue(failed)
        self.assertEqual(self.snapshot(), self.before)
        self.assertFalse(list(self.dir.glob(".apply-*")))

    def test_known_incompatible_model_is_refused_before_start(self):
        self.app.ai.enabled = True
        with patch.object(self.app.ai, "ensure_running"), \
                patch.object(appmod.local_engine, "probe", return_value=[{"name": "qwen3.5:9b"}]), \
                patch.object(appmod.local_engine, "probe_version", return_value="0.30.11"):
            with self.assertRaisesRegex(ValueError, "更新"):
                self.app.start(self.id, "ai", "qwen3.5:9b")
        self.assertEqual(self.app.load(self.id)["status"], "done")
        self.assertEqual(self.snapshot(), self.before)

    def test_decisions_without_metadata_keep_source_hash(self):
        before = json.loads((self.dir / "test.candidates.json").read_text(encoding="utf-8"))
        self.assertTrue(before.get("source_sha256"))
        self.assertTrue(self.app.decisions(self.id, {"candidates": self.decisions()["candidates"]})["verify_ok"])
        after = json.loads((self.dir / "test.candidates.json").read_text(encoding="utf-8"))
        self.assertEqual(after.get("source_sha256"), before["source_sha256"])
        self.assertEqual(after.get("schema"), before.get("schema"))
        self.assertNotIn("source_sha256 がない", self.app.load(self.id)["message"])

    def test_job_file_read_and_replace_retry_after_transient_denial(self):
        # Windows で見られた、置き換えの瞬間の Errno 13 を再現する（1回目だけ拒否、2回目で成功）
        real_read, real_replace = Path.read_text, appmod.os.replace
        denied = {"read": 0, "replace": 0}

        def flaky_read(self_, *a, **kw):
            if self_.name == "job.json" and denied["read"] == 0:
                denied["read"] += 1
                raise PermissionError(13, "Permission denied", str(self_))
            return real_read(self_, *a, **kw)

        def flaky_replace(src, dst):
            if Path(dst).name == "job.json" and denied["replace"] == 0:
                denied["replace"] += 1
                raise PermissionError(13, "Permission denied", str(dst))
            return real_replace(src, dst)

        with patch.object(Path, "read_text", flaky_read), patch.object(appmod.os, "replace", flaky_replace):
            self.assertEqual(self.app.load(self.id)["status"], "done")
            self.assertEqual(self.app.update(self.id, message="再試行の確認")["message"], "再試行の確認")
        self.assertEqual(denied, {"read": 1, "replace": 1})
        self.assertEqual(self.app.load(self.id)["message"], "再試行の確認")

    def test_download_refuses_a_modified_result(self):
        output = self.dir / "test.polished.srt"
        output.write_bytes(output.read_bytes().replace(b"00:00:01,000", b"00:00:01,001"))
        server = ThreadingHTTPServer(("127.0.0.1", 0), None)
        port = server.server_address[1]
        server.RequestHandlerClass = appmod.make_handler(self.app, port)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        try:
            opener.open(base + "/?t=" + self.app.token).read()
            with self.assertRaises(urllib.error.HTTPError) as err:
                opener.open(base + f"/jobs/{self.id}/download")
            self.assertEqual(err.exception.code, 400)
            self.assertIn("書き出しを止めました", err.exception.read().decode())
        finally:
            server.shutdown()
            server.server_close()


class PullRetryTest(unittest.TestCase):
    """モデル取得の digest mismatch（Windows 実機の qwen2.5:14b で1回起きた）は、1回だけ自動で取り直す。"""

    def setUp(self):
        self.ai = appmod.LocalAI(enabled=False)
        self.ai.pull = {"model": "qwen2.5:14b", "status": "pulling", "done": 0, "total": 0, "error": ""}

    def test_digest_mismatch_is_retried_once(self):
        calls = []

        def once(model):
            calls.append(model)
            if len(calls) == 1:
                raise RuntimeError("digest mismatch, file must be downloaded again: want sha256:aa, got sha256:bb")

        with patch.object(self.ai, "_pull_once", side_effect=once):
            self.ai._pull("qwen2.5:14b")
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.ai.pull["status"], "done")
        self.assertEqual(self.ai.pull["error"], "")

    def test_second_mismatch_is_explained_in_japanese(self):
        with patch.object(self.ai, "_pull_once", side_effect=RuntimeError("digest mismatch, want sha256:aa, got sha256:bb")) as once:
            self.ai._pull("qwen2.5:14b")
        self.assertEqual(once.call_count, 2)
        self.assertEqual(self.ai.pull["status"], "error")
        self.assertIn("もう一度「入れる」を押してください", self.ai.pull["error"])

    def test_other_errors_are_not_retried(self):
        with patch.object(self.ai, "_pull_once", side_effect=RuntimeError("pull model manifest: file does not exist")) as once:
            self.ai._pull("qwen2.5:14b")
        self.assertEqual(once.call_count, 1)
        self.assertEqual(self.ai.pull["status"], "error")


if __name__ == "__main__":
    unittest.main()
