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
to handle them. Result: see `results/` / the run output once complete.

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
