"""单据状态适配器 —— 需求 B 的数据来源抽象

青禾现状未定，因此这里定义统一接口，先给一个内存 Mock 实现跑通链路，
确认真实费控/ERP 后只需实现同一接口，上层 service 零改动。

接真实系统时要确认的三件事：
  1. 认证方式（OAuth / 内网 token / 数据库直连）
  2. 状态枚举映射：把对方系统的状态码映射到下面的 ClaimStatus
  3. 数据权限：对方接口若不做行级过滤，必须在本地再兜一层 owner_id 过滤
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ClaimStatus(str, Enum):
    """单据状态

    前四个沿用基座 schema（engine/schemas/expense-report.schema.json），
    后五个是青禾新增的流转态 —— 对应 engine/CHANGES.md 中登记的改造计划。
    """
    DRAFT = "draft"              # 草稿（基座已有）
    INCOMPLETE = "incomplete"    # 材料不全（基座已有）
    READY = "ready"              # 可提交（基座已有）
    SUBMITTED = "submitted"      # 已提交（基座已有）
    APPROVING = "approving"      # 审批中（新增）
    APPROVED = "approved"        # 审批通过（新增）
    REJECTED = "rejected"        # 已驳回（新增）
    PAYABLE = "payable"          # 待付款（新增）
    PAID = "paid"                # 已付款（新增）

    @property
    def label(self) -> str:
        return {
            "draft": "草稿", "incomplete": "材料不全", "ready": "待提交",
            "submitted": "已提交", "approving": "审批中", "approved": "已通过",
            "rejected": "已驳回", "payable": "待付款", "paid": "已付款",
        }[self.value]

    @property
    def terminal(self) -> bool:
        return self in (ClaimStatus.PAID, ClaimStatus.REJECTED)


@dataclass
class ClaimRecord:
    claim_id: str
    owner_id: str
    title: str
    amount: float
    status: str
    submitted_at: str = ""
    updated_at: str = ""
    current_approver: str = ""
    policy_ref: str = ""          # 判定时依据的制度版本
    reject_reason: str = ""
    pending_review: int = 0       # 待财务复核的事实项数量

    @property
    def status_label(self) -> str:
        try:
            return ClaimStatus(self.status).label
        except ValueError:
            return self.status

    def to_text(self) -> str:
        lines = [f"单号 {self.claim_id}｜{self.title}｜{self.amount:.2f} 元",
                 f"状态：{self.status_label}"]
        if self.submitted_at:
            lines.append(f"提交时间：{self.submitted_at}")
        if self.status == ClaimStatus.APPROVING.value and self.current_approver:
            lines.append(f"当前审批人：{self.current_approver}")
        if self.status == ClaimStatus.REJECTED.value and self.reject_reason:
            lines.append(f"驳回原因：{self.reject_reason}")
        if self.pending_review:
            lines.append(f"待财务复核事项：{self.pending_review} 项")
        if self.policy_ref:
            lines.append(f"判定依据：{self.policy_ref}")
        return "\n".join(lines)


class ClaimAdapter(ABC):
    """单据数据源接口"""

    @abstractmethod
    def fetch_by_owner(self, owner_id: str) -> list[ClaimRecord]:
        ...

    @abstractmethod
    def fetch_one(self, claim_id: str) -> ClaimRecord | None:
        ...


class MockClaimAdapter(ClaimAdapter):
    """内存实现，用于跑通链路与演示。生产环境请替换为真实适配器。"""

    def __init__(self, records: list[ClaimRecord] | None = None):
        self.records: dict[str, ClaimRecord] = {}
        for r in (records or self._default_records()):
            self.records[r.claim_id] = r

    @staticmethod
    def _default_records() -> list[ClaimRecord]:
        now = datetime.now().isoformat(timespec="seconds")
        return [
            ClaimRecord("RPT-2026-0001", "E001", "9月深圳客户交流", 3280.00,
                        "approving", "2026-10-08", now, "李经理",
                        "QH-EXP-001@v1", pending_review=2),
            ClaimRecord("RPT-2026-0002", "E001", "10月北京培训", 1560.00,
                        "approved", "2026-10-06", now, "", "QH-EXP-001@v1"),
            ClaimRecord("RPT-2026-0003", "E001", "9月招待客户", 890.00,
                        "rejected", "2026-10-05", now, "", "QH-EXP-001@v1",
                        reject_reason="缺少业务招待审批单，见 QH-EXP-001@v1 §2.4"),
            ClaimRecord("RPT-2026-0004", "E002", "10月上海出差", 5400.00,
                        "submitted", "2026-10-09", now, "", "QH-EXP-001@v1"),
        ]

    def fetch_by_owner(self, owner_id: str) -> list[ClaimRecord]:
        return [r for r in self.records.values() if r.owner_id == owner_id]

    def fetch_one(self, claim_id: str) -> ClaimRecord | None:
        return self.records.get(claim_id)


class HttpClaimAdapter(ClaimAdapter):
    """对接青禾现有费控 / ERP 的适配器骨架 —— 待实现

    实现清单：
      1. 在 __init__ 注入 base_url、认证凭据（从环境变量读，禁止硬编码）
      2. fetch_by_owner 调用对方接口时，必须带上 owner 参数做服务端过滤；
         若对方不支持，则在本地再按 owner_id 过滤一遍（双保险）
      3. 对方状态码 → ClaimStatus 的映射写在 _map_status，改这里即可
      4. 超时与失败要显式抛出，禁止静默返回空列表（会让员工误以为没单据）
    """
    STATUS_MAP = {
        "DRAFT": ClaimStatus.DRAFT, "SAVED": ClaimStatus.DRAFT,
        "SUBMIT": ClaimStatus.SUBMITTED, "AUDITING": ClaimStatus.APPROVING,
        "PASS": ClaimStatus.APPROVED, "REJECT": ClaimStatus.REJECTED,
        "PAYING": ClaimStatus.PAYABLE, "PAID": ClaimStatus.PAID,
    }

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _map_status(self, raw: str) -> ClaimStatus:
        return self.STATUS_MAP.get((raw or "").upper(), ClaimStatus.SUBMITTED)

    def fetch_by_owner(self, owner_id: str) -> list[ClaimRecord]:
        raise NotImplementedError(
            "待接入青禾费控系统。请先提供接口文档，或临时使用 MockClaimAdapter 跑通链路"
        )

    def fetch_one(self, claim_id: str) -> ClaimRecord | None:
        raise NotImplementedError("待接入青禾费控系统")
