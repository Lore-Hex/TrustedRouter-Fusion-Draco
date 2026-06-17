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
