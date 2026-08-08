---
name: grok-work
description: >
  以低主模型成本的 OMP-style orchestrate 模式执行实现任务：Codex/Main 保留需求、架构、拆分、风险取舍、review adjudication、集成与最终 signoff 等关键决策权；代码库探索、代码实现和独立审查交给 Grok Agent Fabric workers。Use when the user runs /grok-work, asks Grok to implement end-to-end, wants 总包/多代理实现, or wants Codex to orchestrate cheaper workers while keeping high-quality decisions. Default worker reasoning effort is max unless explicitly overridden. Prefer grok-fix for an existing findings checklist; lifecycle and safety follow grok-delegation.
---

# Grok Work — Thin-Orchestrator 总包模式

## 目标

优化两个指标，而不是单纯“多开代理”：“把高价值决策留给 Codex，把高 token 的搜集/执行工作下沉给 Grok”。

- **质量**：Codex/Main 负责所有会改变方向、接口、风险和验收结论的决策。
- **成本**：Codex/Main 保持 thin context；workers 读取代码、跑测试、写实现、做独立 review，只向 Main 回传压缩后的证据包。
- **可执行性**：Codex 把决定后的任务加工成明确、细粒度 work order，让较小模型少猜、少返工。

## 权责

| 方 | 可以做 | 不可以做 |
|---|---|---|
| **Codex/Main** | 解释需求、架构/接口决策、拆 DAG、划 ownership、风险取舍、验收标准、findings adjudication、派修复、机械集成、全局验证、signoff | 默认不做实质代码探索、编码或主要 code review |
| **Explorer** | 读取代码/测试/文档，回答一个明确调查问题，给证据和选项 | 改代码、扩大 scope、替 Main 拍架构 |
| **Implementer** | 按已决定的 work order 在 ownership 内实现和自测 | 改需求、改跨 unit contract、自我 signoff |
| **Reviewer** | 独立找 correctness/spec/test 风险，给分级 findings | 改代码、直接命令 Implementer、替 Main 接受风险 |

所有 worker 都是 Main 的直接 peer；禁止 worker 再生成嵌套 subagents（runtime 层 `--no-subagents` 保持强制，`nested_subagents=false`）。

### Main 的保留决策

以下事项只能由 Codex/Main 决定：用户意图与 acceptance criteria；架构、公共接口、数据模型、状态机、兼容策略；unit 边界、ownership、串并行；是否接受 finding / residual risk；P1/P2 修复策略与是否扩 scope；是否集成、是否 signoff。

worker 遇到这些问题时返回 `DECISION_NEEDED`，附 2–3 个选项、证据和 trade-off；不要自行拍板。

## 默认策略

除非用户显式覆盖：

```text
reasoning_effort = max          # worker thinking；不是 Main token budget
orchestration_budget = efficient
main_context_mode = thin
strict_subagents = true
strict_main_decisions = true
max_fix_rounds = 2
nested_subagents = false
```

- `reasoning_effort` 是 **runtime 真实字段**：`create_agent` / `create_agents` 支持传入（含 batch default 与 per-item override），未传时默认 `max` 并持久化在 agent 行上，durable follow-up 继承原值；child CLI 使用已验证的表示 `--reasoning-effort <value>`（grok 1.0.0）。不要把“max”只写在 prompt 里。能力边界见 `references/runtime-contract.md`。
- `efficient` 的含义是**最小充分 agent graph**，不是降低 worker 思考强度。不要因为并发能力存在就无条件 fan-out。

## Workspace 隔离（git-backed 任务默认）

对 git-backed 实现任务，**三个角色默认都在 isolated worktree 执行**：

```text
Explorer:    worktree=true
Reviewer:    worktree=true
Implementer: worktree=true
```

worker 不直接站在用户 dirty working tree 上。例外：**只有任务明确依赖当前未提交工作区内容**，Main 才可以决定 `worktree=false`，并在 work order 中记录：为什么必须读取 dirty state；read-only 仍只是 prompt policy，不能声称硬 sandbox。runtime 保持 generic（显式 `worktree` 覆盖 role 默认；非 git cwd 自动回落共享目录）。

