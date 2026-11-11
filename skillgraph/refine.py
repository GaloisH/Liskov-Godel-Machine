"""接口精化规则（设计文档 §8.1）：新版本的接口是否"不比旧版本差"。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .expr import ExprError, show
from .logic import entails, fmt_cex


@dataclass
class RefineReport:
    old: str
    new: str
    items: list = field(default_factory=list)      # (字段, 是否满足, 说明)
    budget_changed: bool = False

    @property
    def compatible(self) -> bool:
        return all(ok for _, ok, _ in self.items)

    def __str__(self) -> str:
        lines = [f"{self.old} → {self.new}: {'接口兼容' if self.compatible else '接口不兼容'}"
                 + ("（预算有变化，需要核对调用方）" if self.budget_changed else "")]
        lines += [f"  {'✓' if ok else '✗'} {name}: {msg}" for name, ok, msg in self.items]
        return "\n".join(lines)


def _implies(premises, goal, types):
    try:
        cex = entails(premises, goal, types)
    except ExprError as ex:
        return False, str(ex)
    return cex is None, "" if cex is None else f"反例: {fmt_cex(cex)}"


def check_refinement(old, new) -> RefineReport:
    """old、new 是同一个 skill 的两个版本。"""
    a, b = old.interface, new.interface
    types = {**old.types, **new.types, **a.types, **b.types}
    r = RefineReport(old.key, new.key)

    ok, why = _implies([a.pre], b.pre, types)
    r.items.append(("Pre 只能放宽", ok, "旧 Pre 蕴含新 Pre" if ok else f"存在旧版本接受、新版本不接受的输入；{why}"))

    ok, why = _implies([b.post_safe], a.post_safe, types)
    r.items.append(("Post_safe 只能加强", ok, "新 Post 蕴含旧 Post" if ok else
                    f"新版本不再保证 {show(a.post_safe)}；{why}"))

    lost = [show(i) for i in a.inv if not _implies(b.inv, i, types)[0]]
    r.items.append(("Inv 只能加强", not lost, "旧不变量全部保留" if not lost else f"丢失了不变量 {lost}"))

    bad = [k for k, t in a.inputs.items() if b.inputs.get(k) != t]
    bad += [k for k in b.inputs if k not in a.inputs and k not in b.defaults]
    r.items.append(("X_in 不能增加必填项", not bad, "输入不变（或只增加了带默认值的输入）" if not bad
                    else f"输入 {bad} 被删除、改了类型，或是新增的必填项"))

    bad = [k for k, t in a.outputs.items() if b.outputs.get(k) != t]
    r.items.append(("X_out 只能增加", not bad, "旧输出全部保留" if not bad else f"输出 {bad} 被删除或改了类型"))

    r.budget_changed = a.budget != b.budget
    return r
