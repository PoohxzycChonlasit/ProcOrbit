from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping

import psutil

from process_classifier import classify_process
from process_identity import ProcessIdentity


@dataclass(frozen=True, slots=True)
class ProcessMeasurements:
    cpu_total_sec: float | None = None
    cpu_status: str | None = None
    memory_rss_bytes: int | None = None
    memory_status: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    proc: Any
    pid: int
    ppid: int | None
    name: str | None
    created_at: float | None
    measurements: ProcessMeasurements | None = None
    sample_metrics: bool = True

    @property
    def identity(self) -> ProcessIdentity:
        return ProcessIdentity(self.pid, self.created_at)


@dataclass(frozen=True, slots=True)
class _CachedMetrics:
    cpu_pct: float | None
    cpu_status: str | None
    cpu_sampled_at: float
    memory_mb: float | None
    memory_status: str | None
    memory_sampled_at: float

@dataclass(frozen=True, slots=True)
class ProcessSample:
    identity: ProcessIdentity
    name: str | None
    category: str
    category_reason: str
    cpu_pct: float | None
    memory_mb: float | None
    uptime_sec: float | None
    parent_pid: int | None
    parent_name: str | None
    executable: str | None
    cpu_sampled_at: float | None = None
    memory_sampled_at: float | None = None
    cpu_status: str | None = None
    memory_status: str | None = None
    executable_status: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessSnapshot:
    items: tuple[ProcessSample, ...]
    sampled_at: float
    sequence: int
    complete: bool
    permission_denied: int = 0
    ended_during_sampling: int = 0


