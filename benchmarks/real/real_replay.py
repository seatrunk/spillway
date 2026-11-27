"""Real-session replay: measure the Spillway core on tool results recorded in local agent transcripts.

Reads the transcripts that already exist on this machine (Claude Code, Codex, Antigravity ``agy``),
extracts shell and file-read tool results with the exit code when the harness records one, and runs
three arms over each result at the same byte cap:

  none       the recorded result as is.
  truncate   a plain head cut at the cap.
  spillway   ``spillway.apply_budget`` in enforce semantics (what the MCP proxy delivers).

Per arm it scores the bytes the model would see, whether the exit code survives, and whether up to 5
``file:line`` locations from the original survive. Locations are found by this script's own regexes,
not by ``spillway.facts``, so the scoring does not use the code under test.

PRIVACY CONTRACT. Raw transcript content is held in memory only while one result is scored. The report
is built from counters, and ``assert_aggregate_only`` rejects anything that is not a number, a boolean,
null, a known key or a fixed vocabulary word before anything is printed or written. File paths, tool
names, prompts and tool output never reach stdout, stderr (except for a user-supplied option) or disk.

Usage:
    python3 benchmarks/real/real_replay.py [--caps 16384,4096] [--out benchmarks/real/results/real-replay.json]
"""
import argparse
import collections
import json
from pathlib import Path
import re
import sys
from typing import Dict, Iterator, List, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from spillway import MIN_RESULT_CAP, Policy, __version__, apply_budget  # noqa: E402

SCHEMA = "spillway-real-replay/1"
HARNESSES = ("claude_code", "codex", "agy")
KINDS = ("shell", "file_read")
ARMS = ("none", "truncate", "spillway")
MODES = ("full", "preview", "marker", "withheld", "failclosed", "control")
DEFAULT_CAPS = (16384, 4096)
MAX_LOCATIONS = 5
DEFAULT_OUT = Path(__file__).with_name("results") / "real-replay.json"

# --- Aggregate-only guard -------------------------------------------------------------------------

_SCAN_KEYS = ("files_read", "files_unreadable", "lines_skipped", "duplicates_skipped",
              "results_not_scored", "steps_truncated_skipped")
ALLOWED_KEYS = frozenset(
    {"schema", "aggregate_only", "model_calls", "spillway_version", "caps_bytes", "max_locations",
     "harnesses", "caps", "status", "tool_results", "tool_results_seen", "by_kind", "with_exit_code", "with_locations",
     "original_bytes", "over_cap", "scan", "all", "by_harness", "arms", "bytes", "bytes_saved_pct",
     "results_changed", "exit_code", "applicable", "kept", "kept_pct", "locations", "results_with_refs",
     "results_all_kept", "all_kept_pct", "refs", "refs_kept", "refs_kept_pct", "modes", "verified"}
    | set(_SCAN_KEYS) | set(HARNESSES) | set(KINDS) | set(ARMS) | set(MODES))
ALLOWED_STRINGS = frozenset({SCHEMA, __version__, "read", "not_found"})


