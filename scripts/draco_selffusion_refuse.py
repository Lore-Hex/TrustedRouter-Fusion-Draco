#!/usr/bin/env python3
"""Re-fuse a saved self-fusion *research* replay with ANY judge+synthesizer (chair).

The §8 self-fusion experiment generated, per task, RUNS independent agentic research
reports from one base model, then fused the first-N of them (N=1..RUNS) into a final
answer. The drafts (research reports) are now persisted as a ``...-research.jsonl``
replay. This script reads those drafts and re-runs ONLY the cheap fuse step — judge
then synthesizer — with a chair model you choose, so we can isolate the *chair* as the
only variable while holding the drafts fixed:

    Sonnet drafts + Sonnet chair  (cell A)   Sonnet drafts + Haiku chair  (cell B)
    Haiku  drafts + Haiku  chair  (cell C)   Haiku  drafts + Sonnet chair (cell D)

Prompts (JUDGE_SYSTEM / FINAL_INSTRUCTION / panel evidence) are copied verbatim from
``scripts/draco_client_fusion.py`` (which mirrors the enclave fusion.go pipeline), so a
re-fuse is byte-for-byte the same fusion the harness does — only the chair model differs.

Output rows are ``trustedrouter.fusion_draco.replay.v1`` with the panel size N encoded
into ``config_id`` (``<base>__N02 .. __N10``) so ``scripts/draco_rejudge.py`` — which
dedupes on (config_id, task_id) — grades every (task, N) distinctly and resumes cleanly.
N=1 is emitted as the raw first draft (no fuse) so one file holds the whole N=1..RUNS curve.

Example:
  uv run python scripts/draco_selffusion_refuse.py \
    --research-replay replays/fusion-selffusion-sonnet-research.jsonl \
    --output replays/refuse-sonnet-drafts--haiku-chair.jsonl \
    --base-config-id selffusion_sonnetdrafts_haikuchair \
    --judge-model anthropic/claude-haiku-4.5 --fuser-model anthropic/claude-haiku-4.5 \
    --runs 10 --workers 6 --execute
"""
from __future__ import annotations

import argparse
import json
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from trusted_router.evals import tr_sdk
from trusted_router.evals.agentic_tools import strip_tool_markup
from trusted_router.evals.fusion_live import load_eval_key

REPLAY_SCHEMA = "trustedrouter.fusion_draco.replay.v1"

# ---- verbatim from scripts/draco_client_fusion.py (enclave fusion.go) ----
JUDGE_SYSTEM = (
    "You are the TrustedRouter Fusion judge. Compare panel responses and return "
    "compact JSON with keys consensus, contradictions, partial_coverage, "
    "unique_insights, blind_spots, and final_guidance. Do not write the final "
    "answer. Return only JSON; do not include chain-of-thought, hidden reasoning, "
    "or <think> blocks."
)
FINAL_INSTRUCTION = (
    "TrustedRouter Fusion panel answers and judge analysis follow. Use the panel "
    "answers as the primary evidence and the judge analysis as guidance to write "
    "the final answer for the original request. Return only the final visible "
    "answer. Do not include chain-of-thought, hidden reasoning, analysis, "
    "scratchpad text, <think> blocks, or internal model names unless the user "
    "asked for methodology."
)


def _panel_evidence(panel: list[tuple[str, str]]) -> str:
    b = ["Panel answers:\n"]
    for i, (model, text) in enumerate(panel, start=1):
        b.append(f"\n[{i}] model={model}\n{text.strip()}\n")
    return "".join(b)


def _judge_user(problem: str, panel: list[tuple[str, str]]) -> str:
    evidence = _panel_evidence(panel)
    body = evidence[len("Panel answers:\n"):] if evidence.startswith("Panel answers:\n") else evidence
    return f"Original request summary:\n{problem}\n\nPanel responses:\n{body}"


def _content(resp: dict[str, Any]) -> str:
    return (resp.get("choices", [{}])[0].get("message", {}) or {}).get("content") or ""


