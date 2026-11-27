# README artwork

Project illustrations distributed under the repository's existing MIT license. These graphics are documentation visuals — not screenshots of a shipped production runtime or evidence of `enforce` approval.

## README PNGs

| Image | Dimensions | Purpose |
| --- | --- | --- |
| [hero.png](hero.png) | 2560 × 1120 | Top-of-README overview of Spillway's bounded preview and local recovery flow (`rust_cargo_mid`, 640 B cap) |
| [before-after.png](before-after.png) | 2560 × 1440 | Illustrative reconstruction comparing 640 B naive head truncation vs Spillway's core preview (518 B unwrapped / 567 B framed) on `rust_cargo_mid` |
| [benchmark.png](benchmark.png) | 2560 × 1080 | Visual summary of PR #23's single-machine real-session replay (737 tool outputs, 4 KiB and 8 KiB caps, bytes and diagnostic fidelity only) |

README alt text, nearby prose, copyable code excerpts, and Markdown tables provide full accessible equivalents for every graphic.

## Legacy SVG sources and visual guide

The original editable SVGs remain intact and are embedded in the offline [visual guide](../visual-guide.html) (**download and open locally**, since GitHub renders HTML source rather than executing it):

| SVG source | Legacy PNG export | Purpose |
| --- | --- | --- |
| [hero.svg](hero.svg) | — (superseded by standalone [hero.png](hero.png)) | Original concept diagram |
| [output-flow.svg](output-flow.svg) | [output-flow.png](output-flow.png) | Output, metadata, trust boundary, and artifact flow |
| [roadmap.svg](roadmap.svg) | [roadmap.png](roadmap.png) | Historical review status and promotion gates |
| [evaluation.svg](evaluation.svg) | [evaluation.png](evaluation.png) | Comparison arms, semantic oracles, and efficiency metrics |

When regenerating legacy PNG exports from SVGs with librsvg's `rsvg-convert`, export only the matching SVG-backed files (`output-flow.svg`, `roadmap.svg`, `evaluation.svg`) so the standalone README PNGs (`hero.png`, `before-after.png`, `benchmark.png`) are not overwritten. Optional artwork-maintenance tools are not dependencies of Spillway's Python runtime or checks.
