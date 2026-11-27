# Real-session replay (#20)

Measures the Spillway core on tool results that **already exist on your machine**, instead of synthetic fixtures. It runs locally, calls no model and uses no network. It prints and writes **aggregate numbers only**.

```sh
python3 benchmarks/real/real_replay.py                       # caps 2048, 4096, 8192 and 16384 bytes
python3 benchmarks/real/real_replay.py --caps 2048,4096,8192,16384
python3 benchmarks/real/real_replay.py --caps 16384          # one cap (each cap must be at least 256)
python3 benchmarks/real/real_replay.py --out /tmp/report.json
```

The report goes to `benchmarks/real/results/real-replay.json`, which is git-ignored. Do not commit, attach or paste anything from your transcripts. Pasting the printed aggregate report is fine: it holds no transcript text.

## Where it reads

| Harness | Default location (read only) | Result source |
| --- | --- | --- |
| Claude Code | `~/.claude/projects/**/*.jsonl` | `tool_result` blocks, named by their `tool_use` (`Bash` is shell, `Read` is a file read). A failed Bash call starts with `Exit code N`; a successful one records no code |
| Codex | `~/.codex/sessions/**/*.jsonl` | `*_call_output` items, named by their `*_call` (`exec`, `shell` and similar are shell). Exit code from `Exit code: N`, `Process exited with code N` or `metadata.exit_code` when the output has one. The `exec` code-mode tool records none |
| Antigravity (`agy`) | `brain/<id>/.system_generated/logs/` under `~/.gemini` and `~/.t3/userdata/providers/antigravity` | `GENERIC` steps after a `PLANNER_RESPONSE`, matched to its tool calls in order (`run_command` is shell, `read_file` is a file read). Exit code from `Command exited with code N.` |

`agy` transcripts are readable on the machine this was written on: `transcript_full` chunks hold complete step content, and other copies are used only for steps they do not cover. Steps marked `truncated_fields` are skipped because their size is not the original size, and are counted in `steps_truncated_skipped`. Override a location with `--claude-dir`, `--codex-dir`, `--agy-dir` (repeatable) or `--home`. A harness with no transcripts reports `status: not_found` and zero results.

Only shell and file-read results are scored. Other tool results (edits, searches, MCP calls, planner state) are counted in `results_not_scored`, so `tool_results_seen` is the total the transcripts hold and `tool_results` is the scored subset. Results repeated by a resumed session are counted once (`duplicates_skipped`).

## Arms and scoring

Each result runs through three arms at the same byte cap:

- `none`: the recorded result as is.
- `truncate`: a plain head cut at the cap, on a UTF-8 boundary.
- `spillway`: `spillway.apply_budget` with `Policy(result_cap=cap)` and the exit code when known, the same call the MCP proxy makes in `enforce`. The delivered form is the unwrapped payload, so all three arms are measured as bare tool-result text.

Per arm the report gives:

- **bytes**: UTF-8 bytes the model would see, and `bytes_saved_pct` against `none`.
- **exit code**: among results whose harness recorded an exit code, how many still show it (`exit code N`, `exited with code N`, `exit_code=N`, `returncode N` and similar).
- **locations**: for up to 5 `file:line` locations of the original (`path:line`, `File "path", line N`, `path(line,col)`), how many results keep all of them, and how many individual locations survive.
- `results_changed`: results whose visible text differs from the original. For `spillway` this includes results that are only rewritten, not cut: a fact line `[spillway] exit_code=N` is prepended when the text does not say `exit code N` in a form Spillway recognizes, and text that imitates `[spillway …]` or the wrapper tags is neutralized. `modes` counts the cut ones (`preview`, `marker`, ...), and `verified` counts results the independent verifier accepted.

Reading the numbers:

- The reference locations are found by this script's own regexes, not by `spillway.facts`, and they are the **last** 5 unique ones in the original. The core also keeps its own last 5, so when a result has more than 5 locations the two sets can differ, and a miss can come from that and not from a cut. The script's extension list also differs from the core's (for example `.md`, which appears in `grep -n` output).
- Every harness puts the exit code at the **top** of a failed result, so a head cut keeps it. Do not expect the exit-code row to separate `truncate` from `spillway` on these transcripts. Keeping it matters when the code arrives at the end, as in the JSON output form that some Codex versions emit.
- Transcripts record what the model saw **after** each harness's own limits, so a result that a harness already shortened looks small here.
- Most recorded results are small. Compare the `over_cap` counts before reading the byte percentages, and use a small cap such as 4096 to stress the arms.
- The corpus includes the session that runs this script, so two runs a few minutes apart can differ.

## Privacy

Raw content stays in memory while a result is scored. The report is built from counters, and `assert_aggregate_only` rejects any value that is not a number, a boolean, null, a known key or a fixed vocabulary word (harness and kind names, arm and mode names, `read`/`not_found`, the schema id and the version) before anything is printed or written. It never contains file paths, tool names, prompts or tool output. `tests/test_real_replay.py` builds synthetic transcripts full of sentinel strings and checks that neither stdout nor the written report holds any of them, and that the guard rejects free text.

Do not use real transcripts as fixtures, and do not weaken the guard to print more detail. If you need a breakdown the report lacks, add a counter with a fixed key, not a string.

This measures bytes and fidelity on recorded results. It does not measure tokens, cost or task success, and it does not approve `enforce`. See [rollout](../../docs/rollout.md).
