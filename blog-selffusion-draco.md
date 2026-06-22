![Self-fusion pays the smart model more — Sonnet +4.4, Haiku +1.5 and not significant](docs/og-blog-selffusion.svg)

# Self-fusion pays the smart model more

Last month OpenRouter showed a cheap trick: run a small model ten times on a hard research question, have it read its own ten answers, and write one combined answer that beats any single run. MiniMax-M3 went from 66.2 to 69.4 on the DRACO benchmark doing it. I wanted to know whether the trick survives all the way down to the cheapest models, so I ran it with Claude Haiku 4.5 and then Claude Sonnet 4.6, both wired through Claude Code subagents — the model does its own web research, reads its own runs, writes the fusion — graded by Sonnet 4.6.

Fusing helps the smart model and barely touches the cheap one. Sonnet self-fusion climbs from 73 solo to about 79 by the fourth run and holds there, a gain of roughly four points. Haiku self-fusion, across 26 tasks, moves from 60.5 to about 62, a gain of 1.5 points I can't tell apart from zero (95% interval −2.3 to +5.0). Same recipe, same grader, and the payoff tracks how smart the model doing the fusing is.

I almost published a wrong version of this. My first cut was eight tasks, and on those eight Haiku self-fusion looked like it actively hurt, down three points, and the story wrote itself: cheap fusion backfires. Eighteen more tasks killed that story. On the new batch Haiku gained +3.7, and the merged 26-task number settled at +1.5. The dramatic backfire was small-sample luck. Read the directions, not the decimals: the decimals here aren't settled.

You can still watch a weak fuser do the dumb thing on one task. On a needle-in-a-haystack question, where the score hangs on a single buried fact, a lone Haiku run found the needle and scored 87. Fusing ten runs dropped it to 63: nine of the ten runs had missed the needle, and reading all ten, Haiku wrote the consensus and sided with the nine. Sonnet kept its needle on the tasks it covered. That is the mechanism behind the whole effect. Fusing is a vote, and a vote only helps if the model counting it can pick the one right answer out of a crowd of wrong ones.

Fusing is a different skill from researching, and it's the one that scales with raw model strength. The ten runs are raw material. The score comes from the judgment that finds the good answer in the pile and keeps it, and a bigger model has more of that judgment, so it gets more out of the same ten runs. Stacking more cheap runs doesn't manufacture the judgment to combine them.

| self-fusion | solo | fused | gain | grader |
|---|---:|---:|---:|---|
| MiniMax-M3 *(OpenRouter)* | 66.2 | 69.4 | +3.2 | Gemini-3.1-Pro |
| Claude Sonnet 4.6 *(4 tasks)* | 73 | ~79 | +4.4 | Sonnet-4.6 |
| Claude Haiku 4.5 *(26 tasks)* | 60.5 | ~62 | +1.5 *(n.s.)* | Sonnet-4.6 |

![Sonnet self-fusion climbs; Haiku barely moves, on the same four tasks](docs/draco-selffusion-sonnet-vs-haiku.svg)

None of these gains clears statistical significance at the sizes I ran: four tasks for Sonnet, twenty-six for Haiku, one ordering of the runs. The Sonnet-over-Haiku gap, about six points on the four tasks they share, is suggestive and unproven. You'd want roughly thirty tasks per model and a couple of run-orderings to settle it. What I'd stand on is the direction and the mechanism, because the mechanism you can see on a single task.

It's worth saying why I ran twenty-six tasks and not eighty. Doing this through subagents hits hard platform limits. Grading is about a thousand judge calls per pass and gets rate-limited into the ground, so I graded the whole rubric in one call instead of the calibrated three-criteria-at-a-time. And the research is so token-hungry that one eighteen-task batch exhausts a daily quota window before it finishes grading. The cheap, easy version of this experiment is neither.

A caveat on the scores. I graded with Sonnet 4.6 standing in for OpenRouter's Gemini-3.1-Pro grader, after checking that Sonnet tracked Gemini on OpenRouter's own DRACO answers, 0.92 correlation with no average bias. These are different tasks, and a grader that's unbiased on one set can run hot on another. Grading the whole rubric in one call adds about seven points on its own, and Opus as a grader earlier came in about five points high. So the absolute numbers are probably inflated and don't line up with the Gemini-graded M3 figures. Each model measured against itself is the comparison that survives.

OpenRouter's headline holds: a committee of cheap models can reach a frontier answer. The lift from fusing the committee comes from the chair, and the chair has to be smart enough to keep the best answer in the room. Make the chair cheap and the runs pile up without adding up.

---

*Harness, per-task scores, bootstrap intervals, and run traces are in [TrustedRouter-Fusion-Draco](https://github.com/Lore-Hex/TrustedRouter-Fusion-Draco): `docs/FINDINGS.md` §8, `results/rejudge-selffusion-*.jsonl`, workflow scripts under `artifacts/haiku-selffusion/`. This is a pilot — twenty-six Haiku tasks, four Sonnet, one run-ordering. The numbers will move; the shape is what to watch.*
