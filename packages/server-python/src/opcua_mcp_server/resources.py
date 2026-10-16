"""Bound retained data and reject overload without an unbounded request queue."""

from __future__ import annotations

import threading
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from .contract import CONTRACT
from .errors import ApplicationRefusal, message
from .numeric import json_text

_generations: ContextVar[dict | None] = ContextVar("opcua_cache_generations", default=None)


def cache_generation(function):
    """A late answer from an old session cannot refill a cleared cache."""

    @wraps(function)
    def scoped(owner, *args, **kwargs):
        caches = [value for value in vars(owner).values() if isinstance(value, BoundedCache)]
        token = _generations.set({id(cache): cache.generation for cache in caches})
        try:
            return function(owner, *args, **kwargs)
        finally:
            _generations.reset(token)

    return scoped


class BoundedCache(dict):
    def __init__(self, capacity=None, byte_limit=None):
        super().__init__()
        self.capacity = capacity or CONTRACT["limits"]["maxCachedNodes"]
        self.lock = threading.RLock()
        self.byte_limit = byte_limit or CONTRACT["limits"]["maxBufferedBytes"]
        self.byte_count = 0
        self.sizes = {}
        self.generation = 0

    def __setitem__(self, key, value):
        with self.lock:
            if (_generations.get() or {}).get(id(self), self.generation) != self.generation:
                return
            encoded = value.to_json() if hasattr(value, "to_json") else value
            size = len(json_text([key, encoded]).encode("utf-8"))
            if key in self:
                self.byte_count -= self.sizes.pop(key)
                super().__delitem__(key)
            if size > self.byte_limit:
                return
            super().__setitem__(key, value)
            self.sizes[key] = size
            self.byte_count += size
            while len(self) > self.capacity or self.byte_count > self.byte_limit:
                first = next(iter(self))
                self.byte_count -= self.sizes.pop(first)
                super().__delitem__(first)

    def update(self, values):
        for key, value in values.items():
            self[key] = value

    def clear(self):
        with self.lock:
            self.generation += 1
            super().clear()
            self.sizes.clear()
            self.byte_count = 0


class BoundedRecords(deque):
    def __init__(self, size, byte_limit):
        super().__init__(maxlen=size)
        self.byte_limit = byte_limit
        self.byte_count = 0
        self.sizes = deque()

    def append(self, record):
        size = len(json_text(record).encode("utf-8"))
        if size > self.byte_limit:
            return
        while self and (len(self) == self.maxlen or self.byte_count + size > self.byte_limit):
            self.popleft()
        super().append(record)
        self.sizes.append(size)
        self.byte_count += size

    def popleft(self):
        self.byte_count -= self.sizes.popleft()
        return super().popleft()

    def clear(self):
        super().clear()
        self.sizes.clear()
        self.byte_count = 0


class Admission:
    def __init__(self):
        self.active = 0

    @contextmanager
    def enter(self, name):
        counted = name != "get_server_status"
        limit = CONTRACT["limits"]["maxConcurrentCalls"]
        if counted and self.active >= limit:
            raise ApplicationRefusal(message("resourceBusy", resource="tool calls", limit=limit))
        if counted:
            self.active += 1
        try:
            yield
        finally:
            if counted:
                self.active -= 1
