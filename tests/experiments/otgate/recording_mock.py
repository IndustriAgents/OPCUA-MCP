"""Run the bundled simulator and record writes received for the test nodes."""

import json
import sys
from pathlib import Path

import opcua_local_server as mock
from asyncua import ua


def main() -> None:
    trace = Path(sys.argv.pop(1))
    watched = {ua.NodeId(identifier, 2) for identifier in (110, 111, 113)}

    class RecordingAttributeService(mock.MockAttributeService):
        async def write(self, params, *args, **kwargs):
            records = [
                {"node": item.NodeId.to_string(), "value": item.Value.Value.Value}
                for item in params.NodesToWrite
                if item.AttributeId == ua.AttributeIds.Value and item.NodeId in watched
            ]
            if records:
                with trace.open("a", encoding="utf-8") as stream:
                    for record in records:
                        stream.write(json.dumps(record) + "\n")
            return await super().write(params, *args, **kwargs)

    class RecordingServer(mock.Server):
        def __init__(self):
            super().__init__()
            self.aio_obj.iserver.attribute_service = RecordingAttributeService(
                self.aio_obj.iserver.aspace
            )

    # Replace the factory in this test process only. The bundled entry point
    # still creates the address space and runs the normal simulation.
    mock.Server = RecordingServer
    mock.main()


if __name__ == "__main__":
    main()
