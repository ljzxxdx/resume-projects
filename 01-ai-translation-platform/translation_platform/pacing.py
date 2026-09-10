"""全局请求节奏和可安全终止的有限工作队列。"""

from __future__ import annotations

import math
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Generic, Iterable, Optional, Tuple, TypeVar

from translation_platform.errors import ConfigurationFailure


ItemT = TypeVar("ItemT")
ResultT = TypeVar("ResultT")


class GlobalRateLimiter:
    """为所有线程分配共享且不重叠的请求时间槽。"""

    def __init__(
        self,
        min_interval_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        _validate_positive_finite("min_interval_seconds", min_interval_seconds)
        if not callable(monotonic) or not callable(sleep):
            raise ConfigurationFailure("monotonic and sleep must be callable")
        self._min_interval = float(min_interval_seconds)
        self._monotonic = monotonic
        self._sleep = sleep
        self._next_allowed: Optional[float] = None
        self._lock = threading.Lock()

    def wait(self) -> float:
        """等待全局时间槽，并返回本次获准请求的单调时钟时间。"""

        with self._lock:
            now = _read_monotonic(self._monotonic)
            scheduled = (
                now
                if self._next_allowed is None
                else max(now, self._next_allowed)
            )
            delay = scheduled - now
            if delay > 0:
                self._sleep(delay)
            after_wait = _read_monotonic(self._monotonic)
            if after_wait + 1e-9 < scheduled:
                raise ConfigurationFailure(
                    "sleep returned before the reserved request slot"
                )
            acquired = max(scheduled, after_wait)
            self._next_allowed = acquired + self._min_interval
            return acquired


@dataclass(frozen=True)
class QueueTaskResult(Generic[ResultT]):
    """一个队列项目的值或异常，按原始输入序号标识。"""

    index: int
    value: Optional[ResultT]
    error: Optional[BaseException]

    @property
    def succeeded(self) -> bool:
        return self.error is None


class TaskQueueRunner(Generic[ItemT, ResultT]):
    """使用阻塞 ``get`` 和哨兵安全结束最多两个工作线程。"""

    def __init__(
        self,
        workers: int,
        handler: Callable[[ItemT], ResultT],
    ) -> None:
        if (
            isinstance(workers, bool)
            or not isinstance(workers, int)
            or not 1 <= workers <= 2
        ):
            raise ConfigurationFailure("workers must be between 1 and 2")
        if not callable(handler):
            raise ConfigurationFailure("queue handler must be callable")
        self._workers = workers
        self._handler = handler

    def run(self, items: Iterable[ItemT]) -> Tuple[QueueTaskResult[ResultT], ...]:
        """处理有限输入；单项异常会记录但不会阻断队列收尾。"""

        work_queue = queue.Queue()
        sentinel = object()
        results = []
        results_lock = threading.Lock()
        cancellation = threading.Event()
        worker_base_errors = []

        for index, item in enumerate(items):
            work_queue.put((index, item))
        for _ in range(self._workers):
            work_queue.put(sentinel)

        def worker() -> None:
            running = True
            while running:
                entry = work_queue.get()
                try:
                    if entry is sentinel:
                        running = False
                        continue
                    if cancellation.is_set():
                        continue
                    index, item = entry
                    try:
                        result = QueueTaskResult(
                            index=index,
                            value=self._handler(item),
                            error=None,
                        )
                    except BaseException as exc:
                        result = QueueTaskResult(
                            index=index,
                            value=None,
                            error=exc,
                        )
                        if not isinstance(exc, Exception):
                            cancellation.set()
                            with results_lock:
                                worker_base_errors.append(exc)
                    with results_lock:
                        results.append(result)
                finally:
                    work_queue.task_done()

        threads = [
            threading.Thread(
                target=worker,
                name=f"translation-worker-{index + 1}",
                daemon=False,
            )
            for index in range(self._workers)
        ]
        for thread in threads:
            thread.start()
        main_error = None
        try:
            work_queue.join()
        except BaseException as exc:
            main_error = exc
            cancellation.set()
            # 队列输入有限；取消后 worker 只排空任务并执行 task_done。
            work_queue.join()
        finally:
            for thread in threads:
                thread.join()
        if main_error is not None:
            raise main_error
        if worker_base_errors:
            raise worker_base_errors[0]
        return tuple(sorted(results, key=lambda result: result.index))


def _read_monotonic(clock: Callable[[], float]) -> float:
    value = clock()
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ConfigurationFailure(
            "monotonic clock must return a non-negative finite number"
        )
    return float(value)


def _validate_positive_finite(name: str, value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ConfigurationFailure(f"{name} must be a positive finite number")
