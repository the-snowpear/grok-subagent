# Runtime contract for grok-work orchestrate

`grok-work` 的 orchestrate 语义依赖 Agent Fabric runtime 能力。不要只靠 prompt 假装能力存在。

## 1. Reasoning effort — 已实现

canonical 字段统一为：

```text
reasoning_effort
```

Runtime 现状（branch `agent/grok-work-orchestrate-v2` 起）：

- `create_agent` / `create_agents` 的 **model-facing MCP schema**（server.py `tools/list`）暴露 `reasoning_effort`；
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

## 2. Flat topology

Codex/Main 是唯一 orchestrator。所有 Grok workers 是直接 child/peer。保持 worker native nested-subagent 能力关闭（grok_command 始终带 `--no-subagents`），避免双 control plane。

## 3. Roles — 持久化 + Main-owned follow-up scheduling authority

`create_agent` / `create_agents` 的 **model-facing MCP schema** 暴露 `role`；resolved role 持久化到 `agents.role`（迁移 `ALTER TABLE agents ADD COLUMN role TEXT`，幂等；legacy 行保持 `NULL`，不给历史普通 agent 强赋角色）。

```text
explore:    prompt 带只读策略；git-backed 任务由 grok-work 契约默认 isolated worktree
implement:  默认 isolated worktree（worktree_default=True），允许写
review:     prompt 带只读策略；git-backed 任务由 grok-work 契约默认 isolated worktree
```

- worktree precedence：显式 `worktree` > 显式非 default profile > role `worktree_default` > default profile；
  显式安全 profile（如 `isolated`）不会被 role 默认降级；grok-work 的 git-backed role invocation
  默认显式 `worktree=true`（Explorer/Implementer/Reviewer 全隔离）；
- `status` / `result` / viewer public/detail / hub peer list 暴露 role；
- completed follow-up 复用同一 agent 行，role 自然继承，不重新解析。

### Hard runtime guarantee（Main-owned auto follow-up scheduling）

runtime 的 `maybe_schedule_delivery` 对 **role-tagged** completed worker 施加 sender gate：

- `agents.role IN ('explore','implement','review')` 的 completed worker，只有
  `message.from_peer == main_peer_id(agent.thread_id)`（即 Main）的 pending 消息才能被自动 claim
  到一个新的 follow-up turn；
- 来自其他 worker（peer）的消息：不删除、不 mark delivered、不 consumed、`target_turn_id` 保持 NULL、
  不创建 follow-up、保持 pending。即 **reviewer/peer message 不拥有自动 follow-up scheduling authority**；
- 该 gate 对所有 role-tagged workers 生效（explore/implement/review），所以 Main 可以唤醒
  Implementer Fix Order、Explorer clarification follow-up、Reviewer re-review request；
  而 Reviewer→Implementer、Explorer→Reviewer、Implementer→Reviewer 均不能自动创建 turn；
- `agents.role IS NULL` 的 legacy agents 保持历史 Agent Fabric auto-followup 行为不变；
- recovery/delivery sweep 走同一个 gate：peer-only pending 不会唤醒 role-tagged worker；
- authority sender filtering 发生在 delivery batch LIMIT **之前**（SQL 层先按
  `from_peer = main_peer_id(thread_id)` 过滤，再 `ORDER BY created_at,id LIMIT 100`）。
  Unauthorized peer messages cannot consume the authorized delivery selection
  window and therefore cannot starve newer Main-authored work（control-plane liveness invariant）。

worker 如果处于 active/running 状态并主动通过 inbox/wait 读取 peer clarification，现有协作能力继续存在；
只是 completed orchestrate worker 不会被 peer 自动重新激活。

### Prompt policy only（不是 OS sandbox）

以下仍是 prompt 策略，runtime 不做硬强制：

- explore/review 不修改文件；
- implementer 不扩大 scope；
- worker 不根据 peer 建议自行改变架构；
- read-only 不是 OS/tool-level sandbox。不要声称硬隔离。

