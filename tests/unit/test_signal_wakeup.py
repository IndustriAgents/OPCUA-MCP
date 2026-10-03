"""A signal delivered to a worker must wake an idle asyncio loop (#173)."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

if os.name == "posix":

    @pytest.mark.parametrize("signame", ["SIGINT", "SIGTERM"])
    def test_worker_delivered_signal_wakes_an_idle_event_loop(signame):
        script = textwrap.dedent("""
            import asyncio
            import signal
            import sys
            import threading
            import time
            from opcua_mcp_server.server import _exit_on_signal
            from opcua_mcp_server.state import ServerState

            async def main():
                _exit_on_signal(ServerState())
                ready = threading.Event()
                def idle():
                    ready.set()
                    time.sleep(60)
                worker = threading.Thread(target=idle, daemon=True)
                worker.start()
                ready.wait()
                def fire():
                    time.sleep(0.25)
                    signal.pthread_kill(worker.ident, getattr(signal, sys.argv[1]))
                threading.Thread(target=fire, daemon=True).start()
                await asyncio.Event().wait()
            asyncio.run(main())
        """)
        result = subprocess.run(
            [sys.executable, "-c", script, signame],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert "Traceback" not in result.stderr
