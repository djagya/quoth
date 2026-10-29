"""How many threads a long-running embedder should use inside a container.

`os.cpu_count()` reports the host's CPUs. A container's CFS quota (cgroup v2 `cpu.max`)
caps what the process may actually use, and CPU affinity does not show it. ONNX Runtime
with more intra-op threads than the quota allows gets throttled and runs slower.
"""
from __future__ import annotations

import math
import os
from pathlib import Path

CPU_MAX = Path("/sys/fs/cgroup/cpu.max")
HEADROOM = 2  # left for the gateway and everything else sharing the container's quota
ENV = "QUOTH_EMBED_THREADS"


def cgroup_quota(path: Path = CPU_MAX) -> float | None:
    """CPUs allowed by a cgroup v2 quota, or None when unlimited or unreadable."""
    try:
        quota, period = path.read_text().split()[:2]
        if quota == "max":
            return None
        return int(quota) / int(period)
    except (OSError, ValueError):
        return None


def available_cpus(path: Path = CPU_MAX) -> int:
    try:
        cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):  # macOS has no affinity API
        cpus = os.cpu_count() or 4
    quota = cgroup_quota(path)
    if quota is not None:
        cpus = min(cpus, max(1, math.floor(quota)))
    return cpus


def embed_threads(path: Path = CPU_MAX) -> int:
    """`QUOTH_EMBED_THREADS` when set, else the usable CPUs minus headroom (at least 1)."""
    raw = os.environ.get(ENV, "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    cpus = available_cpus(path)
    return max(1, cpus - HEADROOM) if cpus > HEADROOM + 1 else cpus
