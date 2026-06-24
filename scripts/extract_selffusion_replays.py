#!/usr/bin/env python3
"""Recover full self-fusion replays from the Claude Code subagent transcripts.

The §8 workflows returned only scores, not the raw runs. But every subagent transcript
holds the complete trace — the prompt (→ maps to a DRACO task), every tool call
(WebSearch / WebFetch / Bash), and the final output. This reconstructs proper
``trustedrouter.fusion_draco.replay.v1`` rows so they are drop-in for
``scripts/draco_rejudge.py`` and the leak audit:

  replays/fusion-selffusion-{haiku,sonnet}-research.jsonl   solo-style: agentic tool trace + report
  replays/fusion-selffusion-{haiku,sonnet}-fused.jsonl      fusion-style: the first-N fused answers

These are RECOVERED (flag ``recovered_from_transcript: true``): tool names are mapped to the
repo's web_search/web_fetch/bash, ``result_chars`` is the transcript-stored result length (not
the original Exa fetch size), and tokens/timing are null. The web_search queries and web_fetch
URLs themselves are faithful — that is the audit material. Re-fuse / re-grade offline from these
instead of re-running agentic research.

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
SCHEMA = "trustedrouter.fusion_draco.replay.v1"
MIN_OUT = 400  # chars; below this the run never produced a final output
TOOL_MAP = {"WebSearch": "web_search", "WebFetch": "web_fetch", "Bash": "bash"}


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def model_id(kind_text: str) -> tuple[str, str]:
    # Sonnet runs: lean research prompt ("AT MOST ~6") OR a synth/judge prompt whose
    # panel evidence is labeled with the Sonnet model id.
    sonnet = "AT MOST ~6" in kind_text or "anthropic/claude-sonnet-4-6" in kind_text
    return ("sonnet", "anthropic/claude-sonnet-4-6") if sonnet else ("haiku", "anthropic/claude-haiku-4-5")


def tasks_by_problem():
    m = json.loads((ROOT / "data" / "draco-full-100.manifest.json").read_text())
    return {t["problem"].strip(): t for t in m["tasks"]}


def parse_transcript(rows: list[dict]):
    """Return (first_user_text, tool_trace, result_lens, final_text)."""
    msgs = [r.get("message", {}) for r in rows if r.get("type") in ("user", "assistant")]
    first_user = next((text_of(m.get("content")) for m in msgs if m.get("role") == "user"), "")
    result_len: dict[str, int] = {}
    trace: list[dict] = []
    final_text = ""
    for m in msgs:
        c = m.get("content")
        if not isinstance(c, list):
            if m.get("role") == "assistant" and isinstance(c, str) and c.strip():
                final_text = c.strip()
            continue
        for b in c:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result":
                result_len[b.get("tool_use_id", "")] = len(text_of(b.get("content")))
            elif b.get("type") == "tool_use" and b.get("name") in TOOL_MAP:
                trace.append({"id": b.get("id"), "name": TOOL_MAP[b["name"]], "args": b.get("input") or {}})
        t = text_of(c)
        if m.get("role") == "assistant" and t.strip():
            final_text = t.strip()
    tools = [{"name": s["name"], "args": s["args"], "error": None,
              "result_chars": result_len.get(s["id"], 0)} for s in trace]
    return first_user, tools, final_text


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workflows-dir", type=Path, default=DEFAULT_WF)
    args = ap.parse_args()
    by_prob = tasks_by_problem()

    research: dict[str, dict[str, dict]] = {"haiku": {}, "sonnet": {}}  # model -> hash -> row
    fused: dict[str, dict[str, dict]] = {"haiku": {}, "sonnet": {}}
    n_res = n_fus = n_skip = 0

    for jf in sorted(args.workflows_dir.glob("*/agent-*.jsonl")):
        try:
            rows = [json.loads(l) for l in jf.read_text(errors="replace").splitlines() if l.strip()]
        except Exception:
            continue
        first_user, tools, final_text = parse_transcript(rows)
        is_research = "deep-research analyst" in first_user
        is_synth = "TrustedRouter Fusion panel answers and judge analysis follow" in first_user
        if not (is_research or is_synth):
            continue
        if len(final_text) < MIN_OUT:
            n_skip += 1
            continue
        # map to task via the embedded problem
        if is_research and "QUESTION:\n" in first_user:
            prob = first_user.split("QUESTION:\n", 1)[1].split("\n\nInstructions:", 1)[0].strip()
        else:  # synth prompt leads with the bare task problem
            prob = first_user.split("\n\n", 1)[0].strip()
        task = by_prob.get(prob) or next((t for p, t in by_prob.items() if p == prob or p.startswith(prob[:200])), None)
        if not task:
            n_skip += 1
            continue
        mk, mid = model_id(first_user)
        src = f"{jf.parent.name}/{jf.name}"
        if is_research:
            row = {
                "schema": SCHEMA, "recovered_from_transcript": True,
                "config_id": f"selffusion_{mk}_research", "task_id": task["id"], "domain": task["domain"],
                "task": {"domain": task["domain"], "id": task["id"], "problem": task["problem"], "rubric": task["rubric"]},
                "final": {"content": final_text, "model": mid, "finish_reason": "stop",
                          "elapsed_ms": None, "http_status": None, "input_tokens": None,
                          "output_tokens": None, "request_id": None},
                "agentic": {"tools": tools, "tool_calls_made": len(tools), "truncated_loop": False},
                "source": src,
            }
            research[mk][hashlib.md5(final_text.encode()).hexdigest()] = row
            n_res += 1
        else:  # fused (synth)
            n_panel = first_user.count("] model=")  # panel evidence entries == N fused
            row = {
                "schema": SCHEMA, "recovered_from_transcript": True,
                "config_id": f"selffusion_{mk}_x{n_panel}", "task_id": task["id"], "domain": task["domain"],
                "task": {"domain": task["domain"], "id": task["id"], "problem": task["problem"], "rubric": task["rubric"]},
                "final": {"content": final_text, "model": mid, "finish_reason": "stop",
                          "elapsed_ms": None, "http_status": None, "input_tokens": None,
                          "output_tokens": None, "request_id": None},
                "fusion": {"panel_size": n_panel, "base_model": mid, "judge_model": mid, "fuser_model": mid,
                           "self_fusion": True},
                "source": src,
            }
            fused[mk][hashlib.md5((task["id"] + str(n_panel) + final_text[:80]).encode()).hexdigest()] = row
            n_fus += 1

    # drop the earlier report-only files
    for old in ("fusion-selffusion-haiku.jsonl", "fusion-selffusion-sonnet.jsonl"):
        (ROOT / "replays" / old).unlink(missing_ok=True)

    for mk in ("haiku", "sonnet"):
        for kind, store in (("research", research), ("fused", fused)):
            rows_out = sorted(store[mk].values(), key=lambda x: (x["domain"], x["task_id"], x.get("config_id", "")))
            path = ROOT / "replays" / f"fusion-selffusion-{mk}-{kind}.jsonl"
            with path.open("w", encoding="utf-8") as f:
                for r in rows_out:
                    f.write(json.dumps(r, sort_keys=True) + "\n")
            tasks = len({r["task_id"] for r in rows_out})
            print(f"{path.name}: {len(rows_out)} rows / {tasks} tasks")
    print(f"\nresearch reconstructed {n_res} | fused {n_fus} | skipped(no final / unmatched) {n_skip}")


if __name__ == "__main__":
    main()
