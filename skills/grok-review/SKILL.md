---
name: grok-review
description: >
  由 Grok 作为独立审查者对指定变更做只读 code review；Codex 准备 diff/上下文、
  核实 findings，并决定是否进入 fix。Use when the user runs /grok-review or asks
  for an independent Grok code review / 让 Grok 审 / 独立 review. Always ask the
  user for the review target first. Grok must not edit code in this skill.
  生命周期与安全遵循 grok-delegation。
---

# Grok 独立审查（grok-review）

## 角色

| 方 | 角色 |
|----|------|
| 任务负责人 | Codex |
| 实际执行者 | **Codex**（裁决 findings；是否修复另议） |
| Codex | 提交审查材料、验证 findings、建议是否 `grok-fix` |
| Grok | 独立审查者：**只读**，按 severity 出 findings |

## 何时使用 / 何时不用

**使用**：要独立第二审；查 local/分支/路径上的问题；用户明确要求 Grok review。

**不用**：

- 轻量请教无 diff 流程 → `grok-help`
- 直接按单修改 → `grok-fix`（需已有清单）
- 从零实现 → `grok-work`

本 skill 宿主是 **Codex**；与 Grok TUI 内置 `/review` 不同。

## 步骤

1. **先问用户审查目标**（无默认）：例如
   - 工作区未提交变更（local uncommitted）
   - 某分支相对 main/master
   - 指定路径 / 文件列表
   - 其它用户描述的范围  
   用户已在本轮明确目标则可跳过再问。
2. 收集 diff 与必要上下文；**不要把密钥、`.env`、令牌文件塞进 prompt**。
3. `create_agent`：角色=独立只读审查者；附范围说明与 diff/路径信息。
4. 报告一次 agent 信息与 observer 链接。
5. `wait` → `result`。
6. Codex **逐条核对** findings 是否成立（读源码/跑检查），标注成立 / 不成立 / 存疑。
7. `signoff`；向用户呈现 findings + 裁决；*建议*是否进入 `grok-fix`（**不自动进入**）。

大 diff 可按模块拆多个 agent（注意同会话 active 上限，见插件并发策略），仍各自 signoff。

## 给 Grok 的 prompt 要点

- 角色：独立审查者，**只读，禁止改代码**。
- 审查范围与 diff/文件列表位置。
- 输出建议：
  - Summary（2–4 句）
  - Issues：severity（`bug` / `suggestion` / `nit`）、File、Description、Suggestion、Status: open
- 无问题则明确写 Summary + 空 Issues，勿硬凑。

## 退出与 signoff

- 退出：findings 列表 + Codex 简评 + 是否建议 `grok-fix`。
- `accepted`：审查整体可用。
- `partial`：部分成立或范围不足。
- `rejected`：空话、误报主导或失败。

## 禁止

- 在本 skill 内直接修代码（修复走 `grok-fix` 或 Codex 自修）。
- 未询问就假定目标为 local（用户已写明除外）。
- 跳过 signoff。

## 协议

生命周期、安全、observer 链接一次、wait 一次、失败降级：遵循 `grok-delegation`。
