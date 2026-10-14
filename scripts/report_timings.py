"""Time every report of the Reports page on the current database.

    DB_NAME=my_database .venv/bin/python scripts/report_timings.py [--runs 5]

Load a bigger data set first (scripts/load_sample_data.py --scale 4 gives about 100,000 tickets) to see how the reports scale.
Each report is run --runs times; the table shows the number of rows returned and the mean and slowest time.
"""
import argparse
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: E402  (importing the application gives the report definitions and the database helpers)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--runs', type=int, default=5)
    args = ap.parse_args()
    tickets = app.query('SELECT COUNT(*) AS n FROM Ticket')[0]['n']
    print(f"Database {app.db_config['database']}: {tickets:,} tickets, {args.runs} runs per report\n")
    print(f"{'#':>2}  {'Report':<52}{'rows':>6}{'mean ms':>10}{'max ms':>9}")
    total = 0.0
    for i, r in enumerate(app.REPORTS, 1):
        times = []
        for _ in range(args.runs):
            t = time.perf_counter()
            rows = app.query_proc(r['proc']) if r.get('proc') else app.query(r['sql'])
            times.append((time.perf_counter() - t) * 1000)
        total += statistics.mean(times)
        print(f"{i:>2}  {r['title'][:51]:<52}{len(rows):>6}{statistics.mean(times):>10.1f}{max(times):>9.1f}")
    print(f"\nAll {len(app.REPORTS)} reports together: about {total:.0f} ms")


if __name__ == '__main__':
    main()
