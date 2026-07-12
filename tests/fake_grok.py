"""Deterministic Grok CLI stand-in used by observer integration tests."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


# Optional slow mode so tests can race send/wait/cancel while a turn is active.
duration = float(os.environ.get("GROK_FAKE_DURATION", "0.08"))
marker = os.environ.get("GROK_FAKE_MARKER")
if marker:
    Path(marker).write_text(f"started:{time.time()}\n", encoding="utf-8")

emit({"type": "thought", "data": "先检查输入与工作目录。"})
time.sleep(max(duration * 0.4, 0.01))
emit({"type": "text", "data": "已完成测试任务。\n\n```python\nprint('observer ok')\n```"})
time.sleep(max(duration * 0.4, 0.01))
emit({"type": "end", "stopReason": "EndTurn", "sessionId": "fake-session", "requestId": "fake-request"})

if marker:
    with Path(marker).open("a", encoding="utf-8") as handle:
        handle.write(f"finished:{time.time()}\n")

sys.exit(0)
