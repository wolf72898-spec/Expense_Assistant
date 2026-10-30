"""制度中枢 · 存储与状态流转

所有写操作强制走状态机并记录审计。已发布版本一经写入即不可变。
存储：SQLite（与基座引擎同款轻量依赖，无需额外服务）
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, timedelta
from pathlib import Path

from .models import (
    ActorRole, Op, PolicyChange, PolicyClause, PolicyError, PolicyStatus,
    PolicyVersion, assert_can_approve, assert_can_edit, assert_transition,
    diff_content, now_iso,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_versions (
    policy_id      TEXT NOT NULL,
    version        INTEGER NOT NULL,
    title          TEXT NOT NULL,
    status         TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to   TEXT,
    summary        TEXT DEFAULT '',
    content        TEXT NOT NULL,
    created_by     TEXT DEFAULT '',
    created_at     TEXT NOT NULL,
    approved_by    TEXT,
    approved_at    TEXT,
    published_at   TEXT,
    PRIMARY KEY (policy_id, version)
);
CREATE INDEX IF NOT EXISTS idx_pv_status ON policy_versions(policy_id, status);

CREATE TABLE IF NOT EXISTS policy_changes (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_id   TEXT NOT NULL,
    version     INTEGER NOT NULL,
    op          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    actor_role  TEXT NOT NULL,
    at          TEXT NOT NULL,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_pc_ref ON policy_changes(policy_id, version);

CREATE TABLE IF NOT EXISTS policy_clauses (
    clause_id   TEXT PRIMARY KEY,
    policy_id   TEXT NOT NULL,
    version     INTEGER NOT NULL,
    clause_no   TEXT DEFAULT '',
    title       TEXT DEFAULT '',
    text        TEXT NOT NULL,
    category    TEXT DEFAULT '',
    tags        TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_clause_scope ON policy_clauses(policy_id, version);
"""