措辞注意：runtime 实际仍允许 peer `send` 消息，所以不要说“Reviewer cannot send messages”；准确表述是
“Reviewer/peer message does not possess automatic follow-up scheduling authority.”

## 4. Durable follow-up

Main 批准的 reviewer findings 通过 Hub `send` 给原 Implementer：

```text
review finding -> Main adjudication -> Fix Order -> message committed
-> completed implementer follow-up -> same agent/session/effort/role/worktree context repairs
```

不要为每轮小修新建失忆 Implementer。最多 `max_fix_rounds`（默认 2）；反复失败时 Main 重新决策。

## 5. Batch orchestration

`create_agents` 是 targeted discover / independent implementation fan-out 的首选：

- 每个 item 独立 role/worktree/reasoning_effort（item 覆盖 batch 默认）；batch-level 的
  reasoning_effort / role 在循环前验证一次，非法值整批失败且不留 ghost state；
- 返回所有 agent IDs；支持统一 wait/result 聚合；并发限制达到时明确返回未创建 items，不静默丢任务。

不要把 batch 能力解释成“默认多开 agent”；`orchestration_budget=efficient` 决定最小充分 fan-out。

## 6. Result compression / artifact-first

`result` action 已返回结构化 envelope（`kind=agent_result`、`final_text`、`changes`、`isolation`、
`reasoning_effort`、`role`）；worktree patch 与 untracked 文件以无损 artifact 形式外置（raw-gzip patch +
base64 untracked，含 sha256/size 元数据）。不要引入第二次模型 summarization 调用；不要截断证据。

## 7. Worktree ownership

Implementer 默认 isolated worktree；grok-work 契约下 git-backed Explorer/Reviewer 也默认 isolated
worktree（显式 `worktree=false` 仅当任务明确依赖未提交工作区内容，由 Main 决定并记录原因）。

Orchestrator 先划 ownership：同一核心文件有重叠 writer 时不并行；改为重新拆 unit 或串行。

删除安全由 runtime 结构性保证：rmtree 仅允许 `DATA/worktrees` 严格后代，主仓库任何情况下不可成为删除目标。

## 8. Optional orchestration metadata

当前持久化的与 orchestration 相关字段：`reasoning_effort` 与 `role`（agent 行）。
`parent_phase` / `work_order_id` / `decision_snapshot_id` 等追踪字段未持久化（避免无功能价值的 schema churn）；
如后续需要 observer 成本分析再按实际价值添加。runtime 不应接管 Main 的决策逻辑。

## 9. Required regression tests

对应 runtime 能力的测试位于 `tests/test_orchestrate_v2.py` 与 `tests/test_model_boundary.py`
（行为级，非源码字符串断言）：

1. model-facing schema（server.py `tools/list`）暴露 create_agent 的 role/reasoning_effort；
2. model-facing schema 暴露 create_agents 的 batch role/effort 与 per-item role/effort；
3. 真实 tool dispatch（tools/call -> server.call_tool -> daemon.action）原样保留字段并写入
   agents.role / agents.reasoning_effort；
4. omitted effort => resolved `max`；explicit effort survives create -> DB -> result；
   batch default + per-item override；
5. completed-agent follow-up preserves original effort/role（不因 daemon 默认值变化而重解析）；
6. invalid effort/role fails before agent creation（无 ghost worktree/turn/search_index）；
7. role 持久化：create -> DB -> status/result -> follow-up 全程一致；legacy role=NULL 保持旧行为；
8. authority：Reviewer→completed Implementer 不产生 follow-up；Main→Implementer 恰好一次 follow-up；
   mixed queue 只 claim Main 消息；recovery/sweep peer-only 不唤醒；Main→Reviewer re-review 允许；
9. child command receives verified CLI representation（`--reasoning-effort <value>`）；
10. flat-topology invariant（`--no-subagents` 始终存在）；
11. git-backed explore/review 默认 invocation 走 isolated worktree，dirty parent 不被触碰；
12. 结构化 result 指向完整 artifact（无损）；follow-up 工作由 Main 发出的 Fix Order 驱动。
