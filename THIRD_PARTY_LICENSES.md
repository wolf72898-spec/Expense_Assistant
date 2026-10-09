# 第三方组件许可证

本项目由青禾在开源项目基础上二次开发。下方列出全部上游来源及其许可证义务。

## 一、直接修改并再分发的源码

| 组件 | 上游 | License | 位置 | 义务履行 |
|---|---|---|---|---|
| 规则与凭证内核 | `bonc-ai/expense-assistant` @ `673580d` (v0.3.1) | Apache-2.0 | `engine/` | ✅ 保留 `engine/LICENSE-UPSTREAM`<br>✅ `NOTICE` 中声明 Copyright 2026 BONC<br>✅ 修改文件头部标注变更（见 `engine/CHANGES.md`）<br>✅ 重大修改已在 `NOTICE` 声明 |

### Apache-2.0 允许什么

- ✅ 商用
- ✅ 修改
- ✅ 二次分发（含闭源分发）
- ✅ 申请专利

### Apache-2.0 要求什么

1. 分发时附带 LICENSE 副本 → 已放 `engine/LICENSE-UPSTREAM`
2. 修改过的文件需显著标注变更 → 见 `engine/CHANGES.md`
3. 保留 NOTICE 中的版权与声明 → 见根目录 `NOTICE`
4. 若含 NOTICE 文件，衍生作品需保留其内容 → 已履行

### 不提供什么

- ❌ 无商标授权（不得宣称由 BONC 背书）
- ❌ 无担保（AS IS）
- ❌ 无责任承担

---

## 二、仅借鉴设计、未复制源码

| 上游 | License | 借鉴内容 | 代码引用 |
|---|---|---|---|
| `PathiRohith/ExpenseIQ` | MIT | RAG 制度问答、人工覆盖判定、审计日志的数据模型 | 无，重新实现 |
| `zsitvat/Reimbursement-RAG-assistant` | **无 License** | 主动追问机制、确定性计算工具 | 无 |
| `AMANxSHARMA01/auditable-expense-agent` | **无 License** | 确定性护栏、HITL、不可变审计、"LLM 仅建议"原则 | 无 |
| `Chuan1130/invoice-agent-system` | **无 License** | 状态机与事务边界、确认门控的 Agent 动作 | 无 |
| `TheDevilspear/Expense-Claim-Assistant` | **无 License** | 未采用 | 无 |

> ⚠️ **合规红线**：标注「无 License」的项目，其源代码**一律不得复制进本仓库**。
> GitHub 服务条款下未声明 License 即默认「保留所有权利」。
> 新增依赖前请回到 `docs/00-开源调研与二次开发方案.md` 第二节核对。

---

## 三、上游数据免责

上游 `NOTICE` 已声明：示例企业制度包与测试样本均为**虚构数据**；GB/T 42965 仅对齐要素名称、未含标准正文。

青禾的处理：
- 虚构制度包 `company/policy.json` **已整体弃用**，替换为 `policy/packs/qinghe.json`
- 上游测试样本（发票/酒店水单等）仅保留于 `upstream/` 作参考，**不参与任何运行时**
- 涉及强合规判断的票据要素，上线前需由财务核对 GB/T 42965 标准原文

---

## 四、新增依赖的准入流程

引入任何新第三方代码前，必须完成：

1. 确认 License（无 License = 默认不可使用）
2. 确认与本项目许可证兼容（Apache-2.0 项目引入 GPL 代码需特别评估）
3. 更新本文件
4. 更新根目录 `NOTICE`
