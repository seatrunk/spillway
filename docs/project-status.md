# Project status

**November 26, 2026: v0.5.0 — re-targeted to the current stack (T3 Code driving Claude Code, Codex and Antigravity `agy`, plus OpenBot). The Hermes plugin and the private native-runtime track are retired. Core and MCP proxy ship in `shadow` by default. Production `enforce` activation is still BLOCKED.**

## v0.5.0 (re-target, issue #20)

- **Removed:** the Hermes plugin (`adapters/hermes/`), its installer, tests and docs, the `SPILLWAY_FRAMING_RESERVE` setting that only it used, and every reference to the Hermes runtime. The pure core and the MCP proxy are unchanged.
- **Fixed:** the CI badge, clone URLs and links still pointed at the old repository name.
- **Added:** [`benchmarks/real/`](../benchmarks/real/README.md), a stdlib-only local replay that reads Claude Code, Codex and Antigravity transcripts on the machine, runs `none`, `truncate` and `spillway` arms over their shell and file-read results, and writes **aggregate numbers only**. Nothing from real transcripts is committed (`benchmarks/real/results/` is git-ignored).
- The offline replay is unchanged: 12 synthetic cases, 62 oracle checks. Candidate 62/62; unbudgeted 46/62; emulated historical spill 43/62.

## v0.4.0 (rename and review fixes)

- Package `cbg` renamed to `spillway`; model-visible headers are English (`[spillway preview] …`); `SPILLWAY_*` variables with `CBG_*` fallbacks. See the [CHANGELOG](../CHANGELOG.md#040--2026-11-18) for the migration.
- Fixed: equal-share batch crushing (now water-filling), receipt rows treated as flexible, 1/3 budget waste on line-limited reads, unrecoverable 16-char artifact pointer, JSON-escaped tracebacks, tiny MCP caps, a pagination stall in `utf8_slice`, and forgeable recovery slices.
- Added: `pyproject.toml` and `spillway` CLI (`mcp`, `stats`, `prune`, `replay`), the MCP `spillway_read_artifact` recovery tool, `ArtifactStore` with bounded retention, diagnostic clues, line-aligned previews, bounded orphaned notes, CI Python 3.11–3.13 matrix with an install smoke test.

## Stage 1 (PR #14, v0.3.0)

- `cbg/` (now `spillway/`): pure core with no I/O and no dependencies (limits, render, notes, sanitize, facts, budget, verify). It measures the final rendered bytes and emits a receipt that an independent verifier rechecks, failing closed.
- `adapters/mcp_proxy/`: MCP stdio proxy. It defaults to `shadow`, which records receipts and changes nothing.
- Tests: B1–B3 and the core parts of #1 and #3–#9, plus 10,000 random note-ownership sequences.

## Still pending

1. Ricardo's real-task benchmark: shadow first, then A/B enforce, following the README roadmap. The local [real-session replay](../benchmarks/real/README.md) measures fidelity and bytes on recorded tool results; it does not run a model, so it says nothing about task success, tokens or cost.
2. A host integration for the current stack. Only the MCP proxy exists today; there is no native hook for Claude Code, Codex or `agy` tool results.
3. Independent review of the exact `enforce` candidate, after the benchmark.
4. Re-scoping the open note-handling issues (#1, #3–#9) for the current stack. They were written against the Hermes runtime; see the triage in the PR that closed #20.

## Retired native-runtime track (record only)

Until v0.4.0 the project also tracked a private patch for the Hermes runtime. It never passed its combined acceptance gate, and its delivery-support work was rejected in review. With the Hermes runtime no longer in use, that track is closed and nothing in it is a pending gate. The review records are kept as history, with no claim that they describe the current code:

- [Combined assessment and reviewer handoff](reviews/2026-11-04-combined-assessment.md) and its [sanitized JSON](reviews/2026-11-04-combined-assessment.json).
- [Protected-notes cycle report](reviews/2026-11-03-protected-notes.md) and its [sanitized JSON](reviews/2026-11-03-protected-notes.json).

The core defects those reviews found (#6 corrective multimodal byte overflow, #7 re-limit at finalization, #8 literal delimiters in notes, #9 ownership lifetime) were found against that runtime's integration. Whether each still applies to the core on its own is part of the re-scoping above.

## Evaluation next

The [multi-codebase evaluation protocol](multi-codebase-evaluation.md) is specified in [#10](https://github.com/seatrunk/spillway/issues/10). The offline replay ([#11](https://github.com/seatrunk/spillway/issues/11)) implements its first part against the published core with synthetic fixtures, and the real-session replay adds a local measurement on recorded tool results. The task-based, real-model benchmark is still pending.

The historical [off-only baseline](../benchmarks/results/2026-11-02-off-baseline.json) remains unchanged. Beyond the offline and local replays, there is still no real-task on-mode comparison, measured token or cost savings, production rollout, or verified live-backend enforcement. See [benchmarking](benchmarking.md) and [rollout](rollout.md).
