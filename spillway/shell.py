"""Shell adapter: ``spillway run`` budgets one command's output and ``spillway read`` recovers stored originals."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
import uuid

from .artifacts import ArtifactStore, DEFAULT_MAX_ARTIFACTS, DEFAULT_MAX_ARTIFACT_BYTES, is_valid_sha256
from .budget import MIN_RESULT_CAP, Policy, apply_budget
from .sanitize import neutralize_body

SOURCE = "shell"
DEFAULT_READ_LIMIT = 8192
DEFAULT_MAX_CAPTURE_BYTES = 16 * 1024 * 1024
SHELL_ARTIFACT_HINT = "artifact sha256={sha256} (recover: spillway read <sha256>{log_dir} --offset N --limit N)"
_RECEIPT_KEEP = (
    "call_id", "mode", "kind", "cap", "rendered_bytes", "rendered_sha256", "original_bytes", "original_lines",
    "exit_code", "media_parts", "artifact_sha256", "artifact_bytes", "receipt_used", "verified",
)


def _env(primary: str, legacy: str, default: str) -> str:
    return os.environ.get(primary) or os.environ.get(legacy) or default


def _default_log_dir() -> Path:
    from .cli import _default_log_dir as default_log_dir
    return default_log_dir()


def _emit(data: bytes) -> None:
    """Writes bytes to stdout without Python-level buffering so a failure cannot resurface at interpreter exit."""
    try:
        fd = sys.stdout.fileno()
    except (AttributeError, ValueError, OSError):
        sys.stdout.write(data.decode("utf-8", errors="replace"))
        sys.stdout.flush()
        return
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


class _Sink:
    """Delivers output to stdout and remembers the first write failure instead of raising."""

    def __init__(self) -> None:
        self.failed: Optional[OSError] = None

    def write(self, data: bytes) -> None:
        if self.failed is not None or not data:
            return
        try:
            _emit(data)
        except OSError as exc:
            self.failed = exc


class _Forwarder:
    """Forwards SIGINT and SIGTERM received by the wrapper to the child (its process group when it owns one)."""

    def __init__(self, own_group: bool) -> None:
        self.own_group = own_group
        self.proc: Optional[subprocess.Popen] = None
        self.pending: List[int] = []
        self.saved: Dict[int, Any] = {}

    def install(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self.saved[sig] = signal.signal(sig, self._handle)
            except ValueError:
                pass

    def restore(self) -> None:
        for sig, handler in self.saved.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
        self.saved = {}

    def attach(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        for sig in self.pending:
            self.send(sig)
        self.pending = []

    def send(self, signum: int) -> None:
        proc = self.proc
        if proc is None or proc.returncode is not None:
            return
        try:
            if self.own_group:
                os.killpg(proc.pid, signum)
            else:
                proc.send_signal(signum)
        except OSError:
            pass

    def _handle(self, signum: int, frame: Any) -> None:
        if self.proc is None:
            self.pending.append(signum)
        else:
            self.send(signum)


class _Foreground:
    """Hands the controlling terminal to the child's group and gives it back on every exit path."""

    def __init__(self) -> None:
        self.home = 0
        self.active = False
        self.saved: Any = None
        self.masked = False

    def give(self, pgid: int) -> None:
        try:
            if not os.isatty(0) or os.tcgetpgrp(0) != os.getpgrp():
                return
            self.home = os.getpgrp()
            self.saved = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
            self.masked = True
            os.tcsetpgrp(0, pgid)
            self.active = True
            os.killpg(pgid, signal.SIGCONT)
        except OSError:
            self.restore()

    def restore(self) -> None:
        if self.active:
            self.active = False
            try:
                os.tcsetpgrp(0, self.home)
            except OSError:
                pass
        if self.masked:
            self.masked = False
            try:
                signal.signal(signal.SIGTTOU, self.saved)
            except (ValueError, OSError):
                pass


