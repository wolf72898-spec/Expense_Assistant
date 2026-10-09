# 内核变更登记（Apache-2.0 §4(b) 要求）

上游：`bonc-ai/expense-assistant` @ `673580d` (v0.3.1) · Apache-2.0 · Copyright 2026 BONC

> 规则：凡修改 `engine/` 下源自上游的文件，**必须在本表登记**并加 `# 青禾修改` 注释。
> 未登记即视为违规。

## 重要决策：保留上游目录结构

曾尝试把 `lib/` 下的多子目录扁平化到 `engine/`，结果引发一连串路径断点
（`PREP`/`PKG` 路径、`SKILL_ROOT`、包发现、产物检查……）。
**已回退为上游原目录结构**，只做必要的最小改动。

```
engine/
├── expense.py
├── expense-report-prep/     scripts/ ontology/ schemas/
└── expense-report-package/  scripts/ config/ schemas/ standards/
```

## 变更清单

| # | 文件 | 修改 | 原因 |
|---|---|---|---|
| 1 | `expense-report-prep/scripts/expense_prep.py` | 新增 `PROJECT_ROOT` 常量 | 定位项目根下的青禾制度包 |
| 2 | 同上 | `extract_appendices()` 增加 `skill_md.exists()` 保护 | 上游无保护，目录内无 SKILL.md 时 `init` 直接崩溃（FileNotFoundError） |
| 3 | 同上 | 企业制度包路径改为 `PROJECT_ROOT/company/policy.json` | 上游指向 `SKILL_ROOT.parent.parent/company/`（即 `lib/company/`），实际不存在，是上游 bug |
| 4 | 同上 | `policy_to_org()` 中 `cap["per"]` / `cap["amount"]` 改为 `cap.get(...)` | 财务填制度时漏填 `per` 会导致工作区初始化整体失败 |
| 5 | 同上 | `_compile_one()` 中 `r["applies_to"]` 改为 `(r.get("applies_to") or {})` | 票据规则漏填 `applies_to` 会导致整个规则库编译失败 |

## 上游自身存在的缺陷（未修改，但使用时需知道）

1. **自带 schema 与自带制度包字段命名不一致**
   `expense-policy.schema.json` 用 `receipt` / `rule_set` / `coa`，
   而 `company/policy.json` 实际用 `receipt_rules` / `rule_sets` / `chart_of_accounts`，
   且 schema 为 `additionalProperties: false`。
   → 上游自己的制度包也过不了自己的 schema。
   → 青禾改为**校验引擎真正消费的核心结构**（见 `policy/registry.py::validate`）。

2. **`init` 流程依赖 SKILL.md**
   `cmd_init` 会读取 `<pkg-skill>/SKILL.md` 提取附录，但 `expense-report-package/` 下并无此文件。
   → 青禾已加存在性保护（变更 #2）。

3. **无社区验证**
   star 0、仅 1 次提交、无 issue 记录。上述缺陷即为例证。
   → 上线前必须用青禾真实票据做回归。

## 已验证可用的链路（2026-10-09 实测）

```
engine/expense.py doctor                                    → ok: true
prep init --user 张三 --employee-id E001 --root data/ws \
    --pkg-skill engine/expense-report-package               → 工作区建立，规则集编译通过
prep --ws <ws> report new --title ... --project ...         → 建单成功
prep --ws <ws> ingest <RPT> --file <数电票.xml>             → 解析成功
                                                              发票号 26440000000022220001
                                                              金额 600.0，类目 conference
prep --ws <ws> pkg <RPT> gaps                               → 产出核对项
                                                              GAP-0001 missing_evidence(agenda)
                                                              GAP-0002 unmapped_account(conference)
```

> 注：`GAP-0002 无会计科目` 是**青禾制度包 chart_of_accounts 未覆盖 conference 类目**导致的，
> 属制度待补项，不是引擎缺陷。

## 待实施的改造（计划中）

| 模块 | 改造内容 | 对应需求 |
|---|---|---|
| `ReportStatus` | `draft/incomplete/ready/submitted` → 扩展 `approving/approved/rejected/payable/paid` | B |
| `TraceEvent.actor` | 增加 `finance` 角色 | D |
| `FieldProvenance` | 增加 `finance_confirmed` 取值 | D |
| 置信度门控 | 增加 `finance` 档位（强制升级至财务岗） | D |
| 票据解析回归 | 用青禾真实数电票 XML/OFD 验证 | 上线前 |

## 合规说明

修改不得移除上游 `Copyright 2026 BONC` 声明。
`engine/LICENSE-UPSTREAM` 与根目录 `NOTICE` 必须随任何分发保留。
