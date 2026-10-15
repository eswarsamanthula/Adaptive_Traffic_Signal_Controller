"""Verify a live end-to-end run left the right evidence in the database.

Run AFTER the demo (broker + Node-RED + actuator + controller + publisher):

    python scripts/check_live_run.py [path/to/traffic.db]

Prints PASS/FAIL per check and a summary. Paste the full output back.
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "traffic.db"
MIN_G, MAX_G = 10, 60

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def q(c, sql):
    return c.execute(sql).fetchall()


def main():
    if not DB.exists():
        print(f"DB not found: {DB}")
        return 2
    c = sqlite3.connect(DB)
    print(f"DB: {DB}\n")

    print("Row counts:")
    counts = {}
    for (t,) in q(c, "select name from sqlite_master where type='table' and name not like 'sqlite_%'"):
        counts[t] = q(c, f"select count(*) from {t}")[0][0]
        print(f"  {t:<20}{counts[t]}")
    print()

    check("sensor_readings populated", counts.get("sensor_readings", 0) > 0)
    check("signal_decisions populated", counts.get("signal_decisions", 0) > 0,
          f"{counts.get('signal_decisions', 0)} rows")
    check("actuator_commands populated", counts.get("actuator_commands", 0) > 0,
          f"{counts.get('actuator_commands', 0)} rows")

    st = dict(q(c, "select status, count(*) from actuator_commands group by status"))
    print(f"  command status: {st}")
    total = sum(st.values())
    acc = st.get("accepted", 0)
    check("actuator acked >= 95% commands accepted", total > 0 and acc / total >= 0.95,
          f"{acc}/{total}")
    check("no rejected commands", st.get("rejected", 0) == 0, f"{st.get('rejected', 0)} rejected")
    check("no unacknowledged commands", st.get("unacknowledged", 0) == 0,
          f"{st.get('unacknowledged', 0)} unacked")

    bad = q(c, f"select count(*) from signal_decisions where applied_green_time < {MIN_G} "
               f"or applied_green_time > {MAX_G}")[0][0]
    check(f"all applied greens within {MIN_G}-{MAX_G}s", bad == 0, f"{bad} violations")

    ad = q(c, "select count(*) from signal_decisions where requested_green_time != applied_green_time")[0][0]
    print(f"  decisions where requested != applied (clamped): {ad}")

    per = q(c, "select approach, count(*) from signal_decisions group by approach")
    check("every approach got decisions (no starvation)", len({a for a, _ in per}) == 4,
          ", ".join(f"{a}:{n}" for a, n in per))

    fb = q(c, "select count(*) from signal_decisions where decision_reason like 'Fallback%'")[0][0]
    print(f"  fallback decisions (stale/missing sensor): {fb}")

    errs = q(c, "select level, count(*) from system_events group by level")
    print(f"  system_events by level: {dict(errs)}")
    check("no system errors logged", dict(errs).get("error", 0) == 0)

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
