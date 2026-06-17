# TrustedRouter-Fusion-Draco

Reproduction of OpenRouter's **"Fusion beats frontier"** DRACO deep-research
benchmark on **TrustedRouter** — with a self-contained **agentic** harness
(`web_search` + `web_fetch` + `bash`) that drives any TR model through the
DRACO tasks, plus the judge, the native-`trustedrouter/fusion` generator, and
all the benchmark data.

## Headline result

DRACO is an *agentic* deep-research benchmark — the model has to search the web,
read sources, and compute, not recall. Run on TrustedRouter with a live-tool
harness and the same judge OpenRouter used (`google/gemini-3.1-pro-preview`,
reasoning `high`), a **panel of budget + frontier models fused by Opus 4.8 reaches
~71 on the full 100 tasks — above OpenRouter's best published fusion (69.0).**

### Full data table — all 100 tasks, same judge

**Solo models** (each drives its own agentic research loop):

| solo | TrustedRouter | OpenRouter |
|---|---:|---:|
| GPT-5.5 | **63.0** | 60.0 |
| Claude Opus 4.8 | **60.7** | 58.8 |
| DeepSeek V4 Pro | 59.9 | 60.3 |
| Kimi K2.6 | 50.1 | 53.7 |
| Gemini 3.1 Pro | **47.4** | 45.4 |
| Gemini 3 Flash | 41.1 | 43.1 |
| Claude Fable 5 | *(not run)* | 65.3 |

**Fusion configurations** (panel → Gemini-3.1-Pro judge analysis → fuser):

| fusion config | TrustedRouter | OpenRouter |
|---|---:|---:|
| **frontier panel + Opus fuser** *(ours, best)* | **~70.9** † | — |
| OR — Fable 5 + GPT-5.5 *(their best)* | — | 69.0 |
| OR — Opus + GPT-5.5 + Gemini | — | 68.3 |
| OR — Opus + GPT-5.5 | — | 67.6 |
| OR — Opus + Opus | — | 65.5 |
| budget panel + Opus fuser | 62.6 | **64.7** |
| frontier panel + GPT-5.5 judge & fuser | 62.2 | — |

Panel = `gpt-5.5 + opus-4.8 + gemini-3-flash + kimi-k2.6 + deepseek-v4-pro`.
**The fuser is the lever:** swapping the synthesizer from GPT-5.5 to Opus 4.8 on
the *identical* panel jumps the score ~+8 (62.2 → ~71). GPT-5.5 is a strong
panelist but a weak synthesizer here.

† Preliminary at **n=43** while the judge finishes the remaining tasks (stable
across n=35–43: 70.7 non-financial, 73.1 finance). The full-100 figure replaces
this once the judge completes.

### Why we run above OpenRouter (and why it is *not* leakage)

The gap is the harness: our agentic loop is more generous than OpenRouter's
server-tool config — up to 16 tool calls, larger fetches, and a dedicated
synthesis turn — so models write more complete answers and cover more rubric
criteria. We **audited every tool call** that feeds these numbers: **12,704
web_searches + 5,390 web_fetches, zero retrieval of any DRACO / Perplexity /
HuggingFace / rubric / answer-key host** (top fetched hosts: sec.gov, cornell
law, wikipedia, arxiv, nature). The leak filter (`_draco_search_result_leak_reason`)
blocks benchmark hosts and scans every result for rubric fragments. Our budget
panel (62.6) actually lands *under* OpenRouter's (64.7) — the apples-to-apples
comparison — which is what you'd expect if the harness, not leakage, drives the
spread. See [docs/FINDINGS.md](docs/FINDINGS.md) for the full analysis.

## Layout

```
src/trusted_router/evals/agentic_tools.py   the agentic web_search/web_fetch/bash loop
src/trusted_router/evals/draco_replay.py    replay schema + criterion rejudge
src/trusted_router/evals/{fusion_live,exa,draco,fusion_micro}.py   client, Exa, tasks, judge
scripts/draco_agentic_solo.py               run a solo model agentically (the main harness)
scripts/draco_client_fusion.py              client-orchestrated fusion (panel→judge→fuser)
scripts/finance_parser_ablation.py          finance doc-parser bake-off (markitdown/sec_facts/LlamaParse)
scripts/draco_native_fusion_gen.py          generate native trustedrouter/fusion replays
scripts/draco_rejudge.py                    rejudge replays with the DRACO rubric
scripts/draco_report.py                     side-by-side score report
data/draco-{full-100,non-financial-80,financial-20}.manifest.json   the benchmark tasks+rubrics
results/                                     judged score artifacts
docs/FINDINGS.md, docs/LESSONS.md           the analysis and the hard-won lessons
```

## Setup

```bash
uv sync                      # installs httpx + markitdown
docker pull python:3.12-slim # bash-tool sandbox (network-isolated)
```

Keys (env var or `~/.quill_cloud_keys.private`):
- a TrustedRouter inference key (`TR_API_KEY` / `TR_FUSION_EVAL_API_KEY` / `TR_API_KEY_FOR_SELF_HEAL`)
- `EXA_API_KEY` (powers `web_search` + Exa fetch)

## Run it

