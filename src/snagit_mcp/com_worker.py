"""Single dedicated STA thread that owns every COM call.

Snagit's VideoCapture object is an out-of-process COM server that must stay alive
between `start_recording` and `stop_recording` tool calls, so all interaction is
funnelled through one apartment-threaded worker.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from concurrent.futures import Future
from typing import Any, Callable, TypeVar

T = TypeVar("T")

_SHUTDOWN = object()


class ComWorker:
    def __init__(self, name: str = "snagit-com") -> None:
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._started = threading.Event()
        self._init_error: BaseException | None = None
        self._thread.start()
        self._started.wait(timeout=15)
        if self._init_error is not None:
            raise self._init_error

    def _run(self) -> None:
        try:
            import pythoncom

            pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the constructor
            self._init_error = exc
            self._started.set()
            return
        self._started.set()
        try:
            while True:
                item = self._queue.get()
                if item is _SHUTDOWN:
                    return
                future, fn, args, kwargs = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    future.set_result(fn(*args, **kwargs))
                except BaseException as exc:  # noqa: BLE001 - relayed to the caller
                    future.set_exception(exc)
        finally:
            pythoncom.CoUninitialize()

    def submit(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> "Future[T]":
        future: Future[T] = Future()
        self._queue.put((future, fn, args, kwargs))
        return future

    async def call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return await asyncio.wrap_future(self.submit(fn, *args, **kwargs))

    def shutdown(self) -> None:
        self._queue.put(_SHUTDOWN)
        self._thread.join(timeout=10)
