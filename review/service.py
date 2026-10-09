"""财务复核 —— 需求 D：业务事实由财务助手核对

核心原则（建议写进项目 README 第一条）：
> **AI 负责引制度、做初判、标来源；事实认定权与付款决定权归财务岗。**
> AI 的输出是「建议」，不是「结论」。

这一层的技术抓手是基座已有的 **字段级溯源（FieldProvenance）**：
引擎给每个字段打了来源标签，本模块按来源分风险等级，
低可信来源的事实一律进人工复核队列，确认后转为 finance_confirmed。

溯源等级（对应 engine/schemas/expense-report.schema.json 的 method 枚举）：
    xml / ofd_structured  权威  → 直接采信
    user_stated           员工自述 → 抽查
    pdf_text              文档抽取 → 复核
    ocr                   图像识别 → 复核
    inferred              模型推断 → 必须复核，且 AI 不得作为最终依据
    policy_default        制度默认 → 复核
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

# ───────────────── 溯源 → 风险等级 ─────────────────

class Trust(str, Enum):
    AUTHORITATIVE = "authoritative"   # 结构化电子票据，可直接采信
    STATED = "stated"                 # 员工自述
    EXTRACTED = "extracted"           # PDF/图像抽取
    INFERRED = "inferred"             # 模型推断/制度默认值 —— 最高风险


PROVENANCE_TRUST: dict[str, Trust] = {
    "xml": Trust.AUTHORITATIVE,
    "ofd_structured": Trust.AUTHORITATIVE,
    "user_stated": Trust.STATED,
    "user_confirmed": Trust.STATED,
    "pdf_text": Trust.EXTRACTED,
    "ocr": Trust.EXTRACTED,
    "inferred": Trust.INFERRED,
    "policy_default": Trust.INFERRED,
}

# 必须经财务复核才能进入付款环节的来源
REQUIRES_REVIEW = {Trust.EXTRACTED, Trust.INFERRED}


class ReviewStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


@dataclass
class FactItem:
    """一条待核实的业务事实"""
    fact_id: str
    claim_id: str
    item_id: str = ""
    field: str = ""                 # 如 amount / merchant / invoice_no
    value: str = ""
    provenance: str = "inferred"    # 来自引擎的溯源标签
    confidence: float = 0.0
    policy_ref: str = ""            # 涉及的制度条款引用
    status: str = ReviewStatus.PENDING.value
    reviewer: str = ""
    reviewed_at: str = ""
    comment: str = ""

    @property
    def trust(self) -> Trust:
        return PROVENANCE_TRUST.get(self.provenance, Trust.INFERRED)

    @property
    def needs_review(self) -> bool:
        return self.trust in REQUIRES_REVIEW

    def risk_score(self) -> float:
        """队列排序用：来源越不可信、置信度越低，排越前"""
        base = {Trust.AUTHORITATIVE: 0.0, Trust.STATED: 0.3,
                Trust.EXTRACTED: 0.6, Trust.INFERRED: 1.0}[self.trust]
        return round(base * 2 + (1.0 - self.confidence), 4)


class ReviewError(Exception):
    pass


class ReviewService:
    def __init__(self, path: str | Path = "data/review/facts.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.facts: dict[str, FactItem] = {}
        self._log_path = self.path.parent / "review_log.jsonl"
        if self.path.exists():
            for d in json.loads(self.path.read_text(encoding="utf-8")):
                f = FactItem(**d)
                self.facts[f.fact_id] = f

    # ───────────────── 入队 ─────────────────

    def ingest(self, facts: list[FactItem]) -> int:
        """从引擎产出的报销包导入待核事实。只收需要人工核的。"""
        n = 0
        for f in facts:
            if f.needs_review and f.fact_id not in self.facts:
                self.facts[f.fact_id] = f
                n += 1
        self._save()
        return n

    # ───────────────── 队列 ─────────────────

    def queue(self, claim_id: str | None = None,
              only_pending: bool = True) -> list[FactItem]:
        items = [f for f in self.facts.values()
                 if (claim_id is None or f.claim_id == claim_id)]
        if only_pending:
            items = [f for f in items if f.status == ReviewStatus.PENDING.value]
        return sorted(items, key=lambda f: -f.risk_score())

    # ───────────────── 复核动作 ─────────────────

    def confirm(self, fact_id: str, reviewer: str, comment: str = "") -> FactItem:
        """财务确认该事实为真。确认后溯源标签转为 finance_confirmed。"""
        f = self.facts.get(fact_id)
        if f is None:
            raise ReviewError(f"待核事实不存在：{fact_id}")
        if f.status != ReviewStatus.PENDING.value:
            raise ReviewError(f"该事实已处理（{f.status}），不可重复确认")
        f.status = ReviewStatus.CONFIRMED.value
        f.reviewer = reviewer
        f.reviewed_at = datetime.now().isoformat(timespec="seconds")
        f.comment = comment
        f.provenance = "finance_confirmed"
        self._save()
        self._log("confirm", f, reviewer, comment)
        return f

    def reject(self, fact_id: str, reviewer: str, reason: str) -> FactItem:
        if not reason.strip():
            raise ReviewError("驳回必须填写理由，理由将回传给员工")
        f = self.facts.get(fact_id)
        if f is None:
            raise ReviewError(f"待核事实不存在：{fact_id}")
        f.status = ReviewStatus.REJECTED.value
        f.reviewer = reviewer
        f.reviewed_at = datetime.now().isoformat(timespec="seconds")
        f.comment = reason
        self._save()
        self._log("reject", f, reviewer, reason)
        return f

    # ───────────────── 付款闸门（硬规则） ─────────────────

    def can_advance_to_payable(self, claim_id: str) -> tuple[bool, str]:
        """未复核完的事实项一律阻断付款。

        这是「业务事实由财务助手核对」在代码层面的硬闸门 ——
        不是提示，不是建议，是拒绝。
        """
        pending = [f for f in self.facts.values()
                   if f.claim_id == claim_id
                   and f.status == ReviewStatus.PENDING.value
                   and f.needs_review]
        rejected = [f for f in self.facts.values()
                    if f.claim_id == claim_id
                    and f.status == ReviewStatus.REJECTED.value]
        if rejected:
            return False, f"存在 {len(rejected)} 项被财务驳回的事实，单据不可进入付款"
        if pending:
            return False, (f"仍有 {len(pending)} 项业务事实未经财务复核，"
                           f"单据不得进入付款环节")
        return True, "全部业务事实已由财务复核确认"

    # ───────────────── 持久化与留痕 ─────────────────

    def _save(self) -> None:
        self.path.write_text(
            json.dumps([f.__dict__ for f in self.facts.values()],
                       ensure_ascii=False, indent=2), encoding="utf-8")

    def _log(self, op: str, f: FactItem, actor: str, comment: str) -> None:
        rec = {"at": datetime.now().isoformat(timespec="seconds"),
               "op": op, "actor": actor, "fact_id": f.fact_id,
               "claim_id": f.claim_id, "field": f.field, "value": f.value,
               "provenance": f.provenance, "comment": comment}
        with self._log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ───────────────── 展示 ─────────────────

    @staticmethod
    def item_text(f: FactItem) -> str:
        trust_cn = {Trust.AUTHORITATIVE: "权威", Trust.STATED: "员工自述",
                    Trust.EXTRACTED: "文档抽取", Trust.INFERRED: "模型推断"}[f.trust]
        return (f"[{f.fact_id}] {f.claim_id} · {f.field} = {f.value}\n"
                f"     来源：{f.provenance}（{trust_cn}）"
                f"｜置信度 {f.confidence:.2f}｜风险 {f.risk_score()}"
                + (f"\n     制度：{f.policy_ref}" if f.policy_ref else ""))
