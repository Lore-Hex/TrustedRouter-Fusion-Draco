#!/usr/bin/env python3
"""Generate native trustedrouter/fusion DRACO replays with explicit token caps.

This reuses the *exact* task + frozen Exa search bundles from a prior replay
JSONL so the only variables that change are the fusion token caps and panel /
fuser model selection. It POSTs the native ``trustedrouter/fusion`` tool (panel
+ judge + synthesizer run server-side in the attested gateway) and writes rows
in the ``trustedrouter.fusion_draco.replay.v1`` schema consumed by
``scripts/draco_rejudge.py`` and ``scripts/draco_report.py``.

Why two caps:
* ``--inner-max-completion-tokens`` -> ``tools[0].parameters.max_completion_tokens``.
  In the gateway this caps the *panel* and *judge* calls (see
  ``fusionPanelRequest`` / ``fusionJudgeRequest`` in enclave fusion.go). The panel
  answers are the fuser's primary evidence, so starving them starves the fusion.
* ``--outer-max-tokens`` -> top-level ``max_tokens``. The gateway's
  ``fusionFinalRequest`` clones the request *without* resetting MaxTokens, so the
  **fuser (final synthesizer) inherits the OUTER max_tokens**. This is the cap
  that truncated the published-style Opus run (finish_reason=length on 8/10).
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from trusted_router.evals import tr_sdk
from trusted_router.evals.draco import parse_draco_task
from trusted_router.evals.exa import exa_search_bundle_from_replay_dict
from trusted_router.evals.fusion_live import (
    DEFAULT_TR_API_BASE_URL,
    _parse_chat_response,
    format_search_contexts,
    load_eval_key,
    panel_messages,
)

REPLAY_SCHEMA = "trustedrouter.fusion_draco.replay.v1"
NATIVE_FUSION_MODEL = "trustedrouter/fusion"
NATIVE_FUSION_TOOL_TYPE = "trustedrouter:fusion"

DEFAULT_BUDGET_PANEL = (
    "google/gemini-3-flash-preview",
    "moonshotai/kimi-k2.6",
    "deepseek/deepseek-v4-pro",
)
DEFAULT_FINAL_MODEL = "anthropic/claude-opus-4.8"

KEY_NAMES = (
    "TR_FUSION_EVAL_API_KEY",
    "TR_API_KEY",
    "TRUSTEDROUTER_API_KEY",
    "TR_SMOKE_API_KEY",
    "TR_API_KEY_FOR_SELF_HEAL",
)
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-replay",
        type=Path,
        default=Path(
            "artifacts/fusion-draco/native-fusion-or-budget-opus-sample10.replay.private.jsonl"
        ),
        help="Replay JSONL to reuse task + frozen searches from.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Destination replay JSONL (.replay.private.jsonl).",
    )
    parser.add_argument(
        "--task-id",
        action="append",
        default=None,
        help="Only run these task ids (8-char prefix or full uuid). Repeatable.",
    )
    parser.add_argument("--limit", type=int, default=None, help="First N source rows.")
    parser.add_argument(
        "--analysis-model",
        action="append",
        default=None,
        help="Panel model id. Repeatable. Defaults to the OpenRouter budget panel.",
    )
    parser.add_argument("--final-model", default=DEFAULT_FINAL_MODEL)
    parser.add_argument(
        "--selection-strategy",
        default="synthesize",
        choices=("synthesize", "first_success", "first_non_refusal"),
    )
    parser.add_argument("--inner-max-completion-tokens", type=int, default=4000)
    parser.add_argument("--outer-max-tokens", type=int, default=6000)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument(
        "--search-context-chars",
        type=int,
        default=4000,
        help="max_chars_per_result for format_search_contexts (match the source run).",
    )
    parser.add_argument("--config-id", default="fusion_tr_budget_native_opus")
    parser.add_argument("--base-url", default="https://api-us-central1.quillrouter.com/v1")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--retry-attempts", type=int, default=3)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--execute", action="store_true", help="Actually call the gateway.")
    args = parser.parse_args(argv)

    if args.workers < 1 or args.workers > 8:
        print("workers must be between 1 and 8")
        return 2
    if args.inner_max_completion_tokens < 1 or args.outer_max_tokens < 1:
        print("token caps must be positive")
        return 2

    analysis_models = list(args.analysis_model or DEFAULT_BUDGET_PANEL)
    rows = _load_source_rows(args.source_replay, task_filter=args.task_id, limit=args.limit)
    if not rows:
        print("no source rows matched")
        return 2

    completed = _completed_task_ids(args.output) if args.resume else set()
    pending = [r for r in rows if r["task_id"] not in completed]

    print(f"source rows: {len(rows)}")
    print(f"pending rows: {len(pending)}")
    print(f"config_id: {args.config_id}")
    print(f"panel: {analysis_models}")
    print(f"fuser: {args.final_model}  strategy: {args.selection_strategy}")
    print(
        "caps: inner max_completion_tokens="
        f"{args.inner_max_completion_tokens} outer max_tokens={args.outer_max_tokens}"
    )
    print(f"base_url: {args.base_url}")
    print(f"output: {args.output}")
    if not args.execute:
        print("dry run only; add --execute to call the gateway.")
        return 0

    api_key = _first_key()
    if not api_key:
        print("missing TrustedRouter API key (tried: " + ", ".join(KEY_NAMES) + ")")
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    client = tr_sdk.make_client(base_url=args.base_url, api_key=_first_key(), timeout=args.timeout_seconds)
    try:
        with args.output.open("a" if args.resume else "w", encoding="utf-8") as fh:
            _run(pending, args=args, analysis_models=analysis_models, client=client, fh=fh)
    finally:
        client.close()
    return 0


def _run(
    pending: list[dict[str, Any]],
    *,
    args: argparse.Namespace,
    analysis_models: list[str],
    client: "tr_sdk.TrustedRouter",
    fh: Any,
) -> None:
    total = len(pending)
    if args.workers == 1:
        for index, row in enumerate(pending, start=1):
            out = _generate_one(row, args=args, analysis_models=analysis_models, client=client)
            _write(fh, out, index=index, total=total)
        return
    max_workers = min(args.workers, total)
    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures: dict[Future[dict[str, Any]], dict[str, Any]] = {}
        next_index = 0
        done_count = 0

        def submit_next() -> None:
            nonlocal next_index
            if next_index >= total:
                return
            row = pending[next_index]
            next_index += 1
            futures[
                executor.submit(
                    _generate_one,
                    row,
                    args=args,
                    analysis_models=analysis_models,
                    client=client,
                )
            ] = row

        for _ in range(max_workers):
            submit_next()
        while futures:
            ready, _ = wait(futures, return_when=FIRST_COMPLETED)
            for fut in ready:
                row = futures.pop(fut)
                try:
                    out = fut.result()
                except Exception as exc:  # noqa: BLE001
                    out = _failure_row(row, args, analysis_models, exc)
                done_count += 1
                _write(fh, out, index=done_count, total=total)
                submit_next()
    finally:
        executor.shutdown(wait=True)


def _generate_one(
    row: dict[str, Any],
    *,
    args: argparse.Namespace,
    analysis_models: list[str],
    client: "tr_sdk.TrustedRouter",
) -> dict[str, Any]:
    try:
        task = parse_draco_task(row["task"])
        bundles = tuple(exa_search_bundle_from_replay_dict(s) for s in row["searches"])
        search_context = format_search_contexts(
            bundles, max_chars_per_result=args.search_context_chars
        )
        messages = panel_messages(task, search_context)
        request_json = _build_request(messages, args=args, analysis_models=analysis_models)
        started = time.perf_counter()
        response = _post_with_retry(client, args, request_json)
        result = _parse_chat_response(
            model=args.final_model,
            response=response,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        return {
            "schema": REPLAY_SCHEMA,
            "config_id": args.config_id,
            "task_id": row["task_id"],
            "domain": row.get("domain") or task.domain,
            "task": row["task"],
            "searches": row["searches"],
            "fusion_request": {
                "analysis_models": analysis_models,
                "final_model": args.final_model,
                "selection_strategy": args.selection_strategy,
                "inner_max_completion_tokens": args.inner_max_completion_tokens,
                "outer_max_tokens": args.outer_max_tokens,
                "search_context_chars": args.search_context_chars,
            },
            "final": {
                "content": result.content,
                "finish_reason": result.finish_reason,
                "model": result.model,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "elapsed_ms": result.elapsed_ms,
                "http_status": response.status_code,
                "request_id": result.request_id,
            },
        }
    except Exception as exc:  # noqa: BLE001
        return _failure_row(row, args, analysis_models, exc)


def _build_request(
    messages: list[dict[str, str]],
    *,
    args: argparse.Namespace,
    analysis_models: list[str],
) -> dict[str, Any]:
    return {
        "model": NATIVE_FUSION_MODEL,
        "messages": messages,
        "max_tokens": args.outer_max_tokens,
        "temperature": args.temperature,
        "tools": [
            {
                "type": NATIVE_FUSION_TOOL_TYPE,
                "parameters": {
                    "analysis_models": analysis_models,
                    "model": args.final_model,
                    "max_completion_tokens": args.inner_max_completion_tokens,
                    "selection_strategy": args.selection_strategy,
                },
            }
        ],
    }


def _post_with_retry(
    client: "tr_sdk.TrustedRouter", args: argparse.Namespace, request_json: dict[str, Any]
) -> "tr_sdk.SdkResponse":
    # Native trustedrouter/fusion call through the SDK (auth + retries handled there).
    return tr_sdk.chat_response(client, request_json)


def _failure_row(
    row: dict[str, Any],
    args: argparse.Namespace,
    analysis_models: list[str],
    exc: Exception,
) -> dict[str, Any]:
    return {
        "schema": REPLAY_SCHEMA,
        "status": "failed",
        "config_id": args.config_id,
        "task_id": row.get("task_id"),
        "domain": row.get("domain"),
        "error_type": type(exc).__name__,
        "error": str(exc)[:500],
    }


def _write(fh: Any, out: dict[str, Any], *, index: int, total: int) -> None:
    fh.write(json.dumps(out, sort_keys=True) + "\n")
    fh.flush()
    final = out.get("final") or {}
    print(
        f"gen {index}/{total} task_id={str(out.get('task_id'))[:8]} "
        f"status={out.get('status', 'ok')} finish={final.get('finish_reason')} "
        f"out_tok={final.get('output_tokens')} clen={len(final.get('content') or '')} "
        f"elapsed={(final.get('elapsed_ms') or 0) / 1000:.0f}s"
    )


def _load_source_rows(
    path: Path, *, task_filter: list[str] | None, limit: int | None
) -> list[dict[str, Any]]:
    wanted = set(task_filter or ())
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("status") == "failed" or not isinstance(row.get("task"), dict):
            continue
        tid = str(row.get("task_id") or "")
        if wanted and not any(tid == w or tid.startswith(w) for w in wanted):
            continue
        rows.append(row)
    if limit is not None:
        rows = rows[:limit]
    return rows


def _completed_task_ids(path: Path) -> set[str]:
    completed: set[str] = set()
    if not path.exists():
        return completed
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("status") != "failed" and isinstance(row.get("task_id"), str):
            completed.add(row["task_id"])
    return completed


def _first_key() -> str | None:
    for name in KEY_NAMES:
        if value := load_eval_key(name):
            return value
    return None


if __name__ == "__main__":
    raise SystemExit(main())
