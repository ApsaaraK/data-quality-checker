import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dq_check as dq  # noqa: E402

AS_OF = date(2025, 12, 31)
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def dirty_results():
    dirty = ROOT / "data" / "dirty"
    if not (dirty / "PLANTED_ERRORS.json").exists():
        subprocess.run([sys.executable, str(ROOT / "make_dirty_data.py")], check=True, capture_output=True)
    return {r.check_id: r for r in dq.run_checks(dirty, CFG, AS_OF)}


def test_every_planted_error_is_detected_with_exact_counts(dirty_results):
    planted = json.loads((ROOT / "data" / "dirty" / "PLANTED_ERRORS.json").read_text())
    for check_id, expected in planted.items():
        result = dirty_results[check_id]
        if expected == "FAIL":
            assert not result.passed, check_id
        else:
            assert result.rows_failed == expected, f"{check_id}: found {result.rows_failed}, planted {expected}"


def test_every_check_in_config_has_ground_truth():
    planted = json.loads((ROOT / "data" / "dirty" / "PLANTED_ERRORS.json").read_text())
    assert {c["id"] for c in CFG["checks"]} == set(planted)


def test_clean_dataset_passes_every_check():
    results = dq.run_checks(ROOT / "data" / "clean", CFG, AS_OF)
    failing = [r.check_id for r in results if not r.passed]
    assert failing == []
    assert dq.verdict(results) == "PASS"


def test_failed_records_name_the_row_and_the_reason(dirty_results):
    failed = dirty_results["positive_values_order_items"].failed
    assert len(failed) == 21
    assert failed["detail"].str.contains("must be a positive number").all()
    assert (failed["row_number"] >= 2).all()


# ---------------------------------------------------------------- small, hand-checkable cases

def make_cfg(*checks):
    return {"tables": ["t", "parent"], "date_formats": ["%Y-%m-%d"], "min_date": "2015-01-01", "checks": list(checks)}


def write_tables(folder, t_rows, parent_rows=("p1", "p2")):
    (folder / "t.csv").write_text("id,parent_id,day,amount\n" + "\n".join(t_rows) + "\n", encoding="utf-8")
    (folder / "parent.csv").write_text("parent_id\n" + "\n".join(parent_rows) + "\n", encoding="utf-8")


def spec(type_, check_id="c", **kw):
    return {"id": check_id, "family": "f", "type": type_, "table": "t", "severity": "critical", **kw}


def test_duplicate_ids_count_every_row_in_a_group(tmp_path):
    write_tables(tmp_path, ["1,p1,2025-01-01,5", "1,p1,2025-01-02,6", "2,p1,2025-01-03,7", "3,p2,2025-01-04,8"])
    result = dq.run_checks(tmp_path, make_cfg(spec("duplicate_ids", column="id")), AS_OF)[0]
    assert result.rows_failed == 2 and result.rows_checked == 4


def test_blank_keys_are_not_double_counted_as_orphans(tmp_path):
    write_tables(tmp_path, ["1,,2025-01-01,5", "2,p9,2025-01-02,6", "3,p1,2025-01-03,7"])
    fk = spec("foreign_key", column="parent_id", ref_table="parent", ref_column="parent_id")
    req = spec("required_fields", check_id="r", columns=["id", "parent_id"])
    fk_result, req_result = dq.run_checks(tmp_path, make_cfg(fk, req), AS_OF)
    assert fk_result.rows_failed == 1          # only p9
    assert req_result.rows_failed == 1         # only the blank parent_id


def test_dates_future_old_and_unreadable_are_flagged(tmp_path):
    write_tables(tmp_path, ["1,p1,2025-01-01,5", "2,p1,2026-01-01,5", "3,p1,2001-01-01,5", "4,p1,31/02/2025,5", "5,p1,,5"])
    result = dq.run_checks(tmp_path, make_cfg(spec("valid_dates", column="day")), AS_OF)[0]
    assert result.rows_failed == 3 and result.rows_checked == 4  # the blank date is left to the required-fields rule


def test_freshness_limit_is_inclusive_and_ignores_future_dates(tmp_path):
    write_tables(tmp_path, ["1,p1,2025-12-24,5", "2,p1,2035-01-01,5"])
    fresh = spec("freshness", column="day", max_age_days=7)
    assert dq.run_checks(tmp_path, make_cfg(fresh), AS_OF)[0].passed            # exactly 7 days old
    fresh["max_age_days"] = 6
    assert not dq.run_checks(tmp_path, make_cfg(fresh), AS_OF)[0].passed        # 7 days old is now too old


def test_non_numeric_and_negative_amounts_fail(tmp_path):
    write_tables(tmp_path, ["1,p1,2025-01-01,5", "2,p1,2025-01-02,-3", "3,p1,2025-01-03,abc", "4,p1,2025-01-04,0"])
    result = dq.run_checks(tmp_path, make_cfg(spec("positive_values", columns=["amount"])), AS_OF)[0]
    assert result.rows_failed == 3


def test_missing_table_file_is_reported_not_ignored(tmp_path):
    (tmp_path / "t.csv").write_text("id\n1\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        dq.run_checks(tmp_path, make_cfg(spec("duplicate_ids", column="id")), AS_OF)


def test_command_line_exit_codes(tmp_path):
    base = [sys.executable, str(ROOT / "dq_check.py"), "--as-of", "2025-12-31"]
    bad = subprocess.run(base + ["--data", str(ROOT / "data" / "dirty"), "--out", str(tmp_path / "a")], capture_output=True)
    good = subprocess.run(base + ["--data", str(ROOT / "data" / "clean"), "--out", str(tmp_path / "b")], capture_output=True)
    broken = subprocess.run(base + ["--data", str(tmp_path / "nope"), "--out", str(tmp_path / "c")], capture_output=True)
    assert (bad.returncode, good.returncode, broken.returncode) == (1, 0, 2)
    assert (tmp_path / "a" / "dq_report.html").exists() and (tmp_path / "a" / "failed_records.csv").exists()

