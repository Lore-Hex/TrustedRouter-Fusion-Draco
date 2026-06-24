#!/usr/bin/env python3
"""Generate a self-contained Claude Code Workflow (.js) that RE-FUSES persisted
self-fusion drafts with a Haiku fuser (judge+synth), graded by Sonnet-4.6.

This is the fuser-isolation experiment: hold the drafts fixed (the persisted Sonnet
research reports in ``replays/fusion-selffusion-sonnet-research.jsonl``) and swap ONLY
the fuser from Sonnet to Haiku, to see whether a cheap fuser can keep the gain. It runs
entirely on Claude Code subagents (the Workflow tool) — NO TrustedRouter credits, NO TR
key — exactly like the §8 harness ``artifacts/haiku-selffusion/wf_haiku_pilot.js``, but
the Research phase is replaced by the embedded pre-computed drafts.

What it keeps verbatim from the template: the Fuse phase (Haiku judge -> Haiku synth,
first-N of the pool, N=1=raw draft) and the Grade phase (Sonnet-4.6 chunk-all, the same
grader behind the existing Sonnet-chair +8.0 curve, so the two curves are comparable).

Subagents can't read the repo, so the drafts are embedded inline as ``INPUT.tasks[].reports``.
That makes the script large (~100KB/task of report text); the 512KB Workflow script limit
caps a shard at ~4 tasks. Use --task-ids / --limit+--offset to shard; run each shard with
the Workflow tool and concat the returned JSONs for scripts/selffusion_analyze.py.

Example (4-task pilot incl. the needle task):
  python3 scripts/selffusion_refuse_gen_workflow.py \
    --research-replay replays/fusion-selffusion-sonnet-research.jsonl \
    --task-ids ID1,ID2,ID3,ID4 --out artifacts/chair-isolation/wf_haikuchair_pilot.js
"""
from __future__ import annotations

import argparse
import json
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "artifacts" / "haiku-selffusion" / "wf_haiku_pilot.js"

# Splice boundaries in the template (matched as substrings, not line numbers).
RESEARCH_START = "// ============================ RESEARCH ============================"
FUSE_START = "// ============================ FUSE (N=1..RUNS) ============================"

# Replacement for the entire RESEARCH phase: drafts are embedded, not generated.
DRAFTS_BLOCK = """// ============================ DRAFTS (embedded; re-fuse, no research) ============================
// Reports come from a persisted *-research.jsonl replay (embedded in INPUT.tasks[].reports).
// We hold these drafts fixed and only swap the fuser — this is the fuser-isolation run.
const reports = {}
for (const t of TASKS) {
  reports[t.id] = (t.reports || []).slice(0, RUNS).filter((x) => x && x.trim().length > 0)
  log(`  ${t.domain}: ${reports[t.id].length}/${RUNS} embedded Sonnet drafts`)
}

"""


def flat_criteria(rubric: dict) -> list[dict]:
    return [
        {"id": c["id"], "requirement": c["requirement"], "weight": c["weight"]}
        for s in rubric.get("sections", [])
        for c in s.get("criteria", [])
        if isinstance(c.get("id"), str) and isinstance(c.get("requirement"), str) and isinstance(c.get("weight"), int)
    ]


def load_drafts(path: Path, runs: int) -> "OrderedDict[str, dict]":
    out: "OrderedDict[str, dict]" = OrderedDict()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r.get("status") == "failed":
            continue
        tid = r.get("task_id")
        text = (r.get("final") or {}).get("content")
        task = r.get("task") or {}
        if not tid or not isinstance(text, str) or not text.strip():
            continue
        slot = out.setdefault(tid, {"id": tid, "domain": r.get("domain") or task.get("domain"),
                                    "problem": task.get("problem", ""), "rubric": task.get("rubric") or {},
                                    "reports": []})
        if len(slot["reports"]) < runs:
            slot["reports"].append(text)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--research-replay", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--task-ids", default=None, help="comma-separated task ids (overrides limit/offset)")
    ap.add_argument("--fuser", choices=("haiku", "sonnet"), default="haiku",
                    help="judge+synth model. Default haiku (the fuser-isolation arm).")
    ap.add_argument("--judge", choices=("haiku", "sonnet"), default=None,
                    help="judge model (overrides --fuser for the judge role only)")
    ap.add_argument("--synth", choices=("haiku", "sonnet"), default=None,
                    help="synthesizer model (overrides --fuser for the synth role only)")
    args = ap.parse_args()

    drafts = load_drafts(args.research_replay, args.runs)
    if args.task_ids:
        want = [x for x in args.task_ids.split(",") if x]
        chosen = [drafts[t] for t in want if t in drafts]
    else:
        items = list(drafts.values())[args.offset:]
        chosen = items[: args.limit] if args.limit else items

    draft_model = "anthropic/claude-sonnet-4-6"  # the panel members are Sonnet drafts
    payload = json.dumps({
        "runs": args.runs,
        "tasks": [{"id": t["id"], "domain": t["domain"], "problem": t["problem"],
                   "criteria": flat_criteria(t["rubric"]), "reports": t["reports"][: args.runs]}
                  for t in chosen],
    }, ensure_ascii=False)

    src = TEMPLATE.read_text()

    # 1) swap embedded INPUT
    i = src.index("const INPUT = ")
    j = src.index("\nconst TASKS = INPUT.tasks", i)
    src = src[:i] + "const INPUT = " + payload + src[j:]

    # 2) replace the whole RESEARCH phase with the embedded-drafts block
    rs = src.index(RESEARCH_START)
    fs = src.index(FUSE_START)
    src = src[:rs] + DRAFTS_BLOCK + src[fs:]

    # 3) label panel members as Sonnet (cosmetic, used in panelEvidence)
    src = src.replace("const HAIKU_LABEL = 'anthropic/claude-haiku-4-5'",
                      f"const HAIKU_LABEL = '{draft_model}'")

    # 4) judge/synth models, set INDEPENDENTLY (after splice there are exactly 2 "model: 'haiku'":
    #    the judge role first, the synth role second — keyed off their label: `judge:` / `synth:`).
    judge_m = args.judge or args.fuser
    synth_m = args.synth or args.fuser
    assert src.count("model: 'haiku'") == 2, "expected exactly 2 haiku fuse roles after splice"
    ji = src.index("model: 'haiku'", src.index("label: `judge:"))
    si = src.index("model: 'haiku'", src.index("label: `synth:"))
    OLD = "model: 'haiku'"
    for idx, model in sorted([(ji, judge_m), (si, synth_m)], key=lambda x: -x[0]):  # later index first
        src = src[:idx] + f"model: '{model}'" + src[idx + len(OLD):]

    # 5) refresh the human-facing description
    src = src.replace(
        "description: 'Haiku self-fusion DRACO scaling curve N=1..10 (Haiku research+judge+synth, Sonnet-4.6 chunk-of-3 grader)',",
        f"description: 'Role-isolation: Sonnet drafts, {judge_m.upper()} judge + {synth_m.upper()} synth, Sonnet-4.6 grade, N=1..10',")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(src)
    nbytes = len(src.encode("utf-8"))
    warn = "  <-- OVER 512KB, will be rejected; shard smaller" if nbytes > 512_000 else ""
    print(f"wrote {args.out}  ({len(chosen)} tasks, judge={args.judge or args.fuser}, synth={args.synth or args.fuser}, runs={args.runs}, {nbytes} bytes){warn}")
    print("Run with the Workflow tool (scriptPath=this file); save the returned JSON for selffusion_analyze.py")


if __name__ == "__main__":
    main()
