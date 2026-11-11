"""Skill 库：版本管理、入库把关、统计层（设计文档 §1 统计层、§8.2）。

入库规则（可信内核的一部分）：
  1. 必须通过良构性检查 W0–W7 与预算检查；
  2. 小版本升级必须满足接口精化规则，否则只能发新的大版本；
  3. 预算有变化时，重新核对会自动用上新版本的调用方；
  4. 每个版本带一份证书。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import Skill, Version
from .refine import RefineReport, check_refinement
from .trace import TV, Trace
from .wellformed import Report, check_skill


class GateError(Exception):
    """入库被拒绝。"""

    def __init__(self, msg, report=None):
        super().__init__(msg)
        self.report = report


@dataclass
class Beta:
    a: float = 1.0
    b: float = 1.0

    @property
    def mean(self) -> float:
        return self.a / (self.a + self.b)

    def __str__(self):
        return f"Beta({self.a:g}, {self.b:g})  均值 {self.mean:.2f}"


@dataclass
class Stats:
    """统计层：不改变 skill 的含义，只记录它实际表现如何。"""
    reliability: dict = field(default_factory=dict)     # Agent / 调用节点 → 满足契约的 Beta 后验
    fault_models: dict = field(default_factory=dict)    # 节点 → 声明的故障模型
    history: list = field(default_factory=list)         # 历史轨迹 H
    outcomes: dict = field(default_factory=dict)        # 结果标签计数
    post_safe: Beta = field(default_factory=Beta)       # 整体可靠度：事后评估 Post_safe 的后验


@dataclass
class Certificate:
    skill: str
    wf: Report
    w8_proved: bool
    refines: RefineReport | None = None     # 相对于上一个版本的精化检查结果

    def __str__(self):
        s = f"证书 {self.skill}: 良构 ✓，规约一致性{'已证明' if self.w8_proved else '未证明'}"
        if self.refines:
            s += f"，相对 {self.refines.old} {'接口兼容' if self.refines.compatible else '接口不兼容（大版本）'}"
        return s


@dataclass
class Entry:
    skill: Skill
    certificate: Certificate
    stats: Stats


class SkillLibrary:
    def __init__(self):
        self._entries: dict[str, dict[Version, Entry]] = {}

    # ------------------------------------------------------------ 查询

    def versions(self, name: str) -> list[Version]:
        return sorted(self._entries.get(name, {}))

    def get(self, name: str, version) -> Skill:
        return self._entries[name][Version.parse(version)].skill

    def entry(self, name: str, version) -> Entry:
        return self._entries[name][Version.parse(version)]

    def stats(self, name: str, version) -> Stats:
        return self.entry(name, version).stats

    def resolve(self, name: str, min_version: Version | None = None) -> Skill | None:
        """解析 "name >= min_version"：同一个大版本内取最新的小版本。

        不指定下界时取全库最新版本。大版本之间接口不兼容，所以不会跨大版本自动升级。
        """
        vs = self.versions(name)
        if min_version is not None:
            vs = [v for v in vs if v.major == min_version.major and v >= min_version]
        return self._entries[name][vs[-1]].skill if vs else None

    def callers_of(self, name: str) -> list[Skill]:
        return [e.skill for d in self._entries.values() for e in d.values()
                if any(n.skill == name for n in e.skill.calls())]

    # ------------------------------------------------------------ 入库

    def add(self, skill: Skill) -> Certificate:
        have = self._entries.setdefault(skill.name, {})
        if skill.version in have:
            raise GateError(f"{skill.key} 已经在库里了")
        report = check_skill(skill, self.resolve)
        if not report.ok:
            raise GateError(f"{skill.key} 没通过良构性检查 {report.failed()}", report)
        older = [v for v in have if v < skill.version]
        prev = have[max(older)].skill if older else None
        refines = check_refinement(prev, skill) if prev else None
        if prev and prev.version.major == skill.version.major and not refines.compatible:
            raise GateError(f"{skill.key} 的接口不兼容 {prev.key}，只能作为大版本 "
                            f"v{skill.version.major + 1}.0 发布\n{refines}", refines)
        cert = Certificate(skill.key, report, report.w8, refines)
        stats = Stats(reliability={n.id: Beta(*n.prior) if n.kind == "agent" else Beta()
                                   for n in skill.nodes.values() if n.kind in ("agent", "call")},
                      fault_models={n.id: n.fault_model for n in skill.nodes.values() if n.fault_model})
        have[skill.version] = Entry(skill, cert, stats)
        if refines and refines.budget_changed:
            # 预算是唯一需要沿调用链往上核对的项
            for caller in self.callers_of(skill.name):
                r = check_skill(caller, self.resolve)
                if not r.ok:
                    del have[skill.version]
                    raise GateError(f"{skill.key} 的预算变化使调用方 {caller.key} 不再合法 {r.failed()}", r)
        return cert

    def add_all(self, skills) -> dict:
        """批量入库：自动把被调 skill 排在前面。返回 skill.key → 证书或 GateError。"""
        todo, out = sorted(skills, key=lambda s: (s.version, s.name)), {}
        while todo:
            ready = [s for s in todo if all(self.resolve(n.skill, n.min_version) for n in s.calls())] or list(todo)
            for s in ready:
                try:
                    out[s.key] = self.add(s)
                except GateError as ex:
                    out[s.key] = ex
                todo.remove(s)
        return out

    # ------------------------------------------------------------ 统计层更新

    def record(self, tr: Trace):
        """把一次运行（最好已做过事后评估）记入统计层：更新可靠度后验，保存轨迹。"""
        if tr.status == "frontier":
            return
        st = self.stats(tr.skill, tr.version)
        st.history.append(tr)
        st.outcomes[tr.outcome] = st.outcomes.get(tr.outcome, 0) + 1
        if tr.post_eval and tr.outcome == "done":
            self._bump(st.post_safe, tr.post_eval["post_safe"])
        for s in tr.steps:
            if s.node in st.reliability:
                self._bump(st.reliability[s.node], s.contract_post)
            if s.sub_trace:
                self.record(s.sub_trace)

    @staticmethod
    def _bump(beta: Beta, tv):
        if tv is TV.T:
            beta.a += 1
        elif tv is TV.F:
            beta.b += 1         # ? 不更新
