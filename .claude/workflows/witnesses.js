export const meta = {
  name: 'witnesses',
  description: 'Over a list of tasks, generate witness implementations, witness property sets (precondition + generator) and one shared set of properties to test, then cross-check the witnesses. Each subagent handles a batch of tasks one after another.',
  whenToUse: 'Producing candidate implementations and PBT property sets for many specs cheaply, where each subagent follows its own custom instructions (e.g. "write a brute-force implementation")',
  phases: [
    { title: 'Generate', detail: 'per batch of tasks: one subagent per implementer role, per property-set role and one for the properties, each working through the batch sequentially' },
    { title: 'Cross-check', detail: 'per batch: one subagent runs every implementation on every property set (in the sandbox) and reports problems and disagreements' },
  ],
}

// ---------------------------------------------------------------------------
// args (all JSON):
// {
//   tasks: [{ id, prompt, entry_point }],          // prompt = function signature + docstring, as in HumanEval
//   batchSize?: number,                            // tasks handled one-after-another by each subagent (default 5)
//   implementers?:  [{ name, instructions }],      // one subagent per batch each; replaces the default roster
//   propertySets?:  [{ name, instructions }],      // same, for property sets
//   properties?: false | { instructions?: string }, // the properties author (one subagent per batch, writes properties from the spec alone); false disables it
//   model?: string, effort?: string,               // default model / reasoning effort for every subagent (e.g. 'haiku', 'low'); omit to inherit the session's
//   models?: { implementers?, propertySets?, properties?, crossCheck? }   // per-stage model override; a role's own `model` / `effort` field beats this
//   efforts?: { same keys }                        // per-stage effort override
//   sharedInstructions?: string,                   // appended to every subagent's prompt
//   crossCheck?: boolean,                          // default true
//   samples?: number,                              // inputs drawn per property set in cross-check (default 200)
//   sandboxCmd?: string                            // how agents run code (default: the Docker sandbox runner)
// }
// A role may also be given as a bare string, used as both its name and its instructions.
//
// Agent count = ceil(tasks / batchSize) * (implementers + propertySets + 1 properties author + 1 cross-check).
// Each task's output has `properties`: tests written from the spec alone, shared by all witnesses, to be run against the program under test.
// Each property is `def prop_<name>(candidate, *args)` that raises AssertionError when violated. Witnesses (implementations, property sets) are for debugging failures and voting on properties later.
// All generated code is run through the Docker sandbox (see docs/SANDBOX.md).
// ---------------------------------------------------------------------------

const DEFAULT_IMPLEMENTERS = [
  { name: 'straightforward', instructions: 'Write the most direct, readable implementation of the spec.' },
  { name: 'brute-force', instructions: 'Write an obviously-correct brute-force implementation. Prefer exhaustive search or simulation over cleverness; performance does not matter as long as it is correct on small inputs.' },
  { name: 'optimized', instructions: 'Write an efficient implementation (good asymptotic complexity, idiomatic Python, no unnecessary copies).' },
]

const DEFAULT_PROPERTY_SETS = [
  { name: 'spec-valid', instructions: 'Generate inputs that are typical and fully inside the spec. The precondition should be as exact as the spec allows.' },
  { name: 'edge-cases', instructions: 'Bias the generator toward boundary inputs: empty, singleton, duplicates, extreme and negative values, and the boundaries of any ranges the spec mentions. Stay inside the spec: the precondition must still reject anything the spec rules out.' },
]

const IMPL_SCHEMA = {
  type: 'object',
  properties: {
    results: {
      type: 'array',
      description: 'One entry per task, in the order the tasks were given',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string', description: 'The task id, copied exactly' },
          code: { type: 'string', description: 'A complete, self-contained Python 3 module that defines the entry point (include any imports from the prompt)' },
          notes: { type: 'string', description: 'One or two sentences on the approach and any spec ambiguity you resolved' },
        },
        required: ['id', 'code'],
      },
    },
  },
  required: ['results'],
}

const PROPSET_SCHEMA = {
  type: 'object',
  properties: {
    results: {
      type: 'array',
      description: 'One entry per task, in the order the tasks were given',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string', description: 'The task id, copied exactly' },
          precondition: { type: 'string', description: 'Python source defining `def precondition(*args) -> bool`' },
          generator: { type: 'string', description: 'Python source (with imports) defining `def strategy()` returning a hypothesis strategy that yields tuples of positional arguments for the entry point' },
          notes: { type: 'string', description: 'What the property set targets and any spec ambiguity you resolved' },
        },
        required: ['id', 'precondition', 'generator'],
      },
    },
  },
  required: ['results'],
}

