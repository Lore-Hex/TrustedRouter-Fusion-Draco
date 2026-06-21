export const meta = {
  name: 'draco-grade-sliceN',
  description: 'Grade DRACO member-solo chunks (isolated chunk-of-3, Sonnet, effort=high) for the per-task correlation matrix — slice N',
  phases: [{ title: 'Grade', detail: 'one Sonnet subagent per criterion-triplet' }],
};

const DIR = '/tmp/claude/chunkjobs3';
const files = SLICE_FILENAMES; // injected by make_workflows.py (array of "<idx>_<ci>.json")

const SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    judgments: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: { id: { type: 'string' }, met: { type: 'boolean' } },
        required: ['id', 'met'],
      },
    },
  },
  required: ['judgments'],
};

const SYS =
  'You are grading a DRACO deep research response criterion by criterion. ' +
  'Mark met=true only when the candidate answer EXPLICITLY satisfies that criterion. ' +
  'For negative-weight criteria, met=true means the answer contains that error. ' +
  'Judge strictly and literally; do not give credit for partial, implied, or merely plausible satisfaction.';

function P(fn) {
  return (
    SYS +
    '\n\nRead the JSON file at ' + DIR + '/' + fn + '\n' +
    'It contains:\n' +
    '- "problem": the research task\n' +
    '- "criteria": an array of exactly three criterion objects, each with "id", "requirement", and "weight"\n' +
    '- "answer": the candidate answer to grade\n\n' +
    'Grade the candidate answer against EACH of the three criteria, considering them in the order given, ' +
    'applying the rule above. For each criterion decide met=true or met=false.\n\n' +
    'Return exactly one judgment object per criterion, using the criterion\'s exact "id" string. Include all three.'
  );
}

log('grading ' + files.length + ' chunks (slice N)');
const results = await parallel(
  files.map((fn) => async () => {
    const r = await agent(P(fn), { schema: SCHEMA, model: 'sonnet', effort: 'high', phase: 'Grade', label: fn });
    if (!r || !Array.isArray(r.judgments)) return null;
    const u = fn.replace('.json', '').split('_');
    const m = r.judgments.filter((j) => j && j.met === true).map((j) => j.id);
    return { idx: parseInt(u[0], 10), ci: parseInt(u[1], 10), m, n: r.judgments.length };
  })
);
const ok = results.filter(Boolean);
log('graded ' + ok.length + '/' + files.length + ' (slice N)');
return ok;