class PolicyStore:
    def __init__(self, db_path: str | Path = "data/policy.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # 青禾修改：Web 看板中 FastAPI 用线程池处理请求，需允许跨线程使用同一连接，
        # 写操作用 _lock 串行化 —— 见 docs/01-技术架构设计.md
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ─────────────────────── 内部工具 ───────────────────────

    def _log(self, pv: PolicyVersion, op: Op, actor: str, role: ActorRole,
             detail: dict | None = None) -> None:
        ch = PolicyChange(pv.policy_id, pv.version, op.value, actor, role.value,
                          now_iso(), detail or {})
        d = ch.to_dict()
        with self._lock:
            self.conn.execute(
                """INSERT INTO policy_changes
                   (policy_id, version, op, actor, actor_role, at, detail)
                   VALUES (:policy_id,:version,:op,:actor,:actor_role,:at,:detail)""", d)
            self.conn.commit()

    def _save(self, pv: PolicyVersion) -> None:
        d = pv.to_dict()
        d["content"] = json.dumps(pv.content, ensure_ascii=False)
        with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO policy_versions
                   (policy_id,version,title,status,effective_from,effective_to,summary,
                    content,created_by,created_at,approved_by,approved_at,published_at)
                   VALUES (:policy_id,:version,:title,:status,:effective_from,:effective_to,
                           :summary,:content,:created_by,:created_at,:approved_by,
                           :approved_at,:published_at)""", d)
            self.conn.commit()

    def _row_to_pv(self, row: sqlite3.Row) -> PolicyVersion:
        return PolicyVersion(
            policy_id=row["policy_id"], version=row["version"], title=row["title"],
            content=json.loads(row["content"]), effective_from=row["effective_from"],
            effective_to=row["effective_to"], status=row["status"],
            summary=row["summary"], created_by=row["created_by"],
            created_at=row["created_at"], approved_by=row["approved_by"],
            approved_at=row["approved_at"], published_at=row["published_at"],
        )

    def get(self, policy_id: str, version: int) -> PolicyVersion:
        row = self.conn.execute(
            "SELECT * FROM policy_versions WHERE policy_id=? AND version=?",
            (policy_id, version)).fetchone()
        if not row:
            raise PolicyError(f"制度不存在：{policy_id}@v{version}")
        return self._row_to_pv(row)

    # ─────────────────────── 起草 / 编辑 ───────────────────────

    def next_version(self, policy_id: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(version) AS m FROM policy_versions WHERE policy_id=?",
            (policy_id,)).fetchone()
        return (row["m"] or 0) + 1

    def create(self, policy_id: str, title: str, content: dict,
               effective_from: str, actor: str, effective_to: str | None = None,
               summary: str = "") -> PolicyVersion:
        """起草新制度（v1）"""
        if self.conn.execute("SELECT 1 FROM policy_versions WHERE policy_id=?",
                             (policy_id,)).fetchone():
            raise PolicyError(f"制度 {policy_id} 已存在，修改请走 revise() 发新版本")
        pv = PolicyVersion(
            policy_id=policy_id, version=1, title=title, content=content,
            effective_from=effective_from, effective_to=effective_to,
            status=PolicyStatus.DRAFT.value, summary=summary,
            created_by=actor, created_at=now_iso())
        self._save(pv)
        self._log(pv, Op.CREATE, actor, ActorRole.ADMIN, {"title": title})
        return pv

    def revise(self, policy_id: str, content: dict, effective_from: str,
               actor: str, effective_to: str | None = None,
               summary: str = "") -> PolicyVersion:
        """基于现行版本起草新版本（改制度 = 发新版，历史版本永久可追溯）"""
        current = self.effective(policy_id)
        if current is None:
            raise PolicyError(f"制度 {policy_id} 无已发布版本，无法发起修订")
        version = self.next_version(policy_id)
        pv = PolicyVersion(
            policy_id=policy_id, version=version, title=current.title,
            content=content, effective_from=effective_from,
            effective_to=effective_to, status=PolicyStatus.DRAFT.value,
            summary=summary, created_by=actor, created_at=now_iso())
        self._save(pv)
        self._log(pv, Op.CREATE, actor, ActorRole.ADMIN, {
            "based_on": current.ref, "title": current.title,
            "diff_vs_current": diff_content(current.content, content),
        })
        return pv

    def update_draft(self, policy_id: str, version: int, content: dict,
                     actor: str, summary: str = "") -> PolicyVersion:
        """编辑草稿。已提交/已发布的版本一律拒绝修改。"""
        pv = self.get(policy_id, version)
        assert_can_edit(pv)
        diff = diff_content(pv.content, content)
        pv.content = content
        if summary:
            pv.summary = summary
        self._save(pv)
        self._log(pv, Op.UPDATE, actor, ActorRole.ADMIN, {"diff": diff})
        return pv

    # ─────────────────────── 审批流转 ───────────────────────

    def submit(self, policy_id: str, version: int, actor: str) -> PolicyVersion:
        pv = self.get(policy_id, version)
        assert_can_edit(pv)
        assert_transition(pv, PolicyStatus.PENDING)
        pv.status = PolicyStatus.PENDING.value
        self._save(pv)
        self._log(pv, Op.SUBMIT, actor, ActorRole.ADMIN, {"to": "财务负责人审批"})
        return pv

    def approve(self, policy_id: str, version: int, approver: str,
                comment: str = "") -> PolicyVersion:
        pv = self.get(policy_id, version)
        assert_can_approve(pv, approver)
        assert_transition(pv, PolicyStatus.PUBLISHED)
        pv.approved_by = approver
        pv.approved_at = now_iso()
        pv.published_at = now_iso()
        pv.status = PolicyStatus.PUBLISHED.value
        self._supersede_old(pv, approver)
        self._save(pv)
        self._log(pv, Op.APPROVE, approver, ActorRole.FINANCE, {"comment": comment})
        return pv

    def reject(self, policy_id: str, version: int, approver: str,
               reason: str) -> PolicyVersion:
        if not reason.strip():
            raise PolicyError("驳回必须填写理由")
        pv = self.get(policy_id, version)
        assert_transition(pv, PolicyStatus.DRAFT)
        pv.status = PolicyStatus.DRAFT.value
        self._save(pv)
        self._log(pv, Op.REJECT, approver, ActorRole.FINANCE, {"reason": reason})
        return pv

    def _supersede_old(self, new_pv: PolicyVersion, actor: str) -> None:
        """发布新版时，把同一制度的旧生效版本置为 superseded。

        关键：旧版本的 effective_to 必须收敛为「新版生效日的前一天」，
        而不是「今天」。否则历史单据做追溯判定时会查不到当时的制度。
        例：v1(2026-01-01 起) → v2(2026-07-01 起)，则 v1 的区间是 2026-01-01 ~ 2026-06-30。
        """
        policy_id, new_version = new_pv.policy_id, new_pv.version
        try:
            cutoff = (date.fromisoformat(new_pv.effective_from) - timedelta(days=1)).isoformat()
        except ValueError:
            cutoff = new_pv.effective_from
        rows = self.conn.execute(
            """SELECT * FROM policy_versions
               WHERE policy_id=? AND status IN ('published','superseded') AND version<>?""",
            (policy_id, new_version)).fetchall()
        for row in rows:
            old = self._row_to_pv(row)
            if old.status == PolicyStatus.SUPERSEDED.value:
                continue
            old.status = PolicyStatus.SUPERSEDED.value
            # 边界保护：生效区间不得早于起始日
            old.effective_to = max(cutoff, old.effective_from)
            self._save(old)
            self._log(old, Op.SUPERSEDE, actor, ActorRole.SYSTEM,
                      {"superseded_by": new_pv.ref, "effective_to": old.effective_to})

    # ─────────────────────── 查询 ───────────────────────

    def effective(self, policy_id: str, day: str | None = None
                  ) -> PolicyVersion | None:
        """取指定日期生效中的版本。制度问答（需求 A）必须走这个方法，
        确保不会引用到未发布或已过期制度。

        注意 status 包含 superseded：被取代的旧版本在它自己的生效区间内
        仍然是「当时的有效制度」，历史单据追溯判定依赖这一点。
        草稿 / 待审批 / 已归档永远不会命中。"""
        day = day or date.today().isoformat()
        row = self.conn.execute(
            """SELECT * FROM policy_versions
               WHERE policy_id=? AND status IN ('published','superseded')
                 AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?)
               ORDER BY version DESC LIMIT 1""", (policy_id, day, day)).fetchone()
        return self._row_to_pv(row) if row else None

    def effective_all(self, day: str | None = None) -> list[PolicyVersion]:
        """取所有制度在指定日期的生效版本"""
        day = day or date.today().isoformat()
        rows = self.conn.execute(
            """SELECT * FROM policy_versions
               WHERE status IN ('published','superseded') AND effective_from<=?
                 AND (effective_to IS NULL OR effective_to>=?)""", (day, day)).fetchall()
        return [self._row_to_pv(r) for r in rows]

    def history(self, policy_id: str) -> list[PolicyVersion]:
        rows = self.conn.execute(
            "SELECT * FROM policy_versions WHERE policy_id=? ORDER BY version DESC",
            (policy_id,)).fetchall()
        return [self._row_to_pv(r) for r in rows]

    def audit_trail(self, policy_id: str, version: int | None = None) -> list[dict]:
        """制度变更审计轨迹"""
        if version is None:
            rows = self.conn.execute(
                "SELECT * FROM policy_changes WHERE policy_id=? ORDER BY seq",
                (policy_id,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM policy_changes WHERE policy_id=? AND version=? ORDER BY seq",
                (policy_id, version)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail"] or "{}")
            out.append(d)
        return out

    # ─────────────────────── 条款（供 RAG 检索） ───────────────────────

    def clone_clauses(self, policy_id: str, from_version: int,
                      to_version: int) -> int:
        """把某版本的条款复制为新版本条款。

        制度修订时必须继承条款——否则新版发布后问答检索不到任何条款
        （青禾实际踩过的坑：v2 发布后问答仍引用 v1 的旧数值）。
        复制后由调用方按本次变更同步更新条款文本。"""
        rows = self.conn.execute(
            "SELECT * FROM policy_clauses WHERE policy_id=? AND version=?",
            (policy_id, from_version)).fetchall()
        n = 0
        with self._lock:
            for r in rows:
                # clause_id 是主键，加版本后缀避免冲突
                self.conn.execute(
                    """INSERT OR REPLACE INTO policy_clauses
                       (clause_id,policy_id,version,clause_no,title,text,category,tags)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (f"{r['clause_id']}-v{to_version}", policy_id, to_version,
                     r["clause_no"], r["title"], r["text"], r["category"],
                     r["tags"]))
                n += 1
            self.conn.commit()
        return n

    def replace_clause_text(self, policy_id: str, version: int, category: str,
                            old: str, new: str) -> int:
        """按本次制度变更同步更新条款文本（如「150 元/人」→「200 元/人」）。"""
        rows = self.conn.execute(
            """SELECT clause_id, text FROM policy_clauses
               WHERE policy_id=? AND version=? AND category=?""",
            (policy_id, version, category)).fetchall()
        n = 0
        with self._lock:
            for r in rows:
                if old and old in r["text"]:
                    self.conn.execute(
                        "UPDATE policy_clauses SET text=? WHERE clause_id=?",
                        (r["text"].replace(old, new), r["clause_id"]))
                    n += 1
            self.conn.commit()
        return n

    def replace_clauses(self, policy_id: str, version: int,
                        clauses: list[PolicyClause]) -> None:
        self.conn.execute(
            "DELETE FROM policy_clauses WHERE policy_id=? AND version=?",
            (policy_id, version))
        for c in clauses:
            d = c.to_dict()
            self.conn.execute(
                """INSERT INTO policy_clauses
                   (clause_id,policy_id,version,clause_no,title,text,category,tags)
                   VALUES (:clause_id,:policy_id,:version,:clause_no,:title,:text,
                           :category,:tags)""", d)
        self.conn.commit()

    def clauses(self, policy_id: str | None = None, category: str | None = None,
                day: str | None = None) -> list[PolicyClause]:
        """默认只返回【当前生效版本】的条款 —— 防止问答命中草稿或废止条款"""
        sql = """SELECT c.* FROM policy_clauses c
                 JOIN policy_versions v
                   ON v.policy_id=c.policy_id AND v.version=c.version
                  AND v.status IN ('published','superseded')"""
        params: list = []
        if day:
            sql += " AND v.effective_from<=? AND (v.effective_to IS NULL OR v.effective_to>=?)"
            params += [day, day]
        if policy_id:
            sql += " AND c.policy_id=?"
            params.append(policy_id)
        if category:
            sql += " AND c.category=?"
            params.append(category)
        rows = self.conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            out.append(PolicyClause(
                clause_id=r["clause_id"], policy_id=r["policy_id"],
                version=r["version"], clause_no=r["clause_no"], title=r["title"],
                text=r["text"], category=r["category"],
                tags=json.loads(r["tags"] or "[]")))
        return out

    def close(self) -> None:
        self.conn.close()
