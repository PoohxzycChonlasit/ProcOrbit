from __future__ import annotations

import math
from typing import Any, Iterable


SCORING_VERSION = 1
MIN_KNOWN_WEIGHT = 50
MAX_INTERVAL_SECONDS = 30.0


def _number(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _ramp(value: float, full_at: float, zero_at: float) -> float:
    if zero_at <= full_at:
        return 1.0 if value >= zero_at else 0.0
    return _clamp((value - full_at) / (zero_at - full_at))


def _grade(score: int | None) -> str | None:
    if score is None:
        return None
    if score >= 90:
        return "Excellent"
    if score >= 80:
        return "Good"
    if score >= 70:
        return "Fair"
    if score >= 50:
        return "Poor"
    return "Bad"


def _component(name: str, weight: int, points: float | None, explanation: str):
    available = points is not None
    return {
        "name": name,
        "weight": weight,
        "points": round(max(0.0, min(float(weight), points)), 1) if available else None,
        "available": available,
        "explanation": explanation,
    }


def _tree_unknown_count(row: dict[str, Any], resource: str) -> float | None:
    count = _number(row.get(f"tree_{resource}_unknown_count"))
    if count is None:
        count = _number(row.get("tree_unknown_count"))
    return max(0.0, count) if count is not None else None


def _tree_complete(row: dict[str, Any]) -> bool | None:
    cpu_unknown = _tree_unknown_count(row, "cpu")
    ram_unknown = _tree_unknown_count(row, "ram")
    if cpu_unknown is not None and ram_unknown is not None:
        return cpu_unknown == 0.0 and ram_unknown == 0.0
    combined = _number(row.get("tree_unknown_count"))
    return combined == 0.0 if combined is not None else None


def _cpu_complete(row: dict[str, Any]) -> bool:
    unknown = _tree_unknown_count(row, "cpu")
    return (
        unknown == 0.0
        and _number(row.get("cpu_pct")) is not None
        and _number(row.get("host_cpu_pct")) is not None
    )


def _memory_complete(row: dict[str, Any]) -> bool:
    unknown = _tree_unknown_count(row, "ram")
    return (
        unknown == 0.0
        and _number(row.get("rss_mb")) is not None
        and _number(row.get("mem_pct")) is not None
        and _number(row.get("host_ram_pct")) is not None
    )


def score_run_health(
    session: dict[str, Any],
    samples: Iterable[dict[str, Any]],
    *,
    tree_end_state: str | None = None,
    tree_end_detail: str | None = None,
) -> dict[str, Any]:
    """Score observed machine/resource health without inferring task success.

    Samples must be ordered by timestamp. The iterator is consumed once so
    finalizing a long session does not materialize its complete time series.
    """
    sample_count = 0
    cpu_count = 0
    memory_count = 0
    fully_valid_count = 0
    tree_count_known = 0
    tree_complete_count = 0
    cpu_sum = host_cpu_sum = 0.0
    memory_sum = host_ram_sum = 0.0
    cpu_peak = host_cpu_peak = None
    memory_peak = tree_mem_pct_peak = host_ram_peak = None
    child_peak = None

    first_ts = last_ts = max_gap_s = None
    previous_row: dict[str, Any] | None = None
    previous_ts: float | None = None
    cpu_evidence_seconds = cpu_saturation_seconds = 0.0
    host_ram_evidence_seconds = pressure_seconds = 0.0
    memory_evidence_seconds = tree_pressure_seconds = memory_impact_seconds = 0.0

    growth_count = 0
    growth_first_ts: float | None = None
    growth_last_ts: float | None = None
    growth_origin: float | None = None
    growth_mean_x = growth_mean_y = growth_cxx = growth_cxy = 0.0

    for source in samples:
        row = dict(source)
        sample_count += 1
        cpu_ok = _cpu_complete(row)
        memory_ok = _memory_complete(row)
        if cpu_ok and memory_ok:
            fully_valid_count += 1
        complete = _tree_complete(row)
        if complete is not None:
            tree_count_known += 1
            tree_complete_count += int(complete)

        if cpu_ok:
            cpu_value = float(row["cpu_pct"])
            host_cpu_value = float(row["host_cpu_pct"])
            cpu_count += 1
            cpu_sum += cpu_value
            host_cpu_sum += host_cpu_value
            cpu_peak = cpu_value if cpu_peak is None else max(cpu_peak, cpu_value)
            host_cpu_peak = (
                host_cpu_value if host_cpu_peak is None
                else max(host_cpu_peak, host_cpu_value)
            )

        if memory_ok:
            rss_value = float(row["rss_mb"])
            tree_mem_pct = float(row["mem_pct"])
            host_ram_value = float(row["host_ram_pct"])
            memory_count += 1
            memory_sum += rss_value
            host_ram_sum += host_ram_value
            memory_peak = rss_value if memory_peak is None else max(memory_peak, rss_value)
            tree_mem_pct_peak = (
                tree_mem_pct if tree_mem_pct_peak is None
                else max(tree_mem_pct_peak, tree_mem_pct)
            )
            host_ram_peak = (
                host_ram_value if host_ram_peak is None
                else max(host_ram_peak, host_ram_value)
            )

            ts_for_growth = _number(row.get("ts"))
            if ts_for_growth is not None:
                if growth_origin is None:
                    growth_origin = ts_for_growth
                    growth_first_ts = ts_for_growth
                growth_last_ts = ts_for_growth
                x = (ts_for_growth - growth_origin) / 60.0
                y = tree_mem_pct
                growth_count += 1
                dx = x - growth_mean_x
                dy = y - growth_mean_y
                growth_mean_x += dx / growth_count
                growth_mean_y += dy / growth_count
                growth_cxx += dx * (x - growth_mean_x)
                growth_cxy += dx * (y - growth_mean_y)

        child_count = _number(row.get("child_count"))
        if child_count is not None:
            child_peak = child_count if child_peak is None else max(child_peak, child_count)

        ts = _number(row.get("ts"))
        if ts is None:
            continue
        if first_ts is None:
            first_ts = ts
        if previous_ts is not None:
            gap = max(0.0, ts - previous_ts)
            max_gap_s = gap if max_gap_s is None else max(max_gap_s, gap)
            interval = min(MAX_INTERVAL_SECONDS, gap)
            if interval > 0.0 and previous_row is not None:
                cpu_value = _number(previous_row.get("cpu_pct"))
                host_cpu_value = _number(previous_row.get("host_cpu_pct"))
                if _cpu_complete(previous_row):
                    cpu_evidence_seconds += interval
                    if host_cpu_value >= 90.0 and cpu_value >= 10.0:
                        cpu_saturation_seconds += interval

                host_ram_value = _number(previous_row.get("host_ram_pct"))
                if host_ram_value is not None:
                    host_ram_evidence_seconds += interval
                    if host_ram_value >= 85.0:
                        pressure_seconds += interval
                if _memory_complete(previous_row):
                    memory_evidence_seconds += interval
                    tree_mem_pct = float(previous_row["mem_pct"])
                    if host_ram_value >= 85.0:
                        tree_pressure_seconds += interval
                        if tree_mem_pct >= 5.0:
                            memory_impact_seconds += interval

        last_ts = ts
        previous_ts = ts
        previous_row = row

    span_s = max(0.0, last_ts - first_ts) if first_ts is not None and last_ts is not None else 0.0
    tree_complete_fraction = (
        tree_complete_count / tree_count_known if tree_count_known else None
    )
    valid_metric_fraction = fully_valid_count / sample_count if sample_count else 0.0
    growth_span_s = (
        max(0.0, growth_last_ts - growth_first_ts)
        if growth_first_ts is not None and growth_last_ts is not None else 0.0
    )
    growth_per_minute = (
        growth_cxy / growth_cxx
        if growth_count >= 6 and growth_span_s >= 60.0 and growth_cxx > 0.0
        else None
    )

    created = _number(session.get("created_at"))
    first_seen = _number(session.get("first_seen_at"))
    ended = _number(session.get("ended_at"))
    duration = _number(session.get("duration_s"))
    end_reason = session.get("end_reason")
    monitor_interrupted = bool(session.get("monitor_interrupted"))
    expected_interval = _number(session.get("expected_sample_interval_s")) or 10.0
    expected_interval = max(1.0, min(300.0, expected_interval))
    coverage_grace_s = max(60.0, expected_interval * 3.0)
    sample_start_gap_s = (
        max(0.0, first_ts - first_seen)
        if first_ts is not None and first_seen is not None else None
    )
    sample_end_gap_s = (
        max(0.0, ended - last_ts)
        if ended is not None and last_ts is not None else None
    )
    coverage_reasons: list[str] = []
    if first_ts is None or last_ts is None:
        coverage_reasons.append("No stored sample timestamps are available.")
    if first_seen is None:
        coverage_reasons.append("The first observed time is unavailable.")
    elif sample_start_gap_s is not None and sample_start_gap_s > coverage_grace_s:
        coverage_reasons.append(
            f"The first stored sample is {sample_start_gap_s:.0f}s after the first observation."
        )
    if ended is None:
        coverage_reasons.append("The root-process end time is unavailable.")
    elif sample_end_gap_s is not None and sample_end_gap_s > coverage_grace_s:
        coverage_reasons.append(
            f"The last stored sample is {sample_end_gap_s:.0f}s before the root end."
        )
    if max_gap_s is not None and max_gap_s > coverage_grace_s:
        coverage_reasons.append(
            f"A {max_gap_s:.0f}s gap exceeds the expected sampling window."
        )
    sample_evidence_complete = not coverage_reasons
    sample_evidence_detail = (
        "Stored samples cover the observed start and end within the sampling tolerance."
        if sample_evidence_complete else " ".join(coverage_reasons)
    )

    components: list[dict[str, Any]] = []

    # A. Run stability covers observation continuity, never exit-code success.
    if monitor_interrupted:
        components.append(_component(
            "Run stability", 40, None,
            "ProcOrbit restarted while this workload was active; continuous observation cannot be confirmed. No crash or successful completion is inferred.",
        ))
    elif not sample_evidence_complete:
        components.append(_component(
            "Run stability", 40, None,
            "Stored samples do not cover the observed run closely enough to assess continuity. " + sample_evidence_detail,
        ))
    elif end_reason == "process_ended" and ended is not None and duration is not None:
        delay = max(0.0, first_seen - created) if first_seen is not None and created is not None else None
        points = 40.0 if created is not None and first_seen is not None else 25.0
        notes = ["The root process was tracked and its disappearance was observed."]
        if delay is None:
            notes.append("Windows creation time was unavailable.")
        elif delay > 10.0:
            points -= min(12.0, 4.0 + (delay - 10.0) / 30.0)
            notes.append(f"Monitoring first saw it {delay:.0f}s after process creation.")
        if max_gap_s is not None and max_gap_s > 60.0:
            points -= min(12.0, 5.0 + (max_gap_s - 60.0) / 60.0)
            notes.append(f"A sample gap of {max_gap_s:.0f}s limits continuity evidence.")
        notes.append("Exit code is unavailable; this is not a successful-completion claim.")
        components.append(_component("Run stability", 40, points, " ".join(notes)))
    else:
        components.append(_component(
            "Run stability", 40, None,
            "The root process end was not observed directly; crash or success is not inferred.",
        ))

    # B. Large workloads are only penalized during sustained host RAM pressure
    # when complete process-tree RAM is also a material share of host capacity.
    if memory_count >= 2:
        points = 25.0 * (1.0 - _ramp(memory_impact_seconds, 60.0, 300.0))
        explanation = (
            f"Host RAM pressure (>=85%) overlapped a complete tree footprint of >=5% of physical memory for about {memory_impact_seconds:.0f}s of capped sampled time."
        )
        if memory_impact_seconds < 60.0:
            explanation = (
                f"No sustained overlap of high host RAM pressure and a complete tree footprint of >=5% was measured ({pressure_seconds:.0f}s of host pressure in sampled time). Large memory use alone is not penalized."
            )
        elif memory_impact_seconds > 0.0:
            explanation += " Co-occurrence does not prove the workload caused all host pressure."
        components.append(_component("Memory / host pressure", 25, points, explanation))
    else:
        components.append(_component(
            "Memory / host pressure", 25, None,
            "Too few samples have complete process-tree RAM and host RAM measurements.",
        ))

    # C. High workload CPU alone is not a penalty. Both metrics must be complete.
    if cpu_count >= 2:
        points = 15.0 * (1.0 - _ramp(cpu_saturation_seconds, 30.0, 300.0))
        explanation = (
            f"Host CPU saturation (>=90%) overlapped complete workload-tree use of >=10% of host capacity for about {cpu_saturation_seconds:.0f}s of capped sampled time."
        )
        if cpu_saturation_seconds < 30.0:
            explanation = (
                f"No sustained host CPU saturation was measured with meaningful complete workload-tree use ({cpu_saturation_seconds:.0f}s of overlap). High workload CPU alone is not penalized."
            )
        elif cpu_saturation_seconds > 0.0:
            explanation += " Co-occurrence does not prove the workload caused all host load."
        components.append(_component("CPU / host impact", 15, points, explanation))
    else:
        components.append(_component(
            "CPU / host impact", 15, None,
            "Too few samples have complete process-tree CPU and host CPU measurements.",
        ))

    # D. Online regression evaluates complete tree RAM samples without storing
    # the time series. A deduction also requires sustained host RAM pressure.
    if (
        growth_per_minute is not None
        and memory_count >= 6
        and growth_span_s >= 120.0
    ):
        growth = _ramp(growth_per_minute, 0.25, 2.5)
        pressure = _ramp(tree_pressure_seconds, 60.0, 300.0)
        points = 10.0 - 10.0 * growth * pressure
        explanation = (
            f"Complete tree RAM changed at an estimated {growth_per_minute:+.2f}% of physical memory per minute over {growth_span_s:.0f}s."
        )
        if growth_per_minute > 0.25 and tree_pressure_seconds >= 60.0:
            explanation += " A score deduction requires sustained host RAM pressure; growth alone does not establish a leak."
        else:
            explanation += " No deduction is made for growth without sustained host pressure."
        components.append(_component("Resource stability", 10, points, explanation))
    else:
        components.append(_component(
            "Resource stability", 10, None,
            "At least 6 complete tree RAM samples spanning 120 seconds are required to assess a RAM trend.",
        ))

    # E. Only verify identities observed while alive; detached descendants are
    # still checked at the root's end. Expected lifetime is not inferred.
    if tree_end_state == "cleared":
        points = 10.0
        explanation = tree_end_detail or "Previously observed descendants were no longer running when the root ended."
    elif tree_end_state == "residual":
        points = None
        explanation = tree_end_detail or "Previously observed descendants remained when the root ended; their expected lifetime is unknown, so no deduction is assigned."
    else:
        points = None
        explanation = tree_end_detail or "The final state of previously observed descendants could not be verified."
    components.append(_component("Process-tree lifecycle", 10, points, explanation))

    known_weight = sum(item["weight"] for item in components if item["available"])
    earned = sum(item["points"] or 0.0 for item in components if item["available"])
    enough_samples = cpu_count >= 2 or memory_count >= 2
    score = (
        round(100.0 * earned / known_weight)
        if known_weight >= MIN_KNOWN_WEIGHT and enough_samples else None
    )
    grade = _grade(score)

    start_delay = (
        max(0.0, first_seen - created)
        if first_seen is not None and created is not None else None
    )
    high_confidence = (
        score is not None
        and not monitor_interrupted
        and sample_evidence_complete
        and duration is not None and duration >= 60.0
        and sample_count >= 6
        and valid_metric_fraction >= 0.80
        and tree_complete_fraction is not None
        and tree_complete_fraction >= 0.80
        and max_gap_s is not None
        and max_gap_s <= 60.0
        and start_delay is not None and start_delay <= 10.0
        and end_reason == "process_ended"
        and tree_end_state == "cleared"
        and known_weight == 100
    )
    medium_confidence = (
        score is not None
        and not monitor_interrupted
        and sample_evidence_complete
        and duration is not None and duration >= 30.0
        and sample_count >= 3
        and known_weight >= 60
        and valid_metric_fraction >= 0.60
        and tree_complete_fraction is not None
        and tree_complete_fraction >= 0.60
        and end_reason == "process_ended"
        and tree_end_state == "cleared"
        and max_gap_s is not None
        and max_gap_s <= 60.0
    )
    confidence = "HIGH" if high_confidence else "MEDIUM" if medium_confidence else "LOW"

    explanations = [
        item["explanation"] for item in components
        if item["available"] and item["points"] < item["weight"]
    ][:3]
    if not explanations:
        explanations = [
            item["explanation"] for item in components if item["available"]
        ][:3]
    unavailable = [item["name"] for item in components if not item["available"]]
    if unavailable:
        explanations.append("Unavailable evidence: " + ", ".join(unavailable) + ".")
    if monitor_interrupted:
        explanations.insert(0, "ProcOrbit restarted during the workload; confidence is capped at LOW.")
    elif not sample_evidence_complete:
        explanations.insert(0, "Sample coverage is incomplete; confidence is capped at LOW. " + sample_evidence_detail)
    if score is None:
        explanations.insert(0, "Not enough measured data for a defensible 0–100 score.")

    def average(total: float, count: int) -> float | None:
        return total / count if count else None

    metrics = {
        "sample_count": sample_count,
        "valid_cpu_samples": cpu_count,
        "valid_memory_samples": memory_count,
        "tree_cpu_avg_pct": average(cpu_sum, cpu_count),
        "tree_cpu_peak_pct": cpu_peak,
        "host_cpu_avg_pct": average(host_cpu_sum, cpu_count),
        "host_cpu_peak_pct": host_cpu_peak,
        "tree_ram_avg_mb": average(memory_sum, memory_count),
        "tree_ram_peak_mb": memory_peak,
        "tree_ram_peak_host_pct": tree_mem_pct_peak,
        "host_ram_avg_pct": average(host_ram_sum, memory_count),
        "host_ram_peak_pct": host_ram_peak,
        "host_ram_pressure_seconds": round(pressure_seconds, 1),
        "host_ram_evidence_seconds": round(host_ram_evidence_seconds, 1),
        "tree_host_ram_pressure_seconds": round(tree_pressure_seconds, 1),
        "memory_impact_seconds": round(memory_impact_seconds, 1),
        "memory_evidence_seconds": round(memory_evidence_seconds, 1),
        "cpu_saturation_seconds": round(cpu_saturation_seconds, 1),
        "cpu_evidence_seconds": round(cpu_evidence_seconds, 1),
        "peak_descendants_observed": child_peak,
        "max_sample_gap_s": max_gap_s,
        "sample_span_s": span_s,
        "sample_start_gap_s": sample_start_gap_s,
        "sample_end_gap_s": sample_end_gap_s,
        "sample_evidence_complete": sample_evidence_complete,
        "sample_evidence_detail": sample_evidence_detail,
        "expected_sample_interval_s": expected_interval,
        "sample_coverage_grace_s": coverage_grace_s,
        "valid_metric_fraction": round(valid_metric_fraction, 3),
        "tree_complete_fraction": tree_complete_fraction,
        "ram_growth_pct_host_per_min": growth_per_minute,
        "known_weight": known_weight,
        "monitor_interrupted": monitor_interrupted,
    }
    return {
        "version": SCORING_VERSION,
        "score": score,
        "grade": grade,
        "confidence": confidence,
        "known_weight": known_weight,
        "components": components,
        "explanations": explanations[:3],
        "metrics": metrics,
    }
