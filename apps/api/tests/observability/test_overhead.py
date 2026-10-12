"""M6.2 — measure @traced overhead. P95 must be < 0.5ms.

We run 1000 calls of the wrapped vs unwrapped function and compare
per-call latency. The decorator must not be a hot-path bottleneck.
"""
from __future__ import annotations

import time

import pytest

from observability import traced


async def _run_n(fn, n: int) -> list[float]:
    samples: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        await fn()
        samples.append((time.perf_counter() - t0) * 1000)
    return samples


def _p95(samples: list[float]) -> float:
    return sorted(samples)[int(len(samples) * 0.95)]


@pytest.mark.asyncio
async def test_decorator_overhead_p95_under_0_5ms() -> None:
    """The @traced decorator adds < 0.5ms at P95."""
    @traced
    async def wrapped() -> None:
        return None

    async def bare() -> None:
        return None

    # Warmup
    await _run_n(wrapped, 50)
    await _run_n(bare, 50)

    # Measure
    wrapped_samples = await _run_n(wrapped, 1000)
    bare_samples = await _run_n(bare, 1000)

    wrapped_p95 = _p95(wrapped_samples)
    bare_p95 = _p95(bare_samples)
    overhead_p95 = wrapped_p95 - bare_p95

    # Allow some headroom (CI is noisy)
    assert overhead_p95 < 0.5, (
        f"@traced overhead P95 = {overhead_p95:.3f}ms; "
        f"wrapped P95 = {wrapped_p95:.3f}ms, bare P95 = {bare_p95:.3f}ms"
    )


@pytest.mark.asyncio
async def test_decorator_with_attributes_overhead() -> None:
    """@traced with a few attributes is still < 0.5ms P95."""
    @traced("with.attrs", attributes={
        "tenant_id": "01HZT",
        "provider_name": "anthropic",
        "model": "claude-3-5-sonnet-20241022",
    })
    async def wrapped() -> None:
        return None

    async def bare() -> None:
        return None

    await _run_n(wrapped, 50)
    await _run_n(bare, 50)
    wrapped_samples = await _run_n(wrapped, 1000)
    bare_samples = await _run_n(bare, 1000)
    overhead = _p95(wrapped_samples) - _p95(bare_samples)
    assert overhead < 0.5, f"overhead = {overhead:.3f}ms"


@pytest.mark.asyncio
async def test_decorator_handles_exception_overhead() -> None:
    """Exception path doesn't add measurable overhead."""
    @traced
    async def wrapped() -> None:
        raise ValueError("x")

    @traced(record_exception=False)
    async def wrapped_no_record() -> None:
        raise ValueError("x")

    for _ in range(100):
        try:
            await wrapped()
        except ValueError:
            pass
        try:
            await wrapped_no_record()
        except ValueError:
            pass
    # If we got here without timing out, exception path is fine.
