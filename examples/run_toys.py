"""Toy examples for the Coding skill: exercise every mechanism of design doc §1–§8 (evolution not included).

Run:  python examples/run_toys.py
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from examples import toy_world as W                                           # noqa: E402
from skillgraph import (Executor, GateError, ScriptedBackend, SkillLibrary,   # noqa: E402
                        check_refinement, check_skill, load_skill, load_skills,
                        render_skill, render_trace)

SKILLS = Path(__file__).resolve().parent / "skills"
OUTPUT = Path(__file__).resolve().parent / "output"


class Figures:
    """SVG output: one file per skill version under output/skills, one per run under output/runs."""

    def __init__(self, resolve=None):
        self.resolve = resolve

    def skill(self, skill, note=""):
        render_skill(skill, OUTPUT / "skills" / f"{skill.name}_v{skill.version}.svg")

    def trace(self, skill, trace, name):
        render_trace(skill, trace, OUTPUT / "runs" / f"{name}.svg", self.resolve)


def title(s):
    print(f"\n{'=' * 12} {s} {'=' * 12}")


def load(name):
    return load_skill(SKILLS / f"{name}.yaml")


def variant(skill, edit, version=None):
    """Make a small edit to a skill's raw definition to get a new skill (for demonstration, not evolution)."""
    d = copy.deepcopy(skill.raw)
    if version:
        d["version"] = version
    edit(d)
    return load_skill(d)


def executor(lib, scripts):
    return Executor(lib, W.TOOLS, ScriptedBackend(scripts), W.PREDICATES)


LOCATE = lambda view: {"F": ["date_utils.py"]}                                # noqa: E731


def patches(*ps):
    return [{"patch": p} for p in ps]


def ex1_wellformed():
    title("Example 1  Well-formedness: which graphs are legal (§6)")
    out = Figures()
    lib = SkillLibrary()
    skills = load_skills(SKILLS)
    for key, r in lib.add_all(skills).items():
        print(r.wf if not isinstance(r, GateError) else f"✗ {key}: {r}")
        print()
    for s in skills:
        out.skill(s)

    print("---- a few ill-formed variants, all stopped before admission ----")
    debug = load("debug_v1_1")
    bad = {
        "W1 overlapping guards": lambda d: d["edges"][3].update(guard="k < 2"),
        "W1 missing guard": lambda d: d["edges"].pop(4),
        "W3 back edge without counter": lambda d: d["edges"][3].pop("counter"),
        "W4 reads an unwritten variable": lambda d: d["nodes"]["locate"].update(reads=["issue", "repo", "patch"]),
        "W5 contract does not ensure invariant": lambda d: d["nodes"]["write_patch"]["contract"].update(post="files(patch) ⊆ F"),
        "W0 semantic test in edge condition": lambda d: d["edges"][2].update(guard="passes_suite(patch, repo)"),
        "B  budget too small": lambda d: d["interface"]["budget"].update(llm_calls=3),
    }
    for name, edit in bad.items():
        r = check_skill(variant(debug, edit, "9.0"), lib.resolve)
        c = next(c for c in r.checks if not c.ok)
        print(f"[{name}] {c.code} {c.status}: {c.messages[0]}")
    return lib


