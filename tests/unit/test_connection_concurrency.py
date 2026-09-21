"""One rebuild at a time, and nobody waiting behind a sleep (issue #111).

`OpcuaConnection.connect` used to hold its lock across the whole retry loop,
sleeps and all — so every concurrent tool call waited out the full backoff budget
(7s by default, 32s with `OPCUA_RECONNECT_MAX_RETRY=-1`) before it was even told
the server was down. Serialising the callers was right; making them sit through
the sleep was not, and the two are separable.

python-opcua is synchronous and the server calls into it through
`asyncio.to_thread`, so the concurrency here is real threads and these tests use
real ones. They need no OPC UA server: what is under test is the connection's own
state machine, with the open stubbed so a test can decide when it succeeds.
"""

from __future__ import annotations

import threading
import time

import pytest
from opcua_mcp_server.config import ReconnectConfig
from opcua_mcp_server.connection import OpcuaConnection

#: Long enough that a caller that ran its *own* backoff instead of joining
#: someone else's is unmistakable in the wall clock, short enough that the suite
#: stays fast.
DELAY_MS = 200
FAST = ReconnectConfig(initial_delay_ms=DELAY_MS, max_delay_ms=DELAY_MS, max_retry=2)


class _Opener:
    """A stand-in for `_open`, with a script and a record of what was asked of it."""

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls = 0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            self.calls += 1
            failing = self.calls <= self.failures
        if failing:
            raise ConnectionRefusedError("ECONNREFUSED")
        return object()


def connection(opener: _Opener, config: ReconnectConfig = FAST) -> OpcuaConnection:
    subject = OpcuaConnection("opc.tcp://127.0.0.1:1/none", config)
    subject._open = opener
    return subject


