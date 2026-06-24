#!/usr/bin/env python3
"""Role-isolation analysis: split the self-fusion fuser into judge + synthesizer.

Holds the Sonnet research drafts fixed and runs the full 2x2 of {Sonnet,Haiku} judge x
{Sonnet,Haiku} synthesizer, graded by Sonnet-4.6, to find which seat carries the gain:

  SS = Sonnet judge + Sonnet synth   (artifacts/haiku-selffusion/*sonnet*_result.json, the +8.0 run)
  HS = Haiku  judge + Sonnet synth   (artifacts/chair-isolation/results-hs/*_result.json)
  SH = Sonnet judge + Haiku  synth   (artifacts/chair-isolation/results-sh/*_result.json)
  HH = Haiku  judge + Haiku  synth   (artifacts/chair-isolation/results/*_result.json)

Reuses the committed bootstrap (seed 12345, B=20000) and matrix/full_curve_tasks/gain_ci
from scripts/selffusion_analyze.py. Reports each cell's paired gain (mean N>=2 - N=1) with
its 95% CI on the tasks shared across all four cells, and the marginal effect of downgrading
each seat S->H. Writes artifacts/chair-isolation/results/role_isolation_curves.json and a
grouped-bar SVG to docs/draco-selffusion-role-isolation.svg.

Usage:  python3 scripts/selffusion_role_isolation_analyze.py
"""
from __future__ import annotations

import glob
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from selffusion_analyze import boot, full_curve_tasks, gain_ci, matrix, SEED  # noqa: E402

DOCS = ROOT / "docs"
CELLS = {
    "SS": ("Sonnet judge, Sonnet synth", "artifacts/haiku-selffusion/*sonnet*_result.json"),
    "HS": ("Haiku judge, Sonnet synth", "artifacts/chair-isolation/results-hs/*_result.json"),
    "SH": ("Sonnet judge, Haiku synth", "artifacts/chair-isolation/results-sh/*_result.json"),
    "HH": ("Haiku judge, Haiku synth", "artifacts/chair-isolation/results/*_result.json"),
}


def load(pattern: str) -> dict:
    m: dict = {}
    for p in sorted(glob.glob(str(ROOT / pattern))):
        mm = matrix(json.loads(Path(p).read_text()))
        if mm:
            m.update(mm)
    return m


def svg_gain_bars(gains: dict, out: Path) -> None:
    # gains[key] = (mean, lo, hi); grouped by synthesizer (x), colored by judge.
    W, H = 760, 460
    x0, x1, ytop, ybot = 90.0, 700.0, 90.0, 380.0
    ymin, ymax = -3.0, 15.0
    Y = lambda v: ybot - (ybot - ytop) * (v - ymin) / (ymax - ymin)
    groups = [("Sonnet synthesizer", "SS", "HS"), ("Haiku synthesizer", "SH", "HH")]
    blue, coral = "#185FA5", "#D85A30"
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="100%" style="height:auto" font-family="Inter,Arial,sans-serif">',
         f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
         '<text x="44" y="40" font-size="25" font-weight="700" fill="#111827">The synthesizer is the lever, not the judge</text>',
         '<text x="44" y="68" font-size="15" fill="#6b7280">DRACO self-fusion gain (mean N&#8805;2 &#8722; N=1), same Sonnet drafts, 23 shared tasks. Whiskers = 95% bootstrap CI.</text>']
    for v in range(int(ymin), int(ymax) + 1, 3):
        p.append(f'<line x1="{x0}" y1="{Y(v):.1f}" x2="{x1}" y2="{Y(v):.1f}" stroke="#eef0f2" stroke-width="1"/>')
        p.append(f'<text x="{x0-10}" y="{Y(v)+5:.1f}" font-size="13" text-anchor="end" fill="#6b7280">{v:+d}</text>')
    p.append(f'<line x1="{x0}" y1="{Y(0):.1f}" x2="{x1}" y2="{Y(0):.1f}" stroke="#9ca3af" stroke-width="1.4"/>')
    gw = (x1 - x0) / 2
    bw = 96
    for gi, (label, kS, kH) in enumerate(groups):
        gx = x0 + gw * gi + gw / 2
        for j, (key, color, jl) in enumerate([(kS, blue, "Sonnet judge"), (kH, coral, "Haiku judge")]):
            m, lo, hi = gains[key]
            bx = gx + (j - 0.5) * (bw + 14) - bw / 2
            top = Y(max(0, m)); bot = Y(min(0, m))
            p.append(f'<rect x="{bx:.1f}" y="{top:.1f}" width="{bw}" height="{abs(bot-top):.1f}" fill="{color}" rx="3"/>')
            cx = bx + bw / 2
            p.append(f'<line x1="{cx:.1f}" y1="{Y(lo):.1f}" x2="{cx:.1f}" y2="{Y(hi):.1f}" stroke="#374151" stroke-width="1.6"/>')
            p.append(f'<line x1="{cx-6:.1f}" y1="{Y(lo):.1f}" x2="{cx+6:.1f}" y2="{Y(lo):.1f}" stroke="#374151" stroke-width="1.6"/>')
            p.append(f'<line x1="{cx-6:.1f}" y1="{Y(hi):.1f}" x2="{cx+6:.1f}" y2="{Y(hi):.1f}" stroke="#374151" stroke-width="1.6"/>')
            p.append(f'<text x="{cx:.1f}" y="{Y(hi)-8:.1f}" font-size="15" font-weight="700" text-anchor="middle" fill="{color}">{m:+.1f}</text>')
            p.append(f'<text x="{cx:.1f}" y="{ybot+22:.1f}" font-size="12.5" text-anchor="middle" fill="#374151">{jl}</text>')
        p.append(f'<text x="{gx:.1f}" y="{ybot+46:.1f}" font-size="15" font-weight="600" text-anchor="middle" fill="#111827">{label}</text>')
    p.append('</svg>')
    out.write_text("".join(p), encoding="utf-8")


