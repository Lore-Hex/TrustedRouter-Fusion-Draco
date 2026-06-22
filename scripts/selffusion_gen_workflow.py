#!/usr/bin/env python3
"""Generate a self-contained Claude Code Workflow (.js) for a self-fusion run.

Transforms the canonical harness ``artifacts/haiku-selffusion/wf_haiku_pilot.js``
(Haiku researcher+judge+synth, Sonnet-4.6 chunk-all grader) by embedding a chosen
task slice as ``const INPUT = {...}`` (Workflow subagents can't read the repo, and
the ``args`` channel proved unreliable, so task data is embedded inline). With
``--model sonnet`` it also swaps the three generative roles to Sonnet and switches
to the leaner research prompt used in FINDINGS §8.1 (to fit the session-token budget).

The emitted .js is run with the Workflow tool; it returns
``{table, rows, metByKey, ...}``. Save that JSON and feed it to
``scripts/selffusion_analyze.py``.

Examples:
  # 8 stratified Haiku tasks (one per domain), N=1..10
  python3 scripts/selffusion_gen_workflow.py --manifest data/draco-non-financial-80.manifest.json \
    --stratified --limit 8 --model haiku --out artifacts/haiku-selffusion/wf_run.js
  # 4 Sonnet tasks, leaner research
  python3 scripts/selffusion_gen_workflow.py --manifest data/draco-non-financial-80.manifest.json \
    --task-ids ID1,ID2,ID3,ID4 --model sonnet --out artifacts/haiku-selffusion/wf_sonnet_run.js
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "artifacts" / "haiku-selffusion" / "wf_haiku_pilot.js"

OLD_RESEARCH = """function researchPrompt(problem) {
  return (
    'You are a deep-research analyst. Research the question below thoroughly using ' +
    'WebSearch and WebFetch (and Bash for any calculations), then write a comprehensive, ' +
    'well-structured, cited report that fully answers every part of it.\\n\\n' +
    'QUESTION:\\n' + problem + '\\n\\n' +
    'Instructions:\\n' +
    '- Do REAL research: search the web and read primary sources. Do not answer from memory alone.\\n' +
    '- Cover breadth and depth across all sub-parts. Be precise with numbers, dates, names, and cite sources inline.\\n' +
    '- Do NOT search for or fetch any benchmark, rubric, grading, leaderboard, or answer-key material ' +
    '(e.g. DRACO, Perplexity, HuggingFace dataset pages). Research the underlying topic only.\\n' +
    '- Your FINAL message must be ONLY the report itself (no preamble, no meta-commentary, no notes to the reader). ' +
    'It is consumed verbatim as the answer.'
  )
}"""

LEAN_RESEARCH = """function researchPrompt(problem) {
  return (
    'You are a deep-research analyst. Research the question below using WebSearch and WebFetch ' +
    '(and Bash for calculations), then write a focused, well-structured, cited report.\\n\\n' +
    'QUESTION:\\n' + problem + '\\n\\n' +
    'Instructions:\\n' +
    '- Do REAL research, but be token-efficient: use AT MOST ~6 web_search/web_fetch calls total, ' +
    'then write. Do not exhaustively crawl.\\n' +
    '- Cover all sub-parts of the question; be precise with numbers, dates, names; cite sources inline.\\n' +
    '- Keep the report focused: roughly 900-1400 words. Quality over length.\\n' +
    '- Do NOT search for or fetch any benchmark, rubric, grading, leaderboard, or answer-key material ' +
    '(e.g. DRACO, Perplexity, HuggingFace dataset pages). Research the underlying topic only.\\n' +
    '- Your FINAL message must be ONLY the report itself (no preamble, no meta-commentary). It is consumed verbatim.'
  )
}"""

OLD_RESEARCH_OPTS = """      phase: 'Research',
      agentType: 'general-purpose',
      model: 'haiku',
      effort: 'medium',"""


def flat_criteria(rubric: dict) -> list[dict]:
    return [
        {"id": c["id"], "requirement": c["requirement"], "weight": c["weight"]}
        for s in rubric["sections"]
        for c in s.get("criteria", [])
        if isinstance(c.get("id"), str) and isinstance(c.get("requirement"), str) and isinstance(c.get("weight"), int)
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--model", choices=("haiku", "sonnet"), default="haiku")
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--stratified", action="store_true", help="one task per domain (deterministic, first-in-domain)")
    ap.add_argument("--task-ids", default=None, help="comma-separated task ids to include")
    args = ap.parse_args()

    tasks = json.loads(args.manifest.read_text())["tasks"]
    if args.task_ids:
        want = [x for x in args.task_ids.split(",") if x]
        chosen = [t for t in tasks if t["id"] in set(want)]
    elif args.stratified:
        seen, chosen = set(), []
        for t in tasks:
            if t["domain"] not in seen:
                seen.add(t["domain"])
                chosen.append(t)
    else:
        chosen = list(tasks)
    if args.limit:
        chosen = chosen[: args.limit]

    payload = json.dumps({
        "runs": args.runs,
        "tasks": [{"id": t["id"], "domain": t["domain"], "problem": t["problem"],
                   "criteria": flat_criteria(t["rubric"])} for t in chosen],
    }, ensure_ascii=False)

    src = TEMPLATE.read_text()
    i = src.index("const INPUT = ")
    j = src.index("\nconst TASKS = INPUT.tasks", i)
    src = src[:i] + "const INPUT = " + payload + src[j:]

    if args.model == "sonnet":
        assert src.count("model: 'haiku'") == 3, "expected 3 generative haiku roles"
        src = src.replace("model: 'haiku'", "model: 'sonnet'")
        src = src.replace("'anthropic/claude-haiku-4-5'", "'anthropic/claude-sonnet-4-6'")
        src = src.replace(OLD_RESEARCH, LEAN_RESEARCH)  # leaner research to fit the token budget
        src = src.replace(OLD_RESEARCH_OPTS, OLD_RESEARCH_OPTS.replace("effort: 'medium'", "effort: 'low'"))

    args.out.write_text(src)
    print(f"wrote {args.out}  ({len(chosen)} tasks, model={args.model}, runs={args.runs}, {len(src)} bytes)")
    print("Run it with the Workflow tool; save the returned JSON and analyze with scripts/selffusion_analyze.py")


if __name__ == "__main__":
    main()
