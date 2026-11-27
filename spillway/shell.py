"""Shell entry point: ``spillway run`` budgets one command's output, ``spillway read`` recovers a stored original.

    spillway run [--mode shadow|enforce] [--cap N] [--log-dir DIR] -- CMD [ARGS...]
    spillway read SHA256 [--offset N] [--limit N] [--log-dir DIR]

``run`` executes CMD without a shell, merges stdout and stderr in arrival order and exits with CMD's exit code
(128+N after a fatal signal). ``shadow`` (default) prints the original bytes and only records a receipt;
``enforce`` prints the budgeted render and stores the original in the artifact store. Fails open on any
internal error. ``read`` prints one neutralized UTF-8-safe slice of a stored artifact plus a footer.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import List, Optional
import uuid

from .artifacts import ArtifactStore, DEFAULT_MAX_ARTIFACTS, DEFAULT_MAX_ARTIFACT_BYTES, is_valid_sha256
from .budget import MIN_RESULT_CAP, Policy, apply_budget
from .sanitize import neutralize_body

SOURCE = "shell"
DEFAULT_READ_LIMIT = 8192
SHELL_ARTIFACT_HINT = "artifact sha256={sha256} (recover: spillway read <sha256>{log_dir} --offset N --limit N)"


def _env(primary: str, legacy: str, default: str) -> str:
    return os.environ.get(primary) or os.environ.get(legacy) or default


def _default_log_dir() -> Path:
    from .cli import _default_log_dir as default_log_dir
    return default_log_dir()


def _emit(data: bytes) -> None:
    stream = getattr(sys.stdout, "buffer", None)
    try:
        if stream is not None:
            stream.write(data)
            stream.flush()
        else:
            sys.stdout.write(data.decode("utf-8", errors="replace"))
            sys.stdout.flush()
    except BrokenPipeError:
        pass


def _exit_status(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def _artifact_hint(log_dir: Path) -> str:
    suffix = ""
    if log_dir.expanduser() != _default_log_dir().expanduser():
        suffix = " --log-dir " + str(log_dir)
    return SHELL_ARTIFACT_HINT.replace("{log_dir}", suffix.replace("{", "{{").replace("}", "}}"))


def _capture(cmd: List[str], stream_live: bool) -> tuple:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    fd = proc.stdout.fileno()
    chunks: List[bytes] = []
    while True:
        try:
            chunk = os.read(fd, 65536)
        except KeyboardInterrupt:
            continue
        if not chunk:
            break
        chunks.append(chunk)
        if stream_live:
            _emit(chunk)
    proc.stdout.close()
    return b"".join(chunks), _exit_status(proc.wait())


def _budget_and_record(data: bytes, status: int, args, store: ArtifactStore) -> Optional[bytes]:
    """Builds the per-result decision, stores the artifact (enforce) and appends the receipt."""
    enforce = args.mode == "enforce"
    started = time.perf_counter()
    out = apply_budget(
        SOURCE + "-" + uuid.uuid4().hex[:12],
        data.decode("utf-8", errors="replace"),
        policy=Policy(result_cap=max(MIN_RESULT_CAP, args.cap)),
        exit_code=status,
        artifact_hint=_artifact_hint(Path(args.log_dir)) if enforce else None,
    )
    changed = out.mode not in ("full", "control")
    rec = dict(
        out.receipt,
        ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        tool=SOURCE,
        adapter=SOURCE,
        adapter_mode=args.mode,
        source=SOURCE,
        elapsed_us=int((time.perf_counter() - started) * 1e6),
        would_change=changed,
    )
    if enforce and changed and out.artifact is not None:
        store.write_artifact(out.receipt["artifact_sha256"], out.artifact)
    store.append_jsonl("receipts.jsonl", json.dumps(rec, ensure_ascii=False))
    if not enforce or not changed:
        return None
    r = out.rendered
    payload = r.payload if args.wrap_untrusted else r.unwrapped_payload()
    return str(payload).encode("utf-8")


def run_main(argv: List[str]) -> int:
    """Runs ``spillway run``; returns the wrapped command's exit status."""
    argv = list(argv)
    cmd: List[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, cmd = argv[:split], argv[split + 1:]
    ap = argparse.ArgumentParser(
        prog="spillway run",
        description="Run a command and budget its merged stdout/stderr. Pass the command after '--'.",
    )
    ap.add_argument("--mode", choices=["shadow", "enforce"], default="shadow",
                    help="shadow (default): print the original, record a receipt; enforce: print the budgeted render")
    ap.add_argument("--cap", type=int, default=int(_env("SPILLWAY_RESULT_CAP", "CBG_RESULT_CAP", "16384")),
                    help="per-result cap in UTF-8 bytes (default 16384, floor 256)")
    ap.add_argument("--log-dir", type=Path, default=_default_log_dir(),
                    help="holds receipts.jsonl and artifacts/ (default ~/.cache/spillway)")
    ap.add_argument("--wrap-untrusted", action="store_true",
                    help="keep the <untrusted_tool_result> wrapper in the enforced output")
    ap.add_argument("--max-artifacts", type=int,
                    default=int(os.environ.get("SPILLWAY_MAX_ARTIFACTS") or DEFAULT_MAX_ARTIFACTS))
    ap.add_argument("--max-artifact-bytes", type=int,
                    default=int(os.environ.get("SPILLWAY_MAX_ARTIFACT_BYTES") or DEFAULT_MAX_ARTIFACT_BYTES))
    args = ap.parse_args(argv)
    if not cmd:
        ap.error("pass the command to run after --")
    args.log_dir = args.log_dir.expanduser()
    store = ArtifactStore(args.log_dir, max_artifacts=args.max_artifacts, max_bytes=args.max_artifact_bytes)
    try:
        data, status = _capture(cmd, stream_live=args.mode == "shadow")
    except FileNotFoundError:
        print("spillway run: command not found: %s" % cmd[0], file=sys.stderr)
        return 127
    except OSError as exc:
        print("spillway run: cannot execute %s: %s" % (cmd[0], exc.strerror or type(exc).__name__), file=sys.stderr)
        return 126
    shown: Optional[bytes] = None
    try:
        shown = _budget_and_record(data, status, args, store)
    except Exception:
        shown = None
    if args.mode == "enforce":
        _emit(shown if shown is not None else data)
    return status


def _non_negative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return number


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return number


def read_main(argv: List[str]) -> int:
    """Runs ``spillway read``: prints one slice of a stored artifact and records a content-free read event."""
    ap = argparse.ArgumentParser(
        prog="spillway read",
        description="Print a UTF-8-safe slice of a stored artifact, followed by a footer with next_offset and eof.",
    )
    ap.add_argument("sha256", help="64-char hex sha256 from the [spillway preview] header")
    ap.add_argument("--offset", type=_non_negative, default=0, help="byte offset (default 0)")
    ap.add_argument("--limit", type=_positive, default=DEFAULT_READ_LIMIT,
                    help="max bytes (default %d, at most 65536)" % DEFAULT_READ_LIMIT)
    ap.add_argument("--log-dir", type=Path, default=_default_log_dir())
    args = ap.parse_args(argv)
    digest = args.sha256.strip().lower()
    if not is_valid_sha256(digest):
        print("spillway read: invalid sha256 (expected 64 lowercase hex characters)", file=sys.stderr)
        return 2
    store = ArtifactStore(args.log_dir)
    try:
        info = store.read_slice(digest, offset=args.offset, limit=args.limit)
    except FileNotFoundError:
        print("spillway read: artifact not found", file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print("spillway read: cannot read artifact (not a regular file?)", file=sys.stderr)
        return 2
    footer = "[spillway read] sha256=%s offset=%d next_offset=%d total_bytes=%d eof=%s" % (
        digest, info["offset"], info["next_offset"], info["total_bytes"], str(info["eof"]).lower())
    _emit((neutralize_body(info["text"]) + "\n" + footer + "\n").encode("utf-8"))
    event = {
        "event": "read",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "sha256": digest,
        "offset": info["offset"],
        "bytes": info["returned_bytes"],
        "source": SOURCE,
    }
    try:
        store.append_jsonl("receipts.jsonl", json.dumps(event))
    except OSError:
        pass
    return 0
