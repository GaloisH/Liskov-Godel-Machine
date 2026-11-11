# Liskov-Gödel Machine

Research on recursive self-improvement (RSI) of LLM agents. Agent skills are represented as **graphs with contracts**. Before a skill enters the library it is checked for well-formedness and for interface refinement (Liskov substitution). This lays the groundwork for failure-driven skill repair and for formally comparing repair candidates.

The code so far is the first step, `skillgraph`: the graph structure of a skill, well-formedness checks, execution and traces, cached replay, interface refinement and a versioned library. **Evolution is not included yet** (blame assignment, editing and comparison).

## Examples

<table>
<tr>
<td align="center"><b>Skill graph: Debug v1.1</b></td>
<td align="center"><b>Data flow of one run, overlaid on the skill graph</b></td>
</tr>
<tr>
<td><img src="assets/skill_debug_v1_1.svg" alt="Debug v1.1 skill graph"></td>
<td><img src="assets/run_debug_v1_1.svg" alt="Debug v1.1 run trace"></td>
</tr>
</table>

More run traces are in [`assets/`](assets/): giving up after an invariant violation (`run_inv_violation_cheat.svg`), rejection when the precondition fails (`run_reject_no_repro.svg`), and cached replay stopping at the replay frontier (`run_replay_debug_v1_1.svg`).

## Quick start

```bash
pip install -r requirements.txt            # only PyYAML; tests also need pytest
python examples/run_toys.py                # six coding toy examples; SVGs go to examples/output/
python -m pytest tests -q                  # tests
python -m skillgraph check examples/skills                       # admission check for a directory
python -m skillgraph show  examples/skills Coding coding.svg     # render to SVG (needs Graphviz `dot`)
```

## Layout

```
skillgraph/     core implementation (modules and design refinements: skillgraph/README.md)
examples/       toy world, example skills defined in YAML, run_toys.py
tests/          pytest tests
assets/         selected figures used in this README
```
