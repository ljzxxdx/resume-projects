"""正常、异常和用户中断共用的确定性运行收尾流程。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import TracebackType
from typing import Callable, Generic, Optional, Tuple, TypeVar, cast

from translation_platform.errors import (
    ConfigurationFailure,
    ErrorType,
    TranslationPlatformError,
)


ResultT = TypeVar("ResultT")


class RunExitReason(str, Enum):
    """触发本次收尾的运行出口。"""

    NORMAL = "normal"
    ERROR = "error"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class CleanupFailure:
    """不含异常消息的收尾失败摘要。"""

    step: str
    error_name: str


@dataclass(frozen=True)
class FinalizationState:
    """可安全交给断点和摘要写入器的最终状态。"""

    reason: RunExitReason
    primary_error_name: Optional[str]
    primary_error_type: Optional[ErrorType]
    cleanup_failures: Tuple[CleanupFailure, ...]


class LifecycleFinalizationError(ConfigurationFailure, RuntimeError):
    """业务成功但一个或多个收尾步骤失败。"""

    def __init__(self, failures: Tuple[CleanupFailure, ...]) -> None:
        self.failures = failures
        steps = ", ".join(failure.step for failure in failures)
        super().__init__("run finalization failed at: " + steps)


class RunLifecycle(Generic[ResultT]):
    """始终按 Session、断点、摘要的顺序尝试全部收尾动作。"""

    def __init__(
        self,
        session_owner: object,
        save_checkpoint: Callable[[FinalizationState], None],
        write_summary: Callable[[FinalizationState], None],
    ) -> None:
        close = getattr(session_owner, "close", None)
        if not callable(close):
            raise ConfigurationFailure("session owner must provide close")
        if not callable(save_checkpoint) or not callable(write_summary):
            raise ConfigurationFailure(
                "checkpoint and summary writers must be callable"
            )
        self._close_session = close
        self._save_checkpoint = save_checkpoint
        self._write_summary = write_summary
        self.last_finalization: Optional[FinalizationState] = None

    def execute(self, operation: Callable[[], ResultT]) -> ResultT:
        """执行操作并在任何 BaseException 出口完成三步收尾。"""

        result: Optional[ResultT] = None
        primary_error: Optional[BaseException] = None
        primary_traceback: Optional[TracebackType] = None
        reason = RunExitReason.NORMAL
        try:
            if not callable(operation):
                raise ConfigurationFailure("managed operation must be callable")
            result = operation()
        except BaseException as exc:
            primary_error = exc
            primary_traceback = exc.__traceback__
            reason = (
                RunExitReason.INTERRUPTED
                if isinstance(exc, KeyboardInterrupt)
                else RunExitReason.ERROR
            )

        cleanup_failures = []
        cleanup_error = _attempt_cleanup(
            "close_session",
            self._close_session,
            cleanup_failures,
        )
        if primary_error is None and isinstance(cleanup_error, KeyboardInterrupt):
            primary_error = cleanup_error
            primary_traceback = cleanup_error.__traceback__
            reason = RunExitReason.INTERRUPTED

        state = _state_for(reason, primary_error, cleanup_failures)
        cleanup_error = _attempt_cleanup(
            "save_checkpoint",
            lambda: self._save_checkpoint(state),
            cleanup_failures,
        )
        if primary_error is None and isinstance(cleanup_error, KeyboardInterrupt):
            primary_error = cleanup_error
            primary_traceback = cleanup_error.__traceback__
            reason = RunExitReason.INTERRUPTED

        state = _state_for(reason, primary_error, cleanup_failures)
        cleanup_error = _attempt_cleanup(
            "write_summary",
            lambda: self._write_summary(state),
            cleanup_failures,
        )
        if primary_error is None and isinstance(cleanup_error, KeyboardInterrupt):
            primary_error = cleanup_error
            primary_traceback = cleanup_error.__traceback__
            reason = RunExitReason.INTERRUPTED
        self.last_finalization = _state_for(
            reason,
            primary_error,
            cleanup_failures,
        )

        if primary_error is not None:
            raise primary_error.with_traceback(primary_traceback)
        if cleanup_failures:
            raise LifecycleFinalizationError(tuple(cleanup_failures))
        return cast(ResultT, result)


def _attempt_cleanup(
    step: str,
    action: Callable[[], None],
    failures: list,
) -> Optional[BaseException]:
    try:
        action()
    except BaseException as exc:
        failures.append(
            CleanupFailure(
                step=step,
                error_name=type(exc).__name__,
            )
        )
        return exc
    return None


def _state_for(
    reason: RunExitReason,
    error: Optional[BaseException],
    cleanup_failures: list,
) -> FinalizationState:
    return FinalizationState(
        reason=reason,
        primary_error_name=(None if error is None else type(error).__name__),
        primary_error_type=(
            error.error_type
            if isinstance(error, TranslationPlatformError)
            else None
        ),
        cleanup_failures=tuple(cleanup_failures),
    )
