"""Native Grok MCP transport support: capability probe and opt-in plugin generator.

Honest limitation (probed against grok 1.0.0): automatic native MCP
registration is unavailable for the current worker transport. ``grok -p``
(headless prompt mode, the transport the daemon uses to run workers) has no
``--plugin-dir`` / MCP flag, so workers cannot be auto-registered with MCP
plugins. ``grok agent --plugin-dir`` exists, but the daemon does not run
workers on the stdio ``grok agent`` transport.

The generated plugin dir is therefore the opt-in path for future
``grok agent stdio --plugin-dir <dir>``-based workers: it points the MCP
server named ``grok-hub`` at this repository's native_bridge.py so the bridge
exposes its peers/send/inbox/wait tools to a Grok agent runtime. The
grok_hub CLI remains the actual fallback transport for today's
``grok -p``-spawned workers.

The probe never fabricates support: it runs the real ``grok`` binary and
reports what it observes. Plugin generation never mutates the project
``.mcp.json`` or ``~/.grok/config.toml``; it only writes under the requested
(observer data) directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# Suppress console windows when probing a GUI-less subprocess on Windows.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

_PROBE_TIMEOUT_SECONDS = 15

_PLUGIN_NOTE = (
    "grok -p worker transport cannot load MCP plugins; generated plugin dirs "
    "target grok agent stdio --plugin-dir (opt-in)"
)


def _run_grok(args: list[str]) -> str | None:
    """Run ``grok`` with the given args; return combined output or None on failure."""
    try:
        result = subprocess.run(
            ["grok", *args],
            capture_output=True,
            text=True,
            # Explicit UTF-8: grok output is UTF-8; on ACP-936 Windows the
            # implicit locale decode would mojibake or crash the probe.
            encoding="utf-8",
            errors="replace",
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout + result.stderr


def _first_non_empty(text: str) -> str | None:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return None


def probe_grok_transport() -> dict:
    """Probe the installed ``grok`` binary and report its MCP plugin support.

    Returns a dict with the documented keys:

    - version: first non-empty line of ``grok --version`` output, or None.
    - binary_found: True when ``grok`` ran successfully (version is not None).
    - agent_stdio_plugin_dir_supported: whether ``grok agent --help`` mentions
      ``--plugin-dir`` (the stdio agent transport).
    - prompt_mode_plugin_supported: whether ``grok -p --help`` mentions
      ``--plugin-dir`` (the daemon's worker transport).
    - injection: "automatic" when prompt mode supports plugins, else
      "unavailable" (honest: no automatic per-worker registration exists).
    - fallback: "grok_hub CLI" (the transport the daemon actually uses).
    - note: plain-language explanation of the limitation.

    Every probe failure (binary missing, timeout, OSError) is treated as
    missing/unsupported — never as fabricated support.
    """
    version_text = _run_grok(["--version"])
    version = _first_non_empty(version_text) if version_text is not None else None

    agent_help = _run_grok(["agent", "--help"]) or ""
    prompt_help = _run_grok(["-p", "--help"]) or ""

    agent_stdio_plugin_dir_supported = "--plugin-dir" in agent_help
    prompt_mode_plugin_supported = "--plugin-dir" in prompt_help

    return {
        "version": version,
        "binary_found": version is not None,
        "agent_stdio_plugin_dir_supported": agent_stdio_plugin_dir_supported,
        "prompt_mode_plugin_supported": prompt_mode_plugin_supported,
        "injection": "automatic" if prompt_mode_plugin_supported else "unavailable",
        "fallback": "grok_hub CLI",
        "note": _PLUGIN_NOTE,
    }


def write_worker_plugin_dir(agent_id: str, base_dir: Path | None = None) -> Path:
    """Generate an opt-in MCP plugin dir for one worker and return its path.

    Writes ``<base_dir>/<agent_id>/.mcp.json`` declaring an ``mcpServers``
    entry named ``grok-hub`` that launches this repository's native_bridge.py
    with the current Python interpreter. This dir is opt-in: it is consumed by
    future ``grok agent stdio --plugin-dir <target>``-based workers and is
    never auto-registered. The function never mutates the project ``.mcp.json``
    or ``~/.grok/config.toml``; everything it creates lives under
    ``base_dir`` (default: the observer data directory ``data/worker-mcp``).
    """
    if base_dir is None:
        base_dir = Path(__file__).resolve().parent / "data" / "worker-mcp"
    target = base_dir / agent_id
    target.mkdir(parents=True, exist_ok=True)

    bridge_path = str((Path(__file__).resolve().parent / "native_bridge.py").resolve())
    plugin = {
        "mcpServers": {
            "grok-hub": {
                "command": sys.executable,
                "args": [bridge_path],
            }
        }
    }
    (target / ".mcp.json").write_text(
        json.dumps(plugin, indent=2), encoding="utf-8"
    )
    return target


def _print_usage() -> None:
    print(
        "usage: native_transport.py probe\n"
        "       native_transport.py plugin <agent_id> [base_dir]",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    """Small CLI: ``probe`` prints the transport capability report as JSON.

    ``plugin <agent_id> [base_dir]`` generates a worker plugin dir (default
    base_dir: the observer data directory ``data/worker-mcp``) and prints the
    target path. Unknown usage prints usage to stderr and exits 2.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        _print_usage()
        return 2

    command = args[0]
    if command == "probe":
        print(json.dumps(probe_grok_transport(), ensure_ascii=False, indent=2))
        return 0

    if command == "plugin":
        if len(args) < 2:
            _print_usage()
            return 2
        base_dir = Path(args[2]) if len(args) > 2 else None
        print(write_worker_plugin_dir(args[1], base_dir))
        return 0

    _print_usage()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
