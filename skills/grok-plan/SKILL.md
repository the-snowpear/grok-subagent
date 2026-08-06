---
name: grok-plan
description: >
  由 Grok 设计可执行方案，Codex 审查后必须经用户确认，再由 Codex 按计划实现。
  Use when the user runs /grok-plan or asks Grok to design a plan, architecture
  steps, or PR split while Codex will implement — 先出方案 / 让 Grok 设计 / 拆步骤.
  Do not implement until the user confirms. 生命周期与安全遵循 grok-delegation。
---

# Grok 方案设计（grok-plan）

## 角色

| 方 | 角色 |
|----|------|
| 任务负责人 | Codex |
| 实际执行者 | **通常是 Codex**（用户确认计划之后） |
| Codex | 计划验收、请用户确认、再执行实现 |
| Grok | 方案设计者：步骤、风险、验收标准、可选 PR 切分 |

## 何时使用 / 何时不用

**使用**：目标相对清晰，需要结构化计划；实现权希望留在 Codex。

**不用**：

- 方向仍糊、要先辩论 → `grok-discuss`
- 实现也要总包给 Grok → `grok-work`
- 只要轻量意见 → `grok-help`

## 步骤

1. 确认目标足够清晰（否则建议先 `grok-discuss`）。
2. `create_agent`：角色=方案设计者；**不要直接改代码**；要求结构化计划。
3. 报告一次 agent 信息与 observer 链接。
4. 可选 `send` 修订计划（补洞、缩 scope）。
5. `wait` 一次 → `result`；Codex 审计划质量。
6. **`signoff` 针对计划质量**（不是实现结果）。
7. **向用户展示计划，必须得到确认**（OK / 修改点 / 取消）后，才由 Codex 本地实现。
8. 实现阶段通常不再开 Grok；实现完成后按 Codex 常规方式向用户交付。

## 给 Grok 的 prompt 要点

- 角色：方案设计者，禁止直接改仓库。
- 目标、约束、非目标、相关路径线索。
- 输出结构建议：
  - 目标 / 非目标
  - 步骤（有序）
  - 风险与缓解
  - 验收标准
  - 可选 PR / 提交切分
  - 回滚注意

## 退出与 signoff

- 计划阶段 signoff：`accepted` = 计划可执行且经 Codex 认可；`partial` = 需用户补约束；`rejected` = 不可用。
- 流程退出：用户确认后的实现结果，或用户取消。
- **实现功不记在 Grok 上**；Grok 只对方案负责。

## 禁止

- 未经用户确认开始大范围改代码。
- 把 plan 做成暗 `grok-work`（让 Grok 直接实现）。
- 跳过 signoff。

## 协议

生命周期、安全、observer 链接一次、wait 一次、失败降级：遵循 `grok-delegation`。
