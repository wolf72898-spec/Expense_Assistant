"""制度问答服务 —— 需求 A：自助查询有效制度

权限：制度对全员公开，无需特别授权。
（需求 B 的「本人单据」才需要授权，见 claim/）

三条不可妥协的规则：
  1. **检索不到就说检索不到**，转财务人工。宁可答"制度未覆盖"，不可编造条款。
  2. **答案必须带制度引用**（制度号@版本 §条款号），让员工可自行核对。
  3. **金额一律由计算器算出**，模型只负责组织语言，不做算术。

第 1、3 条的设计参考了 zsitvat/Reimbursement-RAG-assistant 与
AMANxSHARMA01/auditable-expense-agent 的公开理念（两者均无 License，未引用其代码）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from policy.registry import PolicyRegistry
from policy.store import PolicyStore

from .calculator import CapCalculator, CalcResult
from .retriever import ClauseRetriever, ScoredClause

# 低于该相关度即视为「制度未覆盖」，拒绝回答
RELEVANCE_FLOOR = 0.35

# 类目别名 → 制度类目键
CATEGORY_ALIASES = {
    "住宿": "lodging", "酒店": "lodging", "房费": "lodging", "宾馆": "lodging",
    "住": "lodging",
    "餐费": "meals", "吃饭": "meals", "用餐": "meals", "餐饮": "meals", "招待用餐": "meals",
    "打车": "transport.taxi", "出租车": "transport.taxi", "市内交通": "transport.taxi",
    "滴滴": "transport.taxi", "网约车": "transport.taxi",
    "机票": "transport.air", "飞机": "transport.air",
    "高铁": "transport.rail", "火车": "transport.rail",
    "招待": "entertainment", "业务招待": "entertainment",
    "会议": "conference", "培训": "conference",
    "办公用品": "office_supplies", "通讯": "communication",
}

# 明确不在助手作用域的话题，直接转人工
OUT_OF_SCOPE = [
    (r"个税|个人所得税|汇算|专项附加", "个人所得税计算不在报销助手服务范围内，请咨询 HR 或税务同事。"),
    (r"劳动法|仲裁|起诉|合同违约", "法律问题不在报销助手服务范围内，请咨询法务。"),
    (r"工资|薪资|调薪|绩效", "薪酬相关问题不在报销助手服务范围内，请咨询 HR。"),
]


@dataclass
class Answer:
    question: str
    text: str
    citations: list[str] = field(default_factory=list)
    calculation: CalcResult | None = None
    escalated: bool = False          # True = 已转人工
    escalation_reason: str = ""
    effective_refs: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        lines = [self.text]
        if self.citations:
            lines.append("\n依据：" + "\n     ".join(self.citations))
        if self.escalated:
            lines.append(f"\n⚠️ {self.escalation_reason}")
        return "\n".join(lines)


class PolicyQAService:
    def __init__(self, store: PolicyStore, registry: PolicyRegistry | None = None):
        self.store = store
        self.registry = registry or PolicyRegistry(store)
        self.retriever = ClauseRetriever(store)
        self.calc = CapCalculator(self.registry)

    # ─────────────────────── 主入口 ───────────────────────

    def answer(self, question: str, day: str | None = None) -> Answer:
        day = day or date.today().isoformat()

        # 1. 越界检查
        for pattern, msg in OUT_OF_SCOPE:
            if re.search(pattern, question):
                return Answer(question, msg, escalated=True,
                              escalation_reason="已超出报销助手服务范围，转人工处理")

        # 2. 生效制度存在性检查
        refs = self.registry.effective_refs(day)
        if not refs:
            return Answer(question, "", escalated=True,
                          escalation_reason="当前无已发布生效的报销制度，请联系财务确认制度发布状态")

        # 3. 算钱类问题 → 走确定性计算器
        calc_req = self._parse_calc(question)
        if calc_req:
            try:
                r = self.calc.calc(day=day, **calc_req)
                cites = [c["citation"] for c in
                         self.registry.clause_citations(r.category, day)]
                return Answer(question, r.to_text(), citations=cites,
                              calculation=r, effective_refs=refs)
            except KeyError as e:
                return Answer(question, "", escalated=True,
                              escalation_reason=f"制度中未定义该类目（{e}），转财务人工确认")

        # 4. 制度咨询 → 条款检索（只取该日期生效版本的条款）
        hits = self.retriever.search(question, top_k=3, day=day)
        if not hits or hits[0].score < RELEVANCE_FLOOR:
            return Answer(
                question,
                "我没有在现行制度中查到与您问题相关的条款。",
                escalated=True,
                escalation_reason="制度未覆盖此问题，请咨询财务同事确认，我无法代为答复",
                effective_refs=refs,
            )

        top = hits[0]
        cites = [h.citation for h in hits]
        text = self._compose(top, hits, question)
        return Answer(question, text, citations=cites, effective_refs=refs)

    # ─────────────────────── 组装 ───────────────────────

    def _compose(self, top: ScoredClause, hits: list[ScoredClause],
                 question: str) -> str:
        c = top.clause
        parts = [f"{c.title}：{c.text}"]
        if len(hits) > 1:
            others = "、".join(h.clause.title for h in hits[1:])
            parts.append(f"\n相关条款：{others}")
        parts.append(f"\n（制度版本 {c.policy_id}@v{c.version}，"
                     f"如与财务口径不一致，以财务解释为准）")
        return "\n".join(parts)

    def _parse_calc(self, question: str) -> dict | None:
        """把「北京住3晚能报多少」这类问题解析成计算器入参。

        命中条件：识别到费用类目 + 至少一个数字。
        解析不出来就交给条款检索，不硬算。
        """
        q = question
        category = None
        for alias, cat in CATEGORY_ALIASES.items():
            if alias in q:
                category = cat
                break
        if not category:
            return None

        numbers = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", q)]
        if not numbers:
            return None          # 没有数字 → 交给条款检索

        # 金额优先认「带单位」的：900元 / 900块
        amount: float | None = None
        m_amount = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块|rmb|RMB)", q)
        if m_amount:
            amount = float(m_amount.group(1))

        # 计量单位认「带量词」的：3晚 / 4人 / 2天
        units = 1.0
        m_units = re.search(r"(\d+(?:\.\d+)?)\s*(?:晚|夜|人|天|次|笔|张|个)", q)
        if m_units:
            units = float(m_units.group(1))

        if amount is None:
            rest = [n for n in numbers
                    if m_units is None or abs(n - units) > 1e-9]
            # 只有一个数字才敢当成金额；多个数字又都没单位，说明语义不清，交给检索
            if len(rest) != 1:
                return None
            amount = rest[0]

        city = None
        for c in ("北京", "上海", "深圳", "广州", "杭州", "成都"):
            if c in q:
                city = c
                break

        return {"category": category, "amount": amount,
                "per_units": units, "city": city}
