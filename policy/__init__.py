"""青禾报销助手 · 制度中枢

需求 C：制度维护 —— 制度的起草、审批、发布、版本管理与变更审计。
同时是需求 A（制度问答）与需求 D（财务核对）的唯一事实源。

典型流程：
    store = PolicyStore("data/policy.db")
    store.create("QH-EXP-001", "差旅费报销制度", content, "2026-01-01", actor="张三")
    store.submit("QH-EXP-001", 1, actor="张三")
    store.approve("QH-EXP-001", 1, approver="李四")     # 起草人与审批人必须不同

    registry = PolicyRegistry(store)
    registry.export("data/compiled/policy.json")         # 编译给引擎
"""
from .models import (
    ActorRole, Op, PolicyChange, PolicyClause, PolicyError, PolicyStatus,
    PolicyVersion, diff_content,
)
from .registry import PolicyRegistry
from .store import PolicyStore

__all__ = [
    "ActorRole", "Op", "PolicyChange", "PolicyClause", "PolicyError",
    "PolicyStatus", "PolicyVersion", "diff_content",
    "PolicyRegistry", "PolicyStore",
]
