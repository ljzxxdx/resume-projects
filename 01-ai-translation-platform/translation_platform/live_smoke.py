"""低负载双向冒烟的固定输入、结果编排和脱敏证据。"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

from translation_platform.cache import build_cache_key
from translation_platform.checkpoint import (
    CheckpointRecord,
    CheckpointStatus,
    JsonlCheckpointStore,
)
from translation_platform.errors import ConfigurationFailure, ErrorType


INPUT_SET_VERSION = "live-smoke-v1"
AVAILABLE_SUCCESS_RATE = 0.90


@dataclass(frozen=True)
class SmokeSample:
    """一条固定、无业务含义的人工冒烟输入。"""

    sample_id: str
    source_lang: str
    target_lang: str
    text: str


LIVE_SMOKE_SAMPLES: Tuple[SmokeSample, ...] = (
    SmokeSample("zh-en-01", "zh-CHS", "en", "你好"),
    SmokeSample("zh-en-02", "zh-CHS", "en", "天气晴朗。"),
    SmokeSample("zh-en-03", "zh-CHS", "en", "请打开窗户。"),
    SmokeSample("zh-en-04", "zh-CHS", "en", "今天是第 3 天。"),
    SmokeSample("zh-en-05", "zh-CHS", "en", "一杯温水"),
    SmokeSample("zh-en-06", "zh-CHS", "en", "你准备好了吗？"),
    SmokeSample("zh-en-07", "zh-CHS", "en", "红色、蓝色和绿色"),
    SmokeSample("zh-en-08", "zh-CHS", "en", "请在 8 点开始。"),
    SmokeSample("zh-en-09", "zh-CHS", "en", "慢慢走，不要跑！"),
    SmokeSample("zh-en-10", "zh-CHS", "en", "桌上有两本书。"),
    SmokeSample("en-zh-01", "en", "zh-CHS", "Hello"),
    SmokeSample("en-zh-02", "en", "zh-CHS", "The sky is clear."),
    SmokeSample("en-zh-03", "en", "zh-CHS", "Please close the door."),
    SmokeSample("en-zh-04", "en", "zh-CHS", "It is day 4."),
    SmokeSample("en-zh-05", "en", "zh-CHS", "A glass of water"),
    SmokeSample("en-zh-06", "en", "zh-CHS", "Are you ready?"),
    SmokeSample("en-zh-07", "en", "zh-CHS", "Red, blue, and green"),
    SmokeSample("en-zh-08", "en", "zh-CHS", "Start at 9 o'clock."),
    SmokeSample("en-zh-09", "en", "zh-CHS", "Walk slowly; do not run!"),
    SmokeSample("en-zh-10", "en", "zh-CHS", "There are two books."),
)


@dataclass(frozen=True)
class SmokeAttempt:
    """生产客户端一次调用映射出的最小结构化结果。"""

    translated_text: Optional[str]
    error_type: Optional[ErrorType]
    request_attempts: int
    retries: int
    proxy_switches: int
    latency_ms: float

    def __post_init__(self) -> None:
        for field_name in ("request_attempts", "retries", "proxy_switches"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ConfigurationFailure(field_name + " 必须是非负整数")
        if self.request_attempts < 1:
            raise ConfigurationFailure("request_attempts 必须至少为 1")
        if self.retries >= self.request_attempts:
            raise ConfigurationFailure("retries 必须小于 request_attempts")
        if (
            isinstance(self.latency_ms, bool)
            or not isinstance(self.latency_ms, (int, float))
            or not math.isfinite(self.latency_ms)
            or self.latency_ms < 0
        ):
            raise ConfigurationFailure("latency_ms 必须是非负有限数值")

        if self.error_type is None:
            if not isinstance(self.translated_text, str) or not self.translated_text.strip():
                raise ConfigurationFailure("成功调用必须包含非空翻译结果")
            object.__setattr__(self, "translated_text", self.translated_text.strip())
        elif not isinstance(self.error_type, ErrorType):
            raise ConfigurationFailure("error_type 必须是 ErrorType")
        elif self.translated_text is not None:
            raise ConfigurationFailure("失败调用不能包含翻译结果")

    @property
    def succeeded(self) -> bool:
        return self.error_type is None


class LiveSmokeRunner:
    """先做双向探针，再按阈值完成固定 20 条冒烟。"""

    def __init__(
        self,
        translate: Callable[[SmokeSample], SmokeAttempt],
        checkpoint_store: JsonlCheckpointStore,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        proxy_enabled: bool = False,
    ) -> None:
        if not callable(translate) or not callable(now):
            raise ConfigurationFailure("translate 和 now 必须是可调用对象")
        if not isinstance(checkpoint_store, JsonlCheckpointStore):
            raise ConfigurationFailure("checkpoint_store 必须是 JsonlCheckpointStore")
        if not isinstance(proxy_enabled, bool):
            raise ConfigurationFailure("proxy_enabled 必须是布尔值")
        self._translate = translate
        self._checkpoint_store = checkpoint_store
        self._now = now
        self._proxy_enabled = proxy_enabled

    def run(self) -> Mapping[str, object]:
        """恢复终态记录，仅执行未完成项，并在每条完成后更新检查点。"""

        started_at = _aware_time(self._now(), "开始时间")
        records = self._checkpoint_store.load()
        checkpoint_changed = False
        for sample in LIVE_SMOKE_SAMPLES:
            cache_key = _cache_key(sample)
            if cache_key not in records:
                records[cache_key] = CheckpointRecord(
                    cache_key=cache_key,
                    status=CheckpointStatus.PENDING,
                    attempts=0,
                    translated_text=None,
                    error_type=None,
                    latency_ms=0.0,
                )
                checkpoint_changed = True
        if checkpoint_changed:
            self._checkpoint_store.save(records)

        ordered_samples = (
            LIVE_SMOKE_SAMPLES[0],
            LIVE_SMOKE_SAMPLES[10],
            *LIVE_SMOKE_SAMPLES[1:10],
            *LIVE_SMOKE_SAMPLES[11:20],
        )
        completed = []
        executed_attempts = []
        cache_hits = 0
        for index, sample in enumerate(ordered_samples):
            cache_key = _cache_key(sample)
            record = records[cache_key]
            if record.status is CheckpointStatus.PENDING:
                attempt = self._translate(sample)
                if not isinstance(attempt, SmokeAttempt):
                    raise ConfigurationFailure("translate 必须返回 SmokeAttempt")
                executed_attempts.append(attempt)
                record = _checkpoint_record(sample, attempt)
                records[cache_key] = record
                self._checkpoint_store.save(records)
            else:
                cache_hits += 1
            completed.append((sample, record))

            if index == 1 and _same_systemic_probe_failure(completed):
                break

        finished_at = _aware_time(self._now(), "结束时间")
        return _build_evidence(
            completed=completed,
            executed_attempts=executed_attempts,
            cache_hits=cache_hits,
            started_at=started_at,
            finished_at=finished_at,
            proxy_enabled=self._proxy_enabled,
        )


def _checkpoint_record(sample: SmokeSample, attempt: SmokeAttempt) -> CheckpointRecord:
    if attempt.succeeded:
        return CheckpointRecord(
            cache_key=_cache_key(sample),
            status=CheckpointStatus.SUCCESS,
            attempts=attempt.request_attempts,
            translated_text=attempt.translated_text,
            error_type=None,
            latency_ms=attempt.latency_ms,
        )
    return CheckpointRecord(
        cache_key=_cache_key(sample),
        status=CheckpointStatus.FAILURE,
        attempts=attempt.request_attempts,
        translated_text=None,
        error_type=attempt.error_type,
        latency_ms=attempt.latency_ms,
    )


def _same_systemic_probe_failure(
    completed: Sequence[Tuple[SmokeSample, CheckpointRecord]],
) -> bool:
    if len(completed) != 2:
        return False
    first = completed[0][1]
    second = completed[1][1]
    return (
        first.status is CheckpointStatus.FAILURE
        and second.status is CheckpointStatus.FAILURE
        and first.error_type is second.error_type
    )


def _build_evidence(
    completed: Sequence[Tuple[SmokeSample, CheckpointRecord]],
    executed_attempts: Sequence[SmokeAttempt],
    cache_hits: int,
    started_at: datetime,
    finished_at: datetime,
    proxy_enabled: bool,
) -> Mapping[str, object]:
    successes = sum(
        record.status is CheckpointStatus.SUCCESS for _, record in completed
    )
    failures = len(completed) - successes
    success_rate = successes / len(completed) if completed else 0.0
    unavailable = len(completed) == 2 and _same_systemic_probe_failure(completed)
    status = (
        "unavailable"
        if unavailable
        else "available"
        if success_rate >= AVAILABLE_SUCCESS_RATE
        else "degraded"
    )
    error_counts = Counter(
        record.error_type.value
        for _, record in completed
        if record.error_type is not None
    )

    directions: Dict[str, Dict[str, object]] = {}
    for source_lang, target_lang in (("zh-CHS", "en"), ("en", "zh-CHS")):
        direction_name = source_lang + "_to_" + target_lang
        direction_records = [
            record
            for sample, record in completed
            if sample.source_lang == source_lang and sample.target_lang == target_lang
        ]
        direction_successes = sum(
            record.status is CheckpointStatus.SUCCESS
            for record in direction_records
        )
        directions[direction_name] = {
            "planned": 10,
            "actual": len(direction_records),
            "successes": direction_successes,
            "failures": len(direction_records) - direction_successes,
        }

    redacted_results = []
    for sample, record in completed:
        if len(redacted_results) == 2:
            break
        result = {
            "sample_id": sample.sample_id,
            "direction": sample.source_lang + "_to_" + sample.target_lang,
            "status": record.status.value,
            "input_summary": "sha256:" + _sha256_text(sample.text),
        }
        if record.status is CheckpointStatus.SUCCESS:
            result["output_summary"] = "sha256:" + _sha256_text(
                record.translated_text or ""
            )
        else:
            result["error_type"] = record.error_type.value
        redacted_results.append(result)

    return {
        "evidence_type": "live_low_load_smoke",
        "status": status,
        "planned_samples": len(LIVE_SMOKE_SAMPLES),
        "actual_samples": len(completed),
        "directions": directions,
        "successes": successes,
        "failures": failures,
        "success_rate": round(success_rate, 4),
        "error_counts": dict(sorted(error_counts.items())),
        "request_count": len(executed_attempts),
        "cache_hits": cache_hits,
        "request_attempts": sum(
            attempt.request_attempts for attempt in executed_attempts
        ),
        "retries": sum(attempt.retries for attempt in executed_attempts),
        "proxy": {
            "enabled": proxy_enabled,
            "switches": sum(
                attempt.proxy_switches for attempt in executed_attempts
            ),
        },
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "input_set": {
            "version": INPUT_SET_VERSION,
            "sample_count": len(LIVE_SMOKE_SAMPLES),
            "sha256": _input_set_digest(),
        },
        "sample_results": redacted_results,
        "success_cache_ready": successes > 0,
    }


def _input_set_digest() -> str:
    payload = [
        {
            "sample_id": sample.sample_id,
            "source_lang": sample.source_lang,
            "target_lang": sample.target_lang,
            "text": sample.text,
        }
        for sample in LIVE_SMOKE_SAMPLES
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _cache_key(sample: SmokeSample) -> str:
    return build_cache_key(sample.source_lang, sample.target_lang, sample.text)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _aware_time(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ConfigurationFailure(label + "必须包含时区")
    return value
