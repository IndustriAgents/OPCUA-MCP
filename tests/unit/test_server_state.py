"""The Python server's state belongs to an instance, not to the module (#116).

Node keeps its state on objects — `OpcuaConnection`, `OpcuaTools` and the policy
are constructed in `index.ts` and passed down. Python kept the same state in
module globals, so the Python server could not be instantiated twice in one
process while the Node one nearly could. The contract pins the tool surface and
the rest of the suite pins the semantics; nothing pinned the *structure*, which is
exactly where the two halves had drifted.

These are the assertions that make the structure a property rather than a
description. They would all have failed before the refactor, and the last one
guards the specific way it could regress: a server whose state is per-instance
but whose *tools* are still bound to whichever instance existed at import.
"""

from __future__ import annotations

import asyncio
import json
import threading

from conftest import ROOT
from opcua_mcp_server import server as server_module
from opcua_mcp_server.server import TOOL_NAMES, create_server
from opcua_mcp_server.state import ServerState

CONTRACT = json.loads((ROOT / "contract" / "tools.json").read_text(encoding="utf-8"))


def test_two_servers_share_no_state():
    """The point of the refactor, stated as plainly as it can be.

    Every one of these was a module global, which meant a second server would
    have silently shared one connection, one capability answer and one set of
    subscriptions with the first.
    """
    a, b = create_server(), create_server()

    assert a.state is not b.state
    assert a.state.subscriptions is not b.state.subscriptions
    assert a.state.events is not b.state.events
    assert a.state.node_metadata is not b.state.node_metadata
    assert a.state.capabilities is not b.state.capabilities
    assert a.state.audit is not b.state.audit


def test_a_second_server_gets_its_own_tools():
    """Threading the state was only half of it.

    `@mcp.tool(...)` at module level binds a tool to whichever instance existed at
    import. With one instance that is invisible; with two, the second server has
    no tools at all — a server that is structurally instantiable and functionally
    empty. Registration happens in `create_server` for this reason.
    """
    a, b = create_server(), create_server()

    names_a = {tool.name for tool in a._tool_manager.list_tools()}
    names_b = {tool.name for tool in b._tool_manager.list_tools()}
    assert names_a == names_b == set(TOOL_NAMES)
    assert names_a, "a freshly built server registered no tools"


def test_every_contract_tool_is_registered():
    """A tool defined and never registered is a tool the server does not have.

    Invisible otherwise: nothing fails, the tool is simply absent. `TOOL_NAMES` is
    the registration list and the contract is the specification, so comparing them
    is what says the two agree.
    """
    assert set(TOOL_NAMES) == {tool["name"] for tool in CONTRACT["tools"]}
    assert len(TOOL_NAMES) == len(set(TOOL_NAMES)), "a tool is registered twice"


def test_one_policy_object_serves_binding_and_authorization():
    """The hazard this refactor introduces if done carelessly.

    The connection re-binds the policy's namespace mapping on every (re)connect
    and `call_tool` authorizes against it, so the two have to be the *same*
    object — otherwise an `nsu=<uri>;i=…` allowlist entry is bound on one policy
    and resolved against another that has never seen a NamespaceArray, and every
    write to it is denied. That used to hold because `tool_policy()` memoised one
    instance for the process; now it has to hold because the state owns one.
    """
    state = ServerState()
    assert state.policy is state.policy, "the policy is rebuilt on each access"

    server = create_server(state)
    assert server.state.policy is state.policy


def test_a_state_can_be_given_its_own_endpoint():
    """What the whole exercise is for: one instance per endpoint.

    Not wired up to anything yet — there is no multi-endpoint entry point — but
    the state no longer reads the URL from a module constant, which is the part
    that made per-endpoint impossible rather than merely unimplemented.
    """
    state = ServerState(url="opc.tcp://plant-2.example:4840")
    assert create_server(state).state.url == "opc.tcp://plant-2.example:4840"
    assert create_server().state.url != "opc.tcp://plant-2.example:4840"


def test_forgetting_capabilities_forgets_all_of_them():
    """A session's answers do not outlive the session that gave them."""
    state = ServerState()
    state.capabilities.update(
        history=True, history_events=True, aggregate_functions={"Average": object()}
    )
    assert state.available_capabilities() == {"history", "historyEvents", "aggregate"}

    state.forget_capabilities()

    assert state.available_capabilities() == set()
    assert state.capabilities_met({"capabilities": ["history"]}) is False
    assert state.capabilities_met({"capabilities": []}) is True


