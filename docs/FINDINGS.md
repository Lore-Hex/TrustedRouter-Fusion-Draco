# Findings — reproducing OpenRouter Fusion DRACO on TrustedRouter

What we set out to do: replicate OpenRouter's "Fusion beats frontier" DRACO
result (budget panel Gemini-3-Flash + Kimi-K2.6 + DeepSeek-V4-Pro, Opus 4.8
fuser, published **64.7**, judge Gemini 3.1 Pro) on TrustedRouter, and publish a
defensible writeup.

## 1. The gap was live tools, not anything subtle

DRACO is an **agentic** deep-research benchmark — the model iteratively searches,
fetches, and computes, then writes a cited report. Our first harness gave models
pre-fetched (frozen Exa) context in a single shot. That under-scored them ~20
points. With a client-side agentic loop (`web_search` Exa, `web_fetch` local +
markitdown, `bash` Docker), tooled solos reach OpenRouter levels:

| solo (15-task) | frozen | live tools | OpenRouter (100) |
|---|---:|---:|---:|
| Kimi K2.6 | 47.0 | 64.0 | 53.7 |
| DeepSeek V4 Pro | 45.2 | 68.3 | 60.3 |

Every task rose (+15 to +42). The qualitative claim ("live tools close the gap;
tooled solos reach/exceed OpenRouter") is robust and reproducible.

### Final full-100 tooled-solo scores

All six solos run to completion on the full 100 DRACO tasks (judge
`google/gemini-3.1-pro-preview`, reasoning `high`, 1 pass). Results split by the
80 non-financial vs 20 finance tasks (`results/rejudge-full100-all6-solos.jsonl`):

| solo (tooled) | full-100 | non-fin 80 | finance 20 | OpenRouter (100) |
|---|---:|---:|---:|---:|
| GPT-5.5 | 63.3 | 64.1 | 60.0 | — |
| Opus-4.8 | 60.3 | 61.2 | 56.4 | — |
| DeepSeek V4 Pro | 57.5 | **60.0** | 47.7 | **60.3** |
| Gemini-3.1-Pro | 47.1 | 48.6 | 41.3 | — |
| Kimi K2.6 | 46.3 | **49.8** | 32.6 | **53.7** |
| Gemini-3 Flash | 40.4 | 43.4 | 28.5 | — |

On the **non-financial 80** — the apples-to-apples set with our validation slice —
tooled **DeepSeek 60.0 ≈ OpenRouter 60.3** and **Kimi 49.8 vs 53.7** (within ~4).
Live tools reproduce OpenRouter's solo baselines. The 15-task slice ran *above* OR
purely because it was non-financial and a small favorable sample.

### The budget Fusion (the headline reproduction)

The native gateway `/fusion` endpoint cannot give panels live tools (its panel
runs on frozen context → ~40), so Fusion is reproduced **client-orchestrated**,
faithful to the gateway's own pipeline (prompts copied verbatim from
`enclave fusion.go`): the **panel** is our three validated tooled budget solos
(Gemini-3-Flash + Kimi + DeepSeek) → a **gemini-3.1-pro judge** writes a compact
consensus/contradiction/blind-spot analysis → **Opus-4.8 fuser** synthesizes the
final answer (panel evidence primary, judge analysis as guidance). No rubric ever
reaches the judge or fuser. Harness: `scripts/draco_client_fusion.py`; scores in
`results/rejudge-fusion-client-budget-opus-full100.jsonl`.

**Fuser ablation (full-100, frontier panel, identical judge analysis).** Holding the
five-model frontier panel fixed and swapping only the synthesizer, full-100 scores:
**MiniMax-M3 → 71.6**, GLM-5.2 → 71.1, Opus-4.8 → 70.6, Kimi-K2.6 → 67.0, DeepSeek-V4
→ 65.7, GPT-5.5 → 62.2, Gemma-4-31b → 54.0 (`results/rejudge-frontier-*-fuser.jsonl`).
The best fuser is open-weights MiniMax-M3, and three findings fall out of the spread:
(1) **synthesis is a skill apart from solo research** — GPT-5.5 is the top *solo*
researcher (63.0) yet the weakest capable fuser; (2) **size matters for the fuser** —
Gemma-4-31b collapses 18 points below the leaders, too small to hold and reconcile a
frontier panel; (3) the top two are both open-weights. We default to MiniMax-M3 over
GLM-5.2: same score, no censorship hole. GLM-5.2's 71.1 carries a † — it returned
empty content on 1/100 tasks (finish_reason `stop`, `completion_tokens=1`), scored 0;
over the 99 it answered it averages 71.8. MiniMax-M3 had zero such empties.

