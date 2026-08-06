---
name: grok-discuss
description: >
  Codex 主持与 Grok 的平等讨论：对齐议题、交锋方案、收束为纪要；讨论期不写代码、
  不自动进入 plan/work。Use when the user runs /grok-discuss or wants to debate
  options, align on design, or 一起讨论 / 辩论方案 / 还没决定. Ends with minutes
  only; wait for the user. 生命周期与安全遵循 grok-delegation。
---

# Grok 平等讨论（grok-discuss）

## 角色

| 方 | 角色 |
|----|------|
| 任务负责人 | Codex |
| 实际执行者 | **无**（讨论阶段不执行实现） |
| Codex | 讨论主持人：定议题、收束分歧、向用户呈现选项 |
| Grok | 平等讨论者：可反驳 Codex 初案、提替代方案 |

## 何时使用 / 何时不用

**使用**：方向未定；需要辩论利弊；要对齐概念再动手。

**不用**：

- 只要短意见、主任务继续 → `grok-help`
- 已要可执行步骤且实现归 Codex → `grok-plan`
- 已要总包实现 → `grok-work`

## 步骤

1. 与用户（或从上下文）明确**议题**；可选写清 Codex 初衷立场。
2. `create_agent` 开场：角色=平等讨论者；本轮只讨论不写代码；给出议题与初衷立场。
3. 报告一次 agent 信息与 observer 链接。
4. 用 `send` 推进 **2–4 轮焦点**（每轮一个问题：利弊、风险、替代、取舍）。超过则**强制收束**；用户要求续轮可再开。
5. 需要阶段结论时可 `wait` → `result`；结束前确保拿到可写纪要的材料。
6. 产出**纪要**（见退出），`signoff`，**停止**——不自动跳转其它 skill；可*建议* plan / work / Codex 自做 / 结束。

## 给 Grok 的 prompt 要点

- 角色：平等讨论者，不是下属执行者。
- 当前议题；Codex/用户立场摘要；请表态、反驳或补洞。
- **禁止写代码、改仓库、假装已决策。**
- 每轮聚焦一个问题，结尾给可写入纪要的短结论。

## 退出与 signoff

退出产物（必须）：

1. **共识**（已对齐点）
2. **未决问题**
3. **建议下一步**（`grok-plan` / `grok-work` / Codex 自做 / 停止）——仅建议，等人决定

signoff：

- `accepted`：讨论有效，纪要可用。
- `partial`：有信息但未收束。
- `rejected`：跑题、失败或不可用。

## 禁止

- 讨论中 `create` 实现向任务或直接改仓库。
- 自动进入 `grok-plan` / `grok-work`。
- 无限辩论不写纪要。

## 协议

生命周期、安全、observer 链接一次、wait 一次、失败降级：遵循 `grok-delegation`。