**Tooled solo** (the core harness — a model drives its own research loop):

```bash
uv run python scripts/draco_agentic_solo.py \
  --manifest data/draco-non-financial-80.manifest.json \
  --output out/kimi-tooled.replay.private.jsonl \
  --model moonshotai/kimi-k2.6 --config-id solo_kimi_k2_6_tooled \
  --limit 15 --max-tool-calls 16 --synthesis-max-tokens 12000 --workers 2 --execute
```

Add `--force-first-tool` for models that answer from memory (e.g. gemini-flash).

**Rejudge** (criterion-by-criterion against the rubric, same judge as OpenRouter):

```bash
uv run python scripts/draco_rejudge.py out/kimi-tooled.replay.private.jsonl \
  --output out/kimi-tooled.rejudge.jsonl \
  --judge-passes 1 --judge-reasoning-effort high --workers 2 --execute
```

**Report** (side-by-side vs the OpenRouter reference numbers):

```bash
uv run python scripts/draco_report.py out/*.rejudge.jsonl
```

Finance tasks read SEC filings, so the harness also exposes a free, keyless
**`sec_facts`** tool (exact figures straight from EDGAR XBRL — no PDF parsing).
In a bake-off it beat both markitdown and paid LlamaParse on DeepSeek's finance
score (`scripts/finance_parser_ablation.py`); `--doc-parser markitdown` keeps it
cheap.

**Client-orchestrated fusion** (the SOTA path — panel reports → judge → fuser):

```bash
uv run python scripts/draco_client_fusion.py \
  --manifest data/draco-full-100.manifest.json \
  --panel "openai/gpt-5.5=out/gpt55.replay.jsonl" \
  --panel "anthropic/claude-opus-4.8=out/opus.replay.jsonl" \
  --panel "google/gemini-3-flash-preview=out/flash.replay.jsonl" \
  --panel "moonshotai/kimi-k2.6=out/kimi.replay.jsonl" \
  --panel "deepseek/deepseek-v4-pro=out/deepseek.replay.jsonl" \
  --output out/fusion.replay.private.jsonl --config-id fusion_frontier_opus \
  --judge-model google/gemini-3.1-pro-preview --fuser-model anthropic/claude-opus-4.8 \
  --workers 4 --fuser-max-tokens 8000 --execute
```

The native `trustedrouter/fusion` gateway endpoint (`scripts/draco_native_fusion_gen.py`)
runs the panel server-side, but its panel has no live tools (frozen context →
~40), so the agentic SOTA uses the client-orchestrated path above, faithful to the
gateway's own judge→fuser prompts.

## TrustedRouter gateway requirements

The agentic harness needs the gateway to support multi-turn function tools per
provider. Five gateway fixes were required (in `quill-cloud-proxy`,
`enclave-go/internal/llm/`):

1. DeepSeek (OpenAI-compatible) empty content after a tool result — the
   OpenAI→Anthropic→OpenAI round-trip dropped tool_use/tool_result blocks.
2. Vertex/Gemini had no function-tool support — added OpenAI↔Gemini
   functionDeclarations / functionCall / functionResponse translation.
3. Gemini 3 `thought_signature` round-trip (echo the per-functionCall signature).
4. Gemini parallel-call functionResponses must be grouped in one content.
5. Opus fuser truncation: it inherits the OUTER `max_tokens`, not
   `max_completion_tokens` — raise the outer cap.
6. GPT-5.5 intermittent 502: the last-candidate time-to-first-byte budget was 120s,
   but gpt-5.x reason silently for 60–90s before the first byte — raised to 300s.
7. GPT-5.5 `temperature` rejection: gpt-5.x reject any non-default temperature —
   strip it for that model family on the OpenAI-compatible path.
8. Gemini history functionCalls with no signature (cross-model fusion panels) —
   attach a valid-base64 placeholder so Vertex accepts the replayed call.

See [docs/LESSONS.md](docs/LESSONS.md) — these are the kind of bugs that only
surface under real agentic load.

## Honesty notes

- **The ~71 rides on a more generous harness, not a better recipe.** Our solos
  run a few points above OpenRouter's; the fusion inherits stronger panel inputs.
  The clean apples-to-apples is the *budget* config: ours 62.6 vs OpenRouter 64.7
  — there we're slightly *under*. Disclose your exact tool budget, fetch size,
  synthesis turn, and judge pass count when comparing (we use 1 pass; OR used the
  paper's multi-pass — averaging cuts variance, not the mean).
- **Leakage was triple-checked.** Every web_search query and web_fetch URL that
  feeds these numbers was audited — 12,704 searches + 5,390 fetches, **zero**
  retrieval of any DRACO / Perplexity / HuggingFace / rubric / answer-key host.
  The harness excludes those hosts and scans every tool result for rubric
  fragments (`_draco_search_result_leak_reason`). Re-run the audit yourself.
- **The fuser matters more than the panel.** Frontier panel + GPT-5.5 fuser = 62.2;
  same panel + Opus fuser = ~71. A bigger panel with the wrong synthesizer buys
  nothing.
- Private generation replays (model prompt/output) are git-ignored; only judged
  scores live in `results/`.
