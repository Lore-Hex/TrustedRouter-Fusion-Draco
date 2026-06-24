#!/usr/bin/env python3
"""Fuser-isolation analysis: hold the Sonnet drafts fixed, swap ONLY the fuser.

Compares two self-fusion N=1..10 curves on the SAME persisted Sonnet research drafts,
graded by the same Sonnet-4.6 chunk-all grader:

  - Sonnet fuser (existing): artifacts/haiku-selffusion/*sonnet*_result.json  (the +8.0 arm)
  - Haiku  fuser (new):      artifacts/chair-isolation/results/*_result.json  (re-fused, this run)

Reuses the repo's bootstrap (seed 12345, B=20000) and the svg_compare renderer (whisker
error bars = 95% bootstrap CI over tasks) from scripts/selffusion_analyze.py — same method
behind the existing curves, so the two are directly comparable. Also reports the paired
GAIN (mean(N>=2) - N=1) with its bootstrap CI, which cancels any cross-run grader-level
drift at N=1 (the raw draft should score identically across fusers).

Usage:  python3 scripts/selffusion_fuser_isolation_analyze.py
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from selffusion_analyze import (  # noqa: E402  (reuse the exact committed methodology)
    B, SEED, boot, full_curve_tasks, gain_ci, matrix, svg_compare,
)

HAIKU_DIR = ROOT / "artifacts" / "chair-isolation" / "results"
SONNET_ART = ROOT / "artifacts" / "haiku-selffusion"
DOCS = ROOT / "docs"


def load_matrix(files: list[Path]) -> dict:
    m: dict[str, dict[int, float]] = {}
    used = []
    for f in sorted(files):
        mm = matrix(json.loads(f.read_text()))
        if mm:
            m.update(mm)  # task ids disjoint across shards
            used.append(f.name)
    return m, used


def curve_with_ci(m: dict, tasks: list[str], rng: random.Random) -> dict:
    out = {"mean": {}, "ci_lo": {}, "ci_hi": {}}
    for n in range(1, 11):
        mm, a, b = boot([m[t][n] for t in tasks], rng)
        out["mean"][n], out["ci_lo"][n], out["ci_hi"][n] = mm, a, b
    return out


def main() -> None:
    rng = random.Random(SEED)
    Mh, hfiles = load_matrix(list(HAIKU_DIR.glob("*_result.json")))
    Ms, sfiles = load_matrix(list(SONNET_ART.glob("*sonnet*_result.json")))
    if not Mh:
        print(f"no Haiku-fuser results yet in {HAIKU_DIR} — run the shards first")
        return
    hf = full_curve_tasks(Mh)
    sf = full_curve_tasks(Ms)
    shared = [t for t in sf if t in hf]  # same drafts, full N=1..10 in BOTH fusers
    print(f"Haiku-fuser shards: {hfiles}")
    print(f"Sonnet-fuser files: {sfiles}")
    print(f"Haiku-fuser full-curve tasks: {len(hf)} | Sonnet-fuser: {len(sf)} | SHARED: {len(shared)}\n")
    if not shared:
        print("no shared full-curve tasks yet (need more shards graded)")
        return

    Sc = curve_with_ci(Ms, shared, rng)
    Hc = curve_with_ci(Mh, shared, rng)
    print(f"{'N':>2}  {'Sonnet fuser [95% CI]':>26}   {'Haiku fuser [95% CI]':>26}")
    for n in range(1, 11):
        print(f"{n:>2}  {Sc['mean'][n]:6.1f} [{Sc['ci_lo'][n]:5.1f},{Sc['ci_hi'][n]:5.1f}]"
              f"        {Hc['mean'][n]:6.1f} [{Hc['ci_lo'][n]:5.1f},{Hc['ci_hi'][n]:5.1f}]")

    sg = gain_ci(Ms, shared, rng)
    hg = gain_ci(Mh, shared, rng)
    def verdict(g): return "excludes 0 (significant)" if (g[1] > 0 or g[2] < 0) else "includes 0 (n.s.)"
    print(f"\nPAIRED GAIN  mean(N>=2) - N=1, on the {len(shared)} shared tasks:")
    print(f"  Sonnet fuser: {sg[0]:+.2f} [{sg[1]:+.2f}, {sg[2]:+.2f}]  {verdict(sg)}")
    print(f"  Haiku  fuser: {hg[0]:+.2f} [{hg[1]:+.2f}, {hg[2]:+.2f}]  {verdict(hg)}")
    print(f"  Δ (Sonnet - Haiku fuser gain): {sg[0]-hg[0]:+.2f}")

    DOCS.mkdir(exist_ok=True)
    out = DOCS / "draco-selffusion-fuser-isolation.svg"
    svg_compare(Sc, Hc, out, len(shared))
    print(f"\nwrote {out}  (Sonnet vs Haiku fuser, same Sonnet drafts, whisker = 95% bootstrap CI)")

    (HAIKU_DIR / "fuser_isolation_curves.json").write_text(json.dumps({
        "n_shared_tasks": len(shared),
        "sonnet_fuser": {"mean_by_N": {str(n): round(Sc["mean"][n], 2) for n in range(1, 11)},
                         "ci_lo": {str(n): round(Sc["ci_lo"][n], 2) for n in range(1, 11)},
                         "ci_hi": {str(n): round(Sc["ci_hi"][n], 2) for n in range(1, 11)},
                         "gain": round(sg[0], 2), "gain_ci": [round(sg[1], 2), round(sg[2], 2)]},
        "haiku_fuser": {"mean_by_N": {str(n): round(Hc["mean"][n], 2) for n in range(1, 11)},
                        "ci_lo": {str(n): round(Hc["ci_lo"][n], 2) for n in range(1, 11)},
                        "ci_hi": {str(n): round(Hc["ci_hi"][n], 2) for n in range(1, 11)},
                        "gain": round(hg[0], 2), "gain_ci": [round(hg[1], 2), round(hg[2], 2)]},
    }, indent=1))
    print(f"wrote {HAIKU_DIR / 'fuser_isolation_curves.json'}")


if __name__ == "__main__":
    main()
