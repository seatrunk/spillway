"""Tests for the local real-session replay (#20): extraction, arms, scoring and the no-raw-content guarantee.

Every transcript here is synthetic and built in a temporary home. The SENTINEL strings stand in for the
private things real transcripts hold (prompts, commands, project paths, tool output); the report must
never contain any of them.
"""
import collections
import contextlib
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks/real"))
import real_replay as rr  # noqa: E402

SENTINELS = ("SENTINEL_PROMPT", "SENTINEL_COMMAND", "SENTINEL_BODY", "SENTINEL_PROJECT", "SENTINEL_CODEX_INPUT",
             "SENTINEL_AGY_PROMPT", "SENTINEL_AGY_CMD", "acme_mid", "acme_tail", "acme_secret")
CAP = 2048


def build_log(mid="src/acme_mid.rs:12:5", tail="src/acme_tail.rs:99:1"):
    lines = ["compiling unit %04d SENTINEL_BODY" % i for i in range(400)]
    lines[200] = "error[E0308]: mismatched types --> " + mid
    lines.append("error: aborting due to previous error at " + tail)
    return "\n".join(lines)


def write_jsonl(path, objs):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(o) + "\n" for o in objs), encoding="utf-8")


def claude_lines():
    def use(tid, name):
        return {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": name, "input": {"command": "SENTINEL_COMMAND"}}]}}

    def result(tid, content, error=False):
        return {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "is_error": error, "content": content}]}}

    failing = result("t1", "Exit code 2\n" + build_log(), True)
    return [
        {"type": "user", "message": {"role": "user", "content": "SENTINEL_PROMPT please fix it"}},
        use("t1", "Bash"), failing,
        use("t2", "Read"), result("t2", [{"type": "text", "text": "     1\tprint('SENTINEL_BODY')"}]),
        use("t3", "Edit"), result("t3", "The file has been updated SENTINEL_BODY"),
        failing,  # the same result copied into a resumed session
        use("t4", "Bash"), result("t4", "ok SENTINEL_BODY"),
    ]


