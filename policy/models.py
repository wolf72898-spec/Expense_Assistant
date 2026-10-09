"""制度中枢 · 数据模型与状态机

设计要点（财务内控要求，代码中强制）：
  1. 制度必须经审批才能发布，且创建人与审批人不得为同一人（职责分离）
  2. 同一制度同时只允许一个「已发布且生效中」的版本，发布新版自动作废旧版
  3. 已发布版本不可变，改制度只能发新版本（保证历史单据可追溯到当时的制度）
  4. 任何操作必须留痕，字段级 diff 可审计

青禾原创实现，未复制任何上游源码。数据格式兼容基座 policy.json（见 registry.py）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from enum import Enum
from typing import Any


class PolicyStatus(str, Enum):
    """制度版本状态机

    draft ──submit──▶ pending ──approve──▶ published ──(新版发布)──▶ superseded
       │                  │                                              ▲
       └──update──────────┘                                              │
                    └──reject──▶ draft                          archived ─┘
    """
    DRAFT = "draft"              # 草稿，可编辑
    PENDING = "pending"          # 待审批，锁定不可编辑
    PUBLISHED = "published"      # 已发布生效
    SUPERSEDED = "superseded"    # 被新版取代，仅供历史追溯
    ARCHIVED = "archived"        # 归档（制度废止）


class ActorRole(str, Enum):
    ADMIN = "admin"        # 制度管理员：可起草、提交
    FINANCE = "finance"    # 财务负责人：可审批、发布
    SYSTEM = "system"      # 系统自动操作


class Op(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    SUBMIT = "submit"
    APPROVE = "approve"
    REJECT = "reject"
    PUBLISH = "publish"
    SUPERSEDE = "supersede"
    ARCHIVE = "archive"


# 允许的状态迁移
_TRANSITIONS: dict[PolicyStatus, set[PolicyStatus]] = {
    PolicyStatus.DRAFT: {PolicyStatus.PENDING, PolicyStatus.ARCHIVED},
    PolicyStatus.PENDING: {PolicyStatus.PUBLISHED, PolicyStatus.DRAFT},  # 驳回回草稿
    PolicyStatus.PUBLISHED: {PolicyStatus.SUPERSEDED, PolicyStatus.ARCHIVED},
    PolicyStatus.SUPERSEDED: {PolicyStatus.ARCHIVED},
    PolicyStatus.ARCHIVED: set(),
}


class PolicyError(Exception):
    """制度操作被拒绝（权限、状态机、内控规则）"""


def _can_transit(frm: PolicyStatus, to: PolicyStatus) -> bool:
    return to in _TRANSITIONS.get(frm, set())


@dataclass
class PolicyVersion:
    """一个制度版本（不可变快照）"""
    policy_id: str
    version: int
    title: str
    content: dict                      # 制度正文，兼容基座 policy.json 格式
    effective_from: str                # ISO date，生效起始日
    effective_to: str | None = None    # ISO date，None = 长期有效
    status: str = PolicyStatus.DRAFT.value
    summary: str = ""                  # 本次变更说明
    created_by: str = ""
    created_at: str = ""
    approved_by: str | None = None
    approved_at: str | None = None
    published_at: str | None = None

    @property
    def ref(self) -> str:
        """制度引用标识，用于问答引用与单据留痕，如 QH-EXP-003@v2"""
        return f"{self.policy_id}@v{self.version}"

    @property
    def status_enum(self) -> PolicyStatus:
        return PolicyStatus(self.status)

    def is_effective_on(self, day: str | None = None) -> bool:
        """判断该版本在指定日期是否生效中"""
        if self.status != PolicyStatus.PUBLISHED.value:
            return False
        day = day or date.today().isoformat()
        if day < self.effective_from:
            return False
        if self.effective_to and day > self.effective_to:
            return False
        return True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PolicyClause:
    """条款级切片 —— 制度问答（需求 A）的检索单元

    制度必须切到条款粒度，问答才能给出「依据第几条」的精确引用，
    而不是整篇制度丢给模型。
    """
    clause_id: str
    policy_id: str
    version: int
    clause_no: str            # 条款号，如 "3.2.1"
    title: str
    text: str
    category: str = ""        # 对应费用类目：lodging / meals / transport.taxi ...
    tags: list[str] = field(default_factory=list)

    def citation(self) -> str:
        """生成可展示的引用串，如：QH-EXP-003@v2 §3.2.1 住宿标准"""
        return f"{self.policy_id}@v{self.version} §{self.clause_no} {self.title}".strip()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tags"] = json.dumps(self.tags, ensure_ascii=False)
        return d


@dataclass
class PolicyChange:
    """变更审计记录（不可删改）"""
    policy_id: str
    version: int
    op: str
    actor: str
    actor_role: str
    at: str
    detail: dict = field(default_factory=dict)   # 字段级 diff / 操作说明

    def to_dict(self) -> dict:
        d = asdict(self)
        d["detail"] = json.dumps(self.detail, ensure_ascii=False)
        return d


def diff_content(old: dict, new: dict, path: str = "") -> list[dict]:
    """递归比对两版制度内容，产出字段级差异清单，用于审批与审计。"""
    changes: list[dict] = []
    for key in sorted(set(old) | set(new)):
        p = f"{path}.{key}" if path else key
        ov, nv = old.get(key), new.get(key)
        if isinstance(ov, dict) and isinstance(nv, dict):
            changes.extend(diff_content(ov, nv, p))
        elif ov != nv:
            changes.append({"field": p, "from": ov, "to": nv})
    return changes


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ─────────────────────────── 内控校验 ───────────────────────────

def assert_can_edit(pv: PolicyVersion) -> None:
    if pv.status_enum is not PolicyStatus.DRAFT:
        raise PolicyError(
            f"制度 {pv.ref} 当前状态为 {pv.status}，不可编辑。"
            f"已发布制度不可直接修改，请创建新版本后重新审批发布。"
        )


def assert_can_approve(pv: PolicyVersion, approver: str) -> None:
    if pv.status_enum is not PolicyStatus.PENDING:
        raise PolicyError(f"制度 {pv.ref} 状态为 {pv.status}，不在待审批状态")
    if approver and approver == pv.created_by:
        raise PolicyError(
            f"职责分离要求：审批人不得为起草人（{approver}）。请由另一位财务负责人审批。"
        )


def assert_transition(pv: PolicyVersion, to: PolicyStatus) -> None:
    if not _can_transit(pv.status_enum, to):
        raise PolicyError(
            f"非法状态迁移：{pv.status} → {to.value}（制度 {pv.ref}）"
        )
