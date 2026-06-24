# Self-fusion via Claude Code subagents (FINDINGS §8)

Reproduction of the §6 DRACO **self-fusion** scaling experiment — run one model N times,
fuse its own N runs, watch the score vs N — but with the whole pipeline driven by **Claude
Code subagents** (the Workflow tool) instead of the TrustedRouter SDK, because TR credits
were out. Question: does self-fusion's gain depend on how strong the *fuser* is?

Answer (FINDINGS §8): **yes.** Sonnet 4.6 self-fuses +4.4 (n=4); Haiku 4.5 self-fuses
+1.5 (n=26), a bump indistinguishable from zero. Neither is significant at these sizes;
the direction (smarter fuser gains more) and the mechanism (a weak fuser averages its own
rare-correct run away — the needle task, 87→63) are the robust parts.

## Method (faithful to the SDK harness)

Per task, for `RUNS=10`: each run is one Haiku/Sonnet **subagent** doing agentic web research
(`WebSearch`/`WebFetch`/`Bash`, rubric never shown → no leakage) and writing a report. Then
for N=1..10 the first-N reports are fused: N=1 = run #1 raw; N≥2 = a **judge** subagent
(verbatim `JUDGE_SYSTEM` + `_judge_user` from `scripts/draco_client_fusion.py`) → a
**synthesizer** subagent (verbatim `FINAL_INSTRUCTION` + panel evidence + judge JSON). Each
(task,N) answer is graded by a **Sonnet-4.6** subagent using the verbatim
`criterion_judge_messages_for_criteria` prompt from `fusion_live.py`, scored with the exact
`criterion_score` math. One nested run-ordering (first-N of a fixed 10-run pool).

## Files

| file | what |
|---|---|
| `wf_haiku_pilot.js` | canonical harness — Haiku researcher+judge+synth, Sonnet-4.6 chunk-all grader, 8 tasks embedded |
| `wf_haiku_rem{1..4}.js` | Haiku, the 72 non-pilot non-financial tasks, 18/shard (only rem1 was run) |
| `wf_sonnet_pilot4.js` | Sonnet 4.6 in all generative roles, 4 tasks, lean research |
| `wf_haiku_selffusion.js` | original template (chunk-of-3 grade; superseded by chunk-all in the pilot) |
| `pilot_result.json` | raw subagent output — 8 Haiku tasks (scores + per-criterion `metByKey`) |
| `rem1_result.json` | raw subagent output — 18 Haiku tasks |
| `pilot_sonnet_result.json` | raw subagent output — 4 Sonnet tasks |
| `haiku_n26_curve.json`, `bootstrap_ci.json`, `compare_*.json`, `pilot_curve.json` | derived curves + bootstrap CIs |
| `../../results/rejudge-selffusion-*.jsonl` | per-(task,N) scores in the repo's `rejudge.v1` schema |
| `../../scripts/selffusion_analyze.py` | re-derives every number + chart from the result JSONs |
| `../../scripts/selffusion_gen_workflow.py` | generates a self-contained Workflow `.js` for any task slice |

## Reproduce

**Analysis only (no LLM calls, seconds)** — re-derive all numbers, CIs, and the two charts
from the committed raw outputs:

```bash
python3 scripts/selffusion_analyze.py
```

Deterministic (bootstrap seed 12345, B=20000); regenerates `docs/draco-selffusion-*.svg` and
`haiku_n26_curve.json` identically.

**Full re-run (needs Claude Code + the Workflow tool)** — regenerate the raw data:

```bash
# 1. generate a self-contained workflow for a task slice + base model
python3 scripts/selffusion_gen_workflow.py \
  --manifest data/draco-non-financial-80.manifest.json --limit 8 --stratified \
  --model haiku --runs 10 --out artifacts/haiku-selffusion/wf_run.js
# 2. run it with the Workflow tool (task data is embedded; subagents can't read the repo)
#    -> it returns {table, rows, metByKey, ...}; save that JSON
# 3. point selffusion_analyze.py at the saved result JSON(s)
```

## Caveats / honest limits

- **chunk-all grader.** The calibrated grader is Sonnet-4.6 chunk-of-3 (~13 calls/answer), but
  ~1010 chunk calls saturate the shared Claude Code subagent quota (429/529). We grade the whole
  rubric in one call (~80 calls). chunk-all inflates ~+7 vs chunk-of-3 — a near-constant offset
  that preserves curve shape but makes absolutes non-comparable to the gemini-graded §6 numbers.
- **Session-token limit.** Full-80 is infeasible here: each 18-task shard's agentic research
  alone (~24M tokens) exhausts a daily quota window before grading. Resume reuses cached research
  and runs only the cheap fuse+grade pass. Haiku stopped at n=26; Sonnet at n=4.
- **Underpowered.** Per-task SD ≈ 13–18; gains' 95% CIs include 0. ~30 tasks/model + ≥2 orderings
  would settle it. Bootstrap captures task-sampling variance only, not run-ordering variance.
- **Sonnet self-grades** (synth = grader) and used leaner research than Haiku (token budget), so
  the N=1 Haiku-vs-Sonnet gap conflates researcher quality + grading; only each model-vs-itself
  is clean.
- **Grader vs gemini.** Sonnet-4.6 was measured as a 0.92-correlation, ~zero-bias proxy for
  `gemini-3.1-pro-preview` on OpenRouter's DRACO sample — but this is a different sample and may
  overgrade (plausibly +5, as Opus did). Treat absolute scores as inflated.
- **Full replays were recovered** from the subagent transcripts by
  `scripts/extract_selffusion_replays.py` as drop-in `trustedrouter.fusion_draco.replay.v1` rows
  (flagged `recovered_from_transcript: true`):
  - `replays/fusion-selffusion-{haiku,sonnet}-research.jsonl` — solo-style: the full agentic
    trace (`agentic.tools` = every `web_search` query / `web_fetch` URL / `bash` call + the
    final report) + the full task. Haiku 459 / Sonnet 340 (~10/task). This is the reusable panel
    material — re-fuse with a different judge/synth/ordering or re-grade offline (`draco_rejudge.py`)
    without re-running agentic research.
  - `replays/fusion-selffusion-{haiku,sonnet}-fused.jsonl` — fusion-style: the N-fused answers,
    `config_id=selffusion_<model>_x<N>`. Haiku 578 / Sonnet 330.
  Leak audit over the recovered traces: 12,768 `web_search` + 4,894 `web_fetch`, **zero**
  benchmark-host (DRACO/Perplexity/HuggingFace/rubric) retrievals. Caveat: these are recovered,
  so tool names are mapped to the repo's `web_search/web_fetch/bash`, `result_chars` is the
  transcript-stored length, and tokens/timing are null; the queries and URLs are faithful.
