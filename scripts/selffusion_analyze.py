#!/usr/bin/env python3
"""Reproduce every self-fusion number and chart from the committed result JSONs.

This re-derives FINDINGS §8 (Haiku vs Sonnet self-fusion via Claude Code subagents)
WITHOUT re-running any subagents. It reads the per-(task,N) scores saved under
``artifacts/haiku-selffusion/`` and emits:

  - curve JSONs:  haiku_n26_curve.json, bootstrap_ci.json, compare_sonnet_vs_haiku_4task.json
  - charts:       docs/draco-selffusion-haiku-scaling.svg, docs/draco-selffusion-sonnet-vs-haiku.svg
  - a printed summary table with bootstrap 95% CIs and the paired self-fusion gain.

Inputs (raw subagent outputs, committed):
  artifacts/haiku-selffusion/pilot_result.json         8 Haiku tasks   (wf_haiku_pilot.js)
  artifacts/haiku-selffusion/rem1_result.json         18 Haiku tasks   (wf_haiku_rem1.js, resumed)
  artifacts/haiku-selffusion/pilot_sonnet_result.json  4 Sonnet tasks  (wf_sonnet_pilot4.js)

Method: nonparametric bootstrap over TASKS (resample tasks with replacement,
B=20000, seed=12345), 95% percentile CI. Captures task-sampling variance only —
NOT run-ordering variance (one nested ordering was run). Deterministic: same
inputs + seed reproduce the committed outputs exactly.

Usage:  python3 scripts/selffusion_analyze.py
"""
from __future__ import annotations

import json
import random
import statistics as st
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ART = ROOT / "artifacts" / "haiku-selffusion"
DOCS = ROOT / "docs"
SEED = 12345
B = 20000

# M3 reference curve (OpenRouter / FINDINGS §6, gemini-3.1-pro grader, 100 tasks)
M3 = [66.2, 66.1, 67.7, 68.1, 68.1, 68.2, 69.5, 69.2, 68.4, 69.4]


def matrix(result: dict) -> dict[str, dict[int, float]]:
    """task_id -> {N: score}, keeping only fully-graded (task,N) cells."""
    m: dict[str, dict[int, float]] = defaultdict(dict)
    for r in result["rows"]:
        if r["covered"] == r["total"] and r["total"] > 0:
            m[r["taskId"]][r["N"]] = r["score"]
    return m


def full_curve_tasks(m: dict) -> list[str]:
    return [t for t in m if all(n in m[t] for n in range(1, 11))]


def mean_by_n(m: dict, tasks: list[str]) -> dict[int, float]:
    return {n: sum(m[t][n] for t in tasks) / len(tasks) for n in range(1, 11)}