const PROPERTIES_SCHEMA = {
  type: 'object',
  properties: {
    results: {
      type: 'array',
      description: 'One entry per task, in the order the tasks were given',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string', description: 'The task id, copied exactly' },
          properties: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                name: { type: 'string', description: 'Short snake_case name' },
                description: { type: 'string', description: 'One sentence: what must hold, and what kind of bug it catches' },
                code: { type: 'string', description: 'Python source (with imports) defining `def prop_<name>(candidate, *args) -> None`; raises AssertionError if violated' },
              },
              required: ['name', 'description', 'code'],
            },
          },
          notes: { type: 'string' },
        },
        required: ['id', 'properties'],
      },
    },
  },
  required: ['results'],
}

const CHECK_SCHEMA = {
  type: 'object',
  properties: {
    results: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string', description: 'The task id, copied exactly' },
          propertySets: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                name: { type: 'string' },
                generatorRuns: { type: 'boolean' },
                preconditionPassRate: { type: 'number', description: 'Fraction of generated inputs accepted by the precondition' },
                problems: { type: 'array', items: { type: 'string' } },
              },
              required: ['name', 'generatorRuns', 'problems'],
            },
          },
          implementations: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                name: { type: 'string' },
                crashes: { type: 'integer' },
                timeouts: { type: 'integer' },
                problems: { type: 'array', items: { type: 'string' } },
              },
              required: ['name', 'problems'],
            },
          },
          disagreements: {
            type: 'array',
            description: 'Precondition-valid inputs on which implementations returned different results',
            items: {
              type: 'object',
              properties: {
                input: { type: 'string' },
                outputs: { type: 'object', description: 'implementation name -> repr of output' },
              },
              required: ['input', 'outputs'],
            },
          },
          summary: { type: 'string' },
        },
        required: ['id', 'propertySets', 'implementations', 'disagreements'],
      },
    },
  },
  required: ['results'],
}

function normalize(roles, defaults, kind) {
  const list = roles === undefined ? defaults : roles
  if (!Array.isArray(list) || list.length === 0) throw new Error(`args.${kind} must be a non-empty array`)
  return list.map((r, i) => (typeof r === 'string'
    ? { name: `${kind}-${i}`, instructions: r }
    : { name: r.name || `${kind}-${i}`, instructions: r.instructions || '', model: r.model, effort: r.effort }))
}

function chunk(xs, n) {
  const out = []
  for (let i = 0; i < xs.length; i += n) out.push(xs.slice(i, i + n))
  return out
}

if (!args || !Array.isArray(args.tasks) || args.tasks.length === 0) {
  throw new Error('args.tasks must be a non-empty array of {id, prompt, entry_point}')
}
for (const t of args.tasks) {
  if (!t || !t.id || !t.prompt || !t.entry_point) throw new Error(`every task needs id, prompt and entry_point (got ${JSON.stringify(t && t.id)})`)
}
if (new Set(args.tasks.map((t) => t.id)).size !== args.tasks.length) throw new Error('task ids must be unique')

const implementers = normalize(args.implementers, DEFAULT_IMPLEMENTERS, 'implementers')
const propertySets = normalize(args.propertySets, DEFAULT_PROPERTY_SETS, 'propertySets')
const batchSize = Math.max(1, Math.floor(args.batchSize || 5))
const samples = args.samples || 200
const sandboxCmd = args.sandboxCmd || 'uv run python -m pbt_agents.sandbox.runner'
const shared = args.sharedInstructions ? `\n\nAdditional instructions for everyone:\n${args.sharedInstructions}` : ''

// model / effort for one stage; a role's own setting wins, then args.models / args.efforts, then args.model / args.effort, else inherit
function tier(stage, role) {
  const o = {}
  const model = (role && role.model) || (args.models && args.models[stage]) || args.model
  const effort = (role && role.effort) || (args.efforts && args.efforts[stage]) || args.effort
  if (model) o.model = model
  if (effort) o.effort = effort
  return o
}

const batches = chunk(args.tasks, batchSize)
log(`${args.tasks.length} tasks in ${batches.length} batches of up to ${batchSize}; ` +
  `${batches.length * (implementers.length + propertySets.length + (args.properties === false ? 0 : 1) + (args.crossCheck === false ? 0 : 1))} subagents`)

const taskBlock = (t, i) =>
  `### Task ${i + 1}: id=${t.id}, entry point \`${t.entry_point}\`\n\`\`\`python\n${t.prompt}\n\`\`\``

const SEQUENTIAL =
  'You have several tasks. Work through them one after another, in order: finish and check one before starting the next, ' +
  'and treat each as independent (do not let one task\'s approach or assumptions leak into another). ' +
  'Return one result per task, using the task ids exactly as given.'

