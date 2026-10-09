"""IM 机器人意图路由 —— 企业微信 / 钉钉 入口

路由用规则匹配而非模型分类，理由：
  意图判断错了会直接走错权限分支（比如把"查制度"判成"查单据"），
  财务场景下这种错误的代价不对等。规则可测试、可审计、零成本；
  模型分类留作规则未命中时的兜底。

权限门在这里第一道拦截：财务/管理员能力在进入业务逻辑前就校验角色。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from claim.authz import Principal


class Intent(str, Enum):
    POLICY_QA = "policy_qa"          # 需求 A：问制度
    CLAIM_LIST = "claim_list"        # 需求 B：我的单据列表
    CLAIM_STATUS = "claim_status"    # 需求 B：某单状态
    REVIEW_QUEUE = "review_queue"    # 需求 D：财务待核队列（需 finance）
    REVIEW_CONFIRM = "review_confirm"  # 需求 D：财务确认（需 finance）
    POLICY_ADMIN = "policy_admin"    # 需求 C：制度维护（需 admin/finance）
    HELP = "help"
    UNKNOWN = "unknown"


# 需要特定角色的意图 —— 进入业务逻辑前强制校验
ROLE_GATE: dict[Intent, set[str]] = {
    Intent.REVIEW_QUEUE: {"finance"},
    Intent.REVIEW_CONFIRM: {"finance"},
    Intent.POLICY_ADMIN: {"admin", "finance"},
}

# 顺序即优先级：更具体的模式放前面
RULES: list[tuple[str, Intent]] = [
    (r"(待核|待复核|复核队列|核实清单)", Intent.REVIEW_QUEUE),
    (r"(确认|核实|通过|驳回)\s*(事实|F\d+|该笔)", Intent.REVIEW_CONFIRM),
    (r"(发布|修订|起草|审批)\s*制度|制度(版本|草稿|审批)", Intent.POLICY_ADMIN),
    (r"帮助|怎么用|能做什么|help", Intent.HELP),
    # 列表要先于状态匹配：一句「我的报销单」是列表诉求，不是查某单状态
    (r"(我的|我有).*(报销)?单(有哪些|列表)?$|多少(张)?单|单据列表|"
     r"(我的|查).*(报销)?单(子)?(列表|有哪些)", Intent.CLAIM_LIST),
    (r"单据状态|什么状态|进度|到哪一步|审批到谁|打款|付款了吗|退回|驳回原因",
     Intent.CLAIM_STATUS),
    (r"(能|可以|能不|可不).*(报|报销|报吗)|标准|限额|上限|多少(钱|元)|"
     r"怎么报|需要什么(材料|票据|发票)|制度", Intent.POLICY_QA),
]

CLAIM_ID_RE = re.compile(r"(RPT-\d{4}-\d{4})", re.I)


@dataclass
class Route:
    intent: Intent
    claim_id: str | None = None
    denied: bool = False
    deny_reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.denied


def route(text: str, principal: Principal | None) -> Route:
    t = (text or "").strip()
    if not t:
        return Route(Intent.UNKNOWN)

    # 出现具体单号 → 无条件按「查该单状态」处理，不再猜意图
    claim_id = None
    m = CLAIM_ID_RE.search(t)
    if m:
        claim_id = m.group(1).upper()
        intent = Intent.CLAIM_STATUS
    else:
        intent = Intent.UNKNOWN
        for pattern, it in RULES:
            if re.search(pattern, t):
                intent = it
                break
        if intent is Intent.UNKNOWN:
            intent = Intent.POLICY_QA      # 兜底：报销助手默认按制度咨询处理

    # 权限门
    need = ROLE_GATE.get(intent)
    if need:
        if principal is None:
            return Route(intent, denied=True, deny_reason="未获取到身份，请重新从企业微信进入")
        if not (need & principal.roles):
            return Route(intent, denied=True,
                         deny_reason=f"该功能仅面向 {'/'.join(sorted(need))} 角色开放")

    return Route(intent, claim_id=claim_id)


HELP_TEXT = """青禾报销助手可以帮你：

· 问制度 —— "住宿费标准多少""北京住3晚900能报多少"
· 查单据 —— "我的报销单""RPT-2026-0001 什么状态"
· 看驳回原因 —— 驳回时我会一并说明缺什么材料

说明：
· 我只能查**你本人**的单据，查不到同事的
· 涉及票据真伪、金额认定，会以**财务复核结论**为准，我不代替财务下结论
· 制度里没写的，我会直接说不知道，不会替你猜"""

FINANCE_HELP = """
财务角色可用：
· "待核队列" —— 查看待核实事实清单（按风险排序）
· "确认 F3" / "驳回 F2 抬头不符" —— 逐项核实
· 单据在全部事实核实完成前，不会进入付款环节"""
