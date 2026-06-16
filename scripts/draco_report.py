#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from trusted_router.evals.draco_replay import iter_jsonl, markdown_report, summarize_score_rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a side-by-side DRACO score report from replay or rejudge JSONL."
    )
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--title", default="TrustedRouter DRACO replication report")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)

    rows = [row for path in args.inputs for row in iter_jsonl(path)]
    summaries = summarize_score_rows(rows)
    report = markdown_report(summaries, title=args.title)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        print(f"report: {args.output}")
    else:
        print(report, end="")
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(
                [
                    {
                        "config_id": item.config_id,
                        "completed": item.completed,
                        "failed": item.failed,
                        "mean_score": item.mean_score,
                        "openrouter_score": item.openrouter_score,
                        "delta_from_openrouter": item.delta_from_openrouter,
                    }
                    for item in summaries
                ],
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"json: {args.json_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
