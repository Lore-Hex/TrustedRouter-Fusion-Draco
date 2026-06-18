#!/usr/bin/env python3
"""Client-orchestrated TrustedRouter Fusion over pre-computed tooled panel solos.

The native gateway /fusion endpoint cannot give panels live tools (its panel runs
on frozen context -> ~40 on DRACO). OpenRouter's 64.7 comes from giving the panel
LIVE tools. So we reproduce Fusion client-side: the panel == our validated tooled
solos (gemini-flash + kimi + deepseek, full-100), then replicate the gateway's
exact judge->fuser pipeline (prompts copied verbatim from enclave fusion.go):

  panel reports -> Fusion judge (gemini-3.1-pro, compact JSON analysis)
               -> Opus-4.8 fuser (panel evidence primary + judge analysis guidance)

Writes ``trustedrouter.fusion_draco.replay.v1`` rows so scripts/draco_rejudge.py
scores them unchanged. No rubric is ever shown to judge or fuser (no leakage).
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

import httpx

from trusted_router.evals.agentic_tools import strip_tool_markup
from trusted_router.evals.draco_replay import load_manifest
from trusted_router.evals.fusion_live import load_eval_key

REPLAY_SCHEMA = "trustedrouter.fusion_draco.replay.v1"

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
FUSER_SYSTEM = (
    "You are an expert research analyst. Write a thorough, well-structured, "
    "accurate final report that directly and completely answers the user's task."
)


def _panel_evidence(panel: list[tuple[str, str]]) -> str:
    b = ["Panel answers:"]
    for i, (model, text) in enumerate(panel, start=1):
        b.append(f"\n[{i}] model={model}\n{text.strip()}\n")
    return "".join(b)


def _judge_user(problem: str, panel: list[tuple[str, str]]) -> str:
    evidence = _panel_evidence(panel)
    body = evidence[len("Panel answers:\n"):] if evidence.startswith("Panel answers:\n") else evidence
    return f"Original request summary:\n{problem}\n\nPanel responses:\n{body}"


def _post(client: httpx.Client, base_url: str, api_key: str, body: dict[str, Any]) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last = None
    for attempt in range(1, 5):
        try:
            resp = client.post(url, headers=headers, json=body)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last = exc
            continue
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 4:
            continue
        resp.raise_for_status()
        return resp.json()
    if last:
        raise last
    raise RuntimeError("no response")


def _content(resp: dict[str, Any]) -> str:
    return (resp.get("choices", [{}])[0].get("message", {}) or {}).get("content") or ""


def _load_panel(path: Path) -> dict[str, str]:
    """task_id -> final report text (last non-failed row wins)."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r.get("status") == "failed":
            continue
        t = r.get("task_id")
        c = (r.get("final") or {}).get("content")
        if t and isinstance(c, str) and c.strip():
            out[t] = c
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", type=Path, default=Path("artifacts/fusion-draco/draco-full-100.manifest.json"))
    p.add_argument("--panel", action="append", required=True,
                   help="model_label=replay_path. Repeat for each panel member.")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--config-id", default="fusion_client_budget_opus")
    p.add_argument("--judge-model", default="google/gemini-3.1-pro-preview")
    p.add_argument("--fuser-model", default="anthropic/claude-opus-4.8")
    p.add_argument("--fallback-fuser-model", default=None,
                   help="If the primary fuser returns (near-)empty output — e.g. a silent "
                        "refusal — re-synthesize the same panel with this model instead.")
    p.add_argument("--judge-max-tokens", type=int, default=3000)
    p.add_argument("--fuser-max-tokens", type=int, default=8000)
    p.add_argument("--base-url", default="https://api-us-central1.quillrouter.com/v1")
    p.add_argument("--api-key-name", default="TR_FUSION_EVAL_API_KEY")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--timeout-seconds", type=float, default=600.0)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--execute", action="store_true")
    args = p.parse_args(argv)

    panels: dict[str, dict[str, str]] = {}
    labels: list[str] = []
    for spec in args.panel:
        label, _, path = spec.partition("=")
        panels[label] = _load_panel(Path(path))
        labels.append(label)

    manifest = load_manifest(args.manifest)
    tasks = list(manifest.tasks)
    if args.limit:
        tasks = tasks[: args.limit]
    # only tasks every panel member answered
    tasks = [t for t in tasks if all(t.id in panels[l] for l in labels)]

    completed: set[str] = set()
    if args.resume and args.output.exists():
        for line in args.output.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("status") != "failed" and row.get("task_id"):
                    completed.add(row["task_id"])
    pending = [t for t in tasks if t.id not in completed]

    print(f"panel members: {labels}")
    print(f"tasks with full panel coverage: {len(tasks)}  pending: {len(pending)}")
    print(f"fuser: {args.fuser_model}  judge: {args.judge_model}  config: {args.config_id}")
    print(f"output: {args.output}")
    if not args.execute:
        print("dry run; add --execute")
        return 0

    api_key = load_eval_key(args.api_key_name)
    if not api_key:
        print(f"missing key {args.api_key_name}")
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)

    def run_one(task) -> dict[str, Any]:
        client = httpx.Client(timeout=args.timeout_seconds)
        try:
            panel = [(l, panels[l][task.id]) for l in labels]
            # 1) Fusion judge analysis
            judge_body = {
                "model": args.judge_model, "max_tokens": args.judge_max_tokens,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": JUDGE_SYSTEM},
                    {"role": "user", "content": _judge_user(task.problem, panel)},
                ],
            }
            judge_json = _content(_post(client, args.base_url, api_key, judge_body)).strip()
            # 2) fuser (primary, with optional fallback when it returns near-empty —
            #    e.g. GLM-5.2 silently refusing politically restricted panel content)
            final_user = FINAL_INSTRUCTION + "\n\n" + _panel_evidence(panel) + "\n\nJudge analysis JSON:\n" + judge_json

            def _fuse(model: str) -> tuple[str, dict[str, Any]]:
                body = {
                    "model": model, "max_tokens": args.fuser_max_tokens, "temperature": 0.2,
                    "messages": [
                        {"role": "system", "content": FUSER_SYSTEM},
                        {"role": "user", "content": f"Research task:\n{task.problem}"},
                        {"role": "user", "content": final_user},
                    ],
                }
                resp = _post(client, args.base_url, api_key, body)
                return strip_tool_markup(_content(resp)), resp

            fuser_used = args.fuser_model
            content, fr = _fuse(args.fuser_model)
            fell_back = False
            if len(content.strip()) < 50 and args.fallback_fuser_model:
                fuser_used = args.fallback_fuser_model
                fell_back = True
                content, fr = _fuse(args.fallback_fuser_model)
            usage = fr.get("usage") or {}
            return {
                "schema": REPLAY_SCHEMA, "config_id": args.config_id, "task_id": task.id,
                "domain": task.domain, "task": task.cache_dict(),
                "fusion": {"panel": labels, "judge_model": args.judge_model,
                           "judge_chars": len(judge_json), "fuser_model": args.fuser_model,
                           "fallback_fuser_model": args.fallback_fuser_model,
                           "fuser_used": fuser_used, "fell_back": fell_back},
                "final": {"content": content, "finish_reason": fr.get("choices", [{}])[0].get("finish_reason"),
                          "model": fuser_used, "input_tokens": usage.get("prompt_tokens"),
                          "output_tokens": usage.get("completion_tokens"), "elapsed_ms": None,
                          "http_status": 200, "request_id": fr.get("id")},
            }
        except Exception as exc:  # noqa: BLE001
            return {"schema": REPLAY_SCHEMA, "status": "failed", "config_id": args.config_id,
                    "task_id": task.id, "domain": task.domain,
                    "error_type": type(exc).__name__, "error": str(exc)[:400]}
        finally:
            client.close()

    fh = args.output.open("a" if args.resume else "w", encoding="utf-8")
    done = 0
    total = len(pending)
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, total or 1))) as ex:
        futures: dict[Future, Any] = {}
        nxt = 0

        def submit():
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
                print(f"fusion {done}/{total} task={str(row.get('task_id'))[:8]} "
                      f"status={row.get('status','ok')} clen={len(fin.get('content') or '')} "
                      f"finish={fin.get('finish_reason')}")
                submit()
    fh.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
