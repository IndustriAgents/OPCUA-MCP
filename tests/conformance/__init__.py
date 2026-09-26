"""Real-server conformance harness (#147).

The e2e suite proves the two runtimes agree with each other against mocks this
repository controls. It cannot prove either of them interoperates with anything
else: a vendor's address space, certificate policy, operation limits and
continuation behaviour are exactly what a mock written alongside the client
never disagrees with. This package drives both MCP runtimes, over stdio, through
the same scenarios against an *external* endpoint described by a config file,
and writes one machine-readable result per run.

    cd tests
    uv run --no-sync python -m conformance run --config ../compatibility/labs/milo.json
    uv run --no-sync python -m conformance render      # regenerate the docs matrix
    uv run --no-sync python -m conformance check       # what CI runs: docs match results

It is opt-in and never part of the default pytest run: it needs a server the
contributor chose, and a result is only as good as that choice. See
docs/compatibility.md for the config format and what the outcomes mean.
"""