def _exit_status(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def _artifact_hint(log_dir: Path) -> str:
    suffix = ""
    if log_dir.expanduser() != _default_log_dir().expanduser():
        suffix = " --log-dir " + str(log_dir)
    return SHELL_ARTIFACT_HINT.replace("{log_dir}", suffix.replace("{", "{{").replace("}", "}}"))


def _pump(proc: subprocess.Popen, fwd: _Forwarder, sink: _Sink, enforce: bool, limit: int) -> Tuple[List[bytes], bool]:
    """Reads merged output; retains at most ``limit`` bytes, then streams through without retaining."""
    fd = proc.stdout.fileno()
    chunks: List[bytes] = []
    total = 0
    overflow = False
    terminated = False
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        if not enforce:
            sink.write(chunk)
        if overflow:
            if enforce:
                sink.write(chunk)
        elif total + len(chunk) > limit:
            overflow = True
            if enforce:
                for held in chunks:
                    sink.write(held)
                sink.write(chunk)
            chunks = []
        else:
            chunks.append(chunk)
            total += len(chunk)
        if isinstance(sink.failed, BrokenPipeError) and not terminated:
            terminated = True
            fwd.send(signal.SIGTERM)
    return chunks, overflow


def _content_free(receipt: Dict[str, Any]) -> Dict[str, Any]:
    """Keeps sizes, hashes, mode, exit code and counts; drops every field derived from output text."""
    rec = {key: receipt[key] for key in _RECEIPT_KEEP if key in receipt}
    rec["error_location_count"] = len(receipt.get("error_locations") or [])
    rec["diagnostic_clue_count"] = len(receipt.get("diagnostic_clues") or [])
    rec["violation_count"] = len(receipt.get("violations_before_failclose") or [])
    return rec


def _budget_and_record(data: bytes, status: int, args: argparse.Namespace, store: ArtifactStore) -> Optional[bytes]:
    """Budgets one result, stores the artifact only for a delivered preview, records a receipt; None means print the original."""
    enforce = args.mode == "enforce"
    started = time.perf_counter()
    out = apply_budget(
        SOURCE + "-" + uuid.uuid4().hex[:12],
        data.decode("utf-8", errors="replace"),
        policy=Policy(result_cap=max(MIN_RESULT_CAP, args.cap)),
        exit_code=status,
        artifact_hint=_artifact_hint(Path(args.log_dir)) if enforce else None,
    )
    verified = out.receipt.get("verified") is True
    payload = out.rendered.payload if args.wrap_untrusted else out.rendered.unwrapped_payload()
    text = payload if isinstance(payload, str) else ""
    digest = out.receipt.get("artifact_sha256")
    spilled = (
        out.mode in ("preview", "marker")
        and verified
        and out.artifact is not None
        and isinstance(digest, str)
        and digest in text
    )
    wrapped_full = enforce and args.wrap_untrusted and out.mode == "full" and verified and bool(text)
    rec = dict(
        _content_free(out.receipt),
        ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        tool=SOURCE,
        adapter=SOURCE,
        adapter_mode=args.mode,
        source=SOURCE,
        elapsed_us=int((time.perf_counter() - started) * 1e6),
        would_change=spilled,
    )
    if enforce and spilled:
        store.write_artifact(digest, out.artifact)
    store.append_jsonl("receipts.jsonl", json.dumps(rec, ensure_ascii=False))
    if enforce and (spilled or wrapped_full):
        return text.encode("utf-8")
    return None


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
                    help="wrap and neutralize enforced output, including results that fit the cap")
    ap.add_argument("--max-capture-bytes", type=int,
                    default=int(os.environ.get("SPILLWAY_MAX_CAPTURE_BYTES") or DEFAULT_MAX_CAPTURE_BYTES),
                    help="bytes held in memory (default 16 MiB); beyond it output streams through unbudgeted")
    ap.add_argument("--max-artifacts", type=int,
                    default=int(os.environ.get("SPILLWAY_MAX_ARTIFACTS") or DEFAULT_MAX_ARTIFACTS))
    ap.add_argument("--max-artifact-bytes", type=int,
                    default=int(os.environ.get("SPILLWAY_MAX_ARTIFACT_BYTES") or DEFAULT_MAX_ARTIFACT_BYTES))
    args = ap.parse_args(argv)
    if not cmd:
        ap.error("pass the command to run after --")
    args.log_dir = args.log_dir.expanduser()
    store = ArtifactStore(args.log_dir, max_artifacts=args.max_artifacts, max_bytes=args.max_artifact_bytes)
    enforce = args.mode == "enforce"
    sink = _Sink()
    own_group = os.name == "posix"
    fwd = _Forwarder(own_group)
    foreground = _Foreground()
    fwd.install()
    chunks: List[bytes] = []
    overflow = False
    status = 0
    try:
        try:
            extra: Dict[str, Any] = {"process_group": 0} if own_group else {}
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **extra)
        except FileNotFoundError:
            print("spillway run: command not found: %s" % cmd[0], file=sys.stderr)
            return 127
        except OSError as exc:
            print("spillway run: cannot execute %s: %s" % (cmd[0], exc.strerror or type(exc).__name__), file=sys.stderr)
            return 126
        fwd.attach(proc)
        if own_group:
            foreground.give(proc.pid)
        finished = False
        try:
            chunks, overflow = _pump(proc, fwd, sink, enforce, max(1, args.max_capture_bytes))
            status = _exit_status(proc.wait())
            finished = True
        finally:
            if not finished and proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL) if own_group else proc.kill()
                except OSError:
                    pass
            proc.stdout.close()
            proc.wait()
    finally:
        foreground.restore()
        fwd.restore()
    if not overflow:
        data = b"".join(chunks)
        shown: Optional[bytes] = None
        try:
            shown = _budget_and_record(data, status, args, store)
        except Exception:
            shown = None
        if enforce:
            sink.write(shown if shown is not None else data)
    if sink.failed is not None:
        try:
            print("spillway run: cannot write output: %s" % (sink.failed.strerror or type(sink.failed).__name__),
                  file=sys.stderr)
        except OSError:
            pass
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
    ap.add_argument("sha256", help="64-char lowercase hex sha256 from the [spillway preview] header")
    ap.add_argument("--offset", type=_non_negative, default=0, help="byte offset (default 0)")
    ap.add_argument("--limit", type=_positive, default=DEFAULT_READ_LIMIT,
                    help="max bytes (default %d, at most 65536)" % DEFAULT_READ_LIMIT)
    ap.add_argument("--log-dir", type=Path, default=_default_log_dir())
    args = ap.parse_args(argv)
    digest = args.sha256.strip()
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
    try:
        _emit((neutralize_body(info["text"]) + "\n" + footer + "\n").encode("utf-8"))
    except OSError as exc:
        print("spillway read: cannot write output: %s" % (exc.strerror or type(exc).__name__), file=sys.stderr)
        return 1
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
