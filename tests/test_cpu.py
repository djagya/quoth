from quoth import cpu


def test_quota_caps_threads_below_host_count(tmp_path, monkeypatch):
    monkeypatch.delenv(cpu.ENV, raising=False)
    monkeypatch.setattr(cpu.os, "sched_getaffinity", lambda pid: set(range(16)), raising=False)
    path = tmp_path / "cpu.max"
    path.write_text("800000 100000\n")  # a container capped at 8 CPUs on a 16-core host
    assert cpu.cgroup_quota(path) == 8.0 and cpu.available_cpus(path) == 8 and cpu.embed_threads(path) == 6
    path.write_text("max 100000\n")
    assert cpu.cgroup_quota(path) is None and cpu.embed_threads(path) == 14
    path.write_text("150000 100000\n")  # 1.5 CPUs: never below one thread, no headroom taken
    assert cpu.embed_threads(path) == 1
    assert cpu.cgroup_quota(tmp_path / "missing") is None


def test_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv(cpu.ENV, "3")
    assert cpu.embed_threads(tmp_path / "missing") == 3
    monkeypatch.setenv(cpu.ENV, "junk")
    monkeypatch.setattr(cpu, "available_cpus", lambda path=None: 2)
    assert cpu.embed_threads(tmp_path / "missing") == 2
