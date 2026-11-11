"""Coding skill 的 toy examples：把设计文档 §1–§8 的每个机制都走一遍（不含进化）。

运行:  python examples/run_toys.py
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
    """在 skill 的原始定义上做一点修改，得到一个新的 skill（演示用，不是进化）。"""
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
    title("例 1  良构性检查：什么样的图算合法（§6）")
    out = Figures()
    lib = SkillLibrary()
    skills = load_skills(SKILLS)
    for key, r in lib.add_all(skills).items():
        print(r.wf if not isinstance(r, GateError) else f"✗ {key}: {r}")
        print()
    for s in skills:
        out.skill(s)

    print("---- 几个不合法的变体，都在入库前被拦下 ----")
    debug = load("debug_v1_1")
    bad = {
        "W1 守卫有重叠": lambda d: d["edges"][3].update(guard="k < 2"),
        "W1 守卫有遗漏": lambda d: d["edges"].pop(4),
        "W3 回边没有计数器": lambda d: d["edges"][3].pop("counter"),
        "W4 读了没写过的变量": lambda d: d["nodes"]["locate"].update(reads=["issue", "repo", "patch"]),
        "W5 契约保证不了不变量": lambda d: d["nodes"]["write_patch"]["contract"].update(post="files(patch) ⊆ F"),
        "W0 边条件里做语义判断": lambda d: d["edges"][2].update(guard="passes_suite(patch, repo)"),
        "B  预算不够": lambda d: d["interface"]["budget"].update(llm_calls=3),
    }
    for name, edit in bad.items():
        r = check_skill(variant(debug, edit, "9.0"), lib.resolve)
        c = next(c for c in r.checks if not c.ok)
        print(f"[{name}] {c.code} {c.status}: {c.messages[0]}")
    return lib


def ex2_debug_failure_and_replay():
    title("例 2  Debug v1.0 的一次失败运行、事后评估与缓存重放（§7）")
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
    print("→ 每一步都守约，但 Post_safe = ⊥：交出的补丁修好了复现测试，却把闰年搞坏了。")
    out.trace(v10, old, "run1_debug_v1_0")
    lib.record(old)

    print("\n把 Debug v1.1（只改了守卫）放在这条旧轨迹上重放，不调用任何 Agent:")
    rep = ex.replay(v11, old)
    print(rep.pretty())
    out.trace(v11, rep, "replay_debug_v1_1")
    print(f"→ 重放是否走到终点: {rep.exact}。两次 write_patch 都用缓存，测试重新跑；"
          "第二个补丁 suite = false，于是触发重试而不是交出错误补丁。")

    print("\nDebug v1.1 入库（接口兼容 → 小版本），再真实运行一次:")
    print(lib.add(v11))
    ex = executor(lib, {"locate": LOCATE, "write_patch": patches(W.D1_NOFIX, W.D2_REGRESS, W.D3_DAY_ONLY)})
    new = ex.posthoc(ex.run("Debug", {"issue": W.ISSUE, "repo": W.REPO}))
    print(new.pretty())
    out.trace(v11, new, "run2_debug_v1_1")
    lib.record(new)

    print("\n统计层（可靠度后验）:")
    for ver in ("1.0", "1.1"):
        st = lib.stats("Debug", ver)
        print(f"  Debug v{ver}: 结果 {st.outcomes}，Post_safe {st.post_safe}")
        for node, beta in st.reliability.items():
            print(f"      {node}: {beta}")
    return lib


def ex3_interface_refinement(lib):
    title("例 3  接口精化与版本号：为什么改一个 skill 不会牵连别人（§8）")
    out = Figures()
    v10, v11 = lib.get("Debug", "1.0"), lib.get("Debug", "1.1")
    out.skill(v10, note="v1.0：Done 的守卫只看 rep，W8 证明不了。")
    out.skill(v11, note="v1.1：只改三条出边的守卫，接口不变，W8 可证。")
    print(check_refinement(v10, v11))

    def weaken(d):          # 设计文档 §11.4 的 δ4：把承诺削弱成"只保证复现测试通过"
        d["interface"]["post_safe"] = "outcome == done -> passes_repro(patch, issue, repo)"
        d["edges"][2]["guard"], d["edges"][3]["guard"], d["edges"][4]["guard"] = "rep", "!rep && k < 2", "!rep && k >= 2"

    print("\n尝试把「削弱了 Post」的版本当作 v1.2 入库:")
    try:
        lib.add(variant(v11, weaken, "1.2"))
    except GateError as ex:
        print(f"✗ {ex}")
    print("\n作为大版本 v2.0 入库是允许的:")
    v2 = variant(v11, weaken, "2.0")
    print(lib.add(v2))
    out.skill(v2, note="v2.0：削弱了 Post_safe，与 v1.x 接口不兼容，只能作为大版本发布。")

    coding = load("coding_v1_0")
    lib.add(coding)
    out.skill(coding, note="Coding v1.0：调用节点写的是 \"Debug >= 1.0\"。")
    call = coding.nodes["debug"]
    print(f"\nCoding 声明的是 \"{call.skill} >= {call.min_version}\"，现在解析到: "
          f"{lib.resolve(call.skill, call.min_version).key}（库里有 {[str(v) for v in lib.versions('Debug')]}）")
    print("→ 自动用上 v1.1，但不会被推到削弱了承诺的 v2.0。")


def ex4_coding_calls_debug(lib):
    title("例 4  Coding 调用 Debug：嵌套轨迹，以及「调用方依赖了不够强的承诺」（§3.3、§7.2）")
    out = Figures(lib.get)
    out.skill(lib.get("Coding", "1.0"), note="Coding v1.0：Debug 回来之后直接 done，没有重新验收。")
    inputs = {"spec": W.SPEC, "repo": W.REPO}
    scripts = lambda: {                                                         # noqa: E731
        "understand": [{"plan": "在 parse_date 里校验月份和日期"}],
        "implement": patches(W.D1_NOFIX),
        "locate": LOCATE,
        "write_patch": patches(W.D2_REGRESS, W.D3_DAY_ONLY, W.D4_CORRECT),
    }
    print(check_skill(lib.get("Coding", "1.0"), lib.resolve)["W8"].messages[0])
    ex = executor(lib, scripts())
    tr = ex.posthoc(ex.run("Coding", inputs))
    print(tr.pretty())
    print("→ Debug 兑现了自己的 Post（修好了报告里的 test_invalid_day，且不回归），"
          "但 Coding 的 Post_safe = ⊥：test_invalid_month 还挂着。\n"
          "  这不是 Debug 的责任，是 Coding 的流程漏洞——和入库时 W8 给出的预警一致。")
    out.trace(lib.get("Coding", "1.0"), tr, "coding_v1_0")
    lib.record(tr)

    print("\nCoding v1.1：Debug 回来之后重新验收（接口兼容，只有预算变了）:")
    print(lib.add(load("coding_v1_1")))
    out.skill(lib.get("Coding", "1.1"), note="Coding v1.1：Debug 回来之后回到 test 重新验收，带计数器 r。")
    ex = executor(lib, scripts())
    tr = ex.posthoc(ex.run("Coding", inputs))
    print(tr.pretty())
    out.trace(lib.get("Coding", "1.1"), tr, "coding_v1_1")
    lib.record(tr)


def ex5_kernel_guards(lib):
    title("例 5  内核把关：Reject、不变量、诚实地放弃（§5）")
    out = Figures(lib.get)
    debug = lib.get("Debug", "1.1")
    print("输入不在 Pre 范围内（issue 没有复现测试）→ 直接 Reject，不调用任何 Agent:")
    ex = executor(lib, {"locate": LOCATE, "write_patch": []})
    tr = ex.run(debug, {"issue": W.ISSUE_NO_REPRO, "repo": W.REPO})
    print(tr.pretty())
    out.trace(debug, tr, "reject_no_repro")

    print("\nAgent 想靠改测试过关（§11.4 的 δ3）→ 运行时不变量检查拦下，不交付任何输出:")
    ex = executor(lib, {"locate": LOCATE, "write_patch": patches(W.D_CHEAT)})
    tr = ex.run(debug, {"issue": W.ISSUE, "repo": W.REPO})
    print(tr.pretty())
    print(f"   交回的输出: {tr.outputs}")
    out.trace(debug, tr, "inv_violation_cheat")

    print("\n重试用完仍然修不好 → 诚实地 GiveUp（不违反安全性，只违反活性）:")
    ex = executor(lib, {"locate": LOCATE, "write_patch": patches(W.D1_NOFIX, W.D2_REGRESS, W.D2_REGRESS)})
    tr = ex.posthoc(ex.run(debug, {"issue": W.ISSUE, "repo": W.REPO}))
    print(tr.pretty())
    out.trace(debug, tr, "give_up_retries")


def ex6_semantic_branch_and_single_node():
    title("例 6  语义分支交给 Agent 节点（§4），以及单节点 skill（§13）")
    out = Figures()
    lib = SkillLibrary()
    rp, wd = load("reviewed_patch"), load("write_docstring")
    lib.add(rp)
    lib.add(wd)
    out.skill(rp, note="ReviewedPatch：verdict 由 review 节点给出，边只判定这个枚举值。")
    out.skill(wd, note="WriteDocstring：图退化成「一个 Agent + 一个终点」。")
    ex = executor(lib, {
        "implement": patches(W.D_MESSY, W.D4_CORRECT),
        "review": [{"verdict": "revise", "feedback": "删掉调试用的 print"}, {"verdict": "approve", "feedback": ""}],
        "write": [{"doc": "parse_date: 把 YYYY-MM-DD 解析成 (年, 月, 日)，非法日期抛 ValueError。"}],
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
    print(f"\nSVG 已写入: {OUTPUT}")


if __name__ == "__main__":
    main()
