export const meta = {
  name: 'repair',
  description: 'Repair what the vote found wrong: one subagent per program that returned a wrong output (fixes the program) or per property that rejected correct behaviour (fixes the property), checking each attempt in the sandbox.',
  whenToUse: 'After the harness + vote step (harness --jobs-out) has produced repair jobs. Score the returned repairs with `python -m pbt_agents.repair evaluate`.',
  phases: [
    { title: 'Repair', detail: 'one subagent per program with a confirmed bug and per property judged wrong; it edits the code and rechecks until the confirmed cases pass or it runs out of rounds' },
  ],
}

// ---------------------------------------------------------------------------
// args (all JSON):
// {
//   jobsPath: string,            // repair jobs file written by `harness --jobs-out` or `python -m pbt_agents.repair jobs`
//   ids?: string[],              // program ids to repair (the `jobs` in that file: programs whose output differs from y*)
//   propertyIds?: string[],      // property ids to repair (the `property_jobs`: properties that rejected a correct output; ids look like `HumanEval/12::first_of_ties`)
//   maxRounds?: number,          // check-and-revise rounds per program (default 4)
//   model?: string, effort?: string,   // model / reasoning effort for every subagent; omit to inherit the session's
//   sharedInstructions?: string, // appended to every prompt
//   repairCmd?: string,          // how agents call the repair tool (default: uv run python -m pbt_agents.repair)
//   sandboxCmd?: string          // how agents run other code (default: the Docker sandbox runner)
// }
// Returns [{id, code, notes, rounds, fixed}] (for a property, `code` is the fixed property module). The agent's own `fixed` is only a claim: score the result
// with `uv run python -m pbt_agents.repair evaluate JOBS.json REPAIRS.json`.
// The repair agent never gets the canonical solution; it works from the spec, the confirmed cases
// (an input plus the output the witnesses or the user agreed on) and the property failures.
// Which kind of job a failing property run becomes is decided by the vote: some call returned something
// other than y* -> repair the program; every call returned y* -> repair the property.
// ---------------------------------------------------------------------------

const SCHEMA = {
  type: 'object',
  properties: {
    id: { type: 'string', description: 'The program id, copied exactly' },
    code: { type: 'string', description: 'The complete fixed Python 3 module (a program: keep the imports, signature and docstring; a property: keep the `prop_<name>(candidate, *args)` function)' },
    fixed: { type: 'boolean', description: 'True only if your last check reported success' },
    rounds: { type: 'integer', description: 'How many times you ran the check' },
    notes: { type: 'string', description: 'One or two sentences: what was wrong and what you changed' },
  },
  required: ['id', 'code', 'fixed'],
}

const programIds = args && Array.isArray(args.ids) ? args.ids : []
const propertyIds = args && Array.isArray(args.propertyIds) ? args.propertyIds : []
if (!args || !args.jobsPath || programIds.length + propertyIds.length === 0) {
  throw new Error('args.jobsPath and a non-empty args.ids or args.propertyIds are required')
}
const maxRounds = Math.max(1, Math.floor(args.maxRounds || 4))
const repairCmd = args.repairCmd || 'uv run python -m pbt_agents.repair'
const sandboxCmd = args.sandboxCmd || 'uv run python -m pbt_agents.sandbox.runner'
const shared = args.sharedInstructions ? `\n\nAdditional instructions:\n${args.sharedInstructions}` : ''
const opts = { phase: 'Repair' }
if (args.model) opts.model = args.model
if (args.effort) opts.effort = args.effort

