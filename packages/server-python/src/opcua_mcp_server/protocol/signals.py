"""Bounded signal cleanup and event-loop wakeup for the stdio protocol."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import threading

from ..connection import describe_error
from ..state import ServerState

SHUTDOWN_GRACE_SECONDS = 5.0


def _exit_on_signal(state: ServerState, release_opcua) -> None:
    """Make SIGTERM and SIGINT release the OPC UA side, then exit 0."""
    stopping = threading.Event()

    def release() -> None:
        try:
            connection = state.connection
            if connection is not None:
                # What the lifespan does before it releases, for the same reason:
                # end the warm-up's backoff, and let an attempt already on the
                # wire land before disconnecting, so a round that completes
                # afterwards cannot leave a session open behind us (#136).
                connection.close()
                connection.settle()
            release_opcua(state)
        except Exception as error:
            print(f"Error closing the OPC UA session: {describe_error(error)}", file=sys.stderr)

    def handle(_signum, _frame) -> None:
        if stopping.is_set():
            return
        stopping.set()
        worker = threading.Thread(target=release, name="opcua-shutdown", daemon=True)
        worker.start()
        worker.join(SHUTDOWN_GRACE_SECONDS)
        with contextlib.suppress(Exception):
            sys.stderr.flush()
        os._exit(0)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    for signum in (signal.SIGINT, signal.SIGTERM):
        if loop is not None and os.name == "posix":
            # add_signal_handler installs asyncio's wakeup fd. A signal may
            # arrive on an OPC UA or stdin thread while the main thread is
            # sleeping in the selector; a plain signal.signal handler alone
            # leaves the loop asleep indefinitely in that case (#173).
            loop.add_signal_handler(signum, handle, signum, None)
        else:
            signal.signal(signum, handle)
