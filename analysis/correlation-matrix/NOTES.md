# Per-task error-structure correlation matrix

Goal: prove the **diversity-not-IQ** mechanism behind the fusion ablation
(`docs/FINDINGS.md` §7.4) — show, per task, *which* panel members err together. A member is
valuable to the fusion when it is **competent AND decorrelated** with the rest, not when it
is simply the smartest. The figure is a 100×5 DRACO-score grid → a 5×5 member correlation
matrix; the prediction is that the load-bearing members (M3, DeepSeek) are among the least
correlated and the freeloader (GLM) is the most.

## Data (`data/`, reproducible subset — no model-output text, per repo convention)

- `member_solo_grades.json` — `idx → {member, task_id, gemini}` for all **500** member-solo
  cells (5 members × 100 tasks). `gemini` is the final DRACO score (float 0–100) from the
  canonical `gemini-3.1-pro-preview` chunk-of-3 judge; **308** cells are populated, **192**
  are `null` (gemini ran out of credits) and are graded with Sonnet (below).
- `rubrics.json` — `task_id → {criterion_id: weight}` for the 100 tasks. Weights can be
  **negative** (penalty criteria). DRACO score = `clamp(100 · Σ weight[met] / Σ weight[w>0], 0, 100)`.
- `chunk_counts.json` — `idx → n` expected criterion-chunks per ungraded cell (completeness check).
- `sonnet_results.jsonl` — accumulated Sonnet grades, one row per graded chunk:
  `{idx, ci, m:[met criterion ids], n:#criteria judged}` (partial; appended as grading runs).

The full problem/answer replays live in `quill-router` (model output is not committed here).

## Pipeline

1. **`make_slices.py`** — list `chunkjobs3/*.json` (the 192 ungraded cells split into
   ~2,739 criterion-of-3 chunks, staged in `/tmp/claude/`), round-robin into N balanced
   slices → `slice_<n>.json`.
2. **`make_workflows.py`** — bake each slice's filename list into a self-contained workflow
   script (`grade_slice.template.js` is the committed reference). One **Sonnet** subagent per
   chunk, `effort:high`, schema-forced `{judgments:[{id,met}]}`, judge prompt replicated
   verbatim from `src/trusted_router/evals/fusion_live.py` (criteria-before-answer order is
   load-bearing for calibration). Bounded ≤ ~685 agents/workflow (the 1000-agent cap).
3. Run each slice via the Workflow tool **sequentially** (parallel launches each get their
   own concurrency cap → re-triggers rate-limiting). Re-grade rate-limited stragglers until
   every cell has all its chunks.
4. **`parse_wf.py <output-file>...`** — extract `{idx,ci,m,n}` rows (the workflow output is a
   `{summary,agentCount,logs,result}` wrapper; `result` is a possibly double-encoded JSON
   string) into `data/sonnet_results.jsonl`, deduped; reports per-cell completeness.
5. **`aggregate_matrix.py`** — union met_ids per cell → DRACO score (positive-weight formula)
   → merge with the 308 gemini scores → 100×5 grid → **raw per-task score correlation** (the
   diversity metric; low corr = independent errors = fusion gain) + a leave-one-out residual
   diagnostic + a per-member gemini-vs-Sonnet calibration check. Writes `score_grid.json`,
   `corr_matrix.json`.

## Why raw correlation (not residualized)

Residualizing each task against the 5-member mean forces the residuals to sum to zero, which
makes off-diagonal correlations **artificially negative** (a k=5 artifact, not real
structure). Raw per-task score correlation is artifact-free and is the standard ensemble-
diversity metric. Pearson is invariant to per-member location/scale, so mixing gemini +
Sonnet graders does not bias it. A leave-one-out residual (mean of *others*, excludes self)
is kept only as a secondary diagnostic.

## Status / gotchas

- **Sonnet grading is rate-limited** ("Server is temporarily limiting requests" — transient,
  not the usage cap). Even a bounded ~685-agent workflow at ~14 concurrency loses a large
  fraction; grade in smaller waves and loop until complete. The **first** attempt also hit a
  runaway-loop bug (looping on `budget.remaining()` with no token budget → `Infinity` →
  1000-agent cap) — always give such loops a hard iteration cap.
- **RESUMED (2026-06-22) — conc-2 beats the throttle.** The fix was capping subagent
  concurrency: a manual pool of **≤2 agents in flight** (`grade_gentle.js`, batch-8,
  Sonnet/effort=high) stays under the per-session request throttle that ~14-wide tripped.
  At conc-2 the only failures are the hard session *usage* cap (resets, then resume). One
  ~100-min pass took grading from 223 → **1,479/2,739 chunks**; ~1,260 chunks across 188
  cells remain (the run swept low-`ci` chunks first, so most cells have most-but-not-all
  chunks → only 4/192 cells complete so far). Finish = one more conc-2 pass on the
  remainder, then `aggregate_matrix.py` → the figure. (Earlier history: a per-chunk
  685-agent and a batched 650-agent blast both got ~3–21% as the throttle *escalates with
  back-to-back bursts*; concurrency cap, not batching, is what fixed it.)
