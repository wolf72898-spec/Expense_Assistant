"""确定性限额计算器 —— 需求 A 的「算钱」部分

原则：**任何金额计算必须由代码完成，绝不让模型心算。**
这是从 auditable-expense-agent（无 License，仅借鉴理念）学到的关键一条：
模型适合引制度、做解释；算术一旦交给模型，就会出现看似合理的错数。

计算器直接读取制度中枢编译后的 cap 配置，保证与制度同源。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from policy.registry import PolicyRegistry


@dataclass
class CalcResult:
    category: str
    requested: float
    reimbursable: float
    over_cap: bool
    basis: str            # 人类可读的计算依据
    policy_ref: str       # 依据的制度引用
    mode: str             # clip=截断 warn=提示 warn_only=不限制

    def to_text(self) -> str:
        head = f"可报 {self.reimbursable:.2f} 元"
        if self.over_cap:
            head += f"（申报 {self.requested:.2f} 元，超出标准 {self.requested - self.reimbursable:.2f} 元）"
        return f"{head}\n依据：{self.basis}\n制度：{self.policy_ref}"


class CapCalculator:
    def __init__(self, registry: PolicyRegistry):
        self.registry = registry

    def _policy(self, day: str):
        return self.registry.compile(day)

    def calc(self, category: str, amount: float, day: str | None = None,
             per_units: float = 1.0, city: str | None = None) -> CalcResult:
        """按制度限额计算可报金额

        category   费用类目（lodging / meals / transport.taxi ...）
        amount     申报金额
        per_units  计量单位数（住宿=夜数，餐费=人数，市内交通=笔数）
        city       城市，用于分城市标准（住宿）
        """
        day = day or date.today().isoformat()
        policy = self._policy(day)
        cats = policy.get("categories", {})
        if category not in cats:
            raise KeyError(f"制度中不存在费用类目：{category}")

        spec = cats[category]
        cap = spec.get("cap") or {}
        limit = float(cap.get("amount") or 0)
        mode = cap.get("mode", "warn")
        per = cap.get("per", "item")
        units = max(float(per_units), 1.0)
        label = spec.get("label", category)

        refs = self.registry.effective_refs(day)
        policy_ref = refs[0] if refs else "（无生效制度）"

        # 分城市标准
        if city and cap.get("by_city"):
            limit = float(cap["by_city"].get(city, limit))
            basis_city = f"{city}标准 {limit:.0f} 元/{self._per_label(per)}"
        else:
            basis_city = f"{limit:.0f} 元/{self._per_label(per)}"

        if limit <= 0:
            return CalcResult(category, amount, amount, False,
                              f"{label}：制度未设定金额上限（{basis_city}）",
                              policy_ref, mode)

        ceiling = limit * (units if per in ("night", "person", "day") else 1.0)
        over = amount > ceiling + 1e-9

        if mode == "clip" and over:
            reimb = ceiling
            basis = (f"{label}：{basis_city}，{units:.0f} {self._per_label(per)}"
                     f"× {limit:.0f} = 上限 {ceiling:.0f} 元，超出部分按上限报销")
        else:
            reimb = amount
            basis = (f"{label}：{basis_city}，"
                     f"{units:.0f} {self._per_label(per)}× {limit:.0f} = 上限 {ceiling:.0f} 元，"
                     f"申报 {amount:.0f} 元" + ("，超出标准需补充说明" if over else "，未超标"))

        return CalcResult(category, amount, reimb, over, basis, policy_ref, mode)

    @staticmethod
    def _per_label(per: str) -> str:
        return {"night": "晚", "person": "人", "day": "天",
                "item": "笔", "trip": "次"}.get(per, "笔")
