---
name: grok-work
description: >
  将边界清晰的实现任务总包给 Grok 执行；Codex 作为工头负责边界、监工、本地验收与
  signoff，并对用户明示总包关系。Use when the user runs /grok-work or asks Grok
  to implement end-to-end, take the whole task, or 交给 Grok / 总包实现.
  Prefer grok-fix when applying a findings checklist. 生命周期与安全遵循
  grok-delegation。
---

# Grok 总包执行（grok-work）

## 角色

| 方 | 角色 |
|----|------|
| 任务负责人 | **Grok**（业务实现责任） |
| 实际执行者 | **Grok** |
| Codex | 工头、监工、验收人：拆边界、写验收标准、纠偏、验证、signoff |
| Grok | 总承包执行者 |

与用户对话的宿主仍是 Codex，但须**明示**实现已总包。

## 何时使用 / 何时不用

**使用**：边界清晰、可验收；用户要把整段实现交给 Grok。

**不用**：

- 仅顾问意见 → `grok-help`
- 未定方向 → `grok-discuss`
- 只要计划、实现留 Codex → `grok-plan`
- 按 findings 修 → `grok-fix`
- 架构拍板 / 安全敏感 / 破坏性操作仍糊 → Codex 自做或先问用户

## 步骤

1. **先对用户明示话术**（必要）：  
   「本任务已总包给 Grok 执行；我负责监工与验收。」
2. 写清任务边界、输入、约束、**验收标准**、禁止事项；缺验收标准则先补全或问用户。
3. `create_agent`：完整任务进 prompt；短 `agent_name`（如 `work-add-fts`）；需要时设 `cwd`。
4. 报告一次 agent 信息与 observer 链接。
5. 监工：跑偏用 `update_agent`；后续补充用 `send`；不再需要则 `cancel`。
6. `wait` 一次 → `result`。
7. **本地验证**：看变更、跑相关测试/检查，对照验收标准。
8. `signoff`；向用户交付验收结论。失败时降级：Codex 自做、重开更窄任务、或说明阻塞。

## 给 Grok 的 prompt 要点

- 角色：总承包执行者，在边界内完成实现。
- 确切边界、输入、约束、验收标准、交付说明。
- 禁止项（勿碰路径、勿提交密钥等）。
- 要求自测并说明如何验证。

## 退出与 signoff

- `accepted`：达标且本地验证通过。
- `partial`：部分可用。
- `rejected`：不正确、不安全或不可用。

## 禁止

- 对用户隐瞒总包（说成「我自己做的」却实际 Grok 实现）。
- 无验收标准就 `create_agent`。
- 跳过本地验证与 signoff。
- 把按单修复场景硬走 work（应 `grok-fix`）。

## 协议

生命周期、安全、observer 链接一次、wait 一次、失败降级：遵循 `grok-delegation`。本 skill 是通用委托在「完整总包」场景下的剧本加强版。