## 先定复杂度，再定 agent graph

Codex 仅根据用户请求和已有高层上下文做一次轻量分类，不先自己扫描整个 repo。

- **S / 局部低风险**：1 Explorer（代码+测试合并调查）→ 1 Implementer → 1 Reviewer；通常不再加 aggregate reviewer。
- **M / 跨文件或有未知点**：1–2 Explorer → 1–2 non-overlap Implementer → 1 Reviewer；仅多 unit 时做 aggregate review。
- **L / 高风险**：2–3 Explorer → 1–2 Implementer → spec/code 双 Reviewer → aggregate review。高风险包括 concurrency、durability、migration、安全、破坏性文件操作、公共协议/兼容性。

只有发现新的独立 uncertainty axis 才增加 Explorer/Reviewer。优先**升级深度**而不是增加同质 agent 数量。

## Phase 0 — Intake + decision frame

1. 明示：「本任务按 Grok orchestrate 模式执行；我作为 Codex/Main 保留决策与验收，探索、实现和独立审查交给 Grok workers。」
2. Codex 写一个短 decision frame：目标、非目标、关键约束、已知风险、验收标准。
3. 存在会改变产品/架构方向的歧义时先问用户；不要让 worker 猜。
4. 选择 S/M/L 与 agent graph。

Main 在后续只维护一份**状态摘要**：目标、当前 plan、关键决策、未决问题、agent IDs、最新 verdict。不要反复携带整段对话和原始输出。

## Phase 1 — Targeted evidence acquisition

Explorer 不是“自由探索整个仓库”，而是每人只回答一个 Main 定义的问题。用 `role="explore"` 创建（git-backed 默认 isolated worktree；只读策略）。优先 `create_agents` 批量启动独立调查；一次 batch wait，不逐个轮询。

Explorer 返回 **Evidence Packet**（模板见 `references/decision-budget-contract.md`）。禁止长代码粘贴、完整日志、泛泛教程；详细内容放 artifact/file，只给 Main 路径和必要摘要。

## Phase 2 — Codex decision gate

这是本 workflow 的核心价值点。Codex/Main 根据 Evidence Packets 自己完成：选方案、定不做的备选、划 ownership、定接口与状态不变量、定 required tests 和 review focus、识别必修与可接受风险。不要把“选方案”继续下发给 Implementer。

完成决策后，把 explorer 原始结果压缩成 10–25 行的 `Decision Snapshot`（模板见 `references/decision-budget-contract.md`）。后续 prompts 只带选中的证据和 snapshot。

## Phase 3 — Build precise work orders

每个 Implementer 都收到独立、详细但去噪的 Work Order（模板见 `references/decision-budget-contract.md`）。用 `role="implement"` 创建（默认隔离 worktree）。**详细 ≠ 把聊天记录全塞进去。** Main 先消化复杂上下文，再把决定、证据和边界编译成 task packet。

## Phase 4 — Implement fan-out

只对真正无重叠的 units 并行创建 Implementer；默认 `worktree=true`、`reasoning_effort=<resolved>`。不重新探索整个仓库；不扩大需求；自行跑 focused tests；verbose diff/log 写入 artifact，不向 Main 倾倒；返回目标约 400–700 tokens 的 `Implementation Packet`。若两个 units 会实质修改同一核心文件，Main 改成串行或重新划 ownership。

worker 失败时优先缩小任务/补充 work order；不要自动由 Codex 接管编码。

## Phase 5 — Independent review

每个实现至少有一个 fresh Reviewer。用 `role="review"` 创建（git-backed 默认 isolated worktree；只读策略）。Reviewer 收到：Work Order、acceptance criteria、patch/result artifact、必要的 Decision Snapshot；不需要整段 Explorer 对话。默认只开 **1 个强 Reviewer**；仅 S/M 任务发现高风险，或 L 任务，才增加第二个 specialist reviewer。

Reviewer 只提出 findings（分级模板见 `references/decision-budget-contract.md`），**不直接给 Implementer 下任务**。runtime 保证：reviewer/peer 消息没有自动唤醒 completed role-worker 的 scheduling authority——只有 Main 的 Fix Order 能驱动修改。

