# TrustedRouter-Fusion-Draco

Reproduction of OpenRouter's **"Fusion beats frontier"** DRACO deep-research
benchmark on **TrustedRouter** — with a self-contained **agentic** harness
(`web_search` + `web_fetch` + `bash`) that drives any TR model through the
DRACO tasks, plus the judge, the native-`trustedrouter/fusion` generator, and
all the benchmark data.

## Headline result

DRACO is an *agentic* deep-research benchmark. Feeding models pre-fetched
context in a single shot under-scores them by ~20 points. Give them **live
tools** and they reach (and exceed) OpenRouter's published solos — same judge
(`google/gemini-3.1-pro-preview`), same tasks:

| solo (15-task slice) | frozen context | **live tools** | Δ | OpenRouter (100-task) |
|---|---:|---:|---:|---:|
| Kimi K2.6 | 47.0 | **64.0** | +17.0 | 53.7 |
| DeepSeek V4 Pro | 45.2 | **68.3** | +23.1 | 60.3 |

We run **above** OpenRouter because our agentic harness is more generous than
their server-tool config (more tool calls, larger fetches, a dedicated synthesis
turn) — *not* leakage (audited: 568 searches + 155 fetches, zero rubric/answer
retrieval). See [docs/FINDINGS.md](docs/FINDINGS.md) for the full analysis and the
disclosed deviations.

## Layout

```
src/trusted_router/evals/agentic_tools.py   the agentic web_search/web_fetch/bash loop
src/trusted_router/evals/draco_replay.py    replay schema + criterion rejudge
src/trusted_router/evals/{fusion_live,exa,draco,fusion_micro}.py   client, Exa, tasks, judge
scripts/draco_agentic_solo.py               run a solo model agentically (the main harness)
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

**Native fusion** (panel + Opus fuser inside the attested gateway):

```bash
uv run python scripts/draco_native_fusion_gen.py \
  --source-replay <a replay with task+searches> --output out/fusion.replay.private.jsonl \
  --inner-max-completion-tokens 4000 --outer-max-tokens 6000 --execute
```

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
5. (Opus fuser truncation: it inherits the OUTER `max_tokens`, not
   `max_completion_tokens` — raise the outer cap.)

See [docs/LESSONS.md](docs/LESSONS.md) — these are the kind of bugs that only
surface under real agentic load.

## Honesty notes

- The published **64.7** is the full-100 Fusion number; solo numbers are graded
  with a multi-pass judge. Disclose your exact tool budget, fetch size, synthesis
  turn, and judge pass count when comparing.
- Keep the DRACO dataset/rubric hosts excluded from search+fetch, and audit
  queries/URLs after every run (the harness filters tool results against the
  rubric, but verify).
- Private generation replays (model prompt/output) are git-ignored; only judged
  scores live in `results/`.
