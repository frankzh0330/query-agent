"""Live end-to-end eval: real LLM extraction + real matcher + real LLM SQL generation.

Reuses tests/evals/nl2sql_cases.yaml (text + expectations) but does NOT mock the LLM,
so it measures what the mocked pytest eval cannot: extraction quality and SQL quality.

    python scripts/live_eval.py                 # all runnable cases
    python scripts/live_eval.py -k s0 -k j0     # name substring filter
    python scripts/live_eval.py --out eval_results/run1.json

Per step it records: expectation pass/fail (same assertions as the pytest eval),
SQL static validation (sqlglot), whether the resolved metric expressions appear in the
SQL, latency, and the generated SQL. Cases that need a fake matcher are skipped.
Background memory-judge LLM calls are stubbed so the run only sends the eval questions.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import warnings  # noqa: E402

warnings.filterwarnings("ignore")

import yaml  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
from matcher.matcher_service import MatcherService  # noqa: E402
from service.sql_validator import validate_sql  # noqa: E402
from tests.test_end_to_end_evals import (  # noqa: E402
    _assert_expectations,
    _rebind_runtime,
    _seed_setup,
    _write_project_memory,
)


async def _noop(*_a, **_k):
    return None


def _norm(sql: str) -> str:
    return "".join((sql or "").lower().split())


def run_step(client, step, session_id, service):
    body_req = {"text": step["text"], "project_id": step.get("project_id", 55)}
    if session_id:
        body_req["session_id"] = session_id
    t0 = time.time()
    with mock.patch("app.get_matcher_service", return_value=service):
        resp = client.post("/nl2sql", json=body_req)
    latency = round(time.time() - t0, 2)
    body = resp.json()

    # same assertions as the pytest eval; generate_sql is real so join check uses SQL text
    expect = dict(step["expect"])
    expect.pop("sql_intent_contains_join", None)
    expect.pop("time_expr_contains", None)
    error = None
    try:
        _assert_expectations(body, expect, None)
    except (AssertionError, KeyError, TypeError, IndexError) as e:
        import traceback

        line = traceback.extract_tb(e.__traceback__)[-1].line or ""
        msg = (str(e).splitlines() or [""])[0][:160]
        error = f"{type(e).__name__}: {msg or line.strip()[:160]}"

    sql = body.get("sql") or ""
    row = {
        "text": step["text"],
        "status": body.get("status"),
        "intent_ok": error is None,
        "error": error,
        "latency_s": latency,
        "sql": sql,
        "sql_valid": None,
        "metric_expr_used": None,
        "join_in_sql": None,
        "extraction": body.get("extraction_json"),
        "resolved_intent": body.get("resolved_intent"),
    }
    if body.get("status") == "success" and sql:
        v = validate_sql(sql, service.schema.tables.keys())
        row["sql_valid"] = bool(v.ok)
        row["sql_errors"] = list(getattr(v, "errors", []) or [])
        norm = _norm(sql)
        metrics = (body.get("resolved_intent") or {}).get("metrics") or []
        row["metric_expr_used"] = all(_norm(service.get_metric_expr(m)) in norm for m in metrics) if metrics else None
        want = step["expect"].get("sql_intent_contains_join")
        if want:
            row["join_in_sql"] = f"join{want}" in norm or f"join`{want}`" in norm
    return row, body.get("session_id", session_id)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", action="append", default=[], help="case name substring (repeatable)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cases = yaml.safe_load((ROOT / "tests/evals/nl2sql_cases.yaml").read_text(encoding="utf-8"))["cases"]
    runnable, skipped = [], []
    for c in cases:
        if args.k and not any(k in c["name"] for k in args.k):
            continue
        if c.get("live_skip") or any(s.get("resolver") == "low_confidence_table" for s in c["steps"]):
            skipped.append(c["name"])
        else:
            runnable.append(c)

    backend = os.getenv("LLM_BACKEND", "zhipu")
    print(f"backend={backend} cases={len(runnable)} skipped(fake matcher)={len(skipped)}", flush=True)

    service = MatcherService(catalog_path="catalog")
    results = []
    for case in runnable:
        data_root = tempfile.mkdtemp(prefix="live_eval_")
        _rebind_runtime(data_root)
        app_module.orchestrator.memory.maybe_save = _noop
        app_module._matcher_service = service
        session_id = _seed_setup(case)
        rows = []
        with TestClient(app_module.app) as client:
            for step in case["steps"]:
                try:
                    row, session_id = run_step(client, step, session_id, service)
                except Exception as e:  # LLM/network failure must not abort the run
                    row = {"text": step["text"], "status": "exception", "intent_ok": False,
                           "error": f"{type(e).__name__}: {str(e)[:200]}", "sql": ""}
                rows.append(row)
        ok = all(r["intent_ok"] for r in rows)
        sql_ok = all(r.get("sql_valid") is not False for r in rows)
        results.append({"case": case["name"], "xfail": case.get("xfail"), "pass": ok,
                        "sql_ok": sql_ok, "steps": rows})
        flag = "PASS" if ok else ("KNOWN-GAP" if case.get("xfail") else "FAIL")
        last = rows[-1]
        print(f"[{flag:9}] {case['name']:38} {last['status']:18} {last.get('latency_s','-')}s"
              + (f"  {last['error']}" if not ok else ""), flush=True)

    total = len(results)
    normal = [r for r in results if not r["xfail"]]
    gaps = [r for r in results if r["xfail"]]
    n_pass = sum(r["pass"] for r in normal)
    sql_rows = [s for r in results for s in r["steps"] if s.get("sql_valid") is not None]
    used = [s for s in sql_rows if s.get("metric_expr_used") is not None]
    summary = {
        "backend": backend,
        "cases_run": total,
        "skipped": skipped,
        "regular_pass": f"{n_pass}/{len(normal)}",
        "known_gap_cases_now_passing": [r["case"] for r in gaps if r["pass"]],
        "known_gap_cases_still_failing": [r["case"] for r in gaps if not r["pass"]],
        "sql_generated": len(sql_rows),
        "sql_valid": sum(1 for s in sql_rows if s["sql_valid"]),
        "metric_expr_used": f"{sum(1 for s in used if s['metric_expr_used'])}/{len(used)}",
        "avg_latency_s": round(sum(s.get("latency_s", 0) for r in results for s in r["steps"]) /
                               max(1, sum(len(r["steps"]) for r in results)), 2),
    }
    print("\n" + json.dumps(summary, indent=2, ensure_ascii=False))
    out = Path(args.out or ROOT / "eval_results" / f"live_eval_{time.strftime('%Y%m%d_%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "results": results}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nreport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