def codex_lines():
    exec_out = [{"type": "text", "text": "Script completed\nWall time 1.0 seconds\nOutput:"},
                {"type": "text", "text": build_log("src/acme_secret/x.py:7", "src/acme_secret/y.py:8")}]
    json_out = json.dumps({"output": build_log(), "metadata": {"exit_code": 3, "duration_seconds": 0.1}})
    return [
        {"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "c1", "name": "exec",
                                              "input": "SENTINEL_CODEX_INPUT"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c1", "output": exec_out}},
        {"type": "response_item", "payload": {"type": "function_call", "call_id": "c2", "name": "shell",
                                              "arguments": "{}"}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c2", "output": json_out}},
        {"type": "response_item", "payload": {"type": "function_call", "call_id": "c3", "name": "apply_patch"}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c3", "output": "done"}},
    ]


def agy_files(base):
    logs = base / "brain" / "00000000-0000-4000-8000-000000000001" / ".system_generated" / "logs"
    plan = {"step_index": 1, "type": "PLANNER_RESPONSE", "tool_calls": [
        {"name": "run_command", "args": {"CommandLine": "SENTINEL_AGY_CMD"}},
        {"name": "read_file", "args": {"Path": "/work/acme_secret/f.py"}}]}
    shell = {"step_index": 2, "type": "GENERIC", "content":
             "Created At: t\nCommand exited with code 7.\n\nCommand output:\n" + build_log()}
    read = {"step_index": 3, "type": "GENERIC", "content": "Showing lines 1-3 of 3\n1: SENTINEL_BODY"}
    write_jsonl(logs / "chunks" / "transcript_full" / "a.jsonl", [
        {"step_index": 0, "type": "USER_INPUT", "content": "SENTINEL_AGY_PROMPT"}, plan, shell, read])
    # A truncated copy: step 2 duplicates the full one, step 4 exists only here and must be skipped.
    write_jsonl(logs / "b.jsonl", [
        dict(shell, content="Command exited with code 7.\n[truncated]", truncated_fields=["content"]),
        {"step_index": 4, "type": "GENERIC", "content": "truncated only", "truncated_fields": ["content"]},
    ])


def make_home(tmp):
    home = Path(tmp)
    write_jsonl(home / ".claude/projects/-work-SENTINEL_PROJECT/session.jsonl", claude_lines())
    write_jsonl(home / ".codex/sessions/2026/11/26/rollout.jsonl", codex_lines())
    agy_files(home / ".gemini/antigravity-cli")
    return home


def scan():
    return {h: collections.Counter() for h in rr.HARNESSES}


class Extraction(unittest.TestCase):
    def test_per_harness_results_kinds_exit_codes_and_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(tmp)
            roots = rr._roots(type("A", (), {"home": str(home), "claude_dir": [], "codex_dir": [], "agy_dir": []})())
            stats = scan()
            claude = list(rr.read_claude(roots["claude_code"], stats["claude_code"]))
            codex = list(rr.read_codex(roots["codex"], stats["codex"]))
            agy = list(rr.read_agy(roots["agy"], stats["agy"]))
        self.assertEqual([(r.kind, r.exit_code) for r in claude], [("shell", 2), ("file_read", None), ("shell", None)])
        self.assertEqual([(r.kind, r.exit_code) for r in codex], [("shell", None), ("shell", 3)])
        self.assertEqual([(r.kind, r.exit_code) for r in agy], [("shell", 7), ("file_read", None)])
        self.assertEqual(stats["claude_code"]["duplicates_skipped"], 1)
        self.assertEqual(stats["claude_code"]["results_not_scored"], 1)  # the Edit result
        self.assertEqual(stats["codex"]["results_not_scored"], 1)  # apply_patch
        self.assertEqual(stats["agy"]["steps_truncated_skipped"], 1)  # the only-truncated step; the copy lost to transcript_full
        self.assertEqual(stats["claude_code"]["files_read"], 1)


class Arms(unittest.TestCase):
    def test_spillway_keeps_what_head_truncation_loses_within_the_cap(self):
        failing = rr.Result("claude_code", "shell", "Exit code 2\n" + build_log(), 2)
        for arm in rr.ARMS:
            visible, outcome = rr.run_arm(arm, failing, CAP)
            if arm != "none":
                self.assertLessEqual(len(visible.encode()), CAP, arm)
        none_vis, _ = rr.run_arm("none", failing, CAP)
        trunc_vis, _ = rr.run_arm("truncate", failing, CAP)
        spill_vis, outcome = rr.run_arm("spillway", failing, CAP)
        self.assertEqual(none_vis, failing.text)
        self.assertEqual(outcome.mode, "preview")
        self.assertTrue(outcome.receipt["verified"])
        mid = "src/acme_mid.rs:12"
        self.assertTrue(rr.location_visible(none_vis, mid))
        self.assertFalse(rr.location_visible(trunc_vis, mid))
        self.assertTrue(rr.location_visible(spill_vis, mid))
        tail_exit = rr.Result("codex", "shell", json.dumps({"output": build_log(), "metadata": {"exit_code": 3}}), 3)
        self.assertFalse(rr.exit_code_visible(rr.run_arm("truncate", tail_exit, CAP)[0], 3))
        self.assertTrue(rr.exit_code_visible(rr.run_arm("spillway", tail_exit, CAP)[0], 3))

    def test_small_results_pass_through_unchanged_in_every_arm(self):
        small = rr.Result("codex", "shell", "all good\n", None)
        for arm in rr.ARMS:
            self.assertEqual(rr.run_arm(arm, small, CAP)[0], "all good\n")

    def test_reference_locations_forms_order_and_limit(self):
        text = "\n".join(["a.py:1", 'File "b.py", line 2', "c.ts(3,4)", "a.py:1", "d.go:4:2", "e.rs:5", "f.c:6", "x.com:443"])
        refs = rr.ref_locations(text)
        self.assertEqual(len(refs), 5)
        self.assertNotIn("x.com:443", refs)
        self.assertEqual(refs, ["b.py:2", "c.ts:3", "d.go:4", "e.rs:5", "f.c:6"])
        self.assertTrue(rr.location_visible("see b.py:2 now", "b.py:2"))
        self.assertTrue(rr.location_visible('File "b.py", line 2, in f', "b.py:2"))
        self.assertTrue(rr.location_visible("c.ts(3,4): error", "c.ts:3"))
        self.assertFalse(rr.location_visible("b.py:21", "b.py:2"))
        self.assertFalse(rr.location_visible("xb.py:2", "b.py:2"))

    def test_exit_code_visibility_forms(self):
        for text in ("Exit code 2", "exit_code=2", '"exit_code": 2', "Command exited with code 2.", "returncode 2"):
            self.assertTrue(rr.exit_code_visible(text, 2), text)
        for text in ("exit code 22", "code 2", "exit_code=3"):
            self.assertFalse(rr.exit_code_visible(text, 2), text)


class Report(unittest.TestCase):
    def run_main(self, home, out, *extra):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = rr.main(["--home", str(home), "--out", str(out), "--caps", str(CAP), *extra])
        return rc, buf.getvalue()

    def test_report_and_stdout_contain_no_raw_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(Path(tmp) / "home")
            out = Path(tmp) / "out" / "report.json"
            rc, stdout = self.run_main(home, out)
            written = out.read_text(encoding="utf-8")
        self.assertEqual(rc, 0)
        forbidden = list(SENTINELS) + [str(home), str(tmp), "/home/", "Exit code", "compiling unit", ".system_generated"]
        host = socket.gethostname()
        if len(host) > 3:
            forbidden.append(host)
        for blob, label in ((stdout, "stdout"), (written, "report file")):
            for needle in forbidden:
                self.assertNotIn(needle, blob, "%s leaks %r" % (label, needle))
        report = json.loads(written)
        rr.assert_aggregate_only(report)
        self.assertTrue(report["aggregate_only"])
        self.assertEqual(report["model_calls"], 0)

    def test_aggregate_numbers_on_the_synthetic_corpus(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(Path(tmp) / "home")
            out = Path(tmp) / "report.json"
            self.run_main(home, out)
            report = json.loads(out.read_text(encoding="utf-8"))
        harnesses = report["harnesses"]
        self.assertEqual({h: harnesses[h]["tool_results"] for h in rr.HARNESSES}, {"claude_code": 3, "codex": 2, "agy": 2})
        self.assertEqual(harnesses["claude_code"]["tool_results_seen"], 4)
        self.assertEqual(harnesses["claude_code"]["with_exit_code"], 1)
        self.assertEqual({h: harnesses[h]["status"] for h in rr.HARNESSES}, {h: "read" for h in rr.HARNESSES})
        arms = report["caps"][str(CAP)]["all"]["arms"]
        self.assertEqual(report["caps"][str(CAP)]["all"]["tool_results"], 7)
        self.assertLess(arms["spillway"]["bytes"], arms["none"]["bytes"])
        self.assertEqual(arms["none"]["locations"]["refs_kept"], arms["none"]["locations"]["refs"])
        self.assertEqual(arms["spillway"]["locations"]["refs_kept"], arms["spillway"]["locations"]["refs"])
        self.assertLess(arms["truncate"]["locations"]["refs_kept"], arms["truncate"]["locations"]["refs"])
        self.assertEqual(arms["spillway"]["exit_code"]["kept"], arms["spillway"]["exit_code"]["applicable"])
        self.assertLess(arms["truncate"]["exit_code"]["kept"], arms["truncate"]["exit_code"]["applicable"])
        self.assertEqual(arms["spillway"]["verified"], 7)
        self.assertGreater(arms["spillway"]["modes"]["preview"], 0)

    def test_missing_transcripts_yield_an_empty_report_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.json"
            rc, stdout = self.run_main(Path(tmp) / "empty-home", out)
            report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        self.assertEqual({h: report["harnesses"][h]["status"] for h in rr.HARNESSES}, {h: "not_found" for h in rr.HARNESSES})
        self.assertEqual(report["caps"][str(CAP)]["all"]["tool_results"], 0)
        self.assertIsNone(report["caps"][str(CAP)]["all"]["arms"]["spillway"]["bytes_saved_pct"])

    def test_guard_rejects_free_text_unknown_keys_and_objects(self):
        for bad in ({"tool_results": "SENTINEL_BODY"}, {"SENTINEL_PROJECT": 1}, {"harnesses": ["/some/path"]},
                    {"tool_results": object()}):
            with self.assertRaises(ValueError):
                rr.assert_aggregate_only(bad)
        try:
            rr.assert_aggregate_only({"tool_results": "SENTINEL_BODY"})
        except ValueError as exc:
            self.assertNotIn("SENTINEL_BODY", str(exc))
        rr.assert_aggregate_only({"caps": {"16384": {"all": {"tool_results": 1, "arms": {"none": {"bytes": 3}}}}}})

    def test_caps_below_the_core_floor_are_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            rr.main(["--caps", "100", "--home", tempfile.gettempdir()])


if __name__ == "__main__":
    unittest.main()
