# Raw run data — agentic replay traces

These are the **raw runs behind every published number** — not just the scores,
the actual research. Each line in a `*.jsonl` file is one DRACO task, and it
records the whole agentic loop:

- the **prompts** the model saw (system prompt + the task problem),
- every **tool call** it made — `web_search` queries, `web_fetch` URLs,
  `sec_facts` (EDGAR XBRL) lookups, `bash` commands — with the result size and
  any error,
- the model's **final report** (the text the judge scored),
- token counts, finish reason, and elapsed time.

The judged versions of these same runs (criterion-by-criterion against the
rubric) live in [`../results/`](../results/). These files are the *input* to that
judging — score them yourself with `scripts/draco_rejudge.py`.

Each file is the full 100 tasks, deduped to one successful row per task.

## Solos — each model drives its own research loop

| file | model | DRACO score |
|---|---|---:|
| `solo-gpt55.jsonl` | openai/gpt-5.5 | 63.0 |
| `solo-opus.jsonl` | anthropic/claude-opus-4.8 | 60.7 |
| `solo-deepseek.jsonl` | deepseek/deepseek-v4-pro | 59.9 |
| `solo-kimi.jsonl` | moonshotai/kimi-k2.6 | 50.1 |
| `solo-gemini-31-pro.jsonl` | google/gemini-3.1-pro-preview | 47.4 |
| `solo-gemini-flash.jsonl` | google/gemini-3-flash-preview | 41.1 |

## Fusion — panel reports → Gemini-3.1-Pro judge analysis → fuser

The full fuser leaderboard — same frontier panel (`gpt-5.5 + opus + gemini-flash +
kimi + deepseek`), same judge analysis, swap only the synthesizer:

| file | fuser | DRACO score |
|---|---|---:|
| `fusion-frontier-minimax.jsonl` | MiniMax-M3 *(open weights)* | **71.6 (SOTA)** |
| `fusion-frontier-glm.jsonl` | GLM-5.2 *(open weights)* | 71.1 ‡ |
| `fusion-frontier-opus.jsonl` | Claude Opus 4.8 | 70.6 |
| `fusion-frontier-kimi.jsonl` | Kimi K2.6 *(open weights)* | 67.0 |
| `fusion-frontier-deepseek.jsonl` | DeepSeek V4 Pro *(open weights)* | 65.7 |
| `fusion-frontier-gpt55.jsonl` | GPT-5.5 | 62.2 |
| `fusion-frontier-gemma4.jsonl` | Gemma-4-31b *(open weights)* | 54.0 |
| `fusion-budget-opus.jsonl` | Opus 4.8 (budget 3-model panel) | 62.6 |

The best fuser is open-weights MiniMax-M3, with no censorship hole. GPT-5.5 is the
top *solo* researcher yet a weak fuser — synthesis is a skill apart. Gemma-4-31b
collapses: a 31B model is too small to hold and reconcile a frontier panel.

‡ GLM-5.2 ties at the top (71.1) but returned empty content on 1 of 100 tasks —
**political censorship**, not a context limit: that task's panel covered a *Greater
China* fund's China/Hong-Kong/Taiwan split, and GLM-5.2 (Zhipu / Z.AI) silently
refuses Taiwan/Hong-Kong sovereignty content (neutralize those two words and it fuses
fine). Scored 0; over the 99 it answered it averages 71.8. That blind spot is why we
default to MiniMax-M3.

A fusion row's `fusion.panel` lists the panel roster; the panelists' own tool
traces are the solo files above.

## Verify the leakage audit yourself

Across these runs the models issued **5,251 web searches + 2,207 web fetches + 55
sec_facts lookups** (725 distinct hosts; top: sec.gov, law.cornell.edu,
wikipedia, arxiv, nature, pubmed, worldbank). **Zero** retrieval of any DRACO /
Perplexity / HuggingFace / OpenRouter / rubric / answer-key host. Re-run it:

```python
import json, glob
from urllib.parse import urlparse
BAD = ("perplexity.ai","huggingface.co","draco","openrouter.ai","answer-key","gradeset")
for path in glob.glob("replays/*.jsonl"):
    for line in open(path):
        row = json.loads(line)
        for t in (row.get("agentic") or {}).get("tools", []):
            if t.get("name") == "web_fetch":
                url = (t.get("args") or {}).get("url","")
                assert not any(b in url.lower() for b in BAD), (path, url)
print("clean")
```