class ProcessInventory:
    """Collect an in-memory universal process snapshot on the monitor thread."""

    EXECUTABLE_RETRY_SEC = 30.0
    EXECUTABLE_LOOKUPS_PER_SNAPSHOT = 40
    CPU_BASELINE_MAX_AGE_SEC = 30.0

    def __init__(self, *, cpu_count: int | None = None) -> None:
        self.cpu_count = max(1, int(cpu_count or psutil.cpu_count(logical=True) or 1))
        self._sequence = 0
        self._cpu_baselines: dict[ProcessIdentity, tuple[float, float]] = {}
        self._executable_cache: dict[
            ProcessIdentity, tuple[float, str | None, str | None]
        ] = {}
        self._unverified_executable_attempts: dict[int, float] = {}
        self._executable_cursor_pid = -1
        self._metric_cache: dict[ProcessIdentity, _CachedMetrics] = {}

    def collect(
        self,
        records: Mapping[int, ProcessRecord],
        *,
        sampled_at: float | None = None,
        monotonic_at: float | None = None,
        enumeration_complete: bool = True,
        enumeration_permission_denied: int = 0,
    ) -> ProcessSnapshot:
        wall_now = time.time() if sampled_at is None else float(sampled_at)
        mono_now = time.monotonic() if monotonic_at is None else float(monotonic_at)
        self._sequence += 1
        permission_denied = max(0, int(enumeration_permission_denied))
        ended = 0
        complete = bool(enumeration_complete)
        # Keep tracking maps bounded even if enumeration remains partial for a
        # long time. Expired CPU baselines are not used to calculate a rate.
        self._cpu_baselines = {
            identity: baseline
            for identity, baseline in self._cpu_baselines.items()
            if 0 <= mono_now - baseline[0] <= self.CPU_BASELINE_MAX_AGE_SEC
        }
        self._executable_cache = {
            identity: cached
            for identity, cached in self._executable_cache.items()
            if 0 <= mono_now - cached[0] <= self.EXECUTABLE_RETRY_SEC * 2
        }
        self._unverified_executable_attempts = {
            pid: timestamp
            for pid, timestamp in self._unverified_executable_attempts.items()
            if 0 <= mono_now - timestamp <= self.EXECUTABLE_RETRY_SEC
        }
        self._metric_cache = {
            identity: cached
            for identity, cached in self._metric_cache.items()
            if 0 <= wall_now - max(cached.cpu_sampled_at, cached.memory_sampled_at) <= 30.0
        }
        items: list[ProcessSample] = []
        current_identities = {record.identity for record in records.values()}
        vanished_identities: set[ProcessIdentity] = set()
        executable_lookups = 0
        last_executable_pid = None

        ordered_records = sorted(records.values(), key=lambda item: item.pid)
        cursor_index = next(
            (
                index for index, record in enumerate(ordered_records)
                if record.pid > self._executable_cursor_pid
            ),
            0,
        )
        ordered_records = ordered_records[cursor_index:] + ordered_records[:cursor_index]

        for record in ordered_records:
            identity = record.identity
            cpu_pct, cpu_status, cpu_denied, cpu_ended = self._cpu(
                record, identity, mono_now
            )
            permission_denied += int(cpu_denied)
            if cpu_ended:
                ended += 1
                complete = False
                vanished_identities.add(identity)
                continue

            memory_mb, memory_status, memory_denied, memory_ended = self._memory(record)
            permission_denied += int(memory_denied)
            if memory_ended:
                ended += 1
                complete = False
                vanished_identities.add(identity)
                continue

            executable, exe_status, exe_denied, exe_ended, attempted = self._executable(
                record,
                identity,
                mono_now,
                allow_lookup=executable_lookups < self.EXECUTABLE_LOOKUPS_PER_SNAPSHOT,
            )
            permission_denied += int(exe_denied)
            if attempted:
                executable_lookups += 1
                last_executable_pid = record.pid
            if exe_ended:
                ended += 1
                complete = False
                vanished_identities.add(identity)
                continue

            classification = classify_process(record.name, executable)
            parent = records.get(record.ppid) if record.ppid is not None else None
            uptime = (
                max(0.0, wall_now - identity.created_at)
                if identity.verified else None
            )
            prior_metrics = self._metric_cache.get(identity) if identity.verified else None
            if record.sample_metrics:
                cpu_sampled_at = wall_now
                memory_sampled_at = wall_now
                if identity.verified:
                    self._metric_cache[identity] = _CachedMetrics(
                        cpu_pct=cpu_pct,
                        cpu_status=cpu_status,
                        cpu_sampled_at=wall_now,
                        memory_mb=memory_mb,
                        memory_status=memory_status,
                        memory_sampled_at=wall_now,
                    )
            elif prior_metrics is not None:
                cpu_pct = prior_metrics.cpu_pct
                cpu_status = prior_metrics.cpu_status
                cpu_sampled_at = prior_metrics.cpu_sampled_at
                memory_mb = prior_metrics.memory_mb
                memory_status = prior_metrics.memory_status
                memory_sampled_at = prior_metrics.memory_sampled_at
            else:
                cpu_pct = None
                cpu_status = "Waiting for resource sample"
                cpu_sampled_at = None
                memory_mb = None
                memory_status = "Waiting for resource sample"
                memory_sampled_at = None
            items.append(ProcessSample(
                identity=identity,
                name=record.name,
                category=classification.category,
                category_reason=classification.reason,
                cpu_pct=cpu_pct,
                memory_mb=memory_mb,
                uptime_sec=uptime,
                parent_pid=record.ppid,
                parent_name=parent.name if parent else None,
                executable=executable,
                cpu_sampled_at=cpu_sampled_at,
                memory_sampled_at=memory_sampled_at,
                cpu_status=cpu_status,
                memory_status=memory_status,
                executable_status=exe_status,
            ))

        if last_executable_pid is not None:
            self._executable_cursor_pid = last_executable_pid
        items.sort(key=lambda item: item.identity.pid)

        retained = current_identities - vanished_identities
        if complete:
            self._cpu_baselines = {
                identity: baseline
                for identity, baseline in self._cpu_baselines.items()
                if identity in retained
            }
            self._executable_cache = {
                identity: cached
                for identity, cached in self._executable_cache.items()
                if identity in retained
            }
            self._metric_cache = {
                identity: cached
                for identity, cached in self._metric_cache.items()
                if identity in retained
            }

        return ProcessSnapshot(
            items=tuple(items),
            sampled_at=wall_now,
            sequence=self._sequence,
            complete=complete,
            permission_denied=permission_denied,
            ended_during_sampling=ended,
        )

    def _cpu(
        self,
        record: ProcessRecord,
        identity: ProcessIdentity,
        now: float,
    ) -> tuple[float | None, str | None, bool, bool]:
        if not record.sample_metrics:
            return None, "Waiting for resource sample", False, False
        if not identity.verified:
            return None, "Creation time unavailable; CPU identity is unverified", False, False
        if record.measurements is not None:
            total = record.measurements.cpu_total_sec
            if record.measurements.cpu_status:
                # Enumeration counted this denied attribute when it populated
                # the prefetched measurements.
                return None, record.measurements.cpu_status, False, False
            if total is None:
                return None, "Unavailable", False, False
        else:
            try:
                times = record.proc.cpu_times()
                total = float(times.user) + float(times.system)
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                return None, "Process ended during sampling", False, True
            except psutil.AccessDenied:
                return None, "Access denied", True, False
            except (psutil.Error, OSError, AttributeError, TypeError, ValueError):
                return None, "Unavailable", False, False

        previous = self._cpu_baselines.get(identity)
        self._cpu_baselines[identity] = (now, total)
        if previous is None:
            return None, "Waiting for second sample", False, False
        before_time, before_cpu = previous
        elapsed = now - before_time
        delta = total - before_cpu
        if elapsed <= 0 or elapsed > self.CPU_BASELINE_MAX_AGE_SEC or delta < 0:
            return None, "Waiting for a fresh CPU baseline", False, False
        return max(0.0, delta / elapsed / self.cpu_count * 100.0), None, False, False

    @staticmethod
    def _memory(record: ProcessRecord) -> tuple[float | None, str | None, bool, bool]:
        if not record.sample_metrics:
            return None, "Waiting for resource sample", False, False
        if record.measurements is not None:
            memory_rss = record.measurements.memory_rss_bytes
            if record.measurements.memory_status:
                return None, record.measurements.memory_status, False, False
            if memory_rss is None:
                return None, "Unavailable", False, False
            return max(0.0, float(memory_rss) / (1024 ** 2)), None, False, False
        try:
            memory = record.proc.memory_info()
            return max(0.0, float(memory.rss) / (1024 ** 2)), None, False, False
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return None, "Process ended during sampling", False, True
        except psutil.AccessDenied:
            return None, "Access denied", True, False
        except (psutil.Error, OSError, AttributeError, TypeError, ValueError):
            return None, "Unavailable", False, False

    def _executable(
        self,
        record: ProcessRecord,
        identity: ProcessIdentity,
        now: float,
        *,
        allow_lookup: bool,
    ) -> tuple[str | None, str | None, bool, bool, bool]:
        # Without creation time a PID can be reused between polls; don't reuse
        # an executable path across observations for an unverified identity.
        cached = self._executable_cache.get(identity) if identity.verified else None
        unverified_recent = (
            not identity.verified
            and now - self._unverified_executable_attempts.get(identity.pid, -1e9)
            < self.EXECUTABLE_RETRY_SEC
        )
        if cached is not None and now - cached[0] < self.EXECUTABLE_RETRY_SEC:
            return cached[1], cached[2], False, False, False
        if unverified_recent:
            return None, "Path lookup deferred for unverified identity", False, False, False
        if not allow_lookup:
            if cached is not None:
                return cached[1], "Path refresh queued", False, False, False
            return None, "Path lookup queued", False, False, False
        try:
            executable = str(record.proc.exe())
            if identity.verified:
                self._executable_cache[identity] = (now, executable, None)
            else:
                self._unverified_executable_attempts[identity.pid] = now
            return executable, None, False, False, True
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return None, "Process ended during sampling", False, True, True
        except psutil.AccessDenied:
            if identity.verified:
                self._executable_cache[identity] = (now, None, "Access denied")
            else:
                self._unverified_executable_attempts[identity.pid] = now
            return None, "Access denied", True, False, True
        except (psutil.Error, OSError, AttributeError, TypeError, ValueError):
            if identity.verified:
                self._executable_cache[identity] = (now, None, "Unavailable")
            else:
                self._unverified_executable_attempts[identity.pid] = now
            return None, "Unavailable", False, False, True
