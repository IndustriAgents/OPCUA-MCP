"""Per-attempt session fence and accounting for cancelled control requests."""

from __future__ import annotations

import asyncio
import threading
from contextvars import ContextVar

import anyio

from ..contract import CONTRACT
from ..errors import ApplicationRefusal, UncertainOutcome, describe_error, message

_current: ContextVar[ControlContext | None] = ContextVar("opcua_control", default=None)


class ControlContext:
    def __init__(self, call, connection, policy):
        self.call, self.connection, self.policy = call, connection, policy
        self.sent = False
        self.active = True
        self.lock = threading.Lock()

    def dispatch(self, operation):
        def checked():
            with self.lock:
                if not self.active or self.connection.session_id != self.call.session:
                    raise ApplicationRefusal(message("sessionChanged", tool=self.call.name))
                self.policy.authorize(self.call.name, self.call.arguments)
                self.sent = True
            return operation()

        return self.connection.control_call(checked)


def dispatch_control(operation):
    context = _current.get()
    return operation() if context is None else context.dispatch(operation)


async def run_control(context: ControlContext, operation, tasks: set):
    limit = CONTRACT["limits"]["maxConcurrentCalls"]
    if len(tasks) >= limit:
        raise ApplicationRefusal(message("resourceBusy", resource="owned controls", limit=limit))
    token = _current.set(context)
    task = asyncio.create_task(operation())
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with context.lock:
            sent = context.sent
            if not sent:
                context.active = False
        if not sent:
            task.cancel()
            raise ApplicationRefusal(message("controlCancelled")) from None
        # The caller leaving cannot cancel an already dispatched plant action.
        with anyio.CancelScope(shield=True):
            loop = asyncio.get_running_loop()
            deadline = loop.time() + CONTRACT["limits"]["controlAccountingTimeoutSeconds"]
            while True:
                try:
                    done, _ = await asyncio.wait({task}, timeout=max(0, deadline - loop.time()))
                    break
                except asyncio.CancelledError:
                    # Repeated cancellation cannot erase an already sent control's outcome.
                    if loop.time() >= deadline:
                        done = set()
                        break
        from .control_audit import describe_targets

        if done:
            try:
                return task.result()
            except BaseException as error:
                # The request is already cancelled: recovery would be cancelled too.
                # Preserve the terminal uncertainty without another network await.
                raise UncertainOutcome(
                    message(
                        "uncertainOutcome",
                        tool=context.call.name,
                        reason=f"request cancelled after dispatch: {describe_error(error)}",
                        targets=describe_targets(context.call.spec, context.call.arguments),
                    )
                ) from error

        raise UncertainOutcome(
            message(
                "uncertainOutcome",
                tool=context.call.name,
                reason="request cancelled after control dispatch; accounting deadline expired",
                targets=describe_targets(context.call.spec, context.call.arguments),
            )
        ) from None
    finally:
        # Consume a late native failure; the bounded accounting already records uncertainty.
        task.add_done_callback(
            lambda finished: None if finished.cancelled() else finished.exception()
        )
        _current.reset(token)