const NO_PEEKING =
  'Work only from the specifications given here. Do not look for or read reference solutions, test files or dataset files in the repository or elsewhere; they would make your output useless as an independent witness.'

const SANDBOX =
  `Run every piece of code you write through the sandbox, never directly on this machine: write it to a file in the scratchpad directory and run \`${sandboxCmd} FILE\` ` +
  `(add \`--timeout N\` for longer runs). Each sandbox run is a fresh container with Python 3.13, hypothesis and numpy, no network and no persistent files, ` +
  `so put everything a run needs in the one file. If the sandbox is unavailable (exit code 125, e.g. Docker is not running), stop and say so in your notes instead of falling back to running code locally.`

function implPrompt(batch, role) {
  return `You are one of several independent implementers. Your role: ${role.name}.\n\nRole instructions:\n${role.instructions}\n\n` +
    `${SEQUENTIAL}\n\n${batch.map(taskBlock).join('\n\n')}\n\n` +
    `For each task, return a complete, self-contained Python 3 module defining the entry point (keep the imports the prompt needs; no tests, no __main__ block). ` +
    `Before moving on, run the task's docstring examples against your code and fix any mismatch. ` +
    `If a spec is ambiguous, pick the most natural reading and mention it in that task's notes.\n\n${SANDBOX}\n\n${NO_PEEKING}${shared}`
}

function propPrompt(batch, role) {
  return `You are one of several independent authors of property-based-testing input spaces. Your role: ${role.name}.\n\nRole instructions:\n${role.instructions}\n\n` +
    `${SEQUENTIAL}\n\n${batch.map(taskBlock).join('\n\n')}\n\n` +
    `For each task, produce a witness property set made of two parts:\n` +
    `1. \`def precondition(*args) -> bool\`: True exactly when the arguments are valid inputs under the spec (including constraints only stated in prose). It must never raise.\n` +
    `2. \`def strategy()\`: returns a hypothesis strategy yielding tuples of positional arguments for the entry point. Include the imports it needs.\n` +
    `Together they define which inputs an implementation is expected to be correct on. Do not write an implementation or an oracle. ` +
    `Before moving on, check that the strategy runs and look at what fraction of drawn inputs the precondition accepts; ` +
    `a strategy that mostly produces precondition-rejected inputs is a bug in the property set.\n\n${SANDBOX}\n\n${NO_PEEKING}${shared}`
}

function propertiesPrompt(batch) {
  const extra = (args.properties && args.properties.instructions) ? `\n\nRole instructions:\n${args.properties.instructions}` : ''
  return `You write the properties that property-based tests will check. One shared set of properties per task will be used to test the real program, ` +
    `so write them from the specification alone.${extra}\n\n${SEQUENTIAL}\n\n${batch.map(taskBlock).join('\n\n')}\n\n` +
    `For each task, write 3 to 8 properties. Each is a Python function \`def prop_<name>(candidate, *args) -> None\` where \`candidate\` is the function under test and \`args\` is one valid input ` +
    `(positional arguments, as drawn by a hypothesis strategy that already satisfies the spec's preconditions). It raises AssertionError when the property is violated and must not raise anything else on valid inputs. ` +
    `A harness will call it as \`prop_<name>(candidate, *args)\` inside \`@given\`.\n` +
    `Rules:\n` +
    `- No reference implementation or oracle inside a property: do not re-solve the task and compare. Check things that must hold of the output: ` +
    `output type and shape, relationships between input and output, invariants (ordering, membership, length, value ranges), ` +
    `metamorphic relations (call \`candidate\` on a transformed input and relate the two outputs: permutation, concatenation, scaling, idempotence, symmetry), and any exact answers the docstring pins down.\n` +
    `- \`candidate\` may mutate its arguments: \`copy.deepcopy\` the inputs before every call, and before using them afterwards.\n` +
    `- Prefer properties that catch different kinds of bugs over several variants of one check. Do not make a property so strict that a correct program could violate it (watch ties, float rounding, unspecified orderings).\n` +
    `- Before moving on, check each property in the sandbox against a throwaway implementation of your own on small hand-picked inputs, including the docstring examples, to make sure correct behaviour passes. Do not return that implementation.\n\n` +
    `${SANDBOX}\n\n${NO_PEEKING}${shared}`
}