def assert_aggregate_only(node) -> None:
    """Raises ``ValueError`` unless ``node`` holds only numbers, booleans, null and fixed vocabulary.

    The error never echoes the offending value, so a failed check cannot leak what it rejected.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if not (key in ALLOWED_KEYS or (isinstance(key, str) and key.isdigit())):
                raise ValueError("report contains an unknown key")
            assert_aggregate_only(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            assert_aggregate_only(value)
    elif isinstance(node, str):
        if node not in ALLOWED_STRINGS:
            raise ValueError("report contains a free-text value")
    elif node is not None and not isinstance(node, (bool, int, float)):
        raise ValueError("report contains an unsupported value type")


# --- Extraction -----------------------------------------------------------------------------------

class Result:
    """One recorded tool result. Lives only in memory while it is scored."""
    __slots__ = ("harness", "kind", "text", "exit_code")

    def __init__(self, harness: str, kind: str, text: str, exit_code: Optional[int]):
        self.harness, self.kind, self.text, self.exit_code = harness, kind, text, exit_code


def _clean(text: str) -> str:
    """Replaces lone surrogates (valid in JSON, not in UTF-8) so every later encode is safe."""
    return text.encode("utf-8", "replace").decode("utf-8")


def _files(root: Path, pattern: str, stats: collections.Counter) -> List[Path]:
    try:
        return sorted(p for p in root.rglob(pattern) if p.is_file())
    except OSError:
        stats["files_unreadable"] += 1
        return []


def _json_lines(path: Path, stats: collections.Counter, needles: tuple = ()) -> Iterator[dict]:
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        stats["files_unreadable"] += 1
        return
    with fh:
        stats["files_read"] += 1
        try:
            for line in fh:
                if not line.strip() or (needles and not any(n in line for n in needles)):
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, RecursionError):
                    stats["lines_skipped"] += 1
                    continue
                if isinstance(obj, dict):
                    yield obj
        except OSError:
            stats["files_unreadable"] += 1


def _text_of(content) -> str:
    """Joins the text parts of a recorded result; non-text parts (images, references) are ignored."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return _text_of(content.get("content", content.get("text", "")))
    if isinstance(content, list):
        return "\n".join(_text_of(p) for p in content if isinstance(p, (str, dict)) and (
            isinstance(p, str) or p.get("type") in (None, "text", "output_text", "input_text")))
    return ""


_CLAUDE_EXIT = re.compile(r"Exit code (-?\d+)(?:\n|$)")
_CODEX_EXIT = re.compile(r"(?:Exit code:|Process exited with code) (-?\d+)")
_AGY_EXIT = re.compile(r"Command exited with code (-?\d+)")


def _head(text: str, n: int = 2000) -> str:
    return text[:n]


def read_claude(roots: List[Path], stats: collections.Counter) -> Iterator[Result]:
    """``~/.claude/projects/**/*.jsonl``: ``tool_use`` blocks name the tool, ``tool_result`` blocks carry the text.

    A failed Bash call starts with ``Exit code N``; a successful one records no code.
    """
    names: Dict[str, str] = {}
    seen = set()
    kinds = {"Bash": "shell", "Read": "file_read"}
    for root in roots:
        for path in _files(root, "*.jsonl", stats):
            for obj in _json_lines(path, stats, ('"tool_use"', '"tool_result"')):
                message = obj.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if obj.get("type") == "assistant" and block.get("type") == "tool_use":
                        if isinstance(block.get("id"), str):
                            names[block["id"]] = block.get("name")
                    elif obj.get("type") == "user" and block.get("type") == "tool_result":
                        tid = block.get("tool_use_id")
                        if not isinstance(tid, str) or tid in seen:
                            stats["duplicates_skipped"] += 1
                            continue
                        seen.add(tid)
                        kind = kinds.get(names.get(tid))
                        if kind is None:
                            stats["results_not_scored"] += 1
                            continue
                        text = _clean(_text_of(block.get("content")))
                        match = _CLAUDE_EXIT.match(text) if kind == "shell" else None
                        yield Result("claude_code", kind, text, int(match.group(1)) if match else None)


_CODEX_SHELL = frozenset({"exec", "exec_command", "shell", "shell_command", "local_shell", "container.exec", "bash"})
_CODEX_READ = frozenset({"read_file", "view_file"})


def _codex_exit(text: str) -> Optional[int]:
    match = _CODEX_EXIT.match(text.lstrip())
    if match:
        return int(match.group(1))
    if text.lstrip().startswith("{") and '"exit_code"' in text and len(text) < 2_000_000:
        try:
            meta = json.loads(text).get("metadata")
        except (ValueError, RecursionError, AttributeError):
            return None
        code = meta.get("exit_code") if isinstance(meta, dict) else None
        return code if isinstance(code, int) and not isinstance(code, bool) else None
    return None