def in_parallel(work, threads: int = 8):
    """Run ``work`` on ``threads`` threads released together; return their results."""
    start = threading.Barrier(threads)
    results: list = [None] * threads
    errors: list = [None] * threads

    def run(index: int) -> None:
        start.wait()
        try:
            results[index] = work()
        except BaseException as error:
            errors[index] = error

    workers = [threading.Thread(target=run, args=(index,)) for index in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
    assert not any(worker.is_alive() for worker in workers), "a caller never finished"
    return results, errors


def test_concurrent_callers_share_one_connection_attempt():
    opener = _Opener()
    subject = connection(opener)

    results, errors = in_parallel(subject.ensure_connected)

    assert errors == [None] * 8, errors
    assert opener.calls == 1, "each caller opened its own connection"
    # And they all got the same one, which is the point of sharing the attempt.
    assert len(set(map(id, results))) == 1


def test_a_caller_that_arrives_mid_backoff_does_not_run_its_own():
    """The heart of #111: waiting is fine, waiting N times over is not.

    Two failures then a success is one budget of 2 x 200ms. Eight callers each
    running that would be eight budgets, and the old code did exactly that once
    the lock was released between calls — or, worse, made them wait for it while
    holding it.
    """
    opener = _Opener(failures=2)
    subject = connection(opener)

    began = time.monotonic()
    _, errors = in_parallel(subject.ensure_connected)
    elapsed = time.monotonic() - began

    assert errors == [None] * 8, errors
    assert opener.calls == 3, f"{opener.calls} attempts for one rebuild"
    # One budget, not eight. The margin is generous because this is a thread
    # test; the failure it catches is a multiple, not a few milliseconds.
    assert elapsed < (DELAY_MS * 2 / 1000) * 3, f"took {elapsed:.2f}s"


def test_a_waiting_caller_is_told_what_the_attempt_it_waited_on_found():
    """Not a fresh attempt of its own, which would cost it another whole budget."""
    opener = _Opener(failures=99)
    subject = connection(opener)

    began = time.monotonic()
    results, errors = in_parallel(subject.ensure_connected)
    elapsed = time.monotonic() - began

    assert results == [None] * 8
    assert all(isinstance(error, ConnectionRefusedError) for error in errors), errors
    # Three attempts total (the initial one plus max_retry=2), not three per
    # caller.
    assert opener.calls == 3, f"{opener.calls} attempts for one rebuild"
    assert elapsed < (DELAY_MS * 2 / 1000) * 3, f"took {elapsed:.2f}s"


def test_a_failed_rebuild_does_not_wedge_the_next_one():
    """The claim has to be released whether the attempt worked or not."""
    opener = _Opener(failures=3)
    subject = connection(opener)

    with pytest.raises(ConnectionRefusedError):
        subject.ensure_connected()
    assert opener.calls == 3

    # The fourth open succeeds, and a later caller must be free to reach it.
    assert subject.ensure_connected() is not None
    assert subject.connected


def test_an_established_connection_is_handed_out_without_a_rebuild():
    opener = _Opener()
    subject = connection(opener)

    first = subject.ensure_connected()
    assert subject.ensure_connected() is first
    assert opener.calls == 1


def test_a_rebuild_rebinds_once_per_rebuild_not_once_per_caller():
    """`on_client_replaced` is how the subscriptions follow a new session.

    Once per rebuild, not once per caller waiting on it — re-attaching every
    subscription eight times would be eight times the work and, on a server that
    counts subscriptions, eight times the cost.

    A bare ``reconnect()`` means "replace it, whatever state it is in", so eight
    of them are allowed to be more than one rebuild: a caller that arrives after
    the previous rebuild finished is asking for a new session and gets one. How
    many depends on thread scheduling, so what is asserted is the invariant —
    one rebind per rebuild, and the last of them is the client everyone holds —
    rather than a number that happened to come out of one run.
    ``test_an_outage_that_hits_every_call_at_once_rebuilds_once`` is the
    deterministic counterpart, and the case that actually matters.
    """
    opener = _Opener()
    subject = connection(opener)
    replaced: list = []
    subject.on_client_replaced = replaced.append

    subject.ensure_connected()
    assert len(replaced) == 1

    _, errors = in_parallel(subject.reconnect)
    assert errors == [None] * 8, errors
    assert len(replaced) == opener.calls, f"{len(replaced)} rebinds for {opener.calls} rebuilds"
    assert replaced[-1] is subject.client


def test_an_outage_that_hits_every_call_at_once_rebuilds_once():
    """What an outage actually looks like from a server serving several calls.

    Every in-flight call fails on the same dead session and every one of them
    asks for a rebuild. Without `stale`, the ones that arrive after the first
    rebuild finished start another — tearing down a session that is working and
    re-attaching every subscription on it for nothing, once per caller in turn.
    """
    opener = _Opener()
    subject = connection(opener)
    replaced: list = []
    subject.on_client_replaced = replaced.append

    subject.ensure_connected()
    died = subject.session_id
    assert died is not None

    results, errors = in_parallel(lambda: subject.reconnect(stale=died))

    assert errors == [None] * 8, errors
    assert len(replaced) == 2, f"rebound {len(replaced)} times for one outage"
    # And every caller was handed the one session that replaced the dead one.
    assert len(set(map(id, results))) == 1
    assert subject.session_id != died


def test_a_caller_that_names_no_session_always_rebuilds():
    """`reconnect()` with no argument still means what it says on the tin."""
    opener = _Opener()
    subject = connection(opener)

    subject.ensure_connected()
    first = subject.session_id
    subject.reconnect()

    assert subject.session_id != first
    assert opener.calls == 2


def test_each_session_is_told_apart_from_the_one_before_it():
    """The audit trail joins on this, so two sessions must never share an id."""
    opener = _Opener()
    subject = connection(opener)

    assert subject.session_id is None
    subject.ensure_connected()
    seen = {subject.session_id}
    for _ in range(3):
        subject.reconnect()
        seen.add(subject.session_id)

    assert len(seen) == 4, seen
    subject.disconnect()
    assert subject.session_id is None


def test_the_rebind_is_best_effort():
    """A subscription that cannot be re-established must not undo the recovery."""
    opener = _Opener()
    subject = connection(opener)

    def explode(_client):
        raise RuntimeError("the subscription is gone")

    subject.on_client_replaced = explode
    assert subject.ensure_connected() is not None
    assert subject.connected
