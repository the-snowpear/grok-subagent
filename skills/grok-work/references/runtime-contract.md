# Runtime contract for grok-work orchestrate

`grok-work` 的 orchestrate 语义依赖 Agent Fabric runtime 能力。不要只靠 prompt 假装能力存在。

## 1. Reasoning effort — 已实现

canonical 字段统一为：

```text
reasoning_effort
```

Runtime 现状（branch `agent/grok-work-orchestrate-v2` 起）：

- `create_agent` 支持可选 `reasoning_effort`；
- `create_agents` 支持 batch default 与 per-item override；
- 未传时 runtime default = `max`；
- agent 行持久化 resolved value（`agents.reasoning_effort`，迁移 DEFAULT 'max'，幂等）；
- completed-worker follow-up 继续使用原 agent 的 resolved value（同一 agent 行，不重新解析）；
- status/result/catalog/detail 暴露 resolved value；
- child CLI 表示已对安装版验证：grok 1.0.0（3cd0d0cbce）的 `grok -p` 与 `grok agent` 均提供
  `--reasoning-effort <EFFORT>`（alias `--effort`），CLI 不校验取值（实测任意字符串与 `max` 均被接受），
  因此 runtime 做格式校验（`^[A-Za-z][A-Za-z0-9_-]{0,31}$`）后**原样透传**；`max` 是默认值且为可接受字面量。

优先级：

```text
per-agent explicit
> workflow/batch invocation override
> profile default（当前 profile 无 effort 字段，跳过）
> runtime default=max
```

不要新增 `thinking` / `effort` / `thinking_level` / `reasoning` 等内部别名；用户层 CLI 别名可以存在，但 runtime 存储/API 只用 `reasoning_effort`。

诚实性边界：runtime 能保证 flag 被传递与持久化，但 grok 1.0.0 不提供每 turn“实际思考强度”遥测，模型内部是否完全执行该值无法由 runtime 验证——文档如实说明，不伪造遥测。

## 2. Flat topology and authority

Codex/Main 是唯一 orchestrator。所有 Grok workers 是直接 child/peer。

保持 worker native nested-subagent 能力关闭（grok_command 始终带 `--no-subagents`），避免双 control plane。

Hub 可以支持 peer evidence/clarification，但**任务 authority 属于 Main**：reviewer finding 先交 Main adjudicate，只有 Main 批准的 Fix Order 才发送给 Implementer。Reviewer 本身无写权限策略、不会生成 follow-up turn。

## 3. Role profiles — 已实现（create-time resolution）

`create_agent` / `create_agents` 支持可选 `role`：

```text
explore:   共享 cwd（worktree_default=False），prompt 带只读策略
implement: 隔离 worktree（worktree_default=True），允许写
review:    共享 cwd（worktree_default=False），prompt 带只读策略
```

- 显式 `worktree` 参数总是覆盖 role 默认；
- role 不覆盖显式 profile；只填充 worktree 默认与 prompt 策略后缀；
- 无新 DB 列（role 在创建时解析；follow-up 复用同一 agent 行，设置已固定）；
- **限制（如实记录）**：read-only 是 prompt 策略，不是 OS/tool 级 sandbox。不要声称硬隔离。

## 4. Durable follow-up

Main 批准的 reviewer findings 通过 Hub `send` 给原 Implementer：

```text
review finding -> Main adjudication -> Fix Order -> message committed
-> completed implementer follow-up -> same agent/session/effort/worktree context repairs
```

不要为每轮小修新建失忆 Implementer。最多 `max_fix_rounds`（默认 2）；反复失败时 Main 重新决策。

## 5. Batch orchestration

`create_agents` 是 targeted discover / independent implementation fan-out 的首选：

- 每个 item 独立 role/worktree/reasoning_effort（item 覆盖 batch 默认）；
- 返回所有 agent IDs；
- 支持统一 wait/result 聚合；
- 并发限制达到时明确返回未创建 items，不静默丢任务；
- batch-level 的 reasoning_effort / role 在循环前验证一次，非法值整批失败且不留 ghost state。

不要把 batch 能力解释成“默认多开 agent”；`orchestration_budget=efficient` 决定最小充分 fan-out。

## 6. Result compression / artifact-first

`result` action 已返回结构化 envelope（`kind=agent_result`、`final_text`、`changes`、`isolation`、`reasoning_effort`）；worktree patch 与 untracked 文件以无损 artifact 形式外置（raw-gzip patch + base64 untracked，含 sha256/size 元数据）。为此 iteration **未新增** compact result 模式：Skill 的 packet 契约已把 worker 输出压缩为摘要，verbose 材料走 artifact。不要引入第二次模型 summarization 调用；不要截断证据。

## 7. Worktree ownership

Implementer 默认 isolated worktree。

Orchestrator 先划 ownership：同一核心文件有重叠 writer 时不并行；改为重新拆 unit 或串行。

Reviewer 不需要写 worktree；通过 result/patch artifact 审查实际变更。

删除安全由 runtime 结构性保证：rmtree 仅允许 `DATA/worktrees` 严格后代，主仓库任何情况下不可成为删除目标。

## 8. Optional orchestration metadata

当前持久化的与 orchestration 相关字段：`reasoning_effort`（agent 行）。`role` / `parent_phase` / `work_order_id` / `decision_snapshot_id` 等追踪字段未持久化（避免无功能价值的 schema churn）；如后续需要 observer 成本分析再按实际价值添加。runtime 不应接管 Main 的决策逻辑。

## 9. Required regression tests

对应 runtime 能力的测试位于 `tests/test_orchestrate_v2.py`（行为级，非源码字符串断言）：

1. omitted effort => resolved `max`；
2. explicit effort survives create -> DB -> result；
3. batch default + per-item override；
4. completed-agent follow-up preserves original effort（不因 daemon 默认值变化而重解析）；
5. invalid effort fails before agent creation（无 ghost worktree/turn/search_index）；
6. child command receives verified CLI representation（`--reasoning-effort <value>`）；
7. explorer/reviewer 角色默认共享 cwd + 只读策略 prompt，implement 默认 worktree（显式 worktree 覆盖 role）；
8. flat-topology invariant（`--no-subagents` 始终存在）；
9. 结构化 result 指向完整 artifact（无损）；
10. follow-up 工作由 Main 发出的 Fix Order 驱动（reviewer finding 不直接产生运行时变更）。
