# skillgraph

Implementation of §1–§8 of the Step 1 design document (not yet public): the graph structure of a skill, well-formedness checks, execution and traces, cached replay, interface refinement and a versioned library. **Evolution is not included** (§9 blame assignment, §10 editing and comparison).

```bash
python examples/run_toys.py              # six coding toy examples
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

1. **Loop counters live on edges and are incremented by the executor.** The IR in §11 of the design document has the `write_patch` agent write `k` itself, so termination would depend on the agent keeping its word. Now a back edge declares `counter: k`, the executor increments it on every traversal, and no node may write a counter. W3 therefore becomes a purely structural check: removing the counter edges leaves an acyclic graph, and the guard of every counter edge implies `k < bound`. Note that `k` now means "number of retries so far", so `int[0..2]` with `k < 2` gives 3 attempts in total.
2. **Tool nodes may declare `ensures`.** It links decidable variables to semantic predicates (e.g. `rep == passes_repro(patch, issue, repo)`). Without it, W8 has no way to prove "guard holds ⇒ Post holds".
3. **Exhaustive enumeration plus propositional abstraction instead of SMT.** Decidable variables are enumerated over their finite domains; atoms containing predicates or opaque variables are treated as free Boolean variables. The check is sound but incomplete: it does not know semantic relations between predicates (e.g. `len(F) > 1` implies `len(F) > 0`); such relations must be written into the contracts when needed.
4. **W5 / W6 / W8 share one "fact propagation" analysis** instead of a full wp computation: a node that keeps its contract leaves its postcondition behind, an edge leaves its guard, facts about a variable are invalidated when the variable is overwritten, facts are intersected at join points, and proofs are done per incoming edge. Graphs with loops need no unrolling. When W8 cannot prove something it is only marked "unproved" and a counterexample is reported.
5. **Two new hard checks: W0 (structure and types) and B (budget).** W0 covers: contracts may only reference the node's read/write sets, edge conditions may not call predicates, input variables are read-only, and undeclared identifiers are errors. B statically computes the worst-case number of model calls and requires it not to exceed the declared budget; the cost of a call node is the budget declared by the callee.
6. **Only `done` delivers outputs.** GiveUp / Reject do not hand back partial results; otherwise the caller's variables would be overwritten by a value known to be bad. Accordingly, W4 considers a call node's outputs written only on outgoing edges whose guard implies `outcome == done`.
7. **Two tiers of predicates.** Predicates in `runtime` can be computed at run time; those in `judge` are only available during post-hoc evaluation and are always `?` at run time. The contract of a call node (the callee's Post) usually only gets ⊤ / ⊥ in post-hoc evaluation.
8. **Kernel aborts.** When Pre is ⊥ the run is rejected immediately; when the run-time Inv is ⊥, the budget is exceeded, a write has the wrong type, or a tool raises, the run ends with `give_up`, the reason is recorded in `status`, and no outputs are delivered.
9. **Version resolution never crosses major versions.** `Debug >= 1.0` resolves to the latest minor version in 1.x. A minor version must satisfy the refinement rules to be admitted; if the budget changes, callers are re-checked and the admission is rolled back if they fail.
10. **When the same cache key appears several times during replay** (a retry whose read set did not change), the entries are consumed in their original order, which amounts to reusing the same random seed.

## Not done yet

- A real LLM backend: only `ScriptedBackend` and `ReplayBackend` exist so far. To plug one in, implement `invoke(AgentCall) -> dict`.
- Library persistence: skills are loaded from YAML, but the statistics layer and traces live only in memory (`Trace.to_json()` can export them).
- The budget check is static and covers only `llm_calls`; cost and time are only recorded in traces.
- Fault models are only stored; they are not yet compiled into parametric Markov chains (that belongs to the comparison stage of the verification document).
