"""授权 —— 需求 B：「经授权查询本人单据状态」

安全设计要点：
  1. **默认拒绝**：任何单据访问必须显式带 Principal，无身份一律拒绝。
  2. **越权不泄露存在性**：查别人的单，返回「查无此单」而不是「无权访问」。
     否则攻击者可用单据号枚举探测公司报销数据。
  3. **过滤在查询层**：`owner_id = 当前用户` 作为 SQL 条件，
     不是先查出来再在内存里过滤（后者一旦漏写就全量泄露）。
  4. **财务角色可跨人查**，但每次访问必须留痕（谁在什么时间查了谁的单）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Principal:
    """经 IM 渠道（企业微信 / 钉钉）认证后的用户身份"""
    user_id: str
    name: str = ""
    roles: set[str] = field(default_factory=lambda: {"employee"})
    channel: str = "wecom"          # wecom / dingtalk / web
    authed_at: str = ""

    @property
    def is_finance(self) -> bool:
        return "finance" in self.roles

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles


class AuthzError(Exception):
    """越权访问。对外统一表述为「查无此单」，不区分原因。"""


class NotAuthenticated(Exception):
    """未认证"""


def require_principal(p: Principal | None) -> Principal:
    if p is None:
        raise NotAuthenticated("未获取到用户身份，请重新从企业微信/钉钉进入")
    if not p.user_id:
        raise NotAuthenticated("用户身份为空")
    return p


def check_claim_access(p: Principal, owner_id: str) -> bool:
    """是否有权访问该单据。财务角色可跨人访问（留痕由调用方负责）。"""
    if p.user_id == owner_id:
        return True
    return p.is_finance or p.is_admin


def access_filter(p: Principal) -> tuple[str, tuple]:
    """返回查询用的 WHERE 片段与参数，强制下推到存储层"""
    if p.is_finance or p.is_admin:
        return "1=1", ()
    return "owner_id = ?", (p.user_id,)


def audit_read(p: Principal, owner_id: str, claim_id: str) -> dict:
    """跨人读取留痕 —— 财务查员工单据必须有记录可追溯"""
    return {
        "at": datetime.now().isoformat(timespec="seconds"),
        "actor": p.user_id,
        "actor_roles": sorted(p.roles),
        "channel": p.channel,
        "action": "read_claim",
        "target_owner": owner_id,
        "claim_id": claim_id,
        "cross_access": p.user_id != owner_id,
    }
