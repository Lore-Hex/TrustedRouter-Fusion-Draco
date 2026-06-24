#!/usr/bin/env python3
"""Recover the Haiku-FUSER fused answers from live Workflow subagent transcripts.

The re-fuse workflow (artifacts/chair-isolation/shards/*.js) returns only scores, not the
fused answer texts — same gap that lost the original §8 texts. The synthesizer subagents'
outputs still sit in the workflow transcript dirs; this rebuilds them into a proper
``trustedrouter.fusion_draco.replay.v1`` replay BEFORE the transcripts are pruned.

For every agent-*.jsonl across the given workflow dirs we read the first user prompt and the
final assistant message. Synthesizer prompts are identified by the verbatim FINAL_INSTRUCTION
marker; the task is matched by problem prefix, and N = the panel-member count in the embedded
panel evidence. N=1 rows (raw first draft) are copied straight from the Sonnet research replay.

Usage:
  python3 scripts/selffusion_recover_fused_from_transcripts.py \
    --workflows-dir ~/.claude/projects/<proj>/subagents/workflows \
    --research-replay replays/fusion-selffusion-sonnet-research.jsonl \
    --out replays/fusion-selffusion-sonnetdrafts-haikufuser.jsonl
"""
from __future__ import annotations

import argparse
import glob
import json
from collections import OrderedDict
from pathlib import Path

SYNTH_MARKER = "TrustedRouter Fusion panel answers and judge analysis follow"
REPLAY_SCHEMA = "trustedrouter.fusion_draco.replay.v1"


def msg_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def parse_agent(path: str) -> tuple[str, str] | None:
    """Return (first_user_prompt, final_assistant_text) or None."""
    first_user, last_asst = None, None
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = r.get("type")
        m = r.get("message") or {}
        if t == "user" and first_user is None:
            first_user = msg_text(m.get("content"))
        elif t == "assistant":
            txt = msg_text(m.get("content"))
            if txt.strip():
                last_asst = txt
    if first_user is None or last_asst is None:
        return None
    return first_user, last_asst


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflows-dir", type=Path, required=True)
    ap.add_argument("--research-replay", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--base-config-id", default="selffusion_sonnetdrafts_haikufuser")
    ap.add_argument("--draft-model", default="anthropic/claude-sonnet-4-6")
    ap.add_argument("--fuser-model", default="anthropic/claude-haiku-4.5")
    ap.add_argument("--judge-model", default=None, help="label only; defaults to --fuser-model")
    ap.add_argument("--synth-model", default=None, help="label only; defaults to --fuser-model")
    ap.add_argument("--run-ids", default=None,
                    help="comma-separated wf_* run-id substrings; only these workflow dirs are scanned")
    args = ap.parse_args()
    judge_label = args.judge_model or args.fuser_model
    synth_label = args.synth_model or args.fuser_model
    run_filter = [x for x in args.run_ids.split(",") if x] if args.run_ids else None

    # tasks (id -> problem, domain, rubric) + N=1 raw drafts from the research replay
    tasks: dict[str, dict] = {}
    first_draft: dict[str, str] = {}
    for line in args.research_replay.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("status") == "failed":
            continue
        tid = r["task_id"]; task = r.get("task") or {}
        tasks.setdefault(tid, {"problem": task.get("problem", ""), "domain": r.get("domain"),
                               "rubric": task.get("rubric") or {}})
        first_draft.setdefault(tid, (r.get("final") or {}).get("content") or "")
    # longest-problem-first so prefix matching is unambiguous
    by_problem = sorted(((v["problem"], tid) for tid, v in tasks.items() if v["problem"]),
                        key=lambda x: -len(x[0]))

    def match_task(prompt: str) -> str | None:
        head = prompt[:4000]
        for problem, tid in by_problem:
            if problem and prompt.startswith(problem[:200]) or problem[:120] in head:
                return tid
        return None

    # recover synth answers, keyed by (task_id, N); keep first seen
    fused: "OrderedDict[tuple[str,int], str]" = OrderedDict()
    n_files = n_synth = 0
    for d in sorted(glob.glob(str(args.workflows_dir / "wf_*"))):
        if run_filter and not any(rid in d for rid in run_filter):
            continue
        for f in glob.glob(d + "/agent-*.jsonl"):
            n_files += 1
            pr = parse_agent(f)
            if not pr:
                continue
            prompt, answer = pr
            if SYNTH_MARKER not in prompt:
                continue
            n_synth += 1
            N = prompt.count("] model=")
            tid = match_task(prompt)
            if tid is None or N < 2:
                continue
            fused.setdefault((tid, N), answer)

    # write replay: N=1 raw drafts + recovered N>=2 fused answers
    rows = []
    for tid, t in tasks.items():
        if first_draft.get(tid):
            rows.append((tid, 1, first_draft[tid], None))
    for (tid, N), ans in fused.items():
        rows.append((tid, N, ans, args.fuser_model))
    rows.sort(key=lambda r: (r[0], r[1]))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        for tid, N, content, fuser in rows:
            t = tasks[tid]
            fh.write(json.dumps({
                "schema": REPLAY_SCHEMA,
                "config_id": f"{args.base_config_id}__N{N:02d}",
                "task_id": tid, "domain": t["domain"],
                "task": {"id": tid, "domain": t["domain"], "problem": t["problem"], "rubric": t["rubric"]},
                "fusion": {"base_model": args.draft_model,
                           "fuser_model": (synth_label if fuser else None),
                           "judge_model": (judge_label if fuser else None),
                           "panel_size": N, "self_fusion": True,
                           "recovered_from_transcript": True},
                "final": {"content": content, "model": fuser or "(raw-draft)",
                          "finish_reason": "stop", "elapsed_ms": None, "http_status": None,
                          "input_tokens": None, "output_tokens": None, "request_id": None},
            }, sort_keys=True) + "\n")

    nN = {}
    for tid, N, *_ in rows:
        nN[N] = nN.get(N, 0) + 1
    print(f"scanned {n_files} agent files | synth prompts {n_synth} | recovered fused (N>=2): {len(fused)}")
    print(f"wrote {args.out}  ({len(rows)} rows: N=1 raw + N>=2 fused)")
    print(f"rows per N: {dict(sorted(nN.items()))}  (expect ~34 at N=1, ~34 at each N=2..10)")


if __name__ == "__main__":
    main()
