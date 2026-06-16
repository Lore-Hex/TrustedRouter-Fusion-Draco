#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from trusted_router.evals.draco_replay import (
    iter_jsonl,
    rejudge_replay_row,
    replay_completed_ids,
)
from trusted_router.evals.fusion_live import (
    DEFAULT_JUDGE_REASONING_EFFORT,
    DEFAULT_TR_API_BASE_URL,
    DEFAULT_TR_CRITERION_JUDGE_CHUNK_SIZE,
    DEFAULT_TR_CRITERION_JUDGE_MAX_OUTPUT_TOKENS,
    TrustedRouterChatClient,
    load_eval_key,
)
from trusted_router.evals.fusion_micro import DRACO_JUDGE_MODEL, DRACO_JUDGE_PASSES


def main(argv: list[str] | None = None) -> int:
    _line_buffer_stdout()
    parser = argparse.ArgumentParser(
        description="Rejudge saved DRACO private replay JSONL without rerunning panel models."
    )
    parser.add_argument("replay", nargs="+", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/fusion-draco/rejudge-results.jsonl"),
    )
    parser.add_argument("--config", action="append", help="Only rejudge selected config IDs.")
    parser.add_argument("--judge-model", default=DRACO_JUDGE_MODEL)
    parser.add_argument("--judge-passes", type=int, default=DRACO_JUDGE_PASSES)
    parser.add_argument(
        "--judge-reasoning-effort",
        choices=("default", "low", "high"),
        default=DEFAULT_JUDGE_REASONING_EFFORT,
        help="Reasoning effort for judge calls. Use high for OpenRouter-comparable scoring.",
    )
    parser.add_argument(
        "--criterion-chunk-size", type=int, default=DEFAULT_TR_CRITERION_JUDGE_CHUNK_SIZE
    )
    parser.add_argument(
        "--judge-max-tokens", type=int, default=DEFAULT_TR_CRITERION_JUDGE_MAX_OUTPUT_TOKENS
    )
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--base-url", default=DEFAULT_TR_API_BASE_URL)
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of replay rows to rejudge concurrently.",
    )
    parser.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=5,
        help="Abort after this many consecutive row-level failures. Set 0 to disable.",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if args.workers < 1 or args.workers > 16:
        print("workers must be between 1 and 16")
        return 2
    if args.timeout_seconds <= 0:
        print("timeout-seconds must be positive")
        return 2
    if args.max_consecutive_failures < 0:
        print("max-consecutive-failures must be non-negative")
        return 2

    selected_configs = set(args.config or ())
    rows = [
        row
        for path in args.replay
        for row in iter_jsonl(path)
        if _row_is_rejudgeable(row, selected_configs=selected_configs)
    ]
    completed = replay_completed_ids(args.output) if args.resume else set()
    pending = [
        row
        for row in rows
        if (str(row.get("config_id")), str(row.get("task_id"))) not in completed
    ]
    print(f"replay rows: {len(rows)}")
    print(f"pending rows: {len(pending)}")
    print(f"output: {args.output}")
    if not args.execute:
        print("dry run only; add --execute to call the judge model.")
        return 0

    api_key = _first_key(
        (
            "TR_FUSION_EVAL_API_KEY",
            "TR_API_KEY",
            "TRUSTEDROUTER_API_KEY",
            "TR_SMOKE_API_KEY",
            "TR_API_KEY_FOR_SELF_HEAL",
        )
    )
    if not api_key:
        print("missing TrustedRouter API key in env/key file")
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    client = TrustedRouterChatClient(api_key, base_url=args.base_url, timeout_seconds=args.timeout_seconds)
    try:
        with args.output.open("a" if args.resume else "w", encoding="utf-8") as fh:
            consecutive_failures = 0
            if args.workers == 1:
                for index, row in enumerate(pending, start=1):
                    judged = _rejudge_one(row, args=args, client=client)
                    _write_judged_row(fh, judged, index=index, total=len(pending), source=row)
                    consecutive_failures = (
                        consecutive_failures + 1 if _judged_failed(judged) else 0
                    )
                    if _failure_circuit_open(args, consecutive_failures):
                        return 1
            else:
                max_workers = min(args.workers, len(pending))
                executor = ThreadPoolExecutor(max_workers=max_workers)
                interrupted = False
                try:
                    futures: dict[Future[dict[str, Any]], dict[str, Any]] = {}
                    next_index = 0
                    completed_count = 0

                    def submit_next() -> None:
                        nonlocal next_index
                        if next_index >= len(pending):
                            return
                        row = pending[next_index]
                        next_index += 1
                        futures[executor.submit(_rejudge_one, row, args=args, client=client)] = row

                    for _ in range(max_workers):
                        submit_next()

                    while futures:
                        done, _ = wait(futures, return_when=FIRST_COMPLETED)
                        for future in done:
                            row = futures.pop(future)
                            try:
                                judged = future.result()
                            except Exception as exc:  # noqa: BLE001 - keep other rows running.
                                judged = _failure_row(row, exc)
                            completed_count += 1
                            _write_judged_row(
                                fh,
                                judged,
                                index=completed_count,
                                total=len(pending),
                                source=row,
                            )
                            consecutive_failures = (
                                consecutive_failures + 1 if _judged_failed(judged) else 0
                            )
                            if _failure_circuit_open(args, consecutive_failures):
                                executor.shutdown(wait=False, cancel_futures=True)
                                return 1
                            submit_next()
                except KeyboardInterrupt:
                    interrupted = True
                    print("interrupted; cancelling pending rejudge rows")
                    executor.shutdown(wait=False, cancel_futures=True)
                    return 130
                finally:
                    if not interrupted:
                        executor.shutdown(wait=True)
    finally:
        client.close()
    return 0


