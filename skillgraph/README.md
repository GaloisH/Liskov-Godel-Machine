# skillgraph

Implementation of §1–§8 of the Step 1 design document (not yet public): the graph structure of a skill, well-formedness checks, execution and traces, cached replay, interface refinement and a versioned library.

On top of it, `kernel/` and `meta/` implement the first five steps of the Step 2 design document (also not yet public): a trusted kernel, tickets, blame assignment, a minimal comparison, the Evolve loop running on the executor, and the mandatory audit signals. See [Evolution](#evolution-kernel-and-meta-skills) below.

```bash
python examples/run_toys.py              # six coding toy examples
python examples/run_meta.py              # evolution: failure-driven repair, audit-driven repair, kernel guards
python -m pytest tests -q                # tests
python -m skillgraph check examples/skills          # admission check for a directory
python -m skillgraph show  examples/skills Coding coding.svg   # render to SVG (needs Graphviz `dot`)
```

The only dependency is PyYAML (tests also need pytest).

## Modules and the design document

| Module | Contents | Design doc |
|---|---|---|
| `types.py` | Decidable types (bool / enum / bounded int / finite set) and opaque types | §2 |
| `expr.py` | Expression language shared by edge conditions and contracts; Kleene three-valued evaluation | §3.1, §4 |
| `logic.py` | Entailment checking (replaces SMT, see below) | §6 |
| `model.py` | `Skill` = interface layer + graph layer; four node kinds; YAML loading | §1, §3, §5 |
| `wellformed.py` | W0–W8 and the static budget check | §6 |
| `executor.py` | Executor, post-hoc evaluation, cached replay; the `AgentBackend` interface | §7 |
| `trace.py` | Nested traces | §7.2 |
| `refine.py` | Interface refinement rules | §8.1 |
| `library.py` | Versioned library, admission gate, certificates, statistics layer (Beta posteriors) | §1 statistics layer, §8.2 |
| `viz.py` | SVG rendering with Graphviz: skill graphs, and the data flow of a run overlaid on its skill graph | — |

## Refinements of the design made during implementation

These points are either left open by the design document or turned out to need adjustment during implementation:

1. **Loop counters live on edges and are incremented by the executor.** The IR in §9 of the Step 1 design document has the `write_patch` agent write `k` itself, so termination would depend on the agent keeping its word. Now a back edge declares `counter: k`, the executor increments it on every traversal, and no node may write a counter. W3 therefore becomes a purely structural check: removing the counter edges leaves an acyclic graph, and the guard of every counter edge implies `k < bound`. Note that `k` now means "number of retries so far", so `int[0..2]` with `k < 2` gives 3 attempts in total.
2. **Tool nodes may declare `ensures`.** It links decidable variables to semantic predicates (e.g. `rep == passes_repro(patch, issue, repo)`). Without it, W8 has no way to prove "guard holds ⇒ Post holds".
3. **Exhaustive enumeration plus propositional abstraction instead of SMT.** Decidable variables are enumerated over their finite domains; atoms containing predicates or opaque variables are treated as free Boolean variables. The check is sound but incomplete: it does not know semantic relations between predicates (e.g. `len(F) > 1` implies `len(F) > 0`); such relations must be written into the contracts when needed.
4. **W5 / W6 / W8 share one "fact propagation" analysis** instead of a full wp computation: a node that keeps its contract leaves its postcondition behind, an edge leaves its guard, facts about a variable are invalidated when the variable is overwritten, facts are intersected at join points, and proofs are done per incoming edge. Graphs with loops need no unrolling. When W8 cannot prove something it is only marked "unproved" and a counterexample is reported.
5. **Two new hard checks: W0 (structure and types) and B (budget).** W0 covers: contracts may only reference the node's read/write sets, edge conditions may not call predicates, input variables are read-only, and undeclared identifiers are errors. B statically computes the worst-case number of model calls and requires it not to exceed the declared budget; the cost of a call node is the budget declared by the callee.
6. **Only `done` delivers outputs.** GiveUp / Reject do not hand back partial results; otherwise the caller's variables would be overwritten by a value known to be bad. Accordingly, W4 considers a call node's outputs written only on outgoing edges whose guard implies `outcome == done`.
7. **Two tiers of predicates.** Predicates in `runtime` can be computed at run time; those in `judge` are only available during post-hoc evaluation and are always `?` at run time. The contract of a call node (the callee's Post) usually only gets ⊤ / ⊥ in post-hoc evaluation.
8. **Kernel aborts.** When Pre is ⊥ the run is rejected immediately; when the run-time Inv is ⊥, the budget is exceeded, a write has the wrong type, or a tool raises, the run ends with `give_up`, the reason is recorded in `status`, and no outputs are delivered.
9. **Version resolution never crosses major versions.** `Debug >= 1.0` resolves to the latest minor version in 1.x. A minor version must satisfy the refinement rules to be admitted; if the budget changes, callers are re-checked and the admission is rolled back if they fail.
10. **When the same cache key appears several times during replay** (a retry whose read set did not change), the entries are consumed in their original order, which amounts to reusing the same random seed.

## Evolution: kernel and meta skills

"Improving a skill" is itself written as skills (meta skills). They find problems, assign blame, propose edits and arrange the comparison; what counts as better is decided by a fixed kernel.

| Module | Contents | Step 2 design doc |
|---|---|---|
| `kernel/kernel.py` | `Kernel`: human admission, task runs, Gate 0 on edit scripts, evidence checks, `submit`, ticket processing, the repair ledger | §1.2, §2, §5.6 |
| `kernel/edits.py` | Edit scripts: the operations a repair may consist of, applied to exactly one skill | §5.3 |
| `kernel/tickets.py` | Tickets | §4 |
| `kernel/signals.py` | Mandatory signals A1 / A2 / A8; W8 counterexample → virtual failure trace | §6 |
| `kernel/splits.py` | Train / heldout / test split by seeded hash | §2, §9.2 |
| `kernel/store.py` | Persistence as a directory of YAML / JSON / JSONL files | §1.1 |
| `meta/seeds/*.yaml` | Seed meta skills: `Evolve`, `Diagnose`, `Propose`, `Compare` (minimal), `Audit` (ordering only) | §5, §9.1 |
| `meta/tools.py` | Their tool functions: blame assignment, hint table, lint, candidate selection, ticket ordering | §5.2, §5.4, §5.5 |

```python
from skillgraph import load_skill, meta
from skillgraph.kernel import Kernel

k = Kernel(root="lib/", tools=TOOLS, predicates=PREDICATES, backend=backend)   # root=None keeps everything in memory
meta.install(k)                        # seed meta skills + their tools (call again after loading from disk)
k.install(load_skill("debug.yaml"))    # human admission; W8 unproved → the kernel opens an audit ticket (A2)
tr = k.run("Debug", inputs, task="t-17")   # post-hoc evaluation; Post_safe = ⊥ on a train task → failure ticket (A1)
for record in k.drain():               # run Evolve on every open ticket; one ledger record each
    print(record["ticket"], record["outcome"], record["admitted"])
```

How one ticket is handled (`Kernel.process`): prepare the evidence → run `Evolve` on the executor (`Diagnose` → `Propose` → `kernel.gate0` → `Compare` → `kernel.submit`) → write the ledger record, whatever Evolve did. The Evolve run is itself a stored trace, and the meta skills get reliability statistics like any other skill.

### What the kernel guarantees

- **A meta skill cannot write the library except through `submit`**, which is usable only while a ticket is being processed, only for that ticket, and once per run.
- **A certificate is never trusted.** `submit` re-applies the edit script itself, recomputes Gate 0 and W8, and requires the evidence claim to name a check that the kernel ran for the same ticket and the same script, and that passed. A meta skill can ask for a check; it cannot produce one.
- **Admission standard of this minimal version: the ticket passes after the fix.**
  - Ticket with a real failing trace: the candidate is replayed from the cache on the failing run of the blamed skill, continued with real agent calls past the divergence frontier (`FallbackBackend`), and must end `done` with `Post_safe = ⊤`.
  - Ticket with only a W8 counterexample: the counterexample must be gone (the W8 obligation at that terminal is proved and the counterexample state no longer takes the edge into it), and every truly successful past run of the target must still succeed under replay + continuation.
  - Only interface-compatible candidates are admitted automatically; an incompatible one is recorded as `major_only` and left to a human.
- **Mandatory signals are raised by the kernel**, not by a meta skill: A1 (a train run violates `Post_safe`), A2 (W8 unproved at admission, counterexample attached), A8 (a certificate fails re-verification in `Kernel.audit()`, or a version certified "W8 proved" delivers a result that violates `Post_safe`). The `Audit` skill may order the queue; an overdue mandatory ticket is served first regardless.
- **Evidence is kept current.** If a ticket's evidence is about a superseded version, the run is repeated under the current versions: the ticket closes as `resolved` if the problem is gone, otherwise the new trace replaces the old evidence (and blame may move to another skill).

### Refinements of the Step 2 design made during implementation

1. **`submit` writes `accepted`, and only `accepted` leads to Done.** With an unconditional `submit → done`, Evolve would hand back Done after a rejected submit and its own W8 could not be proved.
2. **"One library write per run" is enforced by a kernel write token**, not declared as an invariant: it is not a blackboard variable and would not pass W0.
3. **Compare expresses its verdict through its outcome** (Done = admit, GiveUp = keep). A call node's outputs are only written on Done, so the caller's guards may not read them (W4).
4. **Blame is assigned on the sub-trace of the blamed skill, and so is the evidence check.** The fix must make the blamed skill's own failing run succeed; whether the outer run then succeeds is a separate question, answered by a later ticket if not.
5. **A recheck ticket with a real trace goes through Evolve like a failure ticket**; one with only a counterexample becomes a virtual trace; one with neither is suspended for a human. Nothing is rolled back automatically.
6. **The statistics layer is not persisted**: it is a fold over the recorded traces and is rebuilt on load.

### Not done yet (later steps of the plan)

- O2–O4 and the statistical tie-break of the verification document: the minimal Compare only knows "passes on the ticket's evidence".
- `Create` (the `create` branch of Evolve ends in GiveUp), `Learn`, the remaining audit signals A3–A7 / A9 / A10, `meta` tickets and the scoring protocol for meta skills.
- A real LLM backend for `Propose.draft` and `Diagnose.investigate`: the candidates in the examples are hand-written and served by `ScriptedBackend`.
- The kernel boundary is an API convention, not a sandbox: meta tools are Python functions holding a reference to the kernel.
- If a meta skill crashes (a backend raises), the ledger records the crash but the partial Evolve trace is lost.

## Not done yet

- A real LLM backend: only `ScriptedBackend`, `ReplayBackend` and `FallbackBackend` exist so far. To plug one in, implement `invoke(AgentCall) -> dict`.
- Persistence requires blackboard values to be JSON-serialisable (sets and tuples do not survive the round trip).
- The budget check is static and covers only `llm_calls`; cost and time are only recorded in traces.
- Fault models are only stored; they are not yet compiled into parametric Markov chains (that belongs to the comparison stage of the verification document).
