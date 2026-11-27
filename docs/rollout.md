# Rollout gates

Production activation is currently **BLOCKED**. This repository ships the core (the `spillway` package, v0.5.0) and an MCP proxy that defaults to `shadow` (receipts only, output unchanged). Running the proxy in shadow mode is observation, not activation. Its `enforce` mode stays behind the gates below. The Hermes plugin and the private native-runtime track were retired in v0.5.0, so their gates are gone.

Before any `enforce` trial:

- Run the real-task benchmark from the README: shadow receipts first, then paired A/B enforce runs, with no fidelity-oracle regressions.
- Get independent review of the exact `enforce` candidate. Core tests and the offline replay (candidate 62/62) do not replace it.
- Resolve the open note-handling issues (#1, #3–#9) or re-scope them for the current stack, adding a synthetic regression for each and inspecting the model-visible output.
- Run the local [real-session replay](../benchmarks/real/README.md) on the target harnesses and read the aggregate fidelity numbers. It is a measurement of the core, not an approval.
- Verify every [design invariant](design.md), including mandatory exceptions, valid media, failure metadata, artifact privacy, and cached profile/session behavior.
- Record the exact revision under review. Test totals alone cannot grant approval.
- Agree on explicit human authorization, isolated synthetic trial scope, stop conditions, and a tested rollback to the prior configuration. Do not use private live transcripts as fixtures.
- Re-run applicable integration checks against the real host. Distinguish those checks from this repository's CI and record unresolved limitations.

Stop if necessary notes disappear, artifacts expose private content, media/control content breaks, failure semantics change, or the reviewed fingerprint differs. Revert the trial through the agreed rollback and verify the resulting state. A fresh patch requires fresh review.

Offline, no-provider replay may characterize a candidate's failures. It is not a live trial or readiness approval. Task-based model experiments, conversation-compaction interactions, paid usage, and any production canary require separate scope and authorization. See the proposed [evaluation protocol](multi-codebase-evaluation.md).

Do not restart services, enable profiles, or claim deployment from this checklist. See [project status](project-status.md) for the current state and [benchmarking](benchmarking.md) for what the off-only evidence can establish.
