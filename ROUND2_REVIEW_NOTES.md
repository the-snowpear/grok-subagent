# Agent Fabric Review Fix Round 2

This branch is a focused follow-up to `4b4dc721d00c28c274218d7215aaf6592e3de163`.

Goals:

- make DB-backed delivery recoverable across daemon restart without killing durable queued turns;
- only mark hub messages delivered after the Grok child actually starts;
- prevent scheduled/auto-injected messages from replaying through inbox/wait;
- remove silent per-message truncation from the delivery prompt;
- keep worktree root separate from worker cwd for subdirectory agents;
- preserve binary untracked worktree artifacts losslessly;
- expose the CLI/native bridge absolute paths to spawned workers so current `grok -p` workers can discover the fallback coordination surface;
- make post-commit scheduling fail-open so a durable send is never reported as failed only because the scheduler callback failed.

Threat-model note: the worker-only TCP surface is protocol isolation, not a same-OS-user security sandbox. A worker with unrestricted shell access can inspect/connect to local processes. Strong adversarial isolation requires an OS/process sandbox, not only loopback capabilities.
