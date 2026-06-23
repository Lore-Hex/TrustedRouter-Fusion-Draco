![Self-fusion pays the smart model more — Sonnet +8.0 (significant), Haiku +2.6](docs/og-blog-selffusion.svg)

# Self-fusion pays the smart model more

Last month OpenRouter showed a cheap trick: run a small model ten times on a hard research question, have it read its own ten answers, and write one combined answer that beats any single run. MiniMax-M3 went from 66.2 to 69.4 on the DRACO benchmark doing it. I wanted to know whether the trick survives all the way down to the cheapest models, so I ran it with Claude Haiku 4.5 and then Claude Sonnet 4.6, both wired through Claude Code subagents — the model does its own web research, reads its own runs, writes the fusion — graded by Sonnet 4.6.

Fusing helps the smart model a lot and the cheap one a little. Sonnet self-fusion climbs from 66 solo to about 74, a gain of eight points that holds up as significant (95% interval +4.6 to +11.2, across 23 tasks). Haiku self-fusion, across 44 tasks, moves from 55 to about 58, a gain of 2.6 points that looks real but doesn't quite clear the bar (−0.3 to +5.4, about 96% of the bootstrap positive). Same recipe, same grader, and the payoff tracks how smart the model doing the fusing is.

I almost published a wrong version of this. My first cut was eight tasks, and on those eight Haiku self-fusion looked like it actively hurt, down three points, and the story wrote itself: cheap fusion backfires. Eighteen more tasks killed that story: that batch gained +3.7, the merged number went to +1.5 at 26 tasks, then +2.6 by 44. The dramatic backfire was small-sample luck. Read the directions, not the decimals — the decimals moved every time I added tasks.

You can still watch a weak fuser do the dumb thing on one task. On a needle-in-a-haystack question, where the score hangs on a single buried fact, a lone Haiku run found the needle and scored 87. Fusing ten runs dropped it to 63: nine of the ten runs had missed the needle, and reading all ten, Haiku wrote the consensus and sided with the nine. Sonnet kept its needle on the tasks it covered. That is the mechanism behind the whole effect. Fusing is a vote, and a vote only helps if the model counting it can pick the one right answer out of a crowd of wrong ones.

Fusing is a different skill from researching, and it's the one that scales with raw model strength. The ten runs are raw material. The score comes from the judgment that finds the good answer in the pile and keeps it, and a bigger model has more of that judgment, so it gets more out of the same ten runs. Stacking more cheap runs doesn't manufacture the judgment to combine them.

| self-fusion | solo | fused | gain | grader |
|---|---:|---:|---:|---|
| MiniMax-M3 *(OpenRouter)* | 66.2 | 69.4 | +3.2 | Gemini-3.1-Pro |
| Claude Sonnet 4.6 *(23 tasks)* | 66 | ~74 | **+8.0** *(significant)* | Sonnet-4.6 |
| Claude Haiku 4.5 *(44 tasks)* | 55 | ~58 | +2.6 *(n.s.)* | Sonnet-4.6 |

![Sonnet self-fusion climbs; Haiku barely moves, on the same four tasks](docs/draco-selffusion-sonnet-vs-haiku.svg)

Sonnet's gain clears significance — 23 tasks, interval +4.6 to +11.2, well clear of zero. Haiku's +2.6 over 44 tasks doesn't quite, though about 96% of the bootstrap is positive, and on the 23 tasks the two models share Haiku gains under a point while Sonnet gains eight. One run-ordering, so the within-N wiggles are noise and the gain is the signal. I'd still want the full eighty and a second ordering to pin Haiku down, but the Sonnet result is solid and the gap between the two is large.

It's worth saying why I'm at forty-four tasks and not eighty. Doing this through subagents hits hard platform limits. Grading is about a thousand judge calls per pass and gets rate-limited into the ground, so I graded the whole rubric in one call instead of the calibrated three-criteria-at-a-time. And the research is so token-hungry that one eighteen-task batch exhausts a daily quota window before it finishes grading. The cheap, easy version of this experiment is neither.

A caveat on the scores. I graded with Sonnet 4.6 standing in for OpenRouter's Gemini-3.1-Pro grader, after checking that Sonnet tracked Gemini on OpenRouter's own DRACO answers, 0.92 correlation with no average bias. These are different tasks, and a grader that's unbiased on one set can run hot on another. Grading the whole rubric in one call adds about seven points on its own, and Opus as a grader earlier came in about five points high. So the absolute numbers are probably inflated and don't line up with the Gemini-graded M3 figures. Each model measured against itself is the comparison that survives.

This is probably why fusion has been a footnote until now. The idea is old — sample a model a few times and have something stitch the samples together — but the stitcher was always the weak link. A model that can't tell its good run from its bad ones blurs them into an average, and the gain washes out, which is what Haiku does here. You need a synthesizer good enough to read ten messy research reports and walk out holding the one correct claim, and that is recent. Sonnet 4.6 is the first cheap model I've watched clear that bar. The fusion recipe didn't get better this year; the models finally got good enough to run it.

OpenRouter's headline holds: a committee of cheap models can reach a frontier answer. The lift from fusing the committee comes from the chair, and the chair has to be smart enough to keep the best answer in the room. Make the chair cheap and the runs pile up without adding up.

---

*Harness, per-task scores, bootstrap intervals, and run traces are in [TrustedRouter-Fusion-Draco](https://github.com/Lore-Hex/TrustedRouter-Fusion-Draco): `docs/FINDINGS.md` §8, `results/rejudge-selffusion-*.jsonl`, workflow scripts under `artifacts/haiku-selffusion/`. This is a pilot — forty-four Haiku tasks, twenty-three Sonnet, one run-ordering. The numbers will move; the shape is what to watch.*