def read_codex(roots: List[Path], stats: collections.Counter) -> Iterator[Result]:
    """``~/.codex/sessions/**/*.jsonl``: ``*_call`` items name the tool, ``*_call_output`` items carry the text."""
    names: Dict[str, str] = {}
    seen = set()
    for root in roots:
        for path in _files(root, "*.jsonl", stats):
            for obj in _json_lines(path, stats, ('"call_id"',)):
                payload = obj.get("payload")
                if not isinstance(payload, dict) or not isinstance(payload.get("call_id"), str):
                    continue
                kind_of_item, cid = payload.get("type"), payload["call_id"]
                if kind_of_item in ("function_call", "custom_tool_call", "local_shell_call"):
                    names[cid] = payload.get("name") or "local_shell"
                elif kind_of_item in ("function_call_output", "custom_tool_call_output"):
                    if cid in seen:
                        stats["duplicates_skipped"] += 1
                        continue
                    seen.add(cid)
                    name = names.get(cid)
                    kind = "shell" if name in _CODEX_SHELL else "file_read" if name in _CODEX_READ else None
                    if kind is None:
                        stats["results_not_scored"] += 1
                        continue
                    text = _clean(_text_of(payload.get("output")))
                    yield Result("codex", kind, text, _codex_exit(text) if kind == "shell" else None)


def _agy_conversations(bases: List[Path], stats: collections.Counter) -> Iterator[Path]:
    """Yields ``brain/<id>`` directories that hold ``.system_generated/logs``, found recursively under ``bases``."""
    for base in bases:
        try:
            brains = sorted(p for p in base.rglob("brain") if p.is_dir())
            for brain in brains:
                for conv in sorted(p for p in brain.iterdir() if (p / ".system_generated" / "logs").is_dir()):
                    yield conv
        except OSError:
            stats["files_unreadable"] += 1


def read_agy(bases: List[Path], stats: collections.Counter) -> Iterator[Result]:
    """Antigravity (``agy``): ``brain/<id>/.system_generated/logs/**/*.jsonl`` steps.

    A ``PLANNER_RESPONSE`` lists tool calls and the ``GENERIC`` steps after it are their results, in order.
    The ``transcript_full`` chunks are complete; other copies are used only for steps they do not cover, and
    results marked ``truncated_fields`` are skipped because their size is not the original size.
    """
    seen_convs = set()
    for conv in _agy_conversations(bases, stats):
        if conv.name in seen_convs:
            stats["duplicates_skipped"] += 1
            continue
        seen_convs.add(conv.name)
        logs = conv / ".system_generated" / "logs"
        steps: Dict[int, dict] = {}
        sources = [(logs / "chunks" / "transcript_full", True), (logs / "chunks" / "transcript", False), (logs, False)]
        for directory, complete in sources:
            for path in sorted(directory.glob("*.jsonl")) if directory.is_dir() else []:
                for obj in _json_lines(path, stats):
                    index = obj.get("step_index")
                    if isinstance(index, int) and (index not in steps or complete):
                        steps[index] = obj
        pending: List[Optional[str]] = []
        for index in sorted(steps):
            step = steps[index]
            if step.get("type") == "PLANNER_RESPONSE":
                calls = step.get("tool_calls")
                pending = [c.get("name") for c in calls if isinstance(c, dict)] if isinstance(calls, list) else []
            elif step.get("type") == "GENERIC":
                name = pending.pop(0) if pending else None
                text = step.get("content")
                if step.get("truncated_fields"):
                    stats["steps_truncated_skipped"] += 1
                    continue
                if not isinstance(text, str):
                    stats["results_not_scored"] += 1
                    continue
                text = _clean(text)
                if name is None and "Command exited with code" in _head(text):
                    name = "run_command"
                kind = "shell" if name == "run_command" else "file_read" if name == "read_file" else None
                if kind is None:
                    stats["results_not_scored"] += 1
                    continue
                match = _AGY_EXIT.search(_head(text)) if kind == "shell" else None
                yield Result("agy", kind, text, int(match.group(1)) if match else None)


# --- Scoring --------------------------------------------------------------------------------------

