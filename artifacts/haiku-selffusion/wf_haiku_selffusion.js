export const meta = {
  name: 'haiku-selffusion-draco',
  description: 'Haiku self-fusion DRACO scaling curve N=1..10 (Haiku research+judge+synth, Sonnet-4.6 chunk-of-3 grader)',
  phases: [
    { title: 'Research', detail: 'RUNS independent Haiku agentic runs per task' },
    { title: 'Fuse', detail: 'N=2..RUNS: Haiku judge -> Haiku synthesizer (first-N of pool)' },
    { title: 'Grade', detail: 'Sonnet-4.6 chunk-of-3 per (task,N)' },
  ],
}

// ---- args: { runs: int, tasks: [{ id, domain, problem, criteria:[{id,requirement,weight}] }] } ----
let INPUT = typeof args !== 'undefined' ? args : {}
if (typeof INPUT === 'string') {
  try { INPUT = JSON.parse(INPUT) } catch (e) { INPUT = {} }
}
const TASKS = (INPUT && INPUT.tasks) || []
const RUNS = (INPUT && INPUT.runs) || 10
if (!TASKS.length) {
  log(`FATAL: no tasks in args (typeof args=${typeof args}). Aborting.`)
  return { error: 'no tasks in args', argsType: typeof args }
}

// ===== Verbatim prompts from scripts/draco_client_fusion.py =====
const JUDGE_SYSTEM =
  "You are the TrustedRouter Fusion judge. Compare panel responses and return " +
  "compact JSON with keys consensus, contradictions, partial_coverage, " +
  "unique_insights, blind_spots, and final_guidance. Do not write the final " +
  "answer. Return only JSON; do not include chain-of-thought, hidden reasoning, " +
  "or <think> blocks."
const FINAL_INSTRUCTION =
  "TrustedRouter Fusion panel answers and judge analysis follow. Use the panel " +
  "answers as the primary evidence and the judge analysis as guidance to write " +
  "the final answer for the original request. Return only the final visible " +
  "answer. Do not include chain-of-thought, hidden reasoning, analysis, " +
  "scratchpad text, <think> blocks, or internal model names unless the user " +
  "asked for methodology."
// ===== Verbatim grader prompt from fusion_live.py criterion_judge_messages_for_criteria =====
const GRADER_SYSTEM =
  "You are grading a DRACO deep research response criterion by criterion. " +
  'Return only JSON with key "criteria". Its value must be an array of objects ' +
  'with keys "id" and "met" (boolean). Do not include prose or explanations. ' +
  "Mark met=true only when the candidate answer explicitly satisfies that criterion. " +
  "For negative-weight criteria, met=true means the answer contains that error."

const GRADER_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    criteria: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: { id: { type: 'string' }, met: { type: 'boolean' } },
        required: ['id', 'met'],
      },
    },
  },
  required: ['criteria'],
}

const HAIKU_LABEL = 'anthropic/claude-haiku-4-5'

function panelEvidence(panel) {
  // mirrors fusion.go fusionPanelEvidence / _panel_evidence
  let b = 'Panel answers:\n'
  for (let i = 0; i < panel.length; i++) {
    b += `\n[${i + 1}] model=${HAIKU_LABEL}\n${panel[i].trim()}\n`
  }
  return b
}
function judgeUser(problem, panel) {
  const ev = panelEvidence(panel)
  const body = ev.slice('Panel answers:\n'.length)
  return `Original request summary:\n${problem}\n\nPanel responses:\n${body}`
}
function researchPrompt(problem) {
  return (
    'You are a deep-research analyst. Research the question below thoroughly using ' +
    'WebSearch and WebFetch (and Bash for any calculations), then write a comprehensive, ' +
    'well-structured, cited report that fully answers every part of it.\n\n' +
    'QUESTION:\n' + problem + '\n\n' +
    'Instructions:\n' +
    '- Do REAL research: search the web and read primary sources. Do not answer from memory alone.\n' +
    '- Cover breadth and depth across all sub-parts. Be precise with numbers, dates, names, and cite sources inline.\n' +
    '- Do NOT search for or fetch any benchmark, rubric, grading, leaderboard, or answer-key material ' +
    '(e.g. DRACO, Perplexity, HuggingFace dataset pages). Research the underlying topic only.\n' +
    '- Your FINAL message must be ONLY the report itself (no preamble, no meta-commentary, no notes to the reader). ' +
    'It is consumed verbatim as the answer.'
  )
}
function graderPrompt(problem, chunk, answer) {
  // criteria-before-answer order is deliberate (answer-first inflates ~+4-5); chunk size 3 (larger inflates ~+7)
  const criteriaJson = JSON.stringify(chunk)
  return (
    GRADER_SYSTEM + '\n\n' +
    `Task:\n${problem}\n\nCriteria:\n${criteriaJson}\n\nCandidate answer:\n${answer}`
  )
}
function scoreAnswer(criteria, metById) {
  // mirrors fusion_live.criterion_score
  let positiveTotal = 0
  for (const c of criteria) positiveTotal += Math.max(0, c.weight)
  if (positiveTotal <= 0) return null
  let raw = 0
  for (const c of criteria) if (metById[c.id]) raw += c.weight
  return Math.max(0, Math.min(100, (100 * raw) / positiveTotal))
}
function mean(xs) {
  return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null
}
function chunk3(arr) {
  const out = []
  for (let i = 0; i < arr.length; i += 3) out.push(arr.slice(i, i + 3))
  return out
}