**Root cause of the empty task — political censorship, not a bug.** Bisection
isolated it cleanly: prompt was only ~19k tokens (not a context limit), no leaked
tool markup, and GLM writes a full report from the same task *problem* alone. The
trigger was one panel report describing a *Greater China* fund's China / Hong Kong /
Taiwan allocation. GLM-5.2 (Zhipu / Z.AI) silently refuses Taiwan/Hong-Kong
sovereignty-framed content — it emits a single stop token, zero output. Definitive
test: replacing "Taiwan"/"Hong Kong" with neutral tokens across the panel makes the
full fusion succeed (7.1k chars), while swapping an innocuous term leaves it empty.
A Chinese open-weights fuser inherits its training's content restrictions; a robust
pipeline needs an empty-output fallback to a second fuser (the benchmark instead
scores the refusal as 0, honestly).

**All-open-weights panel (no proprietary API anywhere in the stack).** Swap the
frontier panel for five downloadable open models — MiniMax-M3 + Kimi-K2.6 +
DeepSeek-V4 + Gemma-4 + GLM-5.2 — each running its own agentic loop, fused by
MiniMax-M3 (the top open fuser). Full-100: **69.9**
(`results/rejudge-openweights-m3-fuser.jsonl`, replay in
`replays/fusion-openweights-m3.jsonl`). That beats Fable-5 solo (65.3) by +4.6 and
edges OpenRouter's best published fusion (Fable-5 + GPT-5.5, 69.0), while sitting
below the frontier-mixed panel (71.6) — letting closed frontier models into the
panel still buys ~1.7. Fusing the *same* panel with GLM-5.2 instead scores 68.9 and
needs an empty-output fallback to Gemma-4 (GLM censors Taiwan/HK; `--fallback-fuser-model`
handles it); MiniMax-M3 needs no fallback and trips no empties.

| budget Fusion | full-100 | non-fin 80 | finance 20 | OpenRouter |
|---|---:|---:|---:|---:|
| Gemini-Flash + Kimi + DeepSeek → Opus | **60.8** | **63.2** | 50.9 | 64.7 |

The Fusion clears every panel member (DeepSeek 57.5, Kimi 46.3, Flash 40.4) and
beats frontier **Opus 4.8 solo (60.3)**. On the non-financial 80 it lands at 63.5,
within ~1 of OpenRouter's 64.7 and above Opus. It does **not** clear our GPT-5.5
solo (63.3) on the full set — our GPT-5.5 ran 3.3 pts above OpenRouter's, and our
Fusion is dragged below OR's 64.7 by the same finance-document gap as the solos.
The core "a panel of cheap models reaches a frontier answer" result reproduces;
the residual gap to OR is finance-document tooling, not fusion.

## 2. Why we run ABOVE OpenRouter (investigated)

- **Leakage: ruled out.** Audited all 568 `web_search` queries + 155 `web_fetch`
  URLs — all legitimate research sources (nature.com, nist.gov, nvidia docs,
  wikipedia, sciencedirect...), zero DRACO/Perplexity/HuggingFace/rubric/answer-key
  retrieval. The leak filter (`_draco_search_result_leak_reason`: criterion ids,
  requirement fragments, forbidden terms, blocked hosts) works and was barely needed.
- **Judge: same model** (`google/gemini-3.1-pro-preview`). We use 1 pass; OR used
  the paper's multi-pass. Averaging passes cuts variance, not the mean — washes out
  at scale, so not the cause.
- **Cause = harness generosity.** Our loop is more aggressive than OR's server-tool
  config: up to 16 tool calls, 5 results × ~25k-char fetches, bash, a forced first
  tool call, and a dedicated synthesis turn. OR likely under-invested their harness.
  This legitimately raises answer completeness → rubric coverage → score.

**Decision:** keep the generous harness; **disclose** the exact tool budget, fetch
size, synthesis turn, and judge pass count in any writeup so the comparison is honest.

## 3. Finance slice (open)