_EXT = (r"(?:py|pyi|ts|tsx|mts|cts|js|jsx|mjs|cjs|vue|svelte|rs|go|c|cc|cpp|cxx|h|hpp|cs|java|kt|swift|"
        r"scala|rb|php|pl|lua|zig|ex|exs|sh|bash|sql|json|toml|yaml|yml|md|css|scss|html)")
_BOUND = r"(?<![\w./\\-])"
_PATH = r"[\w./\-]+\." + _EXT
_COLON = re.compile(_BOUND + "(" + _PATH + r"):(\d+)(?!\d)")
_PYTB = re.compile(r'File "([^"\n]+)", line (\d+)')
_PAREN = re.compile(_BOUND + "(" + _PATH + r")\((\d+),\d+\)")


def ref_locations(text: str, limit: int = MAX_LOCATIONS) -> List[str]:
    """Up to ``limit`` unique ``path:line`` locations of the original, keeping the last ones in order.

    Failure summaries usually come last, so the tail is the reference. This is the script's own regex set.
    """
    found = []
    for pattern in (_COLON, _PYTB, _PAREN):
        for match in pattern.finditer(text):
            found.append((match.start(), match.group(1) + ":" + match.group(2)))
    ordered: List[str] = []
    for _, loc in sorted(found):
        if loc not in ordered:
            ordered.append(loc)
    return ordered[-limit:]


def location_visible(visible: str, loc: str) -> bool:
    path, line = loc.rsplit(":", 1)
    p, n = re.escape(path), re.escape(line)
    return re.search(_BOUND + p + ":" + n + r"(?!\d)|File \"" + p + "\", line " + n + r"(?!\d)|"
                     + _BOUND + p + r"\(" + n + r",\d+\)", visible) is not None


def exit_code_visible(visible: str, code: int) -> bool:
    pattern = (r"(?i)(?:exit(?:ed)?[ _]?(?:with[ _])?(?:code|status)|returncode|exitcode)[\"']?\s*[:=]?\s*"
               + re.escape(str(code)) + r"(?!\d)")
    return re.search(pattern, visible) is not None


def _nbytes(text: str) -> int:
    return len(text.encode("utf-8", "replace"))


def run_arm(arm: str, result: Result, cap: int):
    """Returns ``(visible_text, spillway_outcome_or_None)`` for one arm."""
    if arm == "none":
        return result.text, None
    if arm == "truncate":
        data = result.text.encode("utf-8", "replace")
        return (data[:cap].decode("utf-8", "ignore") if len(data) > cap else result.text), None
    out = apply_budget("real-replay", result.text, policy=Policy(result_cap=cap), exit_code=result.exit_code)
    return out.rendered.unwrapped_payload(), out


class Tally:
    """Counters for one scope (all, one harness or one kind) at one cap."""

    def __init__(self):
        self.tool_results = 0
        self.arms = {arm: collections.Counter() for arm in ARMS}
        self.modes = collections.Counter()

    def add(self, arm: str, visible: str, result: Result, refs: List[str], outcome) -> None:
        c = self.arms[arm]
        c["bytes"] += _nbytes(visible)
        c["changed"] += visible != result.text
        if result.exit_code is not None:
            c["exit_applicable"] += 1
            c["exit_kept"] += exit_code_visible(visible, result.exit_code)
        if refs:
            kept = sum(location_visible(visible, loc) for loc in refs)
            c["loc_results"] += 1
            c["loc_all_kept"] += kept == len(refs)
            c["refs"] += len(refs)
            c["refs_kept"] += kept
        if outcome is not None:
            self.modes[outcome.mode] += 1
            c["verified"] += bool(outcome.receipt.get("verified"))


def _pct(part: int, whole: int) -> Optional[float]:
    return round(100.0 * part / whole, 2) if whole else None


