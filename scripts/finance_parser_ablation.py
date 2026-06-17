#!/usr/bin/env python3
"""Ablate finance-document parsing strategies on DeepSeek V4 over the 20 DRACO
finance tasks, to find the best COST-EFFECTIVE option. Three configs:

  baseline_markitdown : markitdown docs, NO sec_facts        (current production)
  secfacts_markitdown : + free EDGAR XBRL sec_facts tool      (CHEAP candidate)
  secfacts_llamaparse : sec_facts + LlamaParse(cached) docs   (PREMIUM candidate)

Writes one replay file per config (config_id=solo_deepseek_v4_pro_tooled) so
scripts/draco_rejudge.py scores them unchanged. Compare the finance scores +
LlamaParse pages billed to decide whether LlamaParse earns its cost over free XBRL.
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

import httpx

from trusted_router.evals.agentic_tools import (
    DRACO_AGENTIC_SYSTEM_PROMPT,
    DEFAULT_BASH_IMAGE,
    build_tool_executors,
    run_agentic_completion,
)
from trusted_router.evals.draco_replay import load_manifest
from trusted_router.evals.exa import ExaSearchClient
from trusted_router.evals.fusion_live import load_eval_key

CONFIGS: dict[str, dict[str, Any]] = {
    "baseline_markitdown": dict(enable_sec_facts=False, doc_parser="markitdown"),
    "secfacts_markitdown": dict(enable_sec_facts=True, doc_parser="markitdown"),
    "secfacts_llamaparse": dict(enable_sec_facts=True, doc_parser="llamaparse"),
}
MODEL = "deepseek/deepseek-v4-pro"
CONFIG_ID = "solo_deepseek_v4_pro_tooled"
BASE_URL = "https://api-us-central1.quillrouter.com/v1"


def run_one(task, *, cfg: dict[str, Any], api_key: str, exa_key: str) -> dict[str, Any]:
    client = httpx.Client(timeout=600.0)
    exa = ExaSearchClient(exa_key)
    try:
        schemas, execs = build_tool_executors(
            task, exa_client=exa, bash_image=DEFAULT_BASH_IMAGE, enable_bash=True, **cfg
        )
        r = run_agentic_completion(
            client=client, base_url=BASE_URL, api_key=api_key, model=MODEL,
            system_prompt=DRACO_AGENTIC_SYSTEM_PROMPT,
            user_prompt=f"Research task:\n{task.problem}",
            tool_schemas=schemas, executors=execs, max_tool_calls=16,
            max_tokens=8000, synthesis_max_tokens=12000, temperature=0.2,
        )
        return {
            "schema": "trustedrouter.fusion_draco.replay.v1", "config_id": CONFIG_ID,
            "task_id": task.id, "domain": task.domain, "task": task.cache_dict(),
            "agentic": {"tool_calls_made": r.tool_calls_made, "steps": r.steps,
                        "truncated_loop": r.truncated_loop,
                        "tools": [{"name": x.name, "args": x.args, "result_chars": x.result_chars,
                                   "error": x.error} for x in r.tool_records]},
            "final": {"content": r.content, "finish_reason": r.finish_reason, "model": MODEL,
                      "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                      "elapsed_ms": r.elapsed_ms, "http_status": 200, "request_id": None},
        }
    except Exception as exc:  # noqa: BLE001
        return {"schema": "trustedrouter.fusion_draco.replay.v1", "status": "failed",
                "config_id": CONFIG_ID, "task_id": task.id, "domain": task.domain,
                "error_type": type(exc).__name__, "error": str(exc)[:400]}
    finally:
        exa.close(); client.close()


def run_config(name: str, cfg: dict[str, Any], tasks, *, out_dir: Path, api_key: str, exa_key: str,
               workers: int, resume: bool) -> None:
    out = out_dir / f"deepseek-finance-{name}.jsonl"
    done: set[str] = set()
    if resume and out.exists():
        for ln in out.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                row = json.loads(ln)
                if row.get("status") != "failed":
                    done.add(row.get("task_id"))
    pending = [t for t in tasks if t.id not in done]
    print(f"\n=== config {name} {cfg} :: {len(pending)} pending / {len(tasks)} ===")
    fh = out.open("a" if resume else "w", encoding="utf-8")
    total = len(pending); n = 0
    with ThreadPoolExecutor(max_workers=max(1, min(workers, total or 1))) as ex:
        futs: dict[Future, Any] = {}
        i = 0

        def submit():
            nonlocal i
            if i < total:
                futs[ex.submit(run_one, pending[i], cfg=cfg, api_key=api_key, exa_key=exa_key)] = pending[i]
                i += 1
        for _ in range(min(workers, total)):
            submit()
        while futs:
            ready, _ = wait(futs, return_when=FIRST_COMPLETED)
            for f in ready:
                futs.pop(f); row = f.result(); fh.write(json.dumps(row, sort_keys=True) + "\n"); fh.flush()
                n += 1; fin = row.get("final") or {}; ag = row.get("agentic") or {}
                tools = [t["name"] for t in ag.get("tools", [])]
                print(f"  [{name}] {n}/{total} {str(row.get('task_id'))[:8]} "
                      f"status={row.get('status','ok')} sec_facts={tools.count('sec_facts')} "
                      f"web_fetch={tools.count('web_fetch')} clen={len(fin.get('content') or '')}")
                submit()
    fh.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", type=Path, default=Path("artifacts/fusion-draco/draco-financial-20.manifest.json"))
    p.add_argument("--out-dir", type=Path, default=Path("artifacts/fusion-draco/ablation"))
    p.add_argument("--config", action="append", default=None, help="Run only these configs.")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--execute", action="store_true")
    args = p.parse_args()
    tasks = list(load_manifest(args.manifest).tasks)
    if args.limit:
        tasks = tasks[: args.limit]
    configs = {k: v for k, v in CONFIGS.items() if not args.config or k in args.config}
    print(f"tasks: {len(tasks)}  configs: {list(configs)}")
    if not args.execute:
        print("dry run; add --execute"); return 0
    api_key = load_eval_key("TR_FUSION_EVAL_API_KEY")
    exa_key = load_eval_key("EXA_API_KEY")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name, cfg in configs.items():
        run_config(name, cfg, tasks, out_dir=args.out_dir, api_key=api_key, exa_key=exa_key,
                   workers=args.workers, resume=args.resume)
    print("\nABLATION DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
