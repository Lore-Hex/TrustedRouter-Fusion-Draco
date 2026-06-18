#!/usr/bin/env python3
"""Run a DRACO solo model agentically with live tools (web_search/web_fetch/bash).

This replicates OpenRouter's DRACO harness client-side: the model is given the
task problem (NO frozen context) plus the three tools, and iteratively searches /
fetches / computes through the gateway-as-plain-proxy until it writes a final
report. Writes replay rows in ``trustedrouter.fusion_draco.replay.v1`` so the
existing ``scripts/draco_rejudge.py`` can score them unchanged.
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from trusted_router.evals import tr_sdk
from trusted_router.evals.agentic_tools import (
    DRACO_AGENTIC_SYSTEM_PROMPT,
    DEFAULT_BASH_IMAGE,
    build_tool_executors,
    run_agentic_completion,
)
from trusted_router.evals.draco import DracoTask
from trusted_router.evals.draco_replay import load_manifest
from trusted_router.evals.exa import ExaSearchClient
from trusted_router.evals.fusion_live import load_eval_key

REPLAY_SCHEMA = "trustedrouter.fusion_draco.replay.v1"
KEY_NAMES = (
    "TR_FUSION_EVAL_API_KEY",
    "TR_API_KEY",
    "TRUSTEDROUTER_API_KEY",
    "TR_SMOKE_API_KEY",
    "TR_API_KEY_FOR_SELF_HEAL",
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", type=Path,
                   default=Path("artifacts/fusion-draco/draco-non-financial-80.manifest.json"))
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--model", default="deepseek/deepseek-v4-pro")
    p.add_argument("--config-id", default="solo_deepseek_v4_pro_tooled")
    p.add_argument("--task-id", action="append", default=None,
                   help="8-char prefix or full uuid. Repeatable.")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--max-tool-calls", type=int, default=16)
    p.add_argument("--max-tokens", type=int, default=8000)
    p.add_argument("--synthesis-max-tokens", type=int, default=12000)
    p.add_argument("--force-first-tool", action="store_true",
                   help="Force at least one tool call (for reluctant tool-users like gemini-flash).")
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--reasoning-effort", default=None, choices=(None, "low", "high"))
    p.add_argument("--no-bash", action="store_true", help="Disable the bash tool.")
    p.add_argument("--doc-parser", default="llamaparse", choices=("llamaparse", "markitdown", "plain"),
                   help="web_fetch document parser chain (llamaparse=cached LlamaParse first).")
    p.add_argument("--no-sec-facts", action="store_true", help="Disable the EDGAR XBRL sec_facts tool.")
    p.add_argument("--bash-image", default=DEFAULT_BASH_IMAGE)
    p.add_argument("--base-url", default="https://api-us-central1.quillrouter.com/v1")
    p.add_argument("--api-key-name", default=None,
                   help="Use this specific key (e.g. CHATGPT_API_KEY) instead of the TR keys, "
                        "for routing a model directly to its provider.")
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--timeout-seconds", type=float, default=600.0)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--execute", action="store_true")
    args = p.parse_args(argv)

    if args.workers < 1 or args.workers > 8:
        print("workers must be 1-8")
        return 2

    manifest = load_manifest(args.manifest)
    tasks = _select_tasks(manifest.tasks, task_filter=args.task_id, limit=args.limit)
    if not tasks:
        print("no tasks matched")
        return 2

    completed = _completed_ids(args.output) if args.resume else set()
    pending = [t for t in tasks if t.id not in completed]

    print(f"tasks: {len(tasks)}  pending: {len(pending)}")
    print(f"model: {args.model}  config_id: {args.config_id}")
    print(f"tools: web_search, web_fetch{'' if args.no_bash else ', bash'}  max_tool_calls: {args.max_tool_calls}")
    print(f"base_url: {args.base_url}")
    print(f"output: {args.output}")
    if not args.execute:
        print("dry run only; add --execute to call the gateway/Exa/docker.")
        return 0

    if args.api_key_name:
        api_key = load_eval_key(args.api_key_name)
    else:
        api_key = next((load_eval_key(n) for n in KEY_NAMES if load_eval_key(n)), None)
    if not api_key:
        print("missing API key")
        return 2
    exa_key = load_eval_key("EXA_API_KEY")
    if not exa_key:
        print("missing EXA_API_KEY")
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("a" if args.resume else "w", encoding="utf-8") as fh:
        _run(pending, args=args, api_key=api_key, exa_key=exa_key, fh=fh)
    return 0


def _run(pending: list[DracoTask], *, args: argparse.Namespace, api_key: str, exa_key: str, fh: Any) -> None:
    total = len(pending)
    if args.workers == 1:
        for i, task in enumerate(pending, start=1):
            _write(fh, _run_one(task, args=args, api_key=api_key, exa_key=exa_key), index=i, total=total)
        return
    max_workers = min(args.workers, total)
    ex = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures: dict[Future[dict[str, Any]], DracoTask] = {}
        nxt = 0
        done = 0

        def submit() -> None:
            nonlocal nxt
            if nxt >= total:
                return
            task = pending[nxt]
            nxt += 1
            futures[ex.submit(_run_one, task, args=args, api_key=api_key, exa_key=exa_key)] = task

        for _ in range(max_workers):
            submit()
        while futures:
            ready, _ = wait(futures, return_when=FIRST_COMPLETED)
            for fut in ready:
                task = futures.pop(fut)
                try:
                    row = fut.result()
                except Exception as exc:  # noqa: BLE001
                    row = _failure_row(task, args, exc)
                done += 1
                _write(fh, row, index=done, total=total)
                submit()
    finally:
        ex.shutdown(wait=True)


def _run_one(task: DracoTask, *, args: argparse.Namespace, api_key: str, exa_key: str) -> dict[str, Any]:
    client = tr_sdk.make_client(base_url=args.base_url, api_key=api_key, timeout=args.timeout_seconds)
    exa_client = ExaSearchClient(exa_key)
    try:
        schemas, executors = build_tool_executors(
            task, exa_client=exa_client, bash_image=args.bash_image, enable_bash=not args.no_bash,
            enable_sec_facts=not args.no_sec_facts, doc_parser=args.doc_parser,
        )
        result = run_agentic_completion(
            client=client,
            base_url=args.base_url,
            api_key=api_key,
            model=args.model,
            system_prompt=DRACO_AGENTIC_SYSTEM_PROMPT,
            user_prompt=f"Research task:\n{task.problem}",
            tool_schemas=schemas,
            executors=executors,
            max_tool_calls=args.max_tool_calls,
            max_tokens=args.max_tokens,
            synthesis_max_tokens=args.synthesis_max_tokens,
            temperature=args.temperature,
            reasoning_effort=args.reasoning_effort,
            force_first_tool=args.force_first_tool,
        )
        return {
            "schema": REPLAY_SCHEMA,
            "config_id": args.config_id,
            "task_id": task.id,
            "domain": task.domain,
            "task": task.cache_dict(),
            "agentic": {
                "tool_calls_made": result.tool_calls_made,
                "steps": result.steps,
                "truncated_loop": result.truncated_loop,
                "tools": [
                    {"name": r.name, "args": r.args, "result_chars": r.result_chars, "error": r.error}
                    for r in result.tool_records
                ],
            },
            "final": {
                "content": result.content,
                "finish_reason": result.finish_reason,
                "model": args.model,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "elapsed_ms": result.elapsed_ms,
                "http_status": 200,
                "request_id": None,
            },
        }
    except Exception as exc:  # noqa: BLE001
        return _failure_row(task, args, exc)
    finally:
        exa_client.close()
        client.close()


def _failure_row(task: DracoTask, args: argparse.Namespace, exc: Exception) -> dict[str, Any]:
    return {
        "schema": REPLAY_SCHEMA,
        "status": "failed",
        "config_id": args.config_id,
        "task_id": task.id,
        "domain": task.domain,
        "error_type": type(exc).__name__,
        "error": str(exc)[:500],
    }


def _write(fh: Any, row: dict[str, Any], *, index: int, total: int) -> None:
    fh.write(json.dumps(row, sort_keys=True) + "\n")
    fh.flush()
    final = row.get("final") or {}
    ag = row.get("agentic") or {}
    print(
        f"agentic {index}/{total} task={str(row.get('task_id'))[:8]} "
        f"status={row.get('status', 'ok')} tools={ag.get('tool_calls_made')} "
        f"finish={final.get('finish_reason')} clen={len(final.get('content') or '')} "
        f"elapsed={(final.get('elapsed_ms') or 0) / 1000:.0f}s"
    )


def _select_tasks(tasks: tuple[DracoTask, ...], *, task_filter: list[str] | None, limit: int | None) -> list[DracoTask]:
    wanted = set(task_filter or ())
    out = [t for t in tasks if not wanted or any(t.id == w or t.id.startswith(w) for w in wanted)]
    if limit is not None:
        out = out[:limit]
    return out


def _completed_ids(path: Path) -> set[str]:
    done: set[str] = set()
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("status") != "failed" and isinstance(row.get("task_id"), str):
            done.add(row["task_id"])
    return done


if __name__ == "__main__":
    raise SystemExit(main())
