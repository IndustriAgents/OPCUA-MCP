"""Native status read and connection recovery behind the diagnostics port."""

from __future__ import annotations

import asyncio

from ..diagnostics import read_server_status
from ..errors import AdapterFailure, describe_error


class PythonOpcuaDiagnosticsPort:
    def __init__(self, connection, report):
        self.connection = connection
        self.report = report

    def snapshot(self):
        return {
            "endpoint": self.connection.url,
            "connecting": self.connection.connecting,
            "lastError": self.connection.last_error,
        }

    def capabilities(self):
        return self.report()

    async def read(self, security, identity):
        def invoke():
            def read():
                try:
                    return read_server_status(
                        self.connection.client, self.connection.url, security, identity
                    )
                except Exception as error:
                    raise AdapterFailure("diagnostics", describe_error(error), error) from error

            try:
                return self.connection.run(read)
            except AdapterFailure:
                raise
            except Exception as error:
                raise AdapterFailure("diagnostics", describe_error(error), error) from error

        return await asyncio.to_thread(invoke)