function prompt(id) {
  return `Repair one Python program that is wrong on some inputs.

Program id: ${id}
Jobs file: ${args.jobsPath}

1. Run \`${repairCmd} show ${args.jobsPath} ${id}\`. It prints the spec with the current code, and the confirmed wrong outputs: inputs on which the program's output differs from the output that independent implementations (or the user) agreed is correct.
2. Find the defect and fix the program. Work from the spec (the docstring) and those cases. The expected outputs are evidence about the spec, not a table to special-case: the fix must be a general correction, never code that hard-codes inputs or outputs. Change as little as needed; the program is believed to be almost right.
3. Write the full fixed module (imports, signature and docstring kept) to a file in the scratchpad directory and run \`${repairCmd} check ${args.jobsPath} ${id} FILE\`. It runs in the Docker sandbox, reports which confirmed cases are still wrong, and reruns the shared property tests on your fix. Property failures are warnings: a property can itself be wrong, so decide from the spec whether a reported failure is a real remaining bug in your fix or a property you can ignore.
4. Repeat 2-3 for at most ${maxRounds} checks in total. Stop as soon as every confirmed case is fixed and no property failure points at a real bug.

Rules:
- Never run generated or modified code outside the sandbox. To try anything else, put it in one file and run \`${sandboxCmd} FILE\`. If the sandbox is unavailable (exit code 125, e.g. Docker is not running), stop and say so in your notes.
- Do not look for the original or reference solution of this task anywhere on disk (data files, git history, other jobs, other tasks' code): only the spec, the cases and the checker are allowed. It exists only to grade you afterwards.
- Return the best code you have even if you could not fix every case, with fixed=false.${shared}`
}

function propertyPrompt(id) {
  return `Repair one property-based test that wrongly rejects a correct program.

Property id: ${id}
Jobs file: ${args.jobsPath}

A property is \`prop_<name>(candidate, *args)\`: it calls the program under test (\`candidate\`), possibly several times on inputs it derives from its own arguments, and raises AssertionError when the behaviour is wrong. It was written from the spec alone, shared by all programs, and is run by Hypothesis on generated inputs. Independent implementations (witnesses) or the user agreed that the program's output was correct on every call it made in the failing runs below, yet the property raised: so the property is believed to be wrong (too strict, a wrong expectation, a misreading of the spec, or an off-by-one in its own logic).

1. Run \`${repairCmd} show ${args.jobsPath} ${id}\`. It prints the spec, the current property, the failing runs (the property's arguments, each call the property made, and the correct output), and the real bugs the repaired property must still reject.
2. Work out from the spec what the property gets wrong and correct it. Keep it a real test of the spec: it must stay as strong as the spec justifies, and it must still reject the listed real bugs. Never repair it by deleting its assertions, wrapping them in try/except, special-casing the listed inputs, or making it vacuous.
3. Write the full fixed property module (imports, the \`prop_<name>\` function kept with the same name and signature) to a file in the scratchpad directory and run \`${repairCmd} check ${args.jobsPath} ${id} FILE\`. It runs in the Docker sandbox, reports whether the correct outputs are now accepted, whether the real bugs are still rejected, and whether the witnesses pass the fixed property on generated inputs (witnesses are not oracles, but many failing is a warning).
4. Repeat 2-3 for at most ${maxRounds} checks in total. Stop as soon as the check reports success with no witness failures you cannot explain from the spec.

Rules:
- Never run generated or modified code outside the sandbox. To try anything else, put it in one file and run \`${sandboxCmd} FILE\`. If the sandbox is unavailable (exit code 125, e.g. Docker is not running), stop and say so in your notes.
- Do not look for the original or reference solution of this task anywhere on disk: only the spec, the failing runs and the checker are allowed.
- Return the best property you have even if the check is not fully green, with fixed=false.${shared}`
}

const work = [...programIds.map((id) => ({ id, prompt: prompt(id) })), ...propertyIds.map((id) => ({ id, prompt: propertyPrompt(id) }))]
log(`repairing ${programIds.length} programs and ${propertyIds.length} properties, up to ${maxRounds} checks each`)
const results = await pipeline(
  work,
  (w) => agent(w.prompt, { ...opts, label: `repair:${w.id}`, schema: SCHEMA }),
)
const out = results.map((r, i) => (r ? { ...r, id: work[i].id } : null))
const lost = work.filter((_, i) => !out[i]).map((w) => w.id)
if (lost.length) log(`no result for: ${lost.join(', ')}`)
log(`${out.filter((r) => r && r.fixed).length}/${work.length} claim to be fixed (verify with the evaluate command)`)
return out.filter(Boolean)