def _rejudge_one(
    row: dict[str, Any],
    *,
    args: argparse.Namespace,
    client: TrustedRouterChatClient,
) -> dict[str, Any]:
    try:
        return rejudge_replay_row(
            row,
            tr_client=client,
            judge_model=args.judge_model,
            judge_passes=args.judge_passes,
            criterion_chunk_size=args.criterion_chunk_size,
            judge_max_tokens=args.judge_max_tokens,
            timeout_seconds=args.timeout_seconds,
            judge_reasoning_effort=_reasoning_effort_arg(args.judge_reasoning_effort),
        )
    except Exception as exc:  # noqa: BLE001 - record row-level rejudge failure.
        return _failure_row(row, exc)


def _write_judged_row(
    fh: Any,
    judged: dict[str, Any],
    *,
    index: int,
    total: int,
    source: dict[str, Any],
) -> None:
    fh.write(json.dumps(judged, sort_keys=True) + "\n")
    fh.flush()
    print(f"rejudged {index}/{total} task_id={source.get('task_id')} score={judged.get('score')}")


def _judged_failed(judged: dict[str, Any]) -> bool:
    return judged.get("status") == "failed" or bool(judged.get("error"))


def _failure_circuit_open(args: argparse.Namespace, consecutive_failures: int) -> bool:
    if args.max_consecutive_failures == 0:
        return False
    if consecutive_failures < args.max_consecutive_failures:
        return False
    print(
        "aborting after "
        f"{consecutive_failures} consecutive rejudge failures; "
        "use --max-consecutive-failures 0 to disable"
    )
    return True


def _row_is_rejudgeable(row: dict[str, Any], *, selected_configs: set[str]) -> bool:
    config_id = row.get("config_id")
    if not isinstance(config_id, str):
        return False
    if selected_configs and config_id not in selected_configs:
        return False
    return row.get("status") != "failed" and isinstance(row.get("final"), dict)


def _failure_row(row: dict[str, Any], exc: Exception) -> dict[str, Any]:
    return {
        "schema": "trustedrouter.fusion_draco.rejudge.v1",
        "status": "failed",
        "config_id": row.get("config_id"),
        "task_id": row.get("task_id"),
        "domain": row.get("domain"),
        "error_type": type(exc).__name__,
        "error": str(exc)[:500],
    }


def _first_key(names: tuple[str, ...]) -> str | None:
    for name in names:
        if value := load_eval_key(name):
            return value
    return None


def _reasoning_effort_arg(value: str) -> str | None:
    return None if value == "default" else value


def _line_buffer_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)


if __name__ == "__main__":
    raise SystemExit(main())
