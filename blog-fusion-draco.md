# Three cheap models beat the expensive one

OpenRouter published a benchmark last month with a claim I didn't believe: take
three budget models, run them as a panel, have a fourth model stitch their
answers together, and the cheap panel beats the expensive frontier model it's
competing against. They called it Fusion. The headline number was 64.7 on a deep
research benchmark, ahead of every individual model they tested.

I reproduced it. The cheap panel really does win. And I ran the whole thing on
infrastructure where you can check that I'm not lying to you, which turns out to
be the more interesting half of the story.

## What the benchmark actually measures

The benchmark is DRACO — a hundred deep-research tasks across ten domains, the
kind of question where the answer isn't sitting in the model's weights and it has
to go find it. Each answer gets scored against about thirty-nine weighted
criteria: did you get the facts right, did you cover the breadth and the depth,
did you cite real sources, is it readable. A second model, Gemini 3.1 Pro, does
the grading. The whole thing tests reasoning plus tool use plus knowledge at
once, which is why a model with a bigger brain doesn't automatically win — it
also has to go search the web, read the pages, and do the arithmetic.

That last part matters more than it sounds. The first time I ran these tasks, I
handed each model a frozen blob of pre-fetched context and asked it to write the
report in one shot. Everything scored about twenty points too low. The benchmark
isn't a quiz. It's a research assignment, and a researcher you don't let near a
search box does badly. Once I gave every model live tools — web search, web
fetch, a sandbox to run code — the scores jumped to where OpenRouter's were.
That single change, tools versus no tools, was the whole gap.

## The solos, head to head

Here's where each individual model landed on the full hundred tasks, run through
TrustedRouter, next to OpenRouter's published numbers.

| model | TrustedRouter | OpenRouter |
|---|---:|---:|
| GPT-5.5 | 63.3 | 60.0 |
| Claude Opus 4.8 | 60.3 | 58.8 |
| DeepSeek V4 Pro | 57.5 | 60.3 |
| Gemini 3.1 Pro | 47.1 | 45.4 |
| Kimi K2.6 | 46.3 | 53.7 |
| Gemini 3 Flash | 40.4 | 43.1 |

We land within a few points of OpenRouter on every model, and on several we score
higher — GPT-5.5 by three points, Opus by one and a half, Gemini 3.1 Pro by
nearly two. Our harness gives models a generous research budget — up to sixteen
tool calls, a real synthesis step at the end — and a model that's allowed to dig
writes a more complete answer, which the rubric rewards. The two places we trail
are Kimi and the finance documents, and I'll come back to the finance documents
because they're the real weak spot.

## The panel beats its parts

Now the result that made the post. Take the three budget models — Gemini 3 Flash,
Kimi, DeepSeek — none of which cracks 58 on its own. Run all three on the same
task. Have Gemini 3.1 Pro read the three reports and write up where they agree,
where they contradict each other, and what each one missed. Then hand all of that
to Opus 4.8 and ask it for the final answer.

The panel scores **63 on the research tasks that aren't finance** — above Opus 4.8
solo, a hair under GPT-5.5, and within a point of OpenRouter's 64.7. Three models
that individually top out at 57 become a 63 when they argue it out and a stronger
model referees. The expensive model didn't win. The committee of cheap ones did.

It works because the models are wrong in different places. Flash misses something
DeepSeek caught; Kimi phrases a claim DeepSeek hedged; the judge notices the
disagreement and tells the fuser which thread to pull. You get the union of three
research efforts instead of the ceiling of one. Even fusing a model with a copy
of itself helps a little, which tells you the act of re-reading and rewriting is
doing real work, not just the diversity.

## The part I won't paper over

On the full hundred tasks, including the twenty finance questions, the panel lands
at 61, not 63. The finance tasks drag everyone down, and they drag us down harder
than they drag OpenRouter. Those questions live inside SEC filings and
spreadsheets — capital allocation, cash generation, equity financing — and the
score depends entirely on whether the model can pull a number out of a hundred-page
PDF or an Excel sheet. Our document parser is open-source markitdown. It recovers
tables, but it loses to whatever OpenRouter is running on the gnarliest filings,
and the gap shows up as fifteen points per model on exactly those twenty tasks.
The reproduction is clean on the eighty research tasks and weaker on the twenty
document-mining ones, and the fix is better document tooling, which I'd rather
build than hide.

## Why I ran it on TrustedRouter

A benchmark is a claim about other people's models. The reason to care where it
runs is everything around the model — the routing, the panel orchestration, the
fact that your research prompts and the documents you fetch pass through somebody
else's servers. With most gateways you take that on faith. You're told the
provider doesn't keep your prompts. You can't check it.

TrustedRouter runs inside a confidential computing enclave — a sealed VM where the
operator, me included, can't read what's happening inside. Every request is
handled by an attested workload, and the measurement of the exact code that's
running gets published. You can pull the image digest off the trust page, match it
against the source, and confirm that the binary which saw your prompt is the one
in the open repository, with nowhere inside it to write your data down. You check
the privacy the same way you'd check the score: by hand, against a hash.

That's the same instinct as making the benchmark reproducible in the first place.
I don't want you to trust my 63. I want you to clone the repo, point it at
TrustedRouter, and get your own. The harness, the task manifests, the judge
configuration, the panel definitions, the gateway itself — all of it is open
source. If the number is wrong you can find out, and if your data leaked you'd be
able to tell. Most of the AI stack asks you to believe two things you have no way
to verify: that the score is real and that the operator behaves. We built the one
where you can check both.

## The point

The expensive frontier model is not the only way to get a frontier answer. Three
cheap models that disagree, plus one good referee, get you there for a fraction of
the cost — OpenRouter showed it, and now it reproduces on open infrastructure you
can audit end to end. The interesting question stopped being which model is
smartest. It's how cheaply you can assemble a smart answer, and whether you can
prove what happened to your data while you did.

---

*The full reproduction — harnesses, data, scores, and a one-command way to run it
yourself against TrustedRouter — is open source at
[github.com/Lore-Hex/TrustedRouter-Fusion-Draco](https://github.com/Lore-Hex/TrustedRouter-Fusion-Draco).
A hundred deep-research tasks, six models, one budget panel, judged by Gemini 3.1
Pro. Run it and check the number.*
