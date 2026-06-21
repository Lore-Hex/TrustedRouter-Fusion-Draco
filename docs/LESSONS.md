# Lessons: running model benchmarks (agentic / tool-using)

Brief, hard-won notes from reproducing OpenRouter's Fusion DRACO benchmark through
the TrustedRouter gateway (2026-06-16). Most apply to any benchmark where models
call tools.

## Methodology

- **Agentic benchmarks need real tools.** Feeding models frozen/pre-fetched context
  in a single shot under-scores them by ~20 points vs. giving them live tools. If the
  benchmark is "deep research" (DRACO) or otherwise agentic, the model must drive its
  own `web_search` / `web_fetch` / `bash` loop. Single-shot is the wrong harness.
- **Match the judge exactly, then compare apples-to-apples.** Pin the judge *model*
  AND the *pass count*. 1-pass is noisier and can diverge from a multi-pass reference
  number. Re-judge every config with identical settings; never compare a fresh score
  to a published one graded differently.
- **Beware slice-vs-full comparisons.** A 10–15 task slice is not the published
  100-task number. Verify the slice isn't systematically easier/harder before reading
  into deltas (we wrongly called a slice "harder" — it wasn't).
- **Audit for benchmark leakage after every run.** Dump all search queries + fetched
  URLs and grep for the dataset/rubric/answer-key (dataset host, "rubric", "answer
  key", the paper's arxiv id). Filter tool results against the rubric itself
  (criterion ids, first-N-words of each requirement, forbidden terms) and block the
  dataset/paper hosts — but don't blanket-block legit source domains (e.g. arxiv).

## Gateway / provider gotchas (test multi-turn tools per provider!)

Real agentic load surfaced three gateway bugs that single-shot tests missed. Always
run a **2-turn function-tool conversation per provider** before trusting tool results:

- **DeepSeek (OpenAI-compatible) returned EMPTY content after a tool result** — the
  gateway round-trips OpenAI→Anthropic→OpenAI and was passing Anthropic `tool_use`/
  `tool_result` blocks through verbatim to an OpenAI API. Reverse-translate them.
- **Vertex/Gemini had no function-tool support at all** — tools weren't sent, tool
  history wasn't translated. Needed OpenAI↔Gemini `functionDeclarations` /
  `functionCall` / `functionResponse` translation both ways.
- **Gemini 3 requires `thought_signature` round-trip** — every `functionCall` returns
  an opaque signature that must be echoed back on the next turn (400 otherwise).
  OpenAI tool_calls have no field for it; stash it in the tool_call `id`.

## Harness design

- **Don't hard-cap tool calls then abruptly force an answer.** Models leak raw
  tool-call markup into content (DeepSeek `<｜｜DSML｜｜>`, Kimi/Claude `<invoke>`),
  repeat "let me continue", or truncate. Use a generous research budget **+ a
  dedicated synthesis turn** (no tools, "write the final report now"), and strip any
  leaked markup as a safety net.
- **Force the first tool call for reluctant models.** gemini-flash answers from prior
  knowledge unless `tool_choice:"required"` on turn 1.
- **bash tool:** a local Docker container (`python:3.12-slim`, `--network none`) is a
  fine sandbox for calculation tools.
- **Save every replay + report artifact**, and make generation a saved script — a
  one-off that produced a key result and wasn't saved cost a full re-derivation.

## Grading at scale (chunk-of-3, credit-free graders, subagent fan-out)

- **Chunk size and ordering are load-bearing for the grade, not just cost.** Judging
  criteria in **chunks of 3** (separate judge calls) vs all-at-once changes the *number*:
  chunk-all inflates ~+7 avg / +15 worst-case; answer-before-criteria inflates +4–5. Keep
  chunk-of-3 and criteria-before-answer for any score you'll compare to a reference. There
  is no cheap score-neutral lever — valid grading is just expensive.
- **You can swap graders if you validate calibration first.** Out of paid-API credits, we
  replaced gemini with **Claude subagents** (session quota, not billed). Validate the swap
  on a shared slice before trusting it: **Sonnet-4.6 chunk-of-3 matched gemini (bias ≈ 0,
  r = 0.92)**; Opus chunk-all was +4.3/r0.59 and Haiku chunk-3 was −5.2/r0.83 — same data,
  very different graders. Replicate the reference judge's *exact* prompt (system text +
  message order), not a paraphrase.
- **Pearson correlation is invariant to a per-grader mean-shift** — so a correlation/
  structure analysis can safely mix two graders even if one has a small constant offset.
  A *level* comparison (leaderboard) cannot; re-grade those with one grader.
- **Subagent fan-out for grading: bound it and pace it.** Two failure modes bit us:
  (1) a loop on `budget.remaining()` with no token budget returns `Infinity` → runs to the
  1000-agent hard cap (burned ~9M tokens on a dead run) — give every such loop a hard
  iteration cap; (2) even a *bounded* ~685-agent workflow at ~14 concurrency gets
  **server-rate-limited** ("temporarily limiting requests", transient, not the usage cap)
  and loses a large fraction of calls. Grade in smaller waves, run waves **sequentially**
  (parallel workflow launches each get their own concurrency cap and compound the limit),
  and loop re-grading stragglers until complete.
- **Workflow result plumbing:** the tool's output file is `{summary,agentCount,logs,result}`
  and `result` can be a *double-encoded* JSON string — `d=json.loads(file); res=d["result"];
  while isinstance(res,str): res=json.loads(res)`. Have agents return compact rows and
  reconstruct identity (e.g. `idx`,`ci`) from the input filename, not from agent output.
