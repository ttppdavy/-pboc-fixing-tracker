import importlib.util
from pathlib import Path

P = Path(__file__).resolve().parents[1] / "scripts" / "update_data.py"
spec = importlib.util.spec_from_file_location("tracker", P)
tracker = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = tracker
spec.loader.exec_module(tracker)


def test_forecast_regex():
    s = "PBOC is expected to set the USD/CNY reference rate at 6.7706 – Reuters estimate"
    assert float(tracker.FORECAST_RE.search(s).group(1)) == 6.7706


def test_actual_regex():
    s = "PBOC sets USD/ CNY central rate at 6.7917 (vs. estimate at 6.7706)"
    m = tracker.ACTUAL_RE.search(s)
    assert (float(m.group(1)), float(m.group(2))) == (6.7917, 6.7706)


def test_deviation():
    old = {}
    est = {"2026-07-21": {"reuters_estimate": 6.7706}}
    official = {"2026-07-21": 6.7917}
    row = tracker.merge_rows(old, est, official)["2026-07-21"]
    assert row.deviation_points == 211


def test_actual_regex_variants():
    samples = [
        "PBOC sets USD/ CNY reference rate for today at 6.7909 (vs. estimate at 6.7577)",
        "PBOC sets USD/ CNY mid-point today at 6.7910 (vs. estimate at 6.7965)",
        "PBOC set USD/CNY central rate at 7.1020 (vs estimate at 7.1100)",
    ]
    assert all(tracker.ACTUAL_RE.search(x) for x in samples)
