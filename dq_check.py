#!/usr/bin/env python3
"""
dq_check.py - config-driven data quality checks for a set of CSV tables.

Usage:
    python dq_check.py --data data/dirty --out reports/dirty --as-of 2025-12-31
    python dq_check.py --data data/clean --out reports/clean --as-of 2025-12-31 \
                       --baseline reports/dirty/dq_results.csv

Six kinds of rule, all defined in config.json:
    missing required fields | duplicate IDs | invalid dates |
    unexpected values (non-positive, out of range, cost above price) |
    broken references (orphan keys) | data freshness

Outputs in --out:
    dq_report.html       one-page report (open in a browser)
    dq_results.csv       one row per check: rows checked, rows failed, status
    failed_records.csv   the failing rows, with the reason for each

Exit code: 0 = no critical failures, 1 = at least one critical failure, 2 = could not run.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd

MISSING_TOKENS = {"", "n/a", "na", "null", "none", "nan", "-"}
MAX_FAILED_ROWS_SAVED = 500  # per check, to keep the output files readable
EXAMPLES_IN_REPORT = 5


@dataclass
class CheckResult:
    check_id: str
    family: str
    table: str
    severity: str
    description: str
    passed: bool
    rows_checked: int | None
    rows_failed: int | None
    failed: pd.DataFrame  # columns: row_number, key, detail, record

    @property
    def status(self) -> str:
        return "PASS" if self.passed else "FAIL"


# --------------------------------------------------------------------------
# Loading and helpers
# --------------------------------------------------------------------------

def load_tables(folder: Path, names: list[str]) -> dict[str, pd.DataFrame]:
    tables = {}
    for name in names:
        path = Path(folder) / f"{name}.csv"
        if not path.exists():
            raise FileNotFoundError(f"missing file: {path}")
        df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
        df.insert(0, "_row", range(2, len(df) + 2))  # row number as shown in Excel (header = row 1)
        tables[name] = df
    return tables


def is_missing(series: pd.Series) -> pd.Series:
    return series.astype(str).str.strip().str.lower().isin(MISSING_TOKENS)


def parse_dates(series: pd.Series, formats: list[str]) -> pd.Series:
    parsed = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    text = series.astype(str).str.strip()
    for fmt in formats:
        parsed = parsed.fillna(pd.to_datetime(text, format=fmt, errors="coerce"))
    return parsed


def _record(row: pd.Series) -> str:
    return "; ".join(f"{k}={v}" for k, v in row.items() if k != "_row")


def failed_frame(df: pd.DataFrame, mask: pd.Series, key_col: str | None, detail: pd.Series | str) -> pd.DataFrame:
    sub = df[mask]
    if sub.empty:
        return pd.DataFrame(columns=["row_number", "key", "detail", "record"])
    out = pd.DataFrame({
        "row_number": sub["_row"].values,
        "key": sub[key_col].values if key_col else "",
        "detail": (detail[mask].values if isinstance(detail, pd.Series) else detail),
        "record": sub.apply(_record, axis=1).values,
    })
    return out


def make_result(spec: dict, rows_checked, mask: pd.Series, df: pd.DataFrame, key_col, detail) -> CheckResult:
    failed = failed_frame(df, mask, key_col, detail)
    n_failed = int(mask.sum())
    return CheckResult(spec["id"], spec["family"], spec["table"], spec.get("severity", "critical"),
                       spec.get("description", ""), n_failed == 0, int(rows_checked), n_failed, failed)


# --------------------------------------------------------------------------
# Rule implementations
# --------------------------------------------------------------------------

def check_required_fields(spec, tables, cfg, as_of):
    df = tables[spec["table"]]
    cols = spec["columns"]
    missing = pd.DataFrame({c: is_missing(df[c]) for c in cols})
    mask = missing.any(axis=1)
    detail = missing.apply(lambda r: "missing: " + ", ".join(c for c in cols if r[c]), axis=1)
    return make_result(spec, len(df), mask, df, cols[0], detail)


def check_duplicate_ids(spec, tables, cfg, as_of):
    df = tables[spec["table"]]
    col = spec["column"]
    has_key = ~is_missing(df[col])
    counts = df.loc[has_key, col].map(df.loc[has_key, col].value_counts())
    mask = pd.Series(False, index=df.index)
    mask[has_key] = counts > 1
    detail = pd.Series("", index=df.index)
    detail[has_key] = counts.map(lambda n: f"id appears {n} times")
    return make_result(spec, has_key.sum(), mask, df, col, detail)


def check_valid_dates(spec, tables, cfg, as_of):
    df = tables[spec["table"]]
    col = spec["column"]
    present = ~is_missing(df[col])  # blanks belong to the required-fields check
    parsed = parse_dates(df[col], cfg["date_formats"])
    min_date = pd.Timestamp(cfg["min_date"])
    unreadable = present & parsed.isna()
    too_old = present & parsed.notna() & (parsed < min_date)
    future = present & parsed.notna() & (parsed > pd.Timestamp(as_of))
    mask = unreadable | too_old | future
    detail = pd.Series("", index=df.index)
    detail[unreadable] = df.loc[unreadable, col].map(lambda v: f"'{v}' is not a valid date")
    detail[too_old] = f"before {cfg['min_date']}"
    detail[future] = f"after {as_of.isoformat()} (in the future)"
    return make_result(spec, present.sum(), mask, df, None, detail)


def _numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False).str.strip(), errors="coerce")


def check_positive_values(spec, tables, cfg, as_of):
    df = tables[spec["table"]]
    cols = spec["columns"]
    mask = pd.Series(False, index=df.index)
    detail = pd.Series("", index=df.index)
    for col in cols:
        present = ~is_missing(df[col])
        num = _numeric(df[col])
        bad = present & (num.isna() | (num <= 0))
        mask |= bad
        detail[bad] = detail[bad] + df.loc[bad, col].map(lambda v, c=col: f"{c}={v} (must be a positive number); ")
    detail = detail.str.rstrip("; ")
    return make_result(spec, len(df), mask, df, spec.get("key", df.columns[1]), detail)


def check_value_range(spec, tables, cfg, as_of):
    df = tables[spec["table"]]
    col = spec["column"]
    present = ~is_missing(df[col])
    num = _numeric(df[col])
    mask = present & (num.isna() | (num < spec["min"]) | (num > spec["max"]))
    detail = df[col].map(lambda v: f"{col}={v} (allowed {spec['min']} to {spec['max']})")
    return make_result(spec, present.sum(), mask, df, df.columns[1], detail)


def check_not_greater_than(spec, tables, cfg, as_of):
    df = tables[spec["table"]]
    a, b = spec["column"], spec["other_column"]
    both = ~is_missing(df[a]) & ~is_missing(df[b])
    mask = both & (_numeric(df[a]) > _numeric(df[b]))
    detail = df.apply(lambda r: f"{a}={r[a]} is above {b}={r[b]}", axis=1)
    return make_result(spec, both.sum(), mask, df, df.columns[1], detail)


def check_foreign_key(spec, tables, cfg, as_of):
    df = tables[spec["table"]]
    col = spec["column"]
    present = ~is_missing(df[col])  # blanks belong to the required-fields check
    parent_keys = set(tables[spec["ref_table"]][spec["ref_column"]])
    mask = present & ~df[col].isin(parent_keys)
    detail = df[col].map(lambda v: f"{col}={v} not found in {spec['ref_table']}.{spec['ref_column']}")
    return make_result(spec, present.sum(), mask, df, df.columns[1], detail)


def check_freshness(spec, tables, cfg, as_of):
    df = tables[spec["table"]]
    parsed = parse_dates(df[spec["column"]], cfg["date_formats"])
    usable = parsed[parsed.notna() & (parsed <= pd.Timestamp(as_of))]  # ignore invalid and future dates
    if usable.empty:
        age, detail = None, "no usable dates found"
    else:
        latest = usable.max().date()
        age = (as_of - latest).days
        detail = (f"latest {spec['column']} is {latest.isoformat()}, {age} days before {as_of.isoformat()} "
                  f"(limit {spec['max_age_days']} days)")
    passed = age is not None and age <= spec["max_age_days"]
    failed = pd.DataFrame(columns=["row_number", "key", "detail", "record"]) if passed else pd.DataFrame(
        [{"row_number": "", "key": spec["table"], "detail": detail, "record": ""}])
    result = CheckResult(spec["id"], spec["family"], spec["table"], spec.get("severity", "critical"),
                         spec.get("description", ""), passed, None, None, failed)
    result.freshness_detail = detail  # type: ignore[attr-defined]
    return result


CHECKS = {
    "required_fields": check_required_fields, "duplicate_ids": check_duplicate_ids,
    "valid_dates": check_valid_dates, "positive_values": check_positive_values,
    "value_range": check_value_range, "not_greater_than": check_not_greater_than,
    "foreign_key": check_foreign_key, "freshness": check_freshness,
}


def run_checks(data_dir: Path, cfg: dict, as_of: date) -> list[CheckResult]:
    tables = load_tables(Path(data_dir), cfg["tables"])
    return [CHECKS[spec["type"]](spec, tables, cfg, as_of) for spec in cfg["checks"]]


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------

def verdict(results: list[CheckResult]) -> str:
    if any(not r.passed and r.severity == "critical" for r in results):
        return "FAIL"
    if any(not r.passed for r in results):
        return "PASS WITH WARNINGS"
    return "PASS"


def results_frame(results: list[CheckResult]) -> pd.DataFrame:
    rows = []
    for r in results:
        pct = (round(r.rows_failed / r.rows_checked * 100, 2) if r.rows_checked else
              (0.0 if r.rows_failed == 0 else None))
        example = ""
        if not r.failed.empty:
            first = r.failed.iloc[0]
            example = first["detail"] if r.rows_checked is None else f"row {first['row_number']}: {first['detail']}"
        rows.append({"check_id": r.check_id, "family": r.family, "table": r.table, "severity": r.severity,
                     "description": r.description, "status": r.status, "rows_checked": r.rows_checked,
                     "rows_failed": r.rows_failed, "pct_failed": pct, "example": example})
    return pd.DataFrame(rows)


def failed_records_frame(results: list[CheckResult]) -> pd.DataFrame:
    parts = []
    for r in results:
        if r.failed.empty:
            continue
        part = r.failed.head(MAX_FAILED_ROWS_SAVED).copy()
        part.insert(0, "check_id", r.check_id)
        part.insert(1, "table", r.table)
        parts.append(part)
    cols = ["check_id", "table", "row_number", "key", "detail", "record"]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=cols)


CSS = """
body{font-family:Arial,Helvetica,sans-serif;margin:0;background:#f5f6f8;color:#1b2430}
.wrap{max-width:1100px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px}
.sub{color:#5b6573;font-size:13px}
.banner{border-radius:8px;padding:16px 20px;margin:18px 0;color:#fff;font-size:17px;font-weight:bold}
.FAIL{background:#c0392b}.PASS{background:#1e8449}.WARN{background:#b9770e}
.cards{display:flex;gap:12px;flex-wrap:wrap}
.card{background:#fff;border-radius:8px;padding:12px 16px;min-width:150px;box-shadow:0 1px 2px rgba(0,0,0,.08)}
.card b{display:block;font-size:24px}.card span{font-size:12px;color:#5b6573}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;font-size:13px;box-shadow:0 1px 2px rgba(0,0,0,.08)}
th{background:#1f3864;color:#fff;text-align:left;padding:8px 10px;font-weight:600}
td{padding:7px 10px;border-top:1px solid #e6e9ee;vertical-align:top}
.tag{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:bold;color:#fff}
.tag.PASS{background:#1e8449}.tag.FAIL{background:#c0392b}.tag.warning{background:#b9770e}
.bar{background:#e6e9ee;border-radius:4px;height:8px;width:90px;display:inline-block;vertical-align:middle}
.bar i{display:block;height:8px;border-radius:4px;background:#2a9d8f}
details{margin:4px 0}summary{cursor:pointer;color:#1f3864;font-size:12px}
.ex{font-family:Consolas,monospace;font-size:11px;background:#f5f6f8;padding:6px 8px;margin:3px 0;border-radius:4px;word-break:break-all}
.num{text-align:right;white-space:nowrap}.muted{color:#5b6573}
"""


def _fmt(n):
    return "-" if n is None or (isinstance(n, float) and pd.isna(n)) else f"{int(n):,}"


def build_html(results: list[CheckResult], data_dir: str, as_of: date, baseline: pd.DataFrame | None) -> str:
    v = verdict(results)
    failed_checks = [r for r in results if not r.passed]
    total_failed_rows = sum(r.rows_failed or 0 for r in results)
    banner_cls = {"FAIL": "FAIL", "PASS": "PASS", "PASS WITH WARNINGS": "WARN"}[v]
    msg = {"FAIL": "NOT SAFE TO USE: critical data quality checks failed",
           "PASS": "SAFE TO USE: all checks passed",
           "PASS WITH WARNINGS": "USABLE WITH CARE: only warning-level checks failed"}[v]

    out = [f"<!doctype html><html><head><meta charset='utf-8'><title>Data Quality Report</title><style>{CSS}</style></head>"
           f"<body><div class='wrap'><h1>Data Quality Report</h1>"
           f"<div class='sub'>Data: {html.escape(str(data_dir))} &nbsp;|&nbsp; As-of date: {as_of.isoformat()}</div>"
           f"<div class='banner {banner_cls}'>{msg}</div><div class='cards'>"
           f"<div class='card'><b>{len(results)}</b><span>checks run</span></div>"
           f"<div class='card'><b>{len(results) - len(failed_checks)}</b><span>passed</span></div>"
           f"<div class='card'><b>{len(failed_checks)}</b><span>failed</span></div>"
           f"<div class='card'><b>{total_failed_rows:,}</b><span>failing rows (summed across checks)</span></div></div>"]

    if baseline is not None:
        now = results_frame(results).set_index("check_id")
        base = baseline.set_index("check_id")
        out.append("<h2>Before and after</h2><table><tr><th>Check</th><th class='num'>Failing rows before</th>"
                   "<th class='num'>Failing rows now</th><th>Before</th><th>Now</th></tr>")
        for cid in now.index:
            if cid not in base.index:
                continue
            b, n = base.loc[cid], now.loc[cid]
            out.append(f"<tr><td>{html.escape(cid)}</td><td class='num'>{_fmt(b['rows_failed'])}</td>"
                       f"<td class='num'>{_fmt(n['rows_failed'])}</td>"
                       f"<td><span class='tag {b['status']}'>{b['status']}</span></td>"
                       f"<td><span class='tag {n['status']}'>{n['status']}</span></td></tr>")
        b_fail = int((base["status"] == "FAIL").sum())
        n_fail = int((now["status"] == "FAIL").sum())
        out.append(f"</table><p class='sub'>Failed checks: {b_fail} before, {n_fail} now.</p>")

    out.append("<h2>All checks</h2><table><tr><th>Rule</th><th>Table</th><th>Check</th><th>Severity</th>"
               "<th class='num'>Rows checked</th><th class='num'>Rows failed</th><th>Pass rate</th><th>Status</th></tr>")
    for r in results:
        if r.rows_checked:
            pass_pct = 100 - (r.rows_failed / r.rows_checked * 100)
            bar = f"<span class='bar'><i style='width:{max(pass_pct, 0):.0f}%'></i></span> {pass_pct:.1f}%"
        else:
            bar = "<span class='muted'>n/a</span>"
        examples = ""
        if not r.failed.empty:
            lines = []
            for _, row in r.failed.head(EXAMPLES_IN_REPORT).iterrows():
                where = f"row {row['row_number']}: " if row["row_number"] != "" else ""
                rec = f" <span class='muted'>[{html.escape(str(row['record']))}]</span>" if row["record"] else ""
                lines.append(f"<div class='ex'>{html.escape(where + str(row['detail']))}{rec}</div>")
            examples = (f"<details><summary>Example failed records ({min(len(r.failed), EXAMPLES_IN_REPORT)} "
                        f"of {len(r.failed)})</summary>{''.join(lines)}</details>")
        out.append(f"<tr><td>{html.escape(r.family)}</td><td>{html.escape(r.table)}</td>"
                   f"<td><b>{html.escape(r.check_id)}</b><br><span class='muted'>{html.escape(r.description)}</span>{examples}</td>"
                   f"<td><span class='tag' style='background:{'#b9770e' if r.severity == 'warning' else '#5b6573'}'>"
                   f"{r.severity}</span></td>"
                   f"<td class='num'>{_fmt(r.rows_checked)}</td><td class='num'>{_fmt(r.rows_failed)}</td>"
                   f"<td>{bar}</td><td><span class='tag {r.status}'>{r.status}</span></td></tr>")
    out.append("</table>")
    out.append("<p class='sub'>Rows with a blank key are reported by the required-fields rule and are not counted again by the "
               "date, value or reference rules. For duplicate IDs, every row that shares a repeated ID is counted.</p>")
    out.append("</div></body></html>")
    return "".join(out)


def write_outputs(results, out_dir: Path, data_dir: str, as_of: date, baseline: pd.DataFrame | None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    results_frame(results).to_csv(out_dir / "dq_results.csv", index=False, encoding="utf-8-sig")
    failed_records_frame(results).to_csv(out_dir / "failed_records.csv", index=False, encoding="utf-8-sig")
    (out_dir / "dq_report.html").write_text(build_html(results, data_dir, as_of, baseline), encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run data quality checks on a folder of CSV tables.")
    ap.add_argument("--data", required=True, help="folder containing the CSV tables")
    ap.add_argument("--out", required=True, help="folder for the report and result files")
    ap.add_argument("--config", default=str(Path(__file__).parent / "config.json"))
    ap.add_argument("--as-of", default=None, help="date to measure freshness and 'future' dates against (YYYY-MM-DD, default today)")
    ap.add_argument("--baseline", default=None, help="dq_results.csv from an earlier run, for a before/after table")
    args = ap.parse_args(argv)

    try:
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
        as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date() if args.as_of else date.today()
        results = run_checks(Path(args.data), cfg, as_of)
        baseline = pd.read_csv(args.baseline) if args.baseline else None
    except (FileNotFoundError, ValueError, KeyError) as exc:
        print(f"Could not run: {exc}", file=sys.stderr)
        return 2

    write_outputs(results, Path(args.out), args.data, as_of, baseline)
    frame = results_frame(results)
    pd.set_option("display.width", 200)
    print(frame[["check_id", "status", "rows_checked", "rows_failed"]].to_string(index=False))
    print(f"\nOverall: {verdict(results)}  ({int((frame.status == 'FAIL').sum())} of {len(results)} checks failed)")
    print(f"Report: {Path(args.out).resolve() / 'dq_report.html'}")
    return 1 if verdict(results) == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