def ex2_debug_failure_and_replay():
    title("Example 2  A failed Debug v1.0 run, post-hoc evaluation and cached replay (§7)")
    out = Figures()
    lib = SkillLibrary()
    v10, v11 = load("debug_v1_0"), load("debug_v1_1")
    out.skill(v10)
    out.skill(v11)
    lib.add(v10)
    ex = executor(lib, {"locate": LOCATE, "write_patch": patches(W.D1_NOFIX, W.D2_REGRESS, W.D3_DAY_ONLY)})

    old = ex.run("Debug", {"issue": W.ISSUE, "repo": W.REPO})
    ex.posthoc(old)
    print(old.pretty())
    print("→ every step kept its contract, yet Post_safe = ⊥: the delivered patch fixes the repro test but breaks leap years.")
    out.trace(v10, old, "run1_debug_v1_0")
    lib.record(old)

    print("\nReplay Debug v1.1 (only the guards changed) on this old trace, without calling any agent:")
    rep = ex.replay(v11, old)
    print(rep.pretty())
    out.trace(v11, rep, "replay_debug_v1_1")
    print(f"→ replay reached a terminal: {rep.exact}. Both write_patch steps come from the cache, tests are re-run; "
          "the second patch has suite = false, so it triggers a retry instead of delivering a bad patch.")

    print("\nAdmit Debug v1.1 (compatible interface → minor version) and run it for real:")
    print(lib.add(v11))
    ex = executor(lib, {"locate": LOCATE, "write_patch": patches(W.D1_NOFIX, W.D2_REGRESS, W.D3_DAY_ONLY)})
    new = ex.posthoc(ex.run("Debug", {"issue": W.ISSUE, "repo": W.REPO}))
    print(new.pretty())
    out.trace(v11, new, "run2_debug_v1_1")
    lib.record(new)

    print("\nStatistics layer (reliability posteriors):")
    for ver in ("1.0", "1.1"):
        st = lib.stats("Debug", ver)
        print(f"  Debug v{ver}: outcomes {st.outcomes}, Post_safe {st.post_safe}")
        for node, beta in st.reliability.items():
            print(f"      {node}: {beta}")
    return lib


def ex3_interface_refinement(lib):
    title("Example 3  Interface refinement and versions: why changing one skill does not break others (§8)")
    out = Figures()
    v10, v11 = lib.get("Debug", "1.0"), lib.get("Debug", "1.1")
    out.skill(v10, note="v1.0: the guard to Done checks only rep; W8 cannot be proved.")
    out.skill(v11, note="v1.1: only three edge guards change, interface unchanged, W8 provable.")
    print(check_refinement(v10, v11))

    def weaken(d):          # δ4 from design doc §11.4: weaken the promise to "only the repro test passes"
        d["interface"]["post_safe"] = "outcome == done -> passes_repro(patch, issue, repo)"
        d["edges"][2]["guard"], d["edges"][3]["guard"], d["edges"][4]["guard"] = "rep", "!rep && k < 2", "!rep && k >= 2"

    print("\nTry to admit the version with a weakened Post as v1.2:")
    try:
        lib.add(variant(v11, weaken, "1.2"))
    except GateError as ex:
        print(f"✗ {ex}")
    print("\nAdmitting it as major version v2.0 is allowed:")
    v2 = variant(v11, weaken, "2.0")
    print(lib.add(v2))
    out.skill(v2, note="v2.0: weakened Post_safe, incompatible with v1.x, can only be released as a major version.")

    coding = load("coding_v1_0")
    lib.add(coding)
    out.skill(coding, note="Coding v1.0: the call node says \"Debug >= 1.0\".")
    call = coding.nodes["debug"]
    print(f"\nCoding declares \"{call.skill} >= {call.min_version}\", which now resolves to: "
          f"{lib.resolve(call.skill, call.min_version).key} (library has {[str(v) for v in lib.versions('Debug')]})")
    print("→ v1.1 is picked up automatically, but never the weaker v2.0.")


def ex4_coding_calls_debug(lib):
    title("Example 4  Coding calls Debug: nested traces, and 'the caller relies on a promise that is too weak' (§3.3, §7.2)")
    out = Figures(lib.get)
    out.skill(lib.get("Coding", "1.0"), note="Coding v1.0: goes straight to done after Debug returns, without re-acceptance.")
    inputs = {"spec": W.SPEC, "repo": W.REPO}
    scripts = lambda: {                                                         # noqa: E731
        "understand": [{"plan": "validate month and day in parse_date"}],
        "implement": patches(W.D1_NOFIX),
        "locate": LOCATE,
        "write_patch": patches(W.D2_REGRESS, W.D3_DAY_ONLY, W.D4_CORRECT),
    }
    print(check_skill(lib.get("Coding", "1.0"), lib.resolve)["W8"].messages[0])
    ex = executor(lib, scripts())
    tr = ex.posthoc(ex.run("Coding", inputs))
    print(tr.pretty())
    print("→ Debug kept its own Post (fixed the reported test_invalid_day without regressions), "
          "but Coding's Post_safe = ⊥: test_invalid_month still fails.\n"
          "  This is not Debug's fault but a flaw in Coding's workflow, matching the W8 warning at admission.")
    out.trace(lib.get("Coding", "1.0"), tr, "coding_v1_0")
    lib.record(tr)

    print("\nCoding v1.1: re-acceptance after Debug returns (compatible interface, only the budget changed):")
    print(lib.add(load("coding_v1_1")))
    out.skill(lib.get("Coding", "1.1"), note="Coding v1.1: goes back to test for re-acceptance after Debug returns, with counter r.")
    ex = executor(lib, scripts())
    tr = ex.posthoc(ex.run("Coding", inputs))
    print(tr.pretty())
    out.trace(lib.get("Coding", "1.1"), tr, "coding_v1_1")
    lib.record(tr)


