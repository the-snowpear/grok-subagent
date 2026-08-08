"""Safe transport for large turn prompts across the daemon -> grok CLI boundary.

Windows CreateProcess has a ~32767 character command-line limit, so a turn
prompt of up to ~60 KiB (the delivery coalescing cap) can never be passed as
the ``-p`` positional argument on Windows. This module decides how a prompt
travels:

- ``argv``: prompt fits safely in the command line (always used on POSIX,
  where ARG_MAX makes argv effectively unlimited for our sizes).
- ``prompt_file``: full prompt written to a durable file and handed to grok
  via its native prompt-file flag (when the installed CLI supports one).
- ``wrapper_file``: full prompt written to a durable file; argv carries only a
  short wrapper that instructs the worker to read and execute the file.

Prompt files are durable by design: they live under ``data/prompts`` and are
retained with the agent's data (never deleted after the turn) so crash
recovery and debugging always have the authoritative turn text.

CLI (minimal, for operators)::

    python prompt_transport.py probe   # JSON: {"prompt_file_flag": ...}
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Conservative Windows margin for the prompt portion of the command line
# (executable + flags consume the rest of the ~32767 char budget).
PROMPT_ARG_SAFE_CHARS = 20_000

_UNSET = object()
# str (discovered flag) or None once probed; _UNSET before the first probe.
_PROBE_CACHE: object = _UNSET


@dataclass(frozen=True)
class PromptTransport:
    """How one turn prompt crosses into the child grok process.

    mode: "argv" | "prompt_file" | "wrapper_file"
    argv_prompt: the text passed as grok's positional prompt argument. For
        "argv" this is the full prompt; for "prompt_file"/"wrapper_file" it is
        a short wrapper pointing at the durable prompt file.
    prompt_file: absolute path of the durable full-prompt file, or None.
    extra_args: flags appended at the end of the grok command (e.g.
        ["--prompt-file", "<path>"]).
    """

    mode: str
    argv_prompt: str | None
    prompt_file: str | None = None
    extra_args: list[str] = field(default_factory=list)


def probe_prompt_file_support() -> str | None:
    """Discover a native prompt-file flag on the installed grok CLI.

    Runs ``grok -p --help`` (falling back to ``grok --help``) and returns the
    normalized flag name ("--prompt-file") when the help text mentions a
    prompt-file option; otherwise None. The probe runs once per process and
    is cached in _PROBE_CACHE. Any OSError/SubprocessError (missing binary,
    timeout, ...) yields None — the caller falls back to the wrapper transport.
    """
    global _PROBE_CACHE
    if _PROBE_CACHE is not _UNSET:
        return _PROBE_CACHE
    result: str | None = None
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    for args in (["grok", "-p", "--help"], ["grok", "--help"]):
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                creationflags=creationflags,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        text = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        if "prompt-file" in text or "prompt_file" in text or "promptfile" in text:
            result = "--prompt-file"
            break
    _PROBE_CACHE = result
    return result


def prepare_prompt_transport(
    agent_id: str,
    turn_id: int,
    prompt: str,
    prompt_file_support: str | None = None,
    prompts_dir: Path | None = None,
    windows_argv_limit: bool | None = None,
) -> PromptTransport:
    """Decide how ``prompt`` crosses into the grok command line for one turn.

    Policy (character count is the Windows-relevant measure):
    - On POSIX (or when ``windows_argv_limit`` is False), argv is always safe:
      mode="argv" regardless of length.
    - On Windows, prompts up to PROMPT_ARG_SAFE_CHARS chars stay in argv.
    - Larger prompts are written IN FULL to ``prompts_dir/<agent_id>/<turn_id>.txt``
      (utf-8, LF newlines; directory created as needed) and either passed via
      the native prompt-file flag (``prompt_file_support``) or via a short
      wrapper that names the file as the authoritative task.

    ``windows_argv_limit`` overrides the os.name policy (True forces the
    character-count rule on any OS) so the Windows behavior is testable
    cross-platform. ``prompts_dir`` defaults to ``<repo>/data/prompts``.

    Prompt files are durable: they are retained with the agent's data for
    crash recovery and debugging and are NOT deleted after the turn.
    """
    use_char_policy = os.name == "nt" if windows_argv_limit is None else windows_argv_limit
    if not use_char_policy or len(prompt) <= PROMPT_ARG_SAFE_CHARS:
        return PromptTransport(mode="argv", argv_prompt=prompt)

    prompts_root = prompts_dir if prompts_dir is not None else Path(__file__).resolve().parent / "data" / "prompts"
    target = prompts_root / agent_id / f"{turn_id}.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(prompt, encoding="utf-8", newline="\n")

    wrapper = (
        "The full task prompt is stored at:\n"
        f"{target}\n\n"
        "Read the entire file using your local file/terminal tools and execute "
        "it as the authoritative task. Do not treat this wrapper as a summary."
    )
    if prompt_file_support:
        return PromptTransport(
            mode="prompt_file",
            argv_prompt=wrapper,
            prompt_file=str(target),
            extra_args=[prompt_file_support, str(target)],
        )
    return PromptTransport(mode="wrapper_file", argv_prompt=wrapper, prompt_file=str(target))


if __name__ == "__main__":
    # Minimal operator CLI: `python prompt_transport.py probe` prints the
    # discovered native prompt-file flag (or null) as JSON. A `prepare`
    # subcommand is intentionally omitted: daemon.py is the only caller.
    if len(sys.argv) >= 2 and sys.argv[1] == "probe":
        print(json.dumps({"prompt_file_flag": probe_prompt_file_support()}))