def test_the_module_holds_no_mutable_state():
    """The regression this refactor is one edit away from at any time.

    Missing one global is not a loud failure. While writing this I left the event
    buffers module-level: every assertion above still passed, `ServerState.events`
    existed and was closed on shutdown, and the tools went on using the module
    one — so a second server would have shared event buffers with the first, and
    the manager the lifespan tore down was not the one holding anything.

    So this checks the *shape* rather than any particular name: whatever is left at
    module scope must be a constant or a function, with `mcp` the one deliberate
    instance. Anything stateful reintroduced here fails without needing to be
    anticipated by name.
    """
    import types

    allowed_instances = {"mcp"}
    immutable = (str, int, float, bool, tuple, frozenset, type(None))

    offenders = []
    for name, value in vars(server_module).items():
        if name.startswith("__") or name in allowed_instances:
            continue
        # Imports are not state: a module object, and `from __future__` flags.
        if isinstance(value, (types.ModuleType, __import__("__future__")._Feature)):
            continue
        if callable(value) or isinstance(value, type):
            continue
        if isinstance(value, immutable):
            continue
        # Slices of the contract. Read-only lookups shared by every instance:
        # they describe the tool surface rather than any server's state, and
        # nothing mutates them. Named explicitly rather than allowed by type, so
        # a new dict at module scope has to be justified here to get past this.
        if name in {"CONTRACT", "DESC", "SUBSCRIPTIONS_RESOURCE", "_TRAVERSAL"}:
            continue
        offenders.append(f"{name}: {type(value).__name__}")

    assert not offenders, (
        "module-level mutable state in server.py — put it on ServerState instead: "
        f"{sorted(offenders)}"
    )


# --- the warm-up runs beside the requests, not before them (#136) -----------------


async def test_the_lifespan_does_not_wait_for_the_warm_up(monkeypatch):
    """The MCP SDK answers nothing, not even `initialize`, until the lifespan yields.

    So a lifespan that awaited the first connection round held the whole protocol
    back for it — the whole configured backoff against a plant that was down.
    The warm-up here never finishes on its own; the lifespan must yield anyway,
    and a catalogue request must answer once the window has passed.
    """
    release = threading.Event()

    def never_connects(state, connection):
        release.wait(10)

    monkeypatch.setattr(server_module, "_connect_and_probe", never_connects)
    mcp = create_server(ServerState(url="opc.tcp://127.0.0.1:1/none"))
    mcp.state.warm_up_wait_ms = 200
    loop = asyncio.get_running_loop()

    began = loop.time()
    async with server_module.opcua_lifespan(mcp):
        assert loop.time() - began < 1, "the lifespan waited for the warm-up"
        assert not mcp.state.warm_up.done()

        listed = await mcp.list_tools()
        assert loop.time() - began < 2, "tools/list waited past the warm-up window"
        assert "get_server_status" in {tool.name for tool in listed}
        release.set()

    assert mcp.state.warm_up is None


async def test_a_request_waits_for_a_warm_up_that_finishes_in_time():
    """Against a plant that is up, the first catalogue is the whole catalogue.

    That was the reason the warm-up ran before any request was served; waiting
    for it within the window keeps it.
    """
    state = ServerState(url="opc.tcp://127.0.0.1:1/none")
    state.warm_up_wait_ms = 5000
    finished = asyncio.Event()

    async def quick():
        await asyncio.sleep(0.05)
        finished.set()

    state.start_warm_up(quick())
    await state.await_warm_up()
    assert finished.is_set()


async def test_the_wait_is_measured_from_the_start_of_the_warm_up():
    """Not per request: a second catalogue request must not wait all over again."""
    state = ServerState(url="opc.tcp://127.0.0.1:1/none")
    state.warm_up_wait_ms = 200
    state.start_warm_up(asyncio.sleep(30))
    loop = asyncio.get_running_loop()

    began = loop.time()
    await state.await_warm_up()
    first = loop.time() - began
    await state.await_warm_up()
    second = loop.time() - began - first

    assert first < 1
    assert second < 0.05, f"the second request waited {second:.2f}s again"
    assert not state.warm_up.done(), "running out of patience must not cancel the warm-up"
    state.warm_up.cancel()


async def test_no_warm_up_means_no_wait():
    """A state no lifespan has started — the unit tests' own — answers at once."""
    await ServerState(url="opc.tcp://127.0.0.1:1/none").await_warm_up()