## Phase 6 — Codex adjudication + durable fix loop

所有 findings 先回到 Main。Codex 必须：判断 finding 是否成立、决定 severity、解决 reviewer 间冲突、决定修复边界、生成新的精确 Fix Order。只有 Main 批准的 findings 才通过 Agent Fabric `send` 发给**原 Implementer**，利用 completed-worker durable follow-up 保留实现上下文（同一 agent/session/effort/role/worktree 契约）。修复后 Main 将新结果送给原 Reviewer verification；scope 实质扩大才创建 fresh Reviewer。最多 `max_fix_rounds`。

worker-to-worker Hub 消息可用于**事实澄清/证据请求**，不得作为改变 scope 或分配工作的 authority channel。

## Phase 7 — Integration + verify

只集成 Main 已 adjudicate 且 Reviewer 无 blocker 的实现。验证从便宜到昂贵：focused tests / static checks → 受影响模块 tests → 最后才跑 full suite。长日志保存到文件/artifact，仅提取 failure summary；需要诊断时派一个 targeted Explorer/Debugger 读取日志并回传 Evidence Packet。多 unit 或高风险变更才创建 final aggregate Reviewer。最终 signoff 由 Codex/Main 决定。

## Main context discipline

把 Codex context 当昂贵资源：Main **不做 repo-wide grep/read**，除非为解决一个具体决策缺口；不读取完整 patch/log，除非 reviewer 证据冲突且必须亲自裁决；worker 输出必须 summary-first，verbose data 外置 artifact；每阶段结束只保留/复述 `Decision Snapshot`；相同事实只保存一个 canonical 版本；使用 batch create/wait，避免 polling 对话膨胀；completed worker 的小修优先 durable follow-up；不向多个 workers 重复发送无关背景。

原则：**Main 消耗 token 做判断，不消耗 token 做搜索、抄代码、读长日志。**

## Cost guardrails

默认 `orchestration_budget=efficient`：agent 数量取满足独立性和 review 的最小值；S/M 默认 1 Reviewer，不机械双审；final aggregate review 按风险触发；Explorer 只为“会改变 Main 决策”的未知点创建；同一问题不让多个 agent 做同质重复研究；fix loop 最多 2 轮，反复失败时 Main 重新决策；worker `reasoning_effort=max` 与“低成本”不冲突——成本从昂贵 Main context 和无效 fan-out 中省，而不是让小模型少想。

如果用户要求更省，可切到 `lean`：仍保留 1 Explorer + 1 Implementer + 1 Reviewer 的最小独立闭环。

## 失败与升级

- Evidence 不足：Main 精确提出一个新调查问题，而不是让 Explorer“再全面看看”。
- Implementer 返回 `DECISION_NEEDED`：Main 决策后用 `send` 补充，不让 Implementer 自己扩 scope。
- Reviewer 分歧：Main 裁决；必要时只新增一个 tie-break Reviewer。
- Agent Fabric/runtime 缺关键能力：明确 capability gap，不伪造。
- 默认禁止“worker 失败 → Codex 自己写完”；只有用户明确退出 orchestrate 才允许。

## Signoff

- `accepted`：Main 的 acceptance criteria 全部满足；独立 review 无 blocker；集成态验证通过。
- `partial`：存在明确未完成 unit / 环境 blocker。
- `rejected`：无法安全达到验收标准。

最终报告以 Main 的决策为主线：`Decision → Workers executed → Review evidence → Main adjudication → Verification → Signoff`。不要把子代理实现表述成 Codex 自己写的。

## 相关协议

- 生成 Work Order / Evidence Packet / Fix Order / Decision Snapshot 时读取 `references/decision-budget-contract.md`（模板与 context-budget 契约）。
- 需要确认 runtime capability（reasoning_effort、role 持久化、Main-owned follow-up scheduling、flat topology、worktree）时读取 `references/runtime-contract.md`。
- 生命周期、安全、Hub、observer 与 durable follow-up 继续遵循 `grok-delegation`。
