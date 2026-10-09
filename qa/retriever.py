"""条款检索 —— 需求 A「自助查询有效制度」的检索层

硬性约束：**只检索当前生效版本的条款**。
草稿、待审批、已废止的条款绝不出现在结果里，避免员工按未发布制度行事。

当前实现：词项重叠打分（中文 bigram），零依赖、可离线跑通。
接入向量库时替换 `search()` 即可，上层 service 不变。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from policy.models import PolicyClause
from policy.store import PolicyStore


@dataclass
class ScoredClause:
    clause: PolicyClause
    score: float

    @property
    def citation(self) -> str:
        return self.clause.citation()


# 通用词：出现在几乎所有报销问题里，不具备区分度。
# 检索时必须先剥掉它们，否则「团建费用能报销吗」会命中「报销时限」条款，
# 导致答非所问 —— 财务场景下，错误引用比拒答危害更大。
GENERIC_WORDS = [
    "报销", "费用", "请问", "咨询", "一下", "我们", "申请", "提交",
    "可以", "能不能", "能否", "多少", "怎么", "如何", "问题", "标准",
    "限额", "规定", "制度", "我的", "我想", "请问一下", "是不是", "有没有",
    "的", "了", "吗", "是", "有", "在", "和", "与", "我", "请", "帮", "查",
]


def _grams(text: str) -> set[str]:
    """中文按 bigram 切分，英文数字按词切分"""
    t = re.sub(r"\s+", "", text.lower())
    g = {t[i:i + 2] for i in range(len(t) - 1)}
    g |= set(re.findall(r"[a-z0-9]+", text.lower()))
    return g


def _topic_grams(query: str) -> set[str]:
    """剥掉通用词后的主题 bigram。一个都没命中即视为「制度未覆盖」。"""
    q = query
    for w in sorted(GENERIC_WORDS, key=len, reverse=True):
        q = q.replace(w, " ")
    q = re.sub(r"\s+", "", q)
    if not q:
        return set()
    g = {q[i:i + 2] for i in range(len(q) - 1)} or {q}
    g |= set(re.findall(r"[a-z0-9]+", query.lower()))
    return g


class ClauseRetriever:
    def __init__(self, store: PolicyStore):
        self.store = store

    def search(self, query: str, top_k: int = 3,
               category: str | None = None) -> list[ScoredClause]:
        """检索生效条款。返回结果按相关度降序。"""
        clauses = self.store.clauses(category=category or None)
        if not clauses:
            return []
        qg = _grams(query)
        topic = _topic_grams(query)
        if not qg:
            return []
        scored: list[ScoredClause] = []
        for c in clauses:
            haystack = f"{c.title} {c.text} {c.clause_no} {' '.join(c.tags)}"
            cg = _grams(haystack)
            # 硬门槛：问题的主题词在条款里一个都没出现 → 判为不相关，直接丢弃
            if topic and not (topic & cg):
                continue
            overlap = len(qg & cg)
            if overlap == 0:
                continue
            # Jaccard 风格的归一化，长条款不吃亏也不占便宜
            score = overlap / (len(qg) ** 0.5 * len(cg) ** 0.25)
            # 标题与标签命中加权
            if any(g in _grams(c.title) for g in qg):
                score *= 1.4
            if any(t and t in query for t in c.tags):
                score *= 1.3
            scored.append(ScoredClause(c, round(score, 4)))
        scored.sort(key=lambda s: s.score, reverse=True)
        return scored[:top_k]

    def best(self, query: str, category: str | None = None) -> ScoredClause | None:
        r = self.search(query, top_k=1, category=category)
        return r[0] if r else None