function checkPrompt(batch, gen) {
  const per = batch.map((t, i) => {
    const g = gen[t.id]
    return `${taskBlock(t, i)}\n\nImplementations for ${t.id}:\n` +
      g.implementations.map((m) => `#### ${m.name}\n\`\`\`python\n${m.code}\n\`\`\``).join('\n\n') +
      `\n\nProperty sets for ${t.id}:\n` +
      g.propertySets.map((p) => `#### ${p.name}\n\`\`\`python\n${p.precondition}\n\n${p.generator}\n\`\`\``).join('\n\n')
  })
  return `Cross-check the witnesses generated for each task below. ${SEQUENTIAL.replace('Return one result per task', 'Return one result per task that has witnesses')}\n\n${per.join('\n\n---\n\n')}\n\n` +
    `For each task, and for each of its property sets: draw about ${samples} inputs from strategy(), report whether the generator runs, and the fraction accepted by precondition (report 0 and a problem if the precondition raises). ` +
    `Then, on the precondition-valid inputs from all of the task's property sets, run every implementation (deepcopy the inputs per call, since implementations may mutate them) and report crashes, timeouts and any inputs where implementations return different results. ` +
    `Compare floats with a small tolerance. Cap the disagreements you list at 10 per task, most informative first. ` +
    `Do NOT decide which side of a disagreement is right and do not edit any witness; just report.\n\n${SANDBOX}\n\n` +
    `Do not look for reference solutions or tests in the repository.`
}

// Run every role over the batch in parallel; each role handles the whole batch sequentially.
async function generate(batch, bi) {
  const ids = new Set(batch.map((t) => t.id))
  const roleTasks = [
    ...implementers.map((role) => async () => {
      const r = await agent(implPrompt(batch, role), { label: `batch${bi}:impl:${role.name}`, phase: 'Generate', schema: IMPL_SCHEMA, ...tier('implementers', role) })
      return { kind: 'impl', name: role.name, results: (r && r.results) || [] }
    }),
    ...(args.properties === false ? [] : [async () => {
      const r = await agent(propertiesPrompt(batch), { label: `batch${bi}:properties`, phase: 'Generate', schema: PROPERTIES_SCHEMA, ...tier('properties') })
      return { kind: 'properties', name: 'properties', results: (r && r.results) || [] }
    }]),
    ...propertySets.map((role) => async () => {
      const r = await agent(propPrompt(batch, role), { label: `batch${bi}:props:${role.name}`, phase: 'Generate', schema: PROPSET_SCHEMA, ...tier('propertySets', role) })
      return { kind: 'props', name: role.name, results: (r && r.results) || [] }
    }),
  ]
  const outs = (await parallel(roleTasks)).filter(Boolean)
  const gen = {}
  for (const t of batch) gen[t.id] = { implementations: [], propertySets: [], properties: [] }
  for (const o of outs) {
    const seen = new Set()
    for (const res of o.results) {
      if (!ids.has(res.id) || seen.has(res.id)) continue // ignore hallucinated / duplicate ids
      seen.add(res.id)
      const { id, ...rest } = res
      if (o.kind === 'properties') gen[id].properties = rest.properties || []
      else gen[id][o.kind === 'impl' ? 'implementations' : 'propertySets'].push({ name: o.name, ...rest })
    }
    const missing = batch.filter((t) => !seen.has(t.id)).map((t) => t.id)
    if (missing.length) log(`batch ${bi}: ${o.kind} role '${o.name}' returned nothing for ${missing.join(', ')}`)
  }
  return gen
}

const results = await pipeline(
  batches,
  (batch, _prev, bi) => generate(batch, bi),
  async (gen, batch, bi) => {
    const checkable = batch.filter((t) => gen[t.id].implementations.length && gen[t.id].propertySets.length)
    let checks = {}
    if (args.crossCheck !== false && checkable.length) {
      const r = await agent(checkPrompt(checkable, gen), { label: `batch${bi}:cross-check`, phase: 'Cross-check', schema: CHECK_SCHEMA, ...tier('crossCheck') })
      for (const c of (r && r.results) || []) {
        const { id, ...rest } = c
        checks[id] = rest
      }
      const unchecked = checkable.filter((t) => !checks[t.id]).map((t) => t.id)
      if (unchecked.length) log(`batch ${bi}: cross-check returned nothing for ${unchecked.join(', ')}`)
    }
    const skipped = batch.filter((t) => !checkable.includes(t)).map((t) => t.id)
    if (args.crossCheck !== false && skipped.length) log(`batch ${bi}: no cross-check for ${skipped.join(', ')} (needs at least one implementation and one property set)`)
    return batch.map((t) => ({ id: t.id, entry_point: t.entry_point, ...gen[t.id], crossCheck: checks[t.id] || null }))
  },
)

return results.filter(Boolean).flat()