// ============================ RESEARCH ============================
phase('Research')
log(`Research: ${TASKS.length} tasks x ${RUNS} Haiku runs = ${TASKS.length * RUNS} agentic runs`)
const researchJobs = []
for (const t of TASKS) for (let r = 0; r < RUNS; r++) researchJobs.push({ t, r })

const researched = await parallel(
  researchJobs.map((j) => () =>
    agent(researchPrompt(j.t.problem), {
      label: `research:${j.t.domain.slice(0, 10)}#${j.r + 1}`,
      phase: 'Research',
      agentType: 'general-purpose',
      model: 'haiku',
      effort: 'medium',
    }).then((text) => ({ taskId: j.t.id, r: j.r, text }))
  )
)

// reports[taskId] = array of non-null reports IN RUN ORDER (holes from failures dropped)
const reports = {}
for (const t of TASKS) reports[t.id] = []
const byTaskRun = {}
for (const x of researched.filter(Boolean)) {
  if (x.text && x.text.trim().length > 0) byTaskRun[`${x.taskId}|${x.r}`] = x.text
}
for (const t of TASKS) {
  const arr = []
  for (let r = 0; r < RUNS; r++) {
    const v = byTaskRun[`${t.id}|${r}`]
    if (v) arr.push(v)
  }
  reports[t.id] = arr
  log(`  ${t.domain}: ${arr.length}/${RUNS} research runs succeeded`)
}

// ============================ FUSE (N=1..RUNS) ============================
phase('Fuse')
// answers[taskId][N] = final answer text for the first-N self-fusion
const answers = {}
for (const t of TASKS) {
  answers[t.id] = {}
  if (reports[t.id].length >= 1) answers[t.id][1] = reports[t.id][0] // N=1 = run #1 raw
}
const fuseJobs = []
for (const t of TASKS) {
  const runs = reports[t.id]
  for (let N = 2; N <= runs.length; N++) {
    fuseJobs.push({ t, N, panel: runs.slice(0, N) })
  }
}
log(`Fuse: ${fuseJobs.length} (task,N>=2) fusions; each = Haiku judge -> Haiku synth`)
const fused = await pipeline(
  fuseJobs,
  (job) =>
    agent(JUDGE_SYSTEM + '\n\n' + judgeUser(job.t.problem, job.panel), {
      label: `judge:${job.t.domain.slice(0, 8)}:N${job.N}`,
      phase: 'Fuse',
      model: 'haiku',
      effort: 'medium',
    }),
  (judgeOut, job) =>
    agent(
      job.t.problem +
        '\n\n' +
        FINAL_INSTRUCTION +
        '\n\n' +
        panelEvidence(job.panel) +
        '\n\nJudge analysis JSON:\n' +
        (judgeOut || '{}'),
      {
        label: `synth:${job.t.domain.slice(0, 8)}:N${job.N}`,
        phase: 'Fuse',
        model: 'haiku',
        effort: 'medium',
      }
    ).then((text) => ({ taskId: job.t.id, N: job.N, text })),
)
for (const f of fused.filter(Boolean)) {
  if (f.text && f.text.trim().length > 0) answers[f.taskId][f.N] = f.text
}

