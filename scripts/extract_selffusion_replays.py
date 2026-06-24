#!/usr/bin/env python3
"""Recover self-fusion research replays from the Claude Code subagent transcripts.

The §8 self-fusion workflows returned only scores, not the raw report text. But every
research subagent's transcript embeds the task `QUESTION:` (→ maps to a task) and, when
the run completed, ends with the full report as its final assistant message. This walks
the workflow transcript dirs, pulls each completed research report, tags it Haiku vs
Sonnet from the prompt wording, maps it to a DRACO task, dedupes, and writes per-model
replay JSONL — the raw panel material for re-fusing / re-grading offline.

Output: replays/fusion-selffusion-{haiku,sonnet}.jsonl
Rows:   {schema, base_model, task_id, domain, report, chars, source}

Usage:  python3 scripts/extract_selffusion_replays.py [--workflows-dir DIR]
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WF = Path(
    "~/.claude/projects/-Users-jperla-claude-TrustedRouter-Fusion-Draco/"
    "05129cdf-e05a-421f-93c2-2d3d4786c6a9/subagents/workflows"
).expanduser()
RESEARCH_MARKER = "deep-research analyst"
SONNET_MARKER = "AT MOST ~6"  # only the lean (Sonnet) research prompt has this
MIN_REPORT = 600  # chars; below this the run didn't finish a report


def text_of(msg: dict) -> str:
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


def problem_to_task() -> dict[str, tuple[str, str]]:
    m = json.loads((ROOT / "data" / "draco-full-100.manifest.json").read_text())
    return {t["problem"]: (t["id"], t["domain"]) for t in m["tasks"]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workflows-dir", type=Path, default=DEFAULT_WF)
    args = ap.parse_args()

    p2t = problem_to_task()
    # base_model -> task_id -> {report_hash: row}
    out: dict[str, dict[str, dict[str, dict]]] = {"haiku": {}, "sonnet": {}}
    scanned = matched = unmatched = incomplete = 0

    for jf in sorted(args.workflows_dir.glob("*/agent-*.jsonl")):
        try:
            rows = [json.loads(l) for l in jf.read_text(errors="replace").splitlines() if l.strip()]
        except Exception:
            continue
        msgs = [r.get("message", {}) for r in rows if r.get("type") in ("user", "assistant")]
        first_user = next((text_of(m) for m in msgs if m.get("role") == "user"), "")
        if RESEARCH_MARKER not in first_user:
            continue  # not a research subagent (judge/synth/grader)
        scanned += 1
        model = "sonnet" if SONNET_MARKER in first_user else "haiku"
        # task problem sits between "QUESTION:\n" and "\n\nInstructions:"
        if "QUESTION:\n" not in first_user:
            unmatched += 1
            continue
        prob = first_user.split("QUESTION:\n", 1)[1].split("\n\nInstructions:", 1)[0].strip()
        hit = p2t.get(prob)
        if not hit:
            hit = next(((tid, dom) for p, (tid, dom) in p2t.items() if p.strip() == prob), None)
        if not hit:
            unmatched += 1
            continue
        task_id, domain = hit
        # final report = last assistant text message, if substantial
        atexts = [text_of(m) for m in msgs if m.get("role") == "assistant" and text_of(m).strip()]
        report = atexts[-1].strip() if atexts else ""
        if len(report) < MIN_REPORT:
            incomplete += 1
            continue
        matched += 1
        h = hashlib.md5(report.encode()).hexdigest()
        out[model].setdefault(task_id, {})[h] = {
            "schema": "trustedrouter.fusion_draco.selffusion_replay.v1",
            "base_model": "anthropic/claude-haiku-4-5" if model == "haiku" else "anthropic/claude-sonnet-4-6",
            "task_id": task_id,
            "domain": domain,
            "report": report,
            "chars": len(report),
            "source": jf.parent.name + "/" + jf.name,
        }

    repdir = ROOT / "replays"
    for model in ("haiku", "sonnet"):
        rows_out = [r for task in sorted(out[model]) for r in out[model][task].values()]
        path = repdir / f"fusion-selffusion-{model}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for r in sorted(rows_out, key=lambda x: (x["domain"], x["task_id"])):
                f.write(json.dumps(r, sort_keys=True) + "\n")
        ntasks = len(out[model])
        print(f"{path.name}: {len(rows_out)} reports across {ntasks} tasks "
              f"(avg {len(rows_out)/ntasks:.1f}/task)" if ntasks else f"{path.name}: 0")
    print(f"\nscanned {scanned} research transcripts | matched {matched} | "
          f"unmatched {unmatched} | incomplete(no final report) {incomplete}")


if __name__ == "__main__":
    main()