def _scope_report(tally: Tally) -> dict:
    none_bytes = tally.arms["none"]["bytes"]
    arms = {}
    for arm, c in tally.arms.items():
        block = {
            "bytes": c["bytes"],
            "bytes_saved_pct": _pct(none_bytes - c["bytes"], none_bytes),
            "results_changed": c["changed"],
            "exit_code": {"applicable": c["exit_applicable"], "kept": c["exit_kept"],
                          "kept_pct": _pct(c["exit_kept"], c["exit_applicable"])},
            "locations": {"results_with_refs": c["loc_results"], "results_all_kept": c["loc_all_kept"],
                          "all_kept_pct": _pct(c["loc_all_kept"], c["loc_results"]),
                          "refs": c["refs"], "refs_kept": c["refs_kept"],
                          "refs_kept_pct": _pct(c["refs_kept"], c["refs"])},
        }
        if arm == "spillway":
            block["modes"] = {mode: tally.modes[mode] for mode in MODES}
            block["verified"] = c["verified"]
        arms[arm] = block
    return {"tool_results": tally.tool_results, "arms": arms}


def replay(results: Iterator[Result], caps: List[int], scan: Dict[str, collections.Counter]) -> dict:
    """Scores every result at every cap and returns the aggregate report (no raw content)."""
    harness_info = {h: {"tool_results": 0, "by_kind": {k: 0 for k in KINDS}, "with_exit_code": 0,
                        "with_locations": 0, "original_bytes": 0, "over_cap": {str(c): 0 for c in caps}}
                    for h in HARNESSES}
    tallies = {cap: {"all": Tally(), "by_harness": {h: Tally() for h in HARNESSES},
                     "by_kind": {k: Tally() for k in KINDS}} for cap in caps}
    for result in results:
        refs = ref_locations(result.text)
        size = _nbytes(result.text)
        info = harness_info[result.harness]
        info["tool_results"] += 1
        info["by_kind"][result.kind] += 1
        info["with_exit_code"] += result.exit_code is not None
        info["with_locations"] += bool(refs)
        info["original_bytes"] += size
        for cap in caps:
            info["over_cap"][str(cap)] += size > cap
            scopes = (tallies[cap]["all"], tallies[cap]["by_harness"][result.harness], tallies[cap]["by_kind"][result.kind])
            for tally in scopes:
                tally.tool_results += 1
            for arm in ARMS:
                visible, outcome = run_arm(arm, result, cap)
                for tally in scopes:
                    tally.add(arm, visible, result, refs, outcome)
    for harness, info in harness_info.items():
        info["status"] = "read" if scan[harness]["files_read"] else "not_found"
        info["scan"] = {key: scan[harness][key] for key in _SCAN_KEYS}
        # Every tool result the transcripts hold: the scored shell/file-read ones plus those not scored.
        info["tool_results_seen"] = (info["tool_results"] + scan[harness]["results_not_scored"]
                                     + scan[harness]["steps_truncated_skipped"])
    return {
        "schema": SCHEMA,
        "aggregate_only": True,
        "model_calls": 0,
        "spillway_version": __version__,
        "caps_bytes": list(caps),
        "max_locations": MAX_LOCATIONS,
        "harnesses": harness_info,
        "caps": {str(cap): {"all": _scope_report(t["all"]),
                            "by_harness": {h: _scope_report(x) for h, x in t["by_harness"].items()},
                            "by_kind": {k: _scope_report(x) for k, x in t["by_kind"].items()}}
                 for cap, t in tallies.items()},
    }


# --- CLI ------------------------------------------------------------------------------------------

def _caps(value: str) -> List[int]:
    try:
        caps = sorted({int(part) for part in value.split(",") if part.strip()}, reverse=True)
    except ValueError:
        raise argparse.ArgumentTypeError("caps must be comma-separated integers")
    if not caps or caps[-1] < MIN_RESULT_CAP:
        raise argparse.ArgumentTypeError("each cap must be at least %d bytes" % MIN_RESULT_CAP)
    return caps


