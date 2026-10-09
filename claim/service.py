"""单据查询服务 —— 需求 B

只回答两件事：
  1. 我有哪些单？（列表）
  2. 我的某一单现在什么状态？（详情）

明确不做的：不帮员工查同事的单，不给单据真实性结论（那是需求 D 财务复核的事）。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .adapter import ClaimAdapter, ClaimRecord, ClaimStatus, MockClaimAdapter
from .authz import Principal, audit_read, check_claim_access, require_principal

# 越权时的统一对外表述：不透露单据是否存在
NOT_FOUND = "查无此单，或该单不在您名下。如需帮助请联系财务。"


class ClaimService:
    def __init__(self, adapter: ClaimAdapter | None = None,
                 audit_path: str | Path = "data/audit/claim_access.jsonl"):
        self.adapter = adapter or MockClaimAdapter()
        self.audit_path = Path(audit_path)
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)

    def _audit(self, p: Principal, owner_id: str, claim_id: str) -> None:
        rec = audit_read(p, owner_id, claim_id)
        with self.audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def my_claims(self, principal: Principal | None) -> list[ClaimRecord]:
        """我的单据列表。非财务角色只能看到 owner_id = 自己的单。"""
        p = require_principal(principal)
        if p.is_finance or p.is_admin:
            self._audit(p, "*", "*")
            # 财务角色走全量视图，但其访问本身已被留痕
            all_ids = getattr(self.adapter, "records", None)
            if isinstance(all_ids, dict):
                return list(all_ids.values())
            return []
        return self.adapter.fetch_by_owner(p.user_id)

    def my_claim(self, principal: Principal | None, claim_id: str) -> ClaimRecord:
        """我的单据详情。越权一律按「查无此单」处理。"""
        p = require_principal(principal)
        rec = self.adapter.fetch_one(claim_id)
        if rec is None:
            raise LookupError(NOT_FOUND)
        if not check_claim_access(p, rec.owner_id):
            # 不记录 target_owner 之外的信息，避免日志本身泄露
            self._audit(p, "<unauthorized>", claim_id)
            raise LookupError(NOT_FOUND)
        self._audit(p, rec.owner_id, claim_id)
        return rec

    @staticmethod
    def list_text(records: list[ClaimRecord]) -> str:
        if not records:
            return "您当前没有报销单据。"
        lines = [f"共 {len(records)} 张单："]
        for r in records:
            flag = " ⚠️待复核" if r.pending_review else ""
            lines.append(f"  · {r.claim_id}  {r.title}  "
                         f"{r.amount:.2f}元  [{r.status_label}]{flag}")
        return "\n".join(lines)

    @staticmethod
    def status_summary(records: list[ClaimRecord]) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in records:
            out[r.status_label] = out.get(r.status_label, 0) + 1
        return out


__all__ = ["ClaimService", "ClaimRecord", "ClaimStatus", "Principal",
           "MockClaimAdapter", "NOT_FOUND"]
