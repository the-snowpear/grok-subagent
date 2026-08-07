"""MCP entrypoint that starts the observer with live streaming enabled."""

from pathlib import Path

import server


server.DAEMON = Path(__file__).resolve().parent / "live_streaming.py"


if __name__ == "__main__":
    server.main()
