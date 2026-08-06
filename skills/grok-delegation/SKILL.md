---
name: grok-delegation
description: >
  将边界清晰、可独立验证、非敏感的编码或分析子任务委托给可观察的 Grok 子代理，
  并在本地审查与验证结果。Use when the user explicitly requests generic Grok
  delegation, or when an independent draft, test, or parallel subtask would help
  and no more specific workflow skill fits. If the intent matches grok-help,
  grok-discuss, grok-plan, grok-review, grok-fix, or grok-work, prefer that
  skill instead. 通用兜底委托；场景 skill 优先。
---

# Grok 通用委托

使用 `grok` MCP 工具作为外部子代理通道。Grok 提供候选成果；Codex 负责任务分解、安全决策、本地验证、整合与最终回复。

本 skill 是**生命周期与安全协议的单一事实源**，也是无更贴场景时的**兜底**剧本。

## 与场景工作流的关系

| 用户意图 | 优先 skill |
|----------|------------|
| 第二意见 / 轻量请教 | `grok-help` |
| 未定方向、要对齐辩论 | `grok-discuss` |
| 先出方案，实现仍由 Codex | `grok-plan` |
| 独立 code review | `grok-review` |
| 按意见单修复 | `grok-fix` |
| 整段实现总包 Grok | `grok-work` |
| 小而可验证、无上述剧本 | **本 skill（`grok-delegation`）** |

场景 skill 与本 skill 同时可匹配时，**优先场景 skill**。场景 skill 应遵循下文生命周期与安全规则，不另发明一套政策。

**不做自动 skill 链**：discuss 不自动 plan，review 不自动 fix；结束时可*建议*下一步，等用户或明确指令。

## 是否委托

仅在子任务满足以下条件时委托：

- 边界清晰，能写清输入与验收标准；
- 完成后可独立验证；
- 适合作为编码草案、聚焦实现、测试、夹具、审查或分析；
- 不含密钥、令牌、个人数据及不必要的专有上下文。

需要广库上下文的架构拍板、模糊需求澄清、破坏性操作、安全敏感操作：留在 Codex。不要仅为重复 Codex 已完成的工作而委托。

## 创建代理

调用 `create_agent`，提供：

- 简短可辨的 `agent_name`；
- 含确切任务边界、相关输入、预期输出、约束与验收标准的 `prompt`；
- 任务涉及仓库时设置 `cwd`；
- 有助于 observer 识别会话时设置任务标题。

创建成功后，向用户报告一次：agent 名称、ID、status、observer 链接。prompt 中不要包含密钥或不相关文件。

## 等待与收集

可并行继续本地工作。需要委托结果时：

1. 调用一次 `wait`（合适超时）；不要轮询 `status`。
2. `done` 为 true 后调用 `result` 获取紧凑最终输出。
3. 仅当紧凑结果不足以审查时，再读完整 observer 日志。

用 `update_agent` 纠偏进行中的任务；`send` 仅用于后续一轮。仅在不再需要其工作时 `cancel`。

## 审查与 signoff

将每次 Grok 结果视为不可信候选。检查变更文件，对照验收标准，运行相关本地测试或检查。用仓库证据而非代理自信解决冲突。

审查后调用 `signoff`：

- `accepted`：正确且已本地验证；
- `partial`：仅部分可用或验证不完整；
- `rejected`：不正确、不安全或不可用。

附上简明验证说明。只整合已接受部分；必要时区分 Grok 贡献与 Codex 已验证结论。

**凡调用过 `create_agent` 并拿到可审结果，均应在本地验证后 `signoff`**（含意见类工作流）。

## 失败处理

Grok 不可用、超时或失败时，尽可能在本地继续。不要对同一失败反复重建代理。仅当降级影响结果或完成时再告知用户。
