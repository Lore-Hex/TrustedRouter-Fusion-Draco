import json, os, glob, sys

CONC = int(sys.argv[1]) if len(sys.argv) > 1 else 4
BATCH = int(sys.argv[2]) if len(sys.argv) > 2 else 8

CJ="/tmp/claude/chunkjobs3"
all_files=sorted(os.path.basename(f) for f in glob.glob(os.path.join(CJ,"*.json"))
                 if not os.path.basename(f).startswith("_"))
graded=set()
if os.path.exists("/tmp/claude/sonnet_results.jsonl"):
    for line in open("/tmp/claude/sonnet_results.jsonl"):
        line=line.strip()
        if not line: continue
        r=json.loads(line); graded.add(f"{r['idx']}_{r['ci']}.json")
remaining=[f for f in all_files if f not in graded]
def key(fn):
    idx,ci=fn.replace(".json","").split("_"); return (int(ci),int(idx))
remaining.sort(key=key)
batches=[remaining[i:i+BATCH] for i in range(0,len(remaining),BATCH)]
print(f"all={len(all_files)} graded={len(graded)} remaining={len(remaining)} -> {len(batches)} agents (batch={BATCH}, conc={CONC})")

TEMPLATE = r'''export const meta = {
  name: 'draco-grade-gentle',
  description: 'Grade remaining DRACO member-solo chunks at LOW concurrency (beat the session throttle), batched chunk-of-3, Sonnet/effort=high',
  phases: [{ title: 'Grade', detail: 'concurrency-capped Sonnet grading' }],
};

const DIR = '/tmp/claude/chunkjobs3';
const batches = __BATCHES__;
const CONC = __CONC__;

const SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: { results: { type: 'array', items: {
    type: 'object', additionalProperties: false,
    properties: { file: { type: 'string' },
      judgments: { type: 'array', items: { type: 'object', additionalProperties: false,
        properties: { id: { type: 'string' }, met: { type: 'boolean' } }, required: ['id', 'met'] } } },
    required: ['file', 'judgments'] } } },
  required: ['results'],
};

const SYS =
  'You are grading DRACO deep research responses criterion by criterion. ' +
  'Mark met=true only when the candidate answer EXPLICITLY satisfies that criterion. ' +
  'For negative-weight criteria, met=true means the answer contains that error. ' +
  'Judge strictly and literally; do not give credit for partial, implied, or merely plausible satisfaction.';

function P(files) {
  const list = files.map((f) => DIR + '/' + f).join('\n  ');
  return (
    SYS +
    '\n\nYou will grade ' + files.length + ' INDEPENDENT gradings. Read each of these JSON files:\n  ' +
    list +
    '\n\nEach file has "problem", "criteria" (exactly three objects with id/requirement/weight), and "answer". ' +
    'For EACH file, grade its answer against ITS OWN three criteria, in order, applying the rule above. ' +
    'Treat every file as a completely separate, independent task — one file must not influence another.\n\n' +
    'Return one result object per file: its basename (e.g. "' + files[0] + '") as "file", and a judgment ' +
    '{id, met} for each of that file\'s three criteria (exact id strings, all three).'
  );
}

async function gradeOne(files) {
  const r = await agent(P(files), { schema: SCHEMA, model: 'sonnet', effort: 'high', phase: 'Grade', label: files.length + ' files' });
  if (!r || !Array.isArray(r.results)) return [];
  const rows = [];
  for (const res of r.results) {
    if (!res || typeof res.file !== 'string' || !Array.isArray(res.judgments)) continue;
    const base = res.file.split('/').pop();
    const u = base.replace('.json', '').split('_');
    if (u.length !== 2) continue;
    const m = res.judgments.filter((j) => j && j.met === true).map((j) => j.id);
    rows.push({ idx: parseInt(u[0], 10), ci: parseInt(u[1], 10), m, n: res.judgments.length });
  }
  return rows;
}

// manual concurrency pool: at most CONC agents in flight -> low sustained request rate
const out = new Array(batches.length);
let next = 0;
let done = 0;
async function worker() {
  for (;;) {
    const i = next++;
    if (i >= batches.length) return;
    try { out[i] = await gradeOne(batches[i]); } catch (e) { out[i] = []; }
    done++;
    if (done % 25 === 0) log('progress: ' + done + '/' + batches.length + ' batches');
  }
}
log('grading ' + batches.length + ' batches at concurrency ' + CONC);
await Promise.all(Array.from({ length: Math.min(CONC, batches.length) }, () => worker()));
const flat = out.filter(Boolean).flat();
log('graded ' + flat.length + ' chunk rows');
return flat;
'''

js = TEMPLATE.replace("__BATCHES__", json.dumps(batches)).replace("__CONC__", str(CONC))
open("/tmp/claude/grade_gentle.js","w").write(js)
print(f"wrote grade_gentle.js ({len(js)} bytes)")
