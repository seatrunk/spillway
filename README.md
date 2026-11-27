# Spillway

[![CI](https://github.com/seatrunk/spillway/actions/workflows/ci.yml/badge.svg)](https://github.com/seatrunk/spillway/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![Zero runtime dependencies](https://img.shields.io/badge/runtime%20dependencies-zero-brightgreen.svg)

**Cap oversized tool output for LLM agents without losing the clues that matter.** Spillway turns huge build logs, tracebacks, and tool payloads into bounded previews that keep exit codes, `file:line` locations, and head/tail context while saving the complete original locally for paginated recovery.

> **Status: experimental (`v0.5.0`).** Defaults to **`shadow`** (measures and logs; original output passes through unchanged). **`enforce` is experimental and not production-approved** — see [rollout gates](docs/rollout.md) and [project status](docs/project-status.md).

## Before and after

Synthetic Rust build failure from [`benchmarks/replay/fixtures/rust_cargo_mid.json`](benchmarks/replay/fixtures/rust_cargo_mid.json): **37,315 bytes, 502 lines**, `exit_code=101`, capped here at **640 bytes** (default cap is 16,384 bytes).

**Plain head truncation at 640 B (excerpt)** — cuts mid-line at `Compiling 00008 dddifhag` and drops `exit_code=101`, `error[E0308]`, and `crates/store/src/lib.rs:88`:

```text
Compiling 00000 ehbg fjji bafeghcichgecechdfdej abecbdfg cghgbiehcdcebdgdbbf
Compiling 00001 fheacfgdcgjgaffgcdcbahhd aggifad baedaceeibbiaeigdjjfjciaiec
... (lines 00002-00007 omitted) ...
Compiling 00008 dddifhag
```

**Spillway preview at 640 B (`spillway run --mode enforce --cap 640`, exact output)** — retains `exit_code=101`, `crates/store/src/lib.rs:88`, original bytes/lines, SHA-256, and line-aligned head/tail:

```text
[spillway preview] original 37315 bytes, 502 lines; exit_code=101; errors: crates/store/src/lib.rs:88; artifact sha256=1a6f9b8a0e5837c8385d86fb0a1929f6e3fbee36a7566b38b902070770fdeb25 (recover: spillway read <sha256> --offset N --limit N)
Compiling 00000 ehbg fjji bafeghcichgecechdfdej abecbdfg cghgbiehcdcebdgdbbf
Compiling 00001 fheacfgdcgjgaffgcdcbahhd aggifad baedaceeibbiaeigdjjfjciaiec
[...]
note 00248  a jedi djjba  abidbeeedf ahbbejbifibcbjdeigieac jb cbddb gd
note 00249 bbhfchgejbeacehdahdieb cjgjggjgahghiedjchedegi adghibdbej  b
```

**Recovering the exact error slice** (requires an `enforce`-mode capture to write the artifact; `shadow` stores no artifacts; if the capture used a custom `--log-dir` or `SPILLWAY_LOG_DIR`, pass the same directory to `spillway read`):

```sh
spillway read 1a6f9b8a0e5837c8385d86fb0a1929f6e3fbee36a7566b38b902070770fdeb25 --offset 19250 --limit 64
```

```text
error[E0308]: mismatched types
 --> crates/store/src/lib.rs:88:9
[spillway read] sha256=1a6f9b8a0e5837c8385d86fb0a1929f6e3fbee36a7566b38b902070770fdeb25 offset=19250 next_offset=19314 total_bytes=37315 eof=false
```

In MCP `enforce` mode, the proxy exposes `spillway_read_artifact` (`{"sha256": "1a6f9b8a0e5837c8385d86fb0a1929f6e3fbee36a7566b38b902070770fdeb25", "offset": 19250, "limit": 64}`) so clients can fetch the same UTF-8-safe, neutralized slice without filesystem access.

## Evidence

### Real-session replay

Aggregate replay from [PR #23](https://github.com/seatrunk/spillway/pull/23) over **737 recorded shell and file-read tool results** from Claude Code, Codex, and Antigravity on one machine (**0 model calls** during replay; 2,447,196 B unbudgeted):

| Cap | Arm | Bytes saved | Results keeping all `file:line` refs | `file:line` refs kept | Exit codes kept |
| --- | --- | ---: | ---: | ---: | ---: |
| 4 KiB | `truncate` (head cut) | 47.3% | 56 / 88 | 176 / 291 | 129 / 129 |
| 4 KiB | **`spillway`** | **48.0%** | **81 / 88** | **282 / 291** | **129 / 129** |
| 8 KiB | `truncate` (head cut) | 27.7% | 67 / 88 | 212 / 291 | 129 / 129 |
| 8 KiB | **`spillway`** | **28.0%** | **86 / 88** | **288 / 291** | **129 / 129** |

This measures **bytes and diagnostic fidelity only** — not token counts, USD cost, or task success rate — and does not approve `enforce` for production. Exit codes stayed at 129/129 in both arms because those harnesses print exit codes at the top of output. Run the aggregate-only replay on your own transcripts with [`benchmarks/real/README.md`](benchmarks/real/README.md).

### Offline synthetic replay

Separately, the deterministic offline suite ([`benchmarks/replay/README.md`](benchmarks/replay/README.md), committed baseline in [`benchmarks/replay/results/offline-replay.json`](benchmarks/replay/results/offline-replay.json)) runs **12 synthetic cases** with 0 model calls and independent ground-truth oracles: **Spillway (`candidate`) passes 62/62** (146,175 B) vs emulated `historical` **43/62** (163,871 B) and unbudgeted `none` **46/62** (297,924 B). Reproduce without modifying tracked files:

```sh
python3 benchmarks/replay/replay.py --out /tmp/offline-replay.json && cmp /tmp/offline-replay.json benchmarks/replay/results/offline-replay.json
```

## Quick start

Requires Python 3.11+ with zero runtime dependencies. Spillway is not published on PyPI.

```sh
pipx install git+https://github.com/seatrunk/spillway
# or from a local checkout:
git clone https://github.com/seatrunk/spillway && cd spillway && pip install .
```

### Shell wrapper

`spillway run` passes straight through when stdin is an interactive TTY, so agent/non-TTY runs (or `</dev/null` when testing interactively) are what get measured or budgeted:

```sh
spillway run -- pytest -x </dev/null                            # shadow (default): unchanged output + receipt
spillway run --mode enforce --cap 16384 -- pytest -x </dev/null # experimental enforce trial: preview + artifact
# Copy the 64-char sha256 from the [spillway preview] header into $SHA256:
spillway read "$SHA256" --offset 0 --limit 8192                 # recover a slice (same --log-dir / SPILLWAY_LOG_DIR)
spillway stats                                                  # summarize receipts.jsonl
```

### MCP stdio proxy

Wrap any MCP server in your client configuration (defaults to `shadow`):

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "spillway",
      "args": ["mcp", "--mode", "shadow", "--", "npx", "-y", "@modelcontextprotocol/server-filesystem", "."]
    }
  }
}
```

### Python library

The budgeting core does zero I/O; filesystem storage lives in [`spillway/artifacts.py`](spillway/artifacts.py).

```python
import spillway