def boot(vals: list[float], rng: random.Random) -> tuple[float, float, float]:
    n = len(vals)
    means = []
    for _ in range(B):
        s = 0.0
        for _ in range(n):
            s += vals[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    return sum(vals) / n, means[int(0.025 * B)], means[int(0.975 * B) - 1]


def gain_ci(m: dict, tasks: list[str], rng: random.Random) -> tuple[float, float, float]:
    per = [(sum(m[t][n] for n in range(2, 11)) / 9) - m[t][1] for t in tasks]
    return boot(per, rng)


def svg_scaling(curve, lo, hi, out: Path, n_tasks: int) -> None:
    W, H = 1120, 610
    lows = [lo[k] for k in lo]
    his = [hi[k] for k in hi]
    ymin = max(0.0, min(lows) - 3)
    ymax = min(100.0, max(his) + 3)
    x0, x1, ytop, ybot = 92.0, 860.0, 110.0, 520.0
    X = lambda n: x0 + (x1 - x0) * (n - 1) / 9
    Y = lambda v: ybot - (ybot - ytop) * (v - ymin) / (ymax - ymin)
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="100%" style="height:auto" font-family="Inter,Arial,sans-serif">',
         f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
         '<text x="48" y="44" font-size="29" font-weight="700" fill="#111827">Haiku self-fusion: small, noisy gain</text>',
         f'<text x="48" y="74" font-size="16.5" fill="#6b7280">DRACO vs Haiku runs fused — {n_tasks} tasks, Sonnet-4.6 chunk-all grader. Whiskers = 95% bootstrap CI over tasks.</text>']
    for v in range(int(ymin // 5) * 5, int(ymax // 5 + 1) * 5 + 1, 5):
        if v < ymin or v > ymax:
            continue
        p.append(f'<line x1="{x0}" y1="{Y(v):.1f}" x2="{x1}" y2="{Y(v):.1f}" stroke="#eef0f2" stroke-width="1"/>')
        p.append(f'<text x="80" y="{Y(v)+5:.1f}" font-size="15" text-anchor="end" fill="#6b7280">{v}</text>')
    for n in range(1, 11):
        p.append(f'<text x="{X(n):.1f}" y="548.0" font-size="15" text-anchor="middle" fill="#6b7280">{n}</text>')
    p.append(f'<text x="{(x0+x1)/2:.1f}" y="578.0" font-size="16" text-anchor="middle" fill="#111827">number of Haiku runs fused</text>')
    solo = curve[1]
    p.append(f'<line x1="{x0}" y1="{Y(solo):.1f}" x2="{x1}" y2="{Y(solo):.1f}" stroke="#c23b3b" stroke-width="1.3" stroke-dasharray="6 4"/>')
    p.append(f'<text x="{x1+8:.1f}" y="{Y(solo)+5:.1f}" font-size="14" fill="#c23b3b">solo {solo:.1f}</text>')
    for n in range(1, 11):
        x, a, b = X(n), Y(lo[n]), Y(hi[n])
        p.append(f'<line x1="{x:.1f}" y1="{a:.1f}" x2="{x:.1f}" y2="{b:.1f}" stroke="#1d9e75" stroke-width="2"/>')
        p.append(f'<line x1="{x-5:.1f}" y1="{a:.1f}" x2="{x+5:.1f}" y2="{a:.1f}" stroke="#1d9e75" stroke-width="2"/>')
        p.append(f'<line x1="{x-5:.1f}" y1="{b:.1f}" x2="{x+5:.1f}" y2="{b:.1f}" stroke="#1d9e75" stroke-width="2"/>')
    pts = " ".join(f"{X(n):.1f},{Y(curve[n]):.1f}" for n in range(1, 11))
    p.append(f'<polyline points="{pts}" fill="none" stroke="#1d9e75" stroke-width="2.5"/>')
    for n in range(1, 11):
        p.append(f'<circle cx="{X(n):.1f}" cy="{Y(curve[n]):.1f}" r="5" fill="#1d9e75"/>')
        p.append(f'<text x="{X(n):.1f}" y="{Y(curve[n])-12:.1f}" font-size="13" font-weight="600" text-anchor="middle" fill="#0f6e56">{curve[n]:.1f}</text>')
    p.append("</svg>")
    out.write_text("".join(p), encoding="utf-8")


def svg_compare(S, Hh, out: Path, n_shared: int) -> None:
    W, H = 1120, 610
    allv = [S["ci_lo"][n] for n in range(1, 11)] + [S["ci_hi"][n] for n in range(1, 11)]
    allv += [Hh["ci_lo"][n] for n in range(1, 11)] + [Hh["ci_hi"][n] for n in range(1, 11)]
    ymin = max(0.0, min(allv) - 3)
    ymax = min(100.0, max(allv) + 3)
    x0, x1, ytop, ybot = 92.0, 840.0, 110.0, 520.0
    X = lambda n: x0 + (x1 - x0) * (n - 1) / 9
    Y = lambda v: ybot - (ybot - ytop) * (v - ymin) / (ymax - ymin)
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="100%" style="height:auto" font-family="Inter,Arial,sans-serif">',
         f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
         '<text x="48" y="44" font-size="28" font-weight="700" fill="#111827">Sonnet fuser climbs, Haiku barely moves</text>',
         f'<text x="48" y="73" font-size="16" fill="#6b7280">{n_shared} shared tasks, same Sonnet-4.6 grader. Whiskers = 95% bootstrap CI over tasks (B=20k).</text>']
    for v in range(int(ymin // 5) * 5, int(ymax // 5 + 1) * 5 + 1, 5):
        if v < ymin or v > ymax:
            continue
        p.append(f'<line x1="{x0}" y1="{Y(v):.1f}" x2="{x1}" y2="{Y(v):.1f}" stroke="#eef0f2" stroke-width="1"/>')
        p.append(f'<text x="80" y="{Y(v)+5:.1f}" font-size="14" text-anchor="end" fill="#6b7280">{v}</text>')
    for n in range(1, 11):
        p.append(f'<text x="{X(n):.1f}" y="548.0" font-size="15" text-anchor="middle" fill="#6b7280">{n}</text>')
    p.append(f'<text x="{(x0+x1)/2:.1f}" y="578.0" font-size="16" text-anchor="middle" fill="#111827">number of runs fused</text>')

    def series(c, color, off):
        for n in range(1, 11):
            x, a, b = X(n) + off, Y(c["ci_lo"][n]), Y(c["ci_hi"][n])
            p.append(f'<line x1="{x:.1f}" y1="{a:.1f}" x2="{x:.1f}" y2="{b:.1f}" stroke="{color}" stroke-width="1.6" stroke-opacity="0.55"/>')
            p.append(f'<line x1="{x-4:.1f}" y1="{a:.1f}" x2="{x+4:.1f}" y2="{a:.1f}" stroke="{color}" stroke-width="1.6" stroke-opacity="0.55"/>')
            p.append(f'<line x1="{x-4:.1f}" y1="{b:.1f}" x2="{x+4:.1f}" y2="{b:.1f}" stroke="{color}" stroke-width="1.6" stroke-opacity="0.55"/>')
        pts = " ".join(f"{X(n)+off:.1f},{Y(c['mean'][n]):.1f}" for n in range(1, 11))
        p.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2.8"/>')
        for n in range(1, 11):
            p.append(f'<circle cx="{X(n)+off:.1f}" cy="{Y(c["mean"][n]):.1f}" r="4.5" fill="{color}"/>')

    series(Hh, "#D85A30", -4)
    series(S, "#1D9E75", +4)
    p.append(f'<text x="{X(10)+10:.1f}" y="{Y(S["mean"][10])+4:.1f}" font-size="15" font-weight="600" fill="#0F6E56">Sonnet</text>')
    p.append(f'<text x="{X(10)+10:.1f}" y="{Y(Hh["mean"][10])+4:.1f}" font-size="15" font-weight="600" fill="#993C1D">Haiku</text>')
    p.append("</svg>")
    out.write_text("".join(p), encoding="utf-8")


def load_merged(kind: str) -> dict:
    """Merge every committed *_result.json for a base model into one task->{N:score} matrix.
    kind='sonnet' -> files with 'sonnet' in the name; kind='haiku' -> the rest."""
    m: dict[str, dict[int, float]] = {}
    files = sorted(p for p in ART.glob("*_result.json")
                   if (("sonnet" in p.name) == (kind == "sonnet")))
    used = []
    for f in files:
        r = json.loads(f.read_text())
        mm = matrix(r)
        if mm:
            m.update(mm)  # task ids are disjoint across shards
            used.append(f.name)
    return m, used


def main() -> None:
    rng = random.Random(SEED)
    Mh, hfiles = load_merged("haiku")
    Ms, sfiles = load_merged("sonnet")
    haikuTasks = full_curve_tasks(Mh)
    sonnetTasks = full_curve_tasks(Ms)
    shared = [t for t in sonnetTasks if t in Mh]  # same tasks both models ran

    print(f"Haiku  shards {hfiles}")
    print(f"Sonnet shards {sfiles}")
    print(f"Haiku full-curve tasks: {len(haikuTasks)} | Sonnet: {len(sonnetTasks)} | shared: {len(shared)}\n")

    # ---- Haiku scaling curve + bootstrap ----
    ch = mean_by_n(Mh, haikuTasks)
    lo, hi = {}, {}
    print(f"Haiku n={len(haikuTasks)} : N  mean [95% CI]")
    for n in range(1, 11):
        m, a, b = boot([Mh[t][n] for t in haikuTasks], rng)
        ch[n], lo[n], hi[n] = m, a, b
        print(f"  {n:>2}: {m:5.1f} [{a:5.1f}, {b:5.1f}]")
    gm, ga, gb = gain_ci(Mh, haikuTasks, rng)
    print(f"Haiku gain mean(N>=2)-solo: {gm:+.2f} [{ga:+.2f}, {gb:+.2f}]  "
          f"({'excludes' if (ga > 0 or gb < 0) else 'includes'} 0)\n")
    (ART / "haiku_curve.json").write_text(json.dumps({
        "mean_by_N": {str(n): round(ch[n], 2) for n in range(1, 11)},
        "ci_lo": {str(n): round(lo[n], 2) for n in range(1, 11)},
        "ci_hi": {str(n): round(hi[n], 2) for n in range(1, 11)},
        "n_tasks": len(haikuTasks), "gain": round(gm, 2), "gain_ci": [round(ga, 2), round(gb, 2)],
    }, indent=1))

    # ---- Sonnet n=4 + Haiku-on-shared-4 (apples-to-apples comparison) ----
    def curve_with_ci(m, tasks):
        out = {"mean": {}, "ci_lo": {}, "ci_hi": {}}
        for n in range(1, 11):
            mm, a, b = boot([m[t][n] for t in tasks], rng)
            out["mean"][n], out["ci_lo"][n], out["ci_hi"][n] = mm, a, b
        return out

    Sc = curve_with_ci(Ms, sonnetTasks)
    Hc4 = curve_with_ci(Mh, shared)
    sg = gain_ci(Ms, sonnetTasks, rng)
    hg4 = gain_ci(Mh, shared, rng)
    print(f"Sonnet n={len(sonnetTasks)} gain: {sg[0]:+.2f} [{sg[1]:+.2f}, {sg[2]:+.2f}]")
    print(f"Haiku (shared {len(shared)}) gain: {hg4[0]:+.2f} [{hg4[1]:+.2f}, {hg4[2]:+.2f}]")

    (ART / "sonnet_curve.json").write_text(json.dumps({
        "mean_by_N": {str(n): round(Sc["mean"][n], 2) for n in range(1, 11)},
        "ci_lo": {str(n): round(Sc["ci_lo"][n], 2) for n in range(1, 11)},
        "ci_hi": {str(n): round(Sc["ci_hi"][n], 2) for n in range(1, 11)},
        "n_tasks": len(sonnetTasks), "gain": round(sg[0], 2), "gain_ci": [round(sg[1], 2), round(sg[2], 2)],
    }, indent=1))

    # ---- render charts ----
    svg_scaling(ch, lo, hi, DOCS / "draco-selffusion-haiku-scaling.svg", len(haikuTasks))
    svg_compare(Sc, Hc4, DOCS / "draco-selffusion-sonnet-vs-haiku.svg", len(shared))
    print("\nwrote docs/draco-selffusion-haiku-scaling.svg, docs/draco-selffusion-sonnet-vs-haiku.svg")
    print("wrote artifacts/haiku-selffusion/haiku_curve.json + sonnet_curve.json")
    print(f"\nM3 reference (FINDINGS §6, gemini grader): {M3[0]} -> {M3[-1]} (+{M3[-1]-M3[0]:.1f})")


if __name__ == "__main__":
    main()
