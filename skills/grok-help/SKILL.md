---
name: grok-help
description: >
  主任务仍由 Codex 执行，仅向 Grok 征求临时顾问意见（第二思路、排错方向、方案对比），
  默认不改仓库。Use when the user runs /grok-help or asks for a second opinion,
  Grok advice, brainstorming help, or 问问 Grok / 第二意见 / 卡壳请教. Prefer this
  over grok-work when Codex remains the implementer. 生命周期与安全遵循 grok-delegation。
---

# Grok 临时顾问（grok-help）

## 角色

| 方 | 角色 |
|----|------|
| 任务负责人 | Codex |
| 实际执行者 | **Codex**（继续完成主任务） |
| Codex | 主执行者：整理问题、判断是否采纳、继续落地 |
| Grok | 临时顾问：只给意见 / 备选 / 风险，**默认不改仓库** |

## 何时使用 / 何时不用

**使用**：卡壳要第二意见；对比两种思路；轻量请教；用户说「问问 Grok」但未要求其实现。

**不用**：

- 整段实现应交 → `grok-work`
- 要结构化 diff 审查 → `grok-review`
- 方案未定要多轮辩论 → `grok-discuss`
- 要可执行计划再由 Codex 实现 → `grok-plan`

## 步骤

1. 用几句话整理卡点：背景、已尝试、希望答案形态（选项 / 步骤 / 风险）。
2. `create_agent`：短 `agent_name`（如 `help-proxy-bug`）；prompt 标明 **advisory-only，不要修改仓库**。
3. 创建成功后报告一次 agent 信息与 observer 链接。
4. 可选：一轮 `send` 澄清；需要结果时 `wait` 一次 → `result`。
5. 判断是否采纳；说明如何继续主任务。
6. `signoff`（见下）后继续由 Codex 执行主任务。

## 给 Grok 的 prompt 要点

- 角色：临时顾问，只读建议，禁止改文件（除非用户在本轮明确要求，且应改走 work/fix）。
- 问题、约束、已排除项。
- 输出：结论要点 + 可选步骤 + 风险 / 不确定处；少空话。

## 退出与 signoff

- 退出：Codex 说明采纳与否及下一步主任务动作。
- `accepted`：意见有用且可指导行动。
- `partial`：部分有用。
- `rejected`：无关、误导或有害。

## 禁止

- 把本可 `grok-work` 的整段实现塞进 help。
- 在 help 中默认授权 Grok 写仓库。
- 跳过 signoff（凡 create 过必须 signoff）。

## 协议

生命周期、安全、observer 链接一次、wait 一次、失败降级：遵循 `grok-delegation`。