def _load_research(path: Path, runs: int) -> "OrderedDict[str, dict[str, Any]]":
    """task_id -> {task, base_model, reports:[text,...]} in file (run) order, capped at `runs`."""
    out: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r.get("status") == "failed":
            continue
        tid = r.get("task_id")
        text = (r.get("final") or {}).get("content")
        if not tid or not isinstance(text, str) or not text.strip():
            continue
        slot = out.setdefault(tid, {"task": r.get("task") or {"id": tid, "domain": r.get("domain")},
                                    "domain": r.get("domain"),
                                    "base_model": (r.get("final") or {}).get("model"),
                                    "reports": []})
        if len(slot["reports"]) < runs:
            slot["reports"].append(text)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--research-replay", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--base-config-id", required=True,
                   help="N is appended as __N02..__N10 (and __N01 for the raw draft).")
    p.add_argument("--judge-model", default="anthropic/claude-haiku-4.5")
    p.add_argument("--fuser-model", default="anthropic/claude-haiku-4.5")
    p.add_argument("--runs", type=int, default=10)
    p.add_argument("--limit", type=int, default=None, help="first N tasks only")
    p.add_argument("--task-ids", default=None, help="comma-separated task ids")
    p.add_argument("--no-raw-n1", action="store_true", help="skip emitting N=1 raw-draft rows")
    p.add_argument("--judge-max-tokens", type=int, default=3000)
    p.add_argument("--fuser-max-tokens", type=int, default=8000)
    p.add_argument("--base-url", default="https://api.quillrouter.com/v1")
    p.add_argument("--api-key-name", default="TR_FUSION_EVAL_API_KEY")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--timeout-seconds", type=float, default=600.0)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--execute", action="store_true")
    args = p.parse_args(argv)

    research = _load_research(args.research_replay, args.runs)
    if args.task_ids:
        want = {x for x in args.task_ids.split(",") if x}
        research = OrderedDict((k, v) for k, v in research.items() if k in want)
    if args.limit:
        research = OrderedDict(list(research.items())[: args.limit])

    # build the (task, N) job list
    jobs: list[dict[str, Any]] = []
    for tid, slot in research.items():
        reps = slot["reports"]
        start = 1 if not args.no_raw_n1 else 2
        for N in range(start, len(reps) + 1):
            jobs.append({"task_id": tid, "N": N, "slot": slot})

    completed: set[tuple[str, str]] = set()
    if args.resume and args.output.exists():
        for line in args.output.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("status") != "failed" and row.get("task_id"):
                    completed.add((str(row.get("config_id")), str(row.get("task_id"))))
    pending = [j for j in jobs if (f"{args.base_config_id}__N{j['N']:02d}", j["task_id"]) not in completed]

    print(f"research tasks: {len(research)} | (task,N) jobs: {len(jobs)} | pending: {len(pending)}")
    print(f"judge: {args.judge_model}  fuser: {args.fuser_model}  base_config: {args.base_config_id}")
    print(f"output: {args.output}")
    if not args.execute:
        print("dry run; add --execute")
        return 0

    api_key = load_eval_key(args.api_key_name) or (
        Path("~/claude/.tr_key").expanduser().read_text().strip()
        if Path("~/claude/.tr_key").expanduser().exists() else None
    )
    if not api_key:
        print(f"missing key {args.api_key_name}")
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def run_one(job: dict[str, Any]) -> dict[str, Any]:
        tid, N, slot = job["task_id"], job["N"], job["slot"]
        task = slot["task"]
        problem = task.get("problem", "")
        base_model = slot.get("base_model") or "unknown"
        cfg = f"{args.base_config_id}__N{N:02d}"
        client = tr_sdk.make_client(base_url=args.base_url, api_key=api_key, timeout=args.timeout_seconds)
        try:
            panel = [(base_model, slot["reports"][i]) for i in range(N)]
            if N == 1:
                # raw first draft, no fuse
                content = slot["reports"][0]
                judge_json = ""
                fuser_used = "(raw-draft)"
            else:
                # 1) judge
                jbody = {
                    "model": args.judge_model, "max_tokens": args.judge_max_tokens,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": JUDGE_SYSTEM},
                        {"role": "user", "content": _judge_user(problem, panel)},
                    ],
                }
                judge_json = _content(tr_sdk.chat(client, jbody)).strip()
                # 2) synthesizer (fuser)
                final_user = FINAL_INSTRUCTION + "\n\n" + _panel_evidence(panel) + "\n\nJudge analysis JSON:\n" + judge_json
                fbody = {
                    "model": args.fuser_model, "max_tokens": args.fuser_max_tokens, "temperature": 0.2,
                    "messages": [
                        {"role": "user", "content": problem},
                        {"role": "user", "content": final_user},
                    ],
                }
                fr = tr_sdk.chat(client, fbody)
                content = strip_tool_markup(_content(fr))
                fuser_used = args.fuser_model
            return {
                "schema": REPLAY_SCHEMA, "config_id": cfg, "task_id": tid,
                "domain": slot.get("domain"), "task": task,
                "fusion": {"base_model": base_model, "judge_model": (args.judge_model if N > 1 else None),
                           "fuser_model": (args.fuser_model if N > 1 else None), "panel_size": N,
                           "self_fusion": True, "judge_chars": len(judge_json), "run_order": "file"},
                "final": {"content": content, "model": fuser_used, "finish_reason": "stop",
                          "elapsed_ms": None, "http_status": 200, "input_tokens": None,
                          "output_tokens": None, "request_id": None},
            }
        except Exception as exc:  # noqa: BLE001
            return {"schema": REPLAY_SCHEMA, "status": "failed", "config_id": cfg, "task_id": tid,
                    "domain": slot.get("domain"), "error_type": type(exc).__name__, "error": str(exc)[:400]}
        finally:
            client.close()

    fh = args.output.open("a" if args.resume else "w", encoding="utf-8")
    done, total = 0, len(pending)
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, total or 1))) as ex:
        futures: dict[Future, Any] = {}
        nxt = 0

        def submit() -> None:
            nonlocal nxt
            if nxt < total:
                futures[ex.submit(run_one, pending[nxt])] = pending[nxt]
                nxt += 1

        for _ in range(min(args.workers, total)):
            submit()
        while futures:
            ready, _ = wait(futures, return_when=FIRST_COMPLETED)
            for fut in ready:
                futures.pop(fut)
                row = fut.result()
                fh.write(json.dumps(row, sort_keys=True) + "\n")
                fh.flush()
                done += 1
                fin = row.get("final") or {}
                print(f"refuse {done}/{total} task={str(row.get('task_id'))[:8]} N={row.get('fusion',{}).get('panel_size')} "
                      f"status={row.get('status','ok')} clen={len(fin.get('content') or '')}")
                submit()
    fh.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