def ex5_kernel_guards(lib):
    title("Example 5  Kernel guards: Reject, invariants, giving up honestly (§5)")
    out = Figures(lib.get)
    debug = lib.get("Debug", "1.1")
    print("Input outside Pre (the issue has no repro test) → Reject immediately, no agent called:")
    ex = executor(lib, {"locate": LOCATE, "write_patch": []})
    tr = ex.run(debug, {"issue": W.ISSUE_NO_REPRO, "repo": W.REPO})
    print(tr.pretty())
    out.trace(debug, tr, "reject_no_repro")

    print("\nThe agent tries to pass by editing the tests (δ3 in §11.4) → stopped by the run-time invariant check, no outputs delivered:")
    ex = executor(lib, {"locate": LOCATE, "write_patch": patches(W.D_CHEAT)})
    tr = ex.run(debug, {"issue": W.ISSUE, "repo": W.REPO})
    print(tr.pretty())
    print(f"   outputs handed back: {tr.outputs}")
    out.trace(debug, tr, "inv_violation_cheat")

    print("\nStill not fixed after all retries → honest GiveUp (violates liveness, not safety):")
    ex = executor(lib, {"locate": LOCATE, "write_patch": patches(W.D1_NOFIX, W.D2_REGRESS, W.D2_REGRESS)})
    tr = ex.posthoc(ex.run(debug, {"issue": W.ISSUE, "repo": W.REPO}))
    print(tr.pretty())
    out.trace(debug, tr, "give_up_retries")


def ex6_semantic_branch_and_single_node():
    title("Example 6  Semantic branches go to agent nodes (§4), and single-node skills (§13)")
    out = Figures()
    lib = SkillLibrary()
    rp, wd = load("reviewed_patch"), load("write_docstring")
    lib.add(rp)
    lib.add(wd)
    out.skill(rp, note="ReviewedPatch: verdict comes from the review node; edges only test this enum value.")
    out.skill(wd, note="WriteDocstring: the graph degenerates to 'one agent + one terminal'.")
    ex = executor(lib, {
        "implement": patches(W.D_MESSY, W.D4_CORRECT),
        "review": [{"verdict": "revise", "feedback": "remove the debug print"}, {"verdict": "approve", "feedback": ""}],
        "write": [{"doc": "parse_date: parse YYYY-MM-DD into (year, month, day); raise ValueError on invalid dates."}],
    })
    print()
    tr = ex.posthoc(ex.run("ReviewedPatch", {"spec": W.SPEC, "repo": W.REPO}))
    print(tr.pretty())
    out.trace(rp, tr, "reviewed_patch")
    print()
    tr = ex.posthoc(ex.run("WriteDocstring", {"code": W.D4_CORRECT["date_utils.py"]}))
    print(tr.pretty())
    out.trace(wd, tr, "write_docstring")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ex1_wellformed()
    lib = ex2_debug_failure_and_replay()
    ex3_interface_refinement(lib)
    ex4_coding_calls_debug(lib)
    ex5_kernel_guards(lib)
    ex6_semantic_branch_and_single_node()
    print(f"\nSVGs written to: {OUTPUT}")


if __name__ == "__main__":
    main()