out = spillway.apply_budget("call-1", "x\n" * 10_000, exit_code=2)
out.mode                   # "preview" (or "full", "marker", "withheld", "failclosed", "control")
out.rendered.model_text()  # bounded model-visible text (<= 16,384 UTF-8 bytes by default)
out.receipt                # verified metadata (sizes, hashes, extracted facts)
out.artifact               # full original when cut (persist via spillway.ArtifactStore)

outs = spillway.finalize_batch(
    [{"call_id": "a", "result": "ok"}, {"call_id": "b", "result": "y\n" * 15_000}],
    turn_budget=24_000,
)
```

<details>
<summary>Configuration and operational details</summary>

- **Environment variables / flags:** `SPILLWAY_MODE` (`--mode`, default `shadow`), `SPILLWAY_LOG_DIR` (`--log-dir`, default `~/.cache/spillway`), `SPILLWAY_RESULT_CAP` (`--cap`, default `16384`, floor `256`), `SPILLWAY_WRAP_UNTRUSTED` (`--wrap-untrusted`), `SPILLWAY_MAX_ARTIFACTS` (`--max-artifacts`, default `200`), `SPILLWAY_MAX_ARTIFACT_BYTES` (`--max-artifact-bytes`, default `104857600`), `SPILLWAY_MAX_CAPTURE_BYTES` (`--max-capture-bytes` for `spillway run`, default `16777216`).
- **Shell behavior:** `spillway run` executes argv without a shell, merges stdout/stderr, preserves the child exit code, forwards `SIGINT`/`SIGTERM` to the child process group, passes straight through when stdin is a TTY, and streams unbudgeted if output exceeds `--max-capture-bytes`.
- **MCP behavior:** `shadow` is byte-for-byte passthrough; `enforce` caps oversized `tools/call` text, preserves `isError` and media parts, strips `structuredContent` from cut responses, and registers `spillway_read_artifact`. See [`adapters/mcp_proxy/README.md`](adapters/mcp_proxy/README.md).
- **Housekeeping & receipts:** `spillway prune --log-dir DIR` applies retention limits manually (writes also prune automatically). See [docs/design.md](docs/design.md) for the full pipeline, note ownership, and receipt fields.

</details>

## Security and privacy

- **Unredacted `enforce` artifacts with strict permissions:** `enforce` writes the **complete, unredacted** output of cut results to `artifacts/<sha256>.txt` (which may contain secrets or PII); files are created `0600` in `0700` directories with `O_NOFOLLOW` (refusing symlinks and non-regular files) and bounded retention (200 files / 100 MiB default).
- **Keep `SPILLWAY_LOG_DIR` outside repositories:** never point `SPILLWAY_LOG_DIR` inside a repo or commit/share `artifacts/` or `*.jsonl`; `shadow` writes no artifacts, and both shell and MCP receipts record counts instead of result text or file paths, though `receipts.jsonl` still logs operational metadata (such as MCP tool names, sizes, hashes, exit codes, and timestamps — see [receipt fields](docs/design.md#receipt-fields)) and should remain local-only.
- **Fail-open adapters, fail-closed verifier:** on internal budgeting or rewriting errors, the MCP proxy and `spillway run` **fail open** to the original tool output (transport, I/O, and child-process failures still apply, and MCP recovery failures return a bounded error response); the independent core verifier **fails closed** on a receipt or invariant mismatch with a bounded fail-closed marker.
- **Vulnerability reporting:** report issues privately via [SECURITY.md](SECURITY.md) (Spillway is not a sandbox and does not protect against other processes running as the same UID).

## Docs

- [Design & invariants](docs/design.md) — measurement, fact extraction, batch water-filling, trust boundary, and receipt schema
- [Benchmarking](docs/benchmarking.md) — evaluation protocol, offline/real-session replay, and published baseline limits
- [Rollout gates](docs/rollout.md) & [Project status](docs/project-status.md) — what is built and what blocks production `enforce`
- [Visual guide](docs/visual-guide.html) — architecture and flow diagrams
- [Contributing](CONTRIBUTING.md) — stdlib-only guidelines, regression tests, and review boundaries

## Development

Standard library only (Python 3.11+, no dev dependencies):

```sh
python3 -m unittest discover -s tests -v
python3 scripts/check_repo.py
```

## License

[MIT](LICENSE) © Ricardo Margalho. Contributions are welcome; please follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