def main() -> None:
    rng = random.Random(SEED)
    mats = {k: load(pat) for k, (_, pat) in CELLS.items()}
    fc = {k: set(full_curve_tasks(v)) for k, v in mats.items()}
    shared = sorted(set.intersection(*fc.values()))
    print("full-curve per cell:", {k: len(v) for k, v in fc.items()})
    print(f"shared across all 4 cells: {len(shared)} tasks\n")

    out = {"n_shared_tasks": len(shared), "cells": {}}
    gains = {}
    print(f"{'cell':4s} {'config':24s} {'N1':>5} {'N10':>5}   gain  95% CI")
    for k, (desc, _) in CELLS.items():
        m = mats[k]
        n1 = sum(m[t][1] for t in shared) / len(shared)
        n10 = sum(m[t][10] for t in shared) / len(shared)
        g = gain_ci(m, shared, rng)
        gains[k] = g
        by_n = {str(n): round(boot([m[t][n] for t in shared], rng)[0], 2) for n in range(1, 11)}
        out["cells"][k] = {"config": desc, "mean_by_N": by_n,
                           "gain": round(g[0], 2), "gain_ci": [round(g[1], 2), round(g[2], 2)]}
        sig = "sig" if (g[1] > 0 or g[2] < 0) else "n.s."
        print(f"{k:4s} {desc:24s} {n1:5.1f} {n10:5.1f}   {g[0]:+5.2f} [{g[1]:+5.1f},{g[2]:+5.1f}] {sig}")

    G = lambda k: gains[k][0]
    out["marginal"] = {
        "downgrade_synth_judge_sonnet": round(G("SH") - G("SS"), 2),
        "downgrade_synth_judge_haiku": round(G("HH") - G("HS"), 2),
        "downgrade_judge_synth_sonnet": round(G("HS") - G("SS"), 2),
        "downgrade_judge_synth_haiku": round(G("HH") - G("SH"), 2),
    }
    print("\nmarginal Δgain when downgrading a seat Sonnet->Haiku:")
    print(f"  synth (judge=Sonnet): {out['marginal']['downgrade_synth_judge_sonnet']:+.2f}   "
          f"synth (judge=Haiku): {out['marginal']['downgrade_synth_judge_haiku']:+.2f}")
    print(f"  judge (synth=Sonnet): {out['marginal']['downgrade_judge_synth_sonnet']:+.2f}   "
          f"judge (synth=Haiku): {out['marginal']['downgrade_judge_synth_haiku']:+.2f}")

    (ROOT / "artifacts/chair-isolation/results/role_isolation_curves.json").write_text(json.dumps(out, indent=1))
    DOCS.mkdir(exist_ok=True)
    svg_gain_bars(gains, DOCS / "draco-selffusion-role-isolation.svg")
    print("\nwrote artifacts/chair-isolation/results/role_isolation_curves.json")
    print("wrote docs/draco-selffusion-role-isolation.svg")


if __name__ == "__main__":
    main()
