"""Bounded maintained client with application-owned reconnect and subscription identity.

This qualification client is not selected by the production runtime yet. Native
secure-channel renewal remains enabled; recovery and subscription replacement
must run through the application's session-generation and authorization gates.
"""

from __future__ import annotations

import asyncio

from asyncua import Client
from asyncua.client.ua_client import UaClient
from asyncua.common.shortcuts import Shortcuts

from .asyncua_transport import BoundedProtocol


class BoundedUaClient(UaClient):
    def _make_protocol(self):
        protocol = BoundedProtocol(self._timeout, security_policy=self.security_policy)
        protocol.pre_request_hook = self._pre_request_hook
        protocol.on_connection_lost = self._on_transport_lost
        protocol.is_session_closing = self._is_session_closing
        self.protocol = protocol
        return protocol


class ApplicationOwnedClient(Client):
    def __init__(self, url, timeout=4):
        super().__init__(url, timeout=timeout, auto_reconnect=False)
        self.uaclient = BoundedUaClient(timeout)
        self.uaclient.pre_request_hook = self._wait_until_ready
        self.nodes = Shortcuts(self.uaclient.session)

    async def connect(self, **kwargs):
        # Disable both autonomous watchdogs before either scheduled task can run.
        # auto_reconnect=False alone does not disable them in asyncua 2.0.1.
        kwargs["auto_reconnect"] = False
        await super().connect(**kwargs)
        tasks = [task for task in (self._supervisor_task, self._stale_watchdog_task) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._supervisor_task = None
        self._stale_watchdog_task = None
