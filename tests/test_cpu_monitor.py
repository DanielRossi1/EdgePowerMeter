"""CPU monitor /proc/stat parsing tests (guest double-counting)."""
from __future__ import annotations

from app.core.cpu_monitor import CPUUsageMonitor


def test_parse_excludes_guest_from_total():
    # user nice system idle iowait irq softirq steal guest guest_nice
    # guest(=50) and guest_nice(=10) are already folded into user/nice by the
    # kernel and must not be added again.
    line = "cpu 1000 200 300 4000 50 10 20 5 50 10"
    total, idle = CPUUsageMonitor._parse_proc_stat_line(line)
    raw_sum = 1000 + 200 + 300 + 4000 + 50 + 10 + 20 + 5 + 50 + 10
    assert total == raw_sum - 50 - 10
    assert idle == 4000 + 50  # idle + iowait


def test_parse_short_line_without_guest():
    line = "cpu 1000 200 300 4000"
    total, idle = CPUUsageMonitor._parse_proc_stat_line(line)
    assert total == 5500
    assert idle == 4000


def test_parse_rejects_non_cpu_line():
    assert CPUUsageMonitor._parse_proc_stat_line("cpu0 1 2 3 4") is None
    assert CPUUsageMonitor._parse_proc_stat_line("") is None
    assert CPUUsageMonitor._parse_proc_stat_line("cpu 1 2") is None


def test_usage_in_valid_range():
    mon = CPUUsageMonitor()
    mon.get_usage()  # prime
    val = mon.get_usage()
    if val is not None:
        assert 0.0 <= val <= 100.0
