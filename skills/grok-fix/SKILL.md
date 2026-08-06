---
name: grok-fix
description: >
  Codex 将 review 或口头意见固化为具体工单后，由 Grok 按单修复，Codex 本地验收。
  Use when the user runs /grok-fix or asks to fix findings, apply review comments,
  or 按意见改 / 修 findings. Verbal requests must be restated as a checklist and
  confirmed before work. Not for open-ended implementation (use grok-work).
  生命周期与安全遵循 grok-delegation。
---

# Grok 按单修复（grok-fix）

## 角色

| 方 | 角色 |
|----|------|
| 任务负责人 | Codex |
| 实际执行者 | **Grok** |
| Codex | 固化清单、发布**更具体的工单**、监工纠偏、验收 |
| Grok | 按单修复者 |

## 何时使用 / 何时不用

**使用**：已有或可整理出 findings/工单；用户要按意见改。

**不用**：

- 开放需求、从零功能 → `grok-work`
- 只要再审一轮 → `grok-review`
- 无修复、只要意见 → `grok-help`

## 步骤

1. **取得清单**：
   - 结构化：来自刚结束的 `grok-review` 或用户提供的同结构列表；
   - 口头：「把空指针那些修了」等 → Codex **先复述 must-fix / optional 清单**，**请用户确认**后再继续。
2. 无清单且无法复述确认 → 拒绝开修，建议先 `grok-review` 或让用户列点。
3. Codex 写**具体工单**（每条：问题、位置/文件、期望行为、是否 must-fix；全局：允许路径、禁止扩大范围、完成定义）。
4. `create_agent`：完整工单进 prompt；角色=按单修复。
5. 报告一次 agent 信息与 observer 链接。
6. 可选 `update_agent` 纠偏（跑偏、扩 scope 时）。
7. `wait` → `result` → **本地验证**（测试/检查/读 diff）。
8. `signoff`；说明已修 / 未修；*建议*是否再 `grok-review`（**不自动**）。

## 给 Grok 的 prompt 要点

- 角色：按单修复，不是开放总包。
- 完整工单列表（编号、must-fix 标记）。
- 允许修改的路径；**禁止**无关重构与 scope creep。
- 完成定义与自测说明要求。

## 退出与 signoff

- `accepted`：must-fix 项验证通过。
- `partial`：只完成一部分或验证不全。
- `rejected`：修错、乱扩 scope 或不可用。

## 禁止

- 无清单（且未复述确认）开修。
- 借 fix 做无关大重构。
- 把开放需求伪装成 fix（应 `grok-work`）。
- 跳过 signoff。

## 协议

生命周期、安全、observer 链接一次、wait 一次、失败降级：遵循 `grok-delegation`。