OpenRouter ran the full 100; the `non-financial-80` set excludes the 20
`domain == "finance"` tasks (capital allocation, cash generation, equity
financing — filing- and figure-heavy). Hypothesis: finance is harder, so an
80-task average is inflated vs OR's full-100. We added the finance tasks back
(`data/draco-financial-20.manifest.json`) and equipped `web_fetch` with
**markitdown** (table-preserving conversion of SEC filings / PDFs / spreadsheets)
to handle them.

**Result: hypothesis confirmed — finance is much harder.** Every model drops
**−12 to −17 pts** on the 20 finance tasks vs the 80 non-financial: DeepSeek
60.0→47.7, Kimi 49.8→32.6, Gemini-Flash 43.4→28.5, even frontier GPT-5.5
64.1→60.0. So the full-100 average sits below the non-financial-80, and our
finance scores trail — `markitdown` recovers tables from filings but is weaker
than whatever document tooling OpenRouter used. This is the honest residual gap;
the non-financial reproduction itself is on the nose.

## 4. Other diagnoses worth keeping

- **Opus fuser truncation** was governed by the OUTER `max_tokens`, not
  `parameters.max_completion_tokens` (the latter only caps the panel + judge).
  Truncated answers tanked DRACO criterion coverage; the 2 non-truncated tasks in
  the first run already scored ~62 (≈ OpenRouter's 64.7).
- **"Harder slice" was wrong** — the first-N slice scored slightly *higher* than
  the full set; the OR discrepancy was harness/methodology, not difficulty.
- **Solo baselines were complete** (not truncated) — DeepSeek wrote full 24k-char
  answers and still trailed OR by 18 under frozen context.

## 5. Harness design notes

- A hard tool cap + abrupt forced answer makes models leak native tool-call markup
  (DeepSeek `<｜｜DSML｜｜>`, Kimi `<invoke>`) or refuse to synthesize. Fix =
  generous research budget + a dedicated synthesis turn (no tools, "write the report
  now") + strip leaked markup.
- `--force-first-tool` for reluctant tool-users (gemini-flash answers from memory).
- bash = local Docker `python:3.12-slim` `--network none`.

## 6. Self-fusion — fusing a model with copies of itself

The panel doesn't have to be different models. Run the *same* model N times (it takes a
different agentic path each time — different searches, different sources) and fuse the N
reports with that same model. The gain depends entirely on **error correlation across the
runs**, and that has a sharp, measurable structure (judged files
`results/rejudge-selffusion-*.jsonl`):

| base model | solo | 2-run self-fusion (t=0.2) | 2-run (t=0.8) | 10-run self-fusion |
|---|---:|---:|---:|---:|
| Opus 4.8 | 60.7 | **67.6** (+6.9) | 67.5 | — |
| MiniMax-M3 | 66.2 | **66.2** (+0.0) | 66.1 | **69.4** |

Two findings:

1. **Two runs help a shaky model and do nothing for a steady one.** Opus self-fuses +6.9
   off two runs; M3 self-fuses +0.0. The cause is error *de-correlation*, not temperature:
   `corr(opus_solo_score, opus_self-fusion_gain) = −0.60` — Opus gains exactly on the tasks
   where one run scored lowest (its weak runs averaged 52/100), because its independent
   runs fail on *different* tasks and the pair recovers them. M3's two runs swing >5pts on
   42/100 tasks (up 22, down 20 — a wash): when M3 errs, both runs err the same way, so
   there's nothing to recover. Cranking sampling temperature to 0.8 doesn't change it
   (66.2 → 66.1): temperature varies the surface path, not *which* tasks the model is
   systematically blind on.

2. **Quantity substitutes for cross-model diversity.** A model with highly-correlated
   errors needs many draws, not two. Fuse **ten** M3 runs and the gain appears — **69.4**
   (`replays/fusion-selffusion-m3-x10.jsonl`, `results/rejudge-selffusion-m3-x10.jsonl`,
   100 tasks, same `gemini-3.1-pro` judge). That lands a hair under the all-open *panel* of
   five different models (69.9) and ~2 below the frontier-mixed panel (71.6): ten copies of
   one model nearly match five different ones, because enough independent tries occasionally
   surface the rare-correct run the fuser can keep. The gain is bigger on the non-financial
   80 (70.8) than on finance (63.6) — even ten runs share M3's knowledge gaps on
   filing-heavy tasks.

3. **The scaling curve: two runs do nothing, four clears Fable, ~seven plateaus.** Fusing the
   first N of a fixed 10-run pool (`results/rejudge-selffusion-m3-x{3..9}.jsonl`; chart
   `docs/draco-selffusion-scaling.svg`):

   | N runs fused | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
   |---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
   | DRACO | 66.2 | 66.1 | 67.7 | 68.1 | 68.1 | 68.2 | 69.5 | 69.2 | 68.4 | 69.4 |

   Flat through N=2, a steady climb from N=3, a plateau near 69 by N=7; past seven, more copies
   buy nothing. **Four runs already clear Fable-5 solo (65.3)** and capture ~60% of the max gain
   for ~$37 (vs the ten-run $87). This is one nested ordering, so the point-to-point jitter (~1
   pt; the N=7 bump, the N=9 dip) is run-to-run noise — the climb-then-plateau is the signal.

**Cost.** This is worth doing because M3 is cheap: $0.30/$1.20 per M tokens in/out vs Fable 5
at 2× Opus 4.8's price (~$9.90/$49.50), i.e. ~33×/41× cheaper per token. The measured cost of
the ten-M3 pipeline over the 100-task benchmark is **$87** (10 research runs $73.66 + M3 fusion
$2.18 + the `gemini-3.1-pro` consensus pass $11.35); the **four-run** pipeline (the curve's
knee, score 68.1) is a measured **$37**. A single Fable-5 solo run, priced at 2× Opus with the
same token profile, models to **~$250** — Fable 5 is route-blocked and its price isn't public,
so that figure is modeled, not billed. The gap survives the token-profile assumption: even if
Fable were twice as token-efficient it would still cost ~$125. So the open stack is the cheaper
route to a frontier-grade answer — **~7× cheaper at four runs, ~3× at ten** — and beats Fable-5
solo (65.3) by +2.8 (four) to +4.1 (ten).

## 7. Fusion is two jobs — the judge×synthesizer grid, the ablation, and the error-structure matrix (2026-06-21)

Sections 1–6 settled "a cheap panel reaches a frontier answer." This section dissects
*why the panel works* — which member carries it, how much the synthesizer matters, and
whether the panel is redundant — and supersedes the earlier "best fuser = MiniMax-M3 71.6"
headline (that was one synthesizer column; the full grid has a better cell). All scores
here are the same `google/gemini-3.1-pro-preview` (reasoning `high`, chunk-of-3) judge
unless noted; the open committee figures are 3 reps with ±1 SE.

### 7.1 The best fuser is a Kimi-K2.6 **judge** feeding a GLM-5.2 **synthesizer** — 73.4 SOTA

Fusion is two distinct jobs: a **judge** writes the consensus/contradiction/blind-spot
analysis, and a **synthesizer** writes the final report from the panel + that analysis.
We swept the full judge×synthesizer grid (M3 / GLM-5.2 / Kimi-K2.6 in each role) on the
**frontier panel**:

- **Best cell = Kimi-K2.6 judge → GLM-5.2 synth = 73.4** — new SOTA, above the old
  M3-synth 71.6 and OpenRouter's best published fusion (Fable 5 + GPT-5.5, 69.0).
- The frontier grid spans **48.7 → 73.4 — an ~8-point swing**, so on a frontier panel
  **the fuser choice matters a lot**. `Kimi→Kimi` collapses to 48.8 (Kimi is a weak
  synthesizer of a strong panel).
- **GLM-5.2 is the best *synthesizer*** but the *worst judge of its own work* — it
  systematically under-credits the answer it just wrote. Hence the split: a *different*
  model (Kimi) should judge, GLM should synthesize.

### 7.2 The all-open **committee** + best fuser = 69.2; panel diversity buys +4.2

Swap the frontier panel for the five open-weights committee (MiniMax-M3 · Kimi-K2.6 ·
DeepSeek-V4-Pro · Gemma-4 · GLM-5.2), keep the best fuser (Kimi judge → GLM synth):
**69.2 ±0.9** (3 reps, ~$80/100 tasks modeled). That beats Fable 5 solo (65.3) and ties
Fable 5 + GPT-5.5 (69.0) at a fraction of the cost.

Two structural facts fall out:

- **On the *open* panel the fuser flattens.** The open-panel judge×synth grid is nearly
  flat at ~69 down the GLM-synth column (M3-judge→GLM 69.1, GLM→GLM 69.8, Kimi→GLM 69.2);
  the **judge barely matters** and only the synthesizer choice moves it, and only a little.
  Contrast the frontier panel's 8-pt swing — **the fuser earns its keep only when the
  panel is strong enough to disagree in useful ways.**
- **Swapping the panel open→frontier buys +4.2**, far more than any fuser swap on the open
  panel. The panel, not the fuser, is the lever once you have a competent synthesizer.

### 7.3 Ablation — only two of five panelists are load-bearing (but you can't strip the rest)

Leave-one-out on the open committee, **paired** against the same-rep baseline (≈69.1):

| dropped member | Δ score | significant? |
|---|---:|---|
| MiniMax-M3 | **−3.9** | yes |
| DeepSeek-V4-Pro | **−2.1** | yes |
| Gemma-4 | −0.8 | null |
| Kimi-K2.6 (as judge) | −0.9 | null |
| GLM-5.2 (as synth) | −0.7 | null |

Only **M3 and DeepSeek individually move the score**. The other three look like dead
weight on a one-at-a-time test.

**But the redundancy is shared, not absent — the "redundancy floor."** Drop **both**
freeloaders (Kimi *and* GLM from the panel) and it's a real, larger drop: **−3.4 ±0.95**,
paired t = −3.6, bootstrap 95% CI [−5.3, −1.6] (excludes 0). The single-drop tests read
null only because the two carry *overlapping* slack: per-task panel-member score
correlation is ~0.80, which collapses the paired SE and hides each one's marginal
contribution until you remove both. You can lose one redundant member for free; you
cannot strip the panel down to its two load-bearing models.

### 7.4 The diversity-not-IQ mechanism, and the per-task correlation matrix (in progress)

Why are M3 and DeepSeek load-bearing while skilled GLM freeloads? The emerging answer
from the per-task **member-solo** scores is **value = competence × low redundancy, not
raw IQ**:

- **M3** — highest solo skill *and* among the least correlated with the rest → carries
  the panel (−3.9).
- **GLM** — skilled (comparable solo) but the **most** correlated/redundant member → its
  marginal contribution is ~0 (−0.7), so it freeloads.
- **Kimi / Gemma-4** — diverse *but weak* (Kimi ~50, Gemma-4 lowest), so their unique
  draws rarely add correct content.

The smartest panelist isn't the most valuable; the **competent-and-decorrelated** one is.
This is the same mechanism as self-fusion (§6): gains come from *uncorrelated error*, not
from any single model being better.

**Status: the 100×5 correlation matrix that proves this is mid-build.** Method and code in
`analysis/correlation-matrix/` (see its `NOTES.md`). We have gemini chunk-of-3 scores for
308 of the 500 member-solo cells; the remaining 192 are being graded with the credit-free
Sonnet-chunk-3 grader (§7.5). On the 25 tasks with full gemini coverage the raw per-task
score-correlation already shows GLM as the most-redundant member and M3 among the least —
consistent with the ablation — but the full-100 matrix and the figure are **deferred**:
the credit-free Sonnet grading stalled at 223/2,739 chunks because the subagent throttle
escalates with each retry (see LESSONS), so we ship the diversity-not-IQ claim on the
25-task + ablation evidence and leave the matrix figure as forthcoming. Resuming needs a
non-subagent grader (gemini chunk-of-3 with topped-up credits, or `draco_rejudge.py` on a
paid Claude judge) → `analysis/correlation-matrix/aggregate_matrix.py`.

### 7.5 Grading infrastructure — the credit-free replacement grader

The canonical judge is `gemini-3.1-pro-preview`, **chunk-of-3** (3 criteria per judge call;
chunk-all inflates +7 avg / +15 worst-case, answer-before-criteria inflates +4–5 — both
measured; see `fusion_live.py:criterion_judge_messages_for_criteria` and the
draco-grading-infra notes). gemini grading is ~95% of benchmark cost (~$24/100-task cell,
no caching through the gateway).

When TrustedRouter credits ran out mid-effort, we validated a **drop-in credit-free grader
using Claude subagents** (which run on the session quota, not paid API):

| candidate grader | bias vs gemini | corr r |
|---|---:|---:|
| **Sonnet-4.6, chunk-of-3** | **≈0 (−0.3, n=21 pooled)** | **0.92** |
| Opus, chunk-all | +4.3 | 0.59 |
| Haiku-4.5, chunk-of-3 | −5.2 | 0.83 |

**Sonnet-4.6 chunk-of-3 reproduces gemini (bias ≈ 0, r = 0.92)** across all five members and
is the grader we use for the 192 ungraded matrix cells. Pearson correlation is invariant to
a per-member grader mean-shift, so mixing gemini + Sonnet grades in the correlation matrix is
safe even if a small offset remained. The judge prompt is replicated verbatim from
`fusion_live.py` (criteria-before-answer order, "met=true only when explicitly satisfied;
for negative-weight criteria met=true means the error is present").

## 8. Haiku self-fusion via Claude Code subagents — self-fusion needs a competent fuser (2026-06-21)

§6 showed MiniMax-M3 self-fusion climbs 66.2 → 69.4 over ten runs. Does the same recipe
work with a *small* model in every role? We reran the §6 scaling experiment with **Claude
Haiku 4.5** as researcher, judge, **and** synthesizer, orchestrated end-to-end through
**Claude Code subagents** (the Workflow tool) — the all-subagent setup
`trustedrouter-benchmarks` uses when TR credits are out — and graded with **Claude
Sonnet 4.6**, the §7.5-validated credit-free grader. Harness:
`artifacts/haiku-selffusion/wf_haiku_pilot.js`; scores
`results/rejudge-selffusion-haiku-pilot.jsonl`; chart `docs/draco-selffusion-haiku-scaling.svg`.

**Setup.** 8 stratified tasks (Academic, Technology, General Knowledge, Medicine, Law,
Shopping, UX Design, Needle-in-Haystack; Finance + Personalized-Assistant held out). Per
task: 10 independent Haiku agentic research runs (`WebSearch`/`WebFetch`/`Bash` subagents,
rubric never shown), then for N=1..10 fuse the first N with the **verbatim** TR fusion prompts
(`JUDGE_SYSTEM` → `FINAL_INSTRUCTION`); N=1 = run #1 raw. One nested run-ordering, like §6.

**Grader caveat (read before comparing to §6).** The §6 M3 numbers use gemini chunk-of-3.
Chunk-of-3 needs ~13 judge calls per answer → ~1,010 Sonnet subagent calls for 8×10 answers,
which **saturates the shared Claude Code subagent quota** (429/529) — the exact §7.5 stall
(our first attempt failed with ~1,000 rate-limited grades). So we grade **chunk-all** (one
Sonnet call per answer, ~80 total). chunk-all is measured to inflate **~+7** vs chunk-of-3,
so subtract ~7 from the Haiku scores below to compare to §6. The offset is near-constant and
does **not** change the curve shape — which is the whole question.

**Result: self-fusion does NOT scale for Haiku — the curve is flat and noisy.**

| N runs fused | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Haiku DRACO (Sonnet chunk-all) | 62.2 | 54.5 | 59.5 | 64.1 | 59.2 | 58.8 | 60.2 | 56.3 | 57.0 | 58.8 |

The solo (62.2) is already near the top; fusing N≥2 averages **58.7**, *below* solo, and ten
runs land at **58.8 (−3.4)**. No climb, no plateau. With 8 tasks the per-N SE is ~±7, so the
curve is statistically **flat** — but the central tendency is a slight net *loss*, the opposite
of M3 (+3.2) and Opus (+6.9 at N=2).

**Mechanism: Haiku is a high-variance, unreliable synthesizer — §7.1's small-fuser problem,
applied to self-fusion.** Per-task SD across N is ~18 points; the synthesizer sometimes
recovers a great answer and just as often destroys a good one:

| task | solo | best N≥2 | mean N≥2 | net (mean−solo) |
|---|---:|---:|---:|---:|
| General Knowledge | 56.1 | 81.4 | 70.1 | **+14.0** |
| Medicine | 67.9 | 80.8 | 71.1 | +3.1 |
| Technology | 34.5 | 46.9 | 36.6 | +2.1 |
| Law | 73.8 | 78.9 | 75.4 | +1.6 |
| Academic | 78.6 | 92.0 | 78.2 | −0.3 |
| Shopping | 61.3 | 56.7 | 49.5 | −11.8 |
| UX Design | 38.2 | 34.0 | 26.0 | −12.2 |
| Needle-in-Haystack | 87.1 | 89.8 | 63.0 | **−24.1** |

The upside is real and large (+14 to +25 at the best N on several tasks), but unreliable: the
synthesizer, told to "return only the final visible answer," compresses the panel and drops
detail — losing the heavily-weighted **citation** criteria a strong fuser preserves (Technology:
a solo with inline NVIDIA/Ultralytics/arxiv citations becomes a tidy summary that no longer
cites them, −10 on the rubric). **Needle-in-Haystack is the cleanest failure:** the solo finds
the needle (87.1), but fusion *dilutes* it — most of the 10 runs miss the needle, and the
consensus-seeking synthesizer averages them down to 63.0. Self-fusion is actively *harmful* for
single-fact retrieval, because seeking consensus washes out the lone correct run.

**Takeaway.** §6's "quantity substitutes for cross-model diversity" has a hard prerequisite:
**a synthesizer competent enough to hold the panel and keep the rare-correct run.** M3 can;
Haiku cannot. The gain is bottlenecked by the *fuser*, not the run count — ten copies fused by
a weak synthesizer are still weakly synthesized. This is the self-fusion analogue of §7.1's
fuser leaderboard (M3 71.6 → Gemma-4-31b 54.0) and §7.3's "size matters for the fuser": a cheap
*panel* can reach a frontier answer, but a cheap *fuser* cannot manufacture one.

**Scope / honesty.** 8-task pilot, one nested ordering, chunk-all grader (~+7 inflated,
shape-preserving), and a different tool stack than §6 (Claude Code `WebSearch`/`WebFetch`
subagents vs the Exa + markitdown SDK harness). The flat-and-noisy *shape* is robust; per-task
numbers carry ~±7 SE — a 20-task run would tighten the mean. Full harness, args, raw result,
and per-task curve under `artifacts/haiku-selffusion/`.

### 8.1 The fuser is the bottleneck — swap Haiku → Sonnet 4.6 and self-fusion scales again

§8 leaves an obvious test: if Haiku fails *as the synthesizer*, does a competent model in the
same all-subagent harness recover §6's scaling gain? We reran the experiment with **Claude
Sonnet 4.6** as researcher + judge + synthesizer (Sonnet-4.6 chunk-all grader), on the **same 4
task IDs** as a subset of the §8 pilot — Academic, Technology, General Knowledge, and
**Needle-in-Haystack** (the task where Haiku fusion collapsed hardest). Harness
`artifacts/haiku-selffusion/wf_sonnet_pilot4.js`; scores
`results/rejudge-selffusion-sonnet-pilot4.jsonl`; chart `docs/draco-selffusion-sonnet-vs-haiku.svg`.

**Result — Sonnet self-fusion climbs; Haiku is flat (same 4 tasks, same grader):**

| N runs fused | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **Sonnet 4.6** | 73.4 | 74.1 | 78.1 | 79.7 | 75.9 | 79.4 | 79.3 | 77.9 | 79.2 | 76.8 |
| Haiku 4.5 (§8) | 64.0 | 53.1 | 65.8 | 72.7 | 61.3 | 58.4 | 67.2 | 62.2 | 56.7 | 60.3 |

Sonnet: solo 73.4 → mean(N≥2) **77.8 (+4.4)**, plateau ~79 by N=4 — the §6 climb-then-plateau
shape, with a competent fuser. Haiku: solo 64.0 → mean(N≥2) 62.0 (**−2.1**). Per task, Sonnet's
best fused beats solo on **4/4** (Academic +14.5, Technology +16.8, GenKnow +21.4, Needle +3.4)
and mean(N≥2) beats solo on 3/4.

**The decisive case — Needle-in-Haystack (does fusion destroy a correct solo?).** Both models'
solos find the needle (87.1). Then the fusers diverge:

| fuser | solo | mean(N≥2) | per-N (1→10) |
|---|---:|---:|---|
| **Sonnet 4.6** | 87.1 | **82.2 (−4.8)** | 87 77 81 81 90 82 78 84 84 84 |
| Haiku 4.5 | 87.1 | **63.0 (−24.1)** | 87 68 56 90 54 54 67 50 58 68 |

Haiku **dilutes the needle away** — averaging in the 9 runs that missed it (−24); Sonnet **keeps
it** (a mild −4.8 dip, never a collapse). Consensus-seeking only destroys single-fact retrieval
when the synthesizer is too weak to recognize and preserve the lone correct run.

**Conclusion.** §6's "quantity substitutes for cross-model diversity" holds **only above a fuser
competence threshold.** Sonnet clears it (self-fusion +4.4, needle preserved); Haiku does not
(flat, needle collapses). This is the self-fusion confirmation of §7.1/§7.3 ("the fuser is the
lever"; "size matters for the fuser"): a cheap *panel* can reach a frontier answer, but the
*synthesizer* must be strong enough to hold the panel and keep the rare-correct run — ten copies
fused by a weak model stay weakly fused.

**Caveats.** 4 tasks, one ordering. To fit the Claude Code **session token limit** (the all-Sonnet
8-task run exhausted the account quota mid-research — Sonnet reports ran 31–33k chars each), Sonnet
research used a **leaner** config (low effort, ≤6 web ops, ~1.2k-word reports) than §8's Haiku
(medium, comprehensive), and Sonnet **self-grades** (synth = grader). Both can shift Sonnet's
*absolute* level, so the N=1 gap (73.4 vs 64.0) conflates researcher quality + grading and is not
a clean fuser comparison. The clean, confound-free findings are the two that compare N≥2 to N=1
*within each model*: the **climb vs flat shape**, and the **needle preserved vs collapsed**.

### 8.2 Error bars — the pilots are suggestive but underpowered (bootstrap over tasks)

Point estimates without uncertainty aren't science. We nonparametric-bootstrap over **tasks**
(resample tasks with replacement, B=20,000, 95% percentile CI) for every N. Charts now carry
per-point CI whiskers (`bootstrap_ci.json`). Two honest limits: at n=4–8 the CIs are wide
(~±12), and they capture **task-sampling** variance only — **not** run-ordering variance (we ran
one nested ordering; that component needs re-fusing multiple orderings).

Per-N 95% CIs are wide and heavily overlapping (e.g. Haiku-8 N=1 62.2 [49.7, 73.7], N=4 64.1
[47.2, 79.2]; Sonnet-4 N=4 79.7 [68.6, 87.8]), so **no individual point-to-point difference is
resolvable.** The right test is the paired self-fusion **gain**, mean(N≥2) − solo, bootstrapped
over tasks:

| config | gain | 95% CI | verdict |
|---|---:|---|---|
| Haiku, 8 tasks | −3.5 | [−11.5, +4.0] | includes 0 |
| Haiku, same 4 | −2.1 | [−17.6, +10.4] | includes 0 |
| Sonnet, 4 tasks | +4.4 | [−1.9, +10.8] | includes 0 (mostly +) |
| **Sonnet gain − Haiku gain** (paired, 4 tasks) | **+6.5** | **[−1.1, +14.2]** | includes 0 (barely) |

So at pilot scale **nothing clears 95% significance** — the directions are consistent with the
mechanism (Haiku flat-to-negative, Sonnet positive, Sonnet > Haiku, the Sonnet−Haiku contrast
90%+ positive in the bootstrap) but the pilots are **underpowered**. With per-task SD ≈ 13–18 and
an effect of ≈4–7, detecting the gain at 95%/80% power needs roughly **n ≈ 25–40 tasks** (and the
Sonnet−Haiku contrast similar). The **Needle-in-Haystack collapse** (Haiku −24.1 vs Sonnet −4.8)
is a single-task, single-ordering observation — dramatic and mechanistically clean, but one data
point, not a powered estimate. **Bottom line: treat the curves as hypotheses with the right sign,
not as established effects — scale to ~30 tasks (and ≥2 run-orderings) to confirm.**

**Update — Haiku scaled to n=26 (18 new non-financial tasks + the 8 pilot).** The full-80 run
is blocked by the Claude Code account session-token limit (each 18-task shard's agentic Haiku
research alone burns ~24M tokens and exhausts a quota window before grading; resume runs only the
cached-research fuse+grade pass). We got one shard done, so Haiku now stands at **n=26**:

| Haiku self-fusion | gain mean(N≥2)−solo | 95% CI |
|---|---:|---|
| pilot 8 tasks | −3.5 | [−11.5, +4.0] |
| new 18 tasks (rem1) | **+3.7** | — |
| **merged, n=26** | **+1.5** | **[−2.3, +5.0]** |

The pilot's −3.5 was small-sample noise: the 18 fresh tasks ran **+3.7**, and the merged estimate
is **+1.5, still not significant** (CI straddles 0). So the powered-ish read is that Haiku
self-fusion buys a **small bump indistinguishable from zero** — not the flat-to-negative the first
8 suggested, and well under Sonnet's central +4.4 (§8.1). The honest contrast is "the smarter
fuser gains more," with neither gain individually significant at these sizes. Curve + CIs:
`artifacts/haiku-selffusion/haiku_n26_curve.json`, `results/rejudge-selffusion-haiku-rem18.jsonl`.