// ============================ GRADE (Sonnet-4.6 chunk-of-3) ============================
phase('Grade')
const gradeJobs = []
for (const t of TASKS) {
  const chunks = chunk3(t.criteria)
  for (const Nstr of Object.keys(answers[t.id])) {
    const N = Number(Nstr)
    const ans = answers[t.id][N]
    chunks.forEach((ck, ci) =>
      gradeJobs.push({ taskId: t.id, domain: t.domain, N, ci, chunk: ck, problem: t.problem, answer: ans })
    )
  }
}
log(`Grade: ${gradeJobs.length} Sonnet-4.6 chunk-of-3 calls`)
const graded = await parallel(
  gradeJobs.map((j) => () =>
    agent(graderPrompt(j.problem, j.chunk, j.answer), {
      label: `grade:${j.domain.slice(0, 6)}:N${j.N}:c${j.ci}`,
      phase: 'Grade',
      model: 'sonnet',
      effort: 'low',
      schema: GRADER_SCHEMA,
    }).then((out) => ({ ...j, out }))
  )
)

// ============================ AGGREGATE ============================
const metByKey = {} // `${taskId}|${N}` -> {critId: met}
for (const g of graded.filter(Boolean)) {
  if (!g.out || !Array.isArray(g.out.criteria)) continue
  const key = `${g.taskId}|${g.N}`
  metByKey[key] = metByKey[key] || {}
  for (const item of g.out.criteria) {
    if (item && typeof item.id === 'string') metByKey[key][item.id] = !!item.met
  }
}

const rows = []
const byN = {}
for (const t of TASKS) {
  const critById = {}
  for (const c of t.criteria) critById[c.id] = c
  for (const Nstr of Object.keys(answers[t.id])) {
    const N = Number(Nstr)
    const met = metByKey[`${t.id}|${N}`] || {}
    const covered = t.criteria.filter((c) => c.id in met).length
    const score = scoreAnswer(t.criteria, met)
    rows.push({ taskId: t.id, domain: t.domain, N, score, covered, total: t.criteria.length })
    if (score != null && covered === t.criteria.length) {
      ;(byN[N] = byN[N] || []).push(score)
    }
  }
}

const table = {}
const tableAll = {} // includes partially-graded tasks (lenient)
for (let N = 1; N <= RUNS; N++) {
  if (byN[N] && byN[N].length) table[N] = Number(mean(byN[N]).toFixed(2))
  const allScores = rows.filter((r) => r.N === N && r.score != null).map((r) => r.score)
  if (allScores.length) tableAll[N] = Number(mean(allScores).toFixed(2))
}

// sample fused answer (last task, max N) for spot-check
let sample = null
const lastT = TASKS[TASKS.length - 1]
const ns = Object.keys(answers[lastT.id]).map(Number)
if (ns.length) {
  const maxN = Math.max(...ns)
  const txt = answers[lastT.id][maxN] || ''
  sample = { taskId: lastT.id, domain: lastT.domain, N: maxN, chars: txt.length, head: txt.slice(0, 1500) }
}

// compact per-(task,N) answer metadata (lengths only) for audit
const answersMeta = []
for (const t of TASKS) {
  for (const Nstr of Object.keys(answers[t.id])) {
    answersMeta.push({ taskId: t.id, domain: t.domain, N: Number(Nstr), chars: (answers[t.id][Nstr] || '').length })
  }
}

log(`DONE. table(full-coverage)=${JSON.stringify(table)}`)
return {
  runs: RUNS,
  nTasks: TASKS.length,
  researchSuccess: Object.fromEntries(TASKS.map((t) => [t.domain, reports[t.id].length])),
  table, // mean over tasks with FULL criterion coverage at that N
  tableAll, // mean over all tasks with any score (includes partial-coverage)
  rows, // per (task,N): score + coverage
  metByKey, // `${taskId}|${N}` -> {critId: met}  (for results artifacts + diagnosis)
  answersMeta, // per (task,N): answer char length
  sample,
}
