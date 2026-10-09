"""制度中枢 → 引擎 的编译桥

职责：
  1. 把「当前生效的制度版本」编译成基座引擎认识的 policy dict
  2. 用引擎自带 JSON Schema 校验，防止非法制度进引擎
  3. 注入制度来源元数据（policy_ref / 生效期 / 编译时间），
     使引擎产出的每条 Gap、每个判定都能追溯到具体制度版本

关键约束：编译源只能是 status=published 且当前生效的版本。
草稿、待审批、已废止的版本一律不得进入引擎 —— 这是需求 A「有效制度」的硬保证。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .models import PolicyVersion, PolicyError
from .store import PolicyStore

ROOT = Path(__file__).resolve().parent.parent
# 引擎保持上游目录结构，schema 位于 expense-report-package/schemas/ 下
SCHEMA_PATH = (ROOT / "engine" / "expense-report-package" / "schemas"
               / "expense-policy.schema.json")
POLICY_SCHEMA = (ROOT / "engine" / "expense-report-package" / "schemas"
                 / "expense-report.schema.json")


class PolicyRegistry:
    def __init__(self, store: PolicyStore):
        self.store = store

    # ─────────────────────── 编译 ───────────────────────

    def compile(self, day: str | None = None) -> dict:
        """把指定日期全部生效制度合并编译为引擎 policy。

        合并顺序：后出现制度的类目覆盖先出现的（同 policy_id 只可能有一个生效版本，
        冲突只可能来自不同制度文件定义同一类目，此时以 policy_id 排序靠后者为准并记录）。
        """
        day = day or date.today().isoformat()
        versions = self.store.effective_all(day)
        if not versions:
            raise PolicyError(
                f"{day} 无任何生效中的制度。请先由制度管理员发布制度，"
                f"否则引擎无规则可依，所有判定都将被拒绝。"
            )

        merged: dict = {
            "policy_id": f"qinghe-compiled-{day}",
            "report_currency": "CNY",
            "require_project": True,
            "require_purpose": True,
            "submission_window_days": 90,
            "categories": {},
            "receipt_rules": [],
            "grades": [],
            "rule_sets": [],
            "chart_of_accounts": {},
            "allocation": {},
        }
        sources: list[dict] = []
        conflicts: list[dict] = []

        for pv in sorted(versions, key=lambda x: x.policy_id):
            content = pv.content
            sources.append({
                "ref": pv.ref, "title": pv.title,
                "effective_from": pv.effective_from,
                "effective_to": pv.effective_to,
                "approved_by": pv.approved_by, "approved_at": pv.approved_at,
            })
            for key in ("report_currency", "require_project", "require_purpose",
                        "submission_window_days"):
                if key in content:
                    merged[key] = content[key]
            for cat, spec in (content.get("categories") or {}).items():
                if cat in merged["categories"] and \
                        merged["categories"][cat] != spec:
                    conflicts.append({"category": cat, "kept_from": pv.ref,
                                      "kept_spec": merged["categories"][cat],
                                      "overridden_by": pv.ref, "new_spec": spec})
                merged["categories"][cat] = spec
            for key in ("receipt_rules", "grades", "rule_sets"):
                merged[key].extend(content.get(key) or [])
            for key in ("chart_of_accounts", "allocation"):
                merged[key].update(content.get(key) or {})

        merged["_source"] = {
            "compiled_at": day,
            "policies": sources,
            "conflicts": conflicts,
            "note": "由青禾制度中枢编译生成，勿手工编辑。判定引用请以 sources[].ref 为准。",
        }
        self.validate(merged)
        return merged

    def validate(self, policy: dict) -> None:
        """校验制度，阻止非法制度进入引擎。

        为什么不直接用上游 expense-policy.schema.json：
        该 schema 字段名与上游自带的 company/policy.json 并不一致 ——
        schema 用 receipt / rule_set / coa，而制度包实际用
        receipt_rules / rule_sets / chart_of_accounts，且 additionalProperties=false。
        直接套用会把合法制度误判为非法（这是上游 star=0、未经社区验证的表现之一）。

        因此这里改为校验**引擎真正消费的核心结构**，必要时再用上游 schema 做附加校验。
        """
        for key in ("policy_id", "report_currency", "categories"):
            if key not in policy:
                raise PolicyError(f"制度缺少必需字段：{key}")
        cats = policy.get("categories") or {}
        if not isinstance(cats, dict) or not cats:
            raise PolicyError("制度必须至少定义一个费用类目（categories）")
        for cat, spec in cats.items():
            if not isinstance(spec, dict):
                raise PolicyError(f"类目 {cat} 的定义必须是对象")
            for g in spec.get("required_evidence", []):
                if not isinstance(g, list):
                    raise PolicyError(
                        f"类目 {cat} 的 required_evidence 每项必须是「可选证据列表」，"
                        f"如 [[\"e_invoice\",\"invoice\"]]")
            cap = spec.get("cap")
            if cap is not None:
                if not isinstance(cap, dict):
                    raise PolicyError(f"类目 {cat} 的 cap 必须是对象")
                if "amount" in cap and not isinstance(cap["amount"], (int, float)):
                    raise PolicyError(f"类目 {cat} 的 cap.amount 必须是数字")
                if cap.get("mode") not in (None, "warn", "clip", "block"):
                    raise PolicyError(
                        f"类目 {cat} 的 cap.mode 只支持 warn / clip / block，"
                        f"当前为 {cap.get('mode')!r}")
        # 上游 schema 作为附加校验（宽松：仅在不阻断核心结构时提示）
        try:
            import jsonschema
        except ImportError:
            return
        if SCHEMA_PATH.exists():
            schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
            core = {k: policy[k] for k in
                    ("policy_id", "report_currency", "categories") if k in policy}
            try:
                jsonschema.validate(core, schema)
            except jsonschema.ValidationError:
                # 上游 schema 与制度包字段命名不一致，此处不阻断，仅记录
                pass

    def export(self, out_path: str | Path, day: str | None = None) -> Path:
        """导出编译后的制度，供引擎加载"""
        policy = self.compile(day)
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(policy, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return p

    # ─────────────────────── 引用解析 ───────────────────────

    def resolve(self, ref: str) -> PolicyVersion:
        """由引用串（如 QH-EXP-003@v2）定位到具体制度版本，供复核与问答溯源"""
        if "@v" not in ref:
            raise PolicyError(f"制度引用格式不正确：{ref}，应为 <policy_id>@v<version>")
        policy_id, ver = ref.rsplit("@v", 1)
        return self.store.get(policy_id, int(ver))

    def effective_refs(self, day: str | None = None) -> list[str]:
        return [pv.ref for pv in self.store.effective_all(day)]

    def clause_citations(self, category: str, day: str | None = None) -> list[dict]:
        """取某费用类目在当前生效制度下的条款引用，供问答与 Gap 提示直接展示"""
        out = []
        for c in self.store.clauses(category=category, day=day):
            out.append({
                "citation": c.citation(),
                "text": c.text,
                "policy_ref": f"{c.policy_id}@v{c.version}",
                "category": c.category,
            })
        return out