def _roots(args) -> Dict[str, List[Path]]:
    home = Path(args.home).expanduser() if args.home else Path.home()
    claude = [Path(p) for p in args.claude_dir] or [home / ".claude" / "projects"]
    codex = [Path(p) for p in args.codex_dir] or [home / ".codex" / "sessions"]
    agy = [Path(p) for p in args.agy_dir] or [home / ".gemini", home / ".t3" / "userdata" / "providers" / "antigravity"]
    return {"claude_code": claude, "codex": codex, "agy": agy}


def _format(report: dict) -> str:
    lines = ["spillway real-session replay: aggregate numbers only, 0 model calls (spillway %s)" % report["spillway_version"],
             "", "%-12s %-10s %12s %12s %7s %10s %15s %14s" % (
                 "harness", "status", "scored", "seen", "shell", "file_read", "with_exit_code", "with_locations")]
    for name, h in report["harnesses"].items():
        lines.append("%-12s %-10s %12d %12d %7d %10d %15d %14d" % (
            name, h["status"], h["tool_results"], h["tool_results_seen"], h["by_kind"]["shell"], h["by_kind"]["file_read"],
            h["with_exit_code"], h["with_locations"]))
    for cap, block in report["caps"].items():
        over = sum(h["over_cap"][cap] for h in report["harnesses"].values())
        lines += ["", "cap %s bytes: %d tool results, %d over the cap" % (cap, block["all"]["tool_results"], over),
                  "%-9s %12s %8s %9s %14s %16s %13s" % (
                      "arm", "bytes", "saved%", "changed", "exit kept", "locs all kept", "refs kept")]
        for arm, a in block["all"]["arms"].items():
            e, loc = a["exit_code"], a["locations"]
            lines.append("%-9s %12d %8s %9d %14s %16s %13s" % (
                arm, a["bytes"], _fmt_pct(a["bytes_saved_pct"]), a["results_changed"],
                "%d/%d" % (e["kept"], e["applicable"]), "%d/%d" % (loc["results_all_kept"], loc["results_with_refs"]),
                "%d/%d" % (loc["refs_kept"], loc["refs"])))
        spill = block["all"]["arms"]["spillway"]
        lines.append("spillway modes: " + ", ".join("%s=%d" % kv for kv in spill["modes"].items() if kv[1])
                     + "; verified=%d" % spill["verified"])
        for name, scoped in block["by_harness"].items():
            if scoped["tool_results"]:
                lines.append("  %-12s n=%d  " % (name, scoped["tool_results"]) + "  ".join(
                    "%s: %d B, exit %d/%d, locs %d/%d" % (
                        arm, a["bytes"], a["exit_code"]["kept"], a["exit_code"]["applicable"],
                        a["locations"]["results_all_kept"], a["locations"]["results_with_refs"])
                    for arm, a in scoped["arms"].items()))
    return "\n".join(lines)


def _fmt_pct(value: Optional[float]) -> str:
    return "n/a" if value is None else "%.1f" % value


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--caps", type=_caps, default=list(DEFAULT_CAPS),
                    help="comma-separated byte caps, each at least %d (default: %s)" % (
                        MIN_RESULT_CAP, ",".join(map(str, DEFAULT_CAPS))))
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="aggregate JSON report (git-ignored by default)")
    ap.add_argument("--home", help="home directory to search (default: the current user's)")
    ap.add_argument("--claude-dir", action="append", default=[], help="override the Claude Code transcript root")
    ap.add_argument("--codex-dir", action="append", default=[], help="override the Codex session root")
    ap.add_argument("--agy-dir", action="append", default=[], help="override an Antigravity base directory (searched for brain/<id>)")
    args = ap.parse_args(argv)

    roots = _roots(args)
    scan = {h: collections.Counter() for h in HARNESSES}

    def everything() -> Iterator[Result]:
        yield from read_claude(roots["claude_code"], scan["claude_code"])
        yield from read_codex(roots["codex"], scan["codex"])
        yield from read_agy(roots["agy"], scan["agy"])

    report = replay(everything(), args.caps, scan)
    assert_aggregate_only(report)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(_format(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
