"""Fill the Wise cost-of-living cache before a demo or deployment.

The script warms the same versioned cache the deployed app reads. Point
``STORAGE_BACKEND`` at DynamoDB when the cache should be shared with Lambda.
This is optional because Wise pages are public and the finance lane can fetch
them on demand; warming ahead of time keeps a demo fast and resilient.

    # from the repository root, with AWS credentials in the shell
    $env:STORAGE_BACKEND="dynamodb"
    $env:AWS_ACCESS_KEY_ID="..."; $env:AWS_SECRET_ACCESS_KEY="..."; $env:AWS_SESSION_TOKEN="..."
    backend/.venv/Scripts/python.exe tools/prewarm_costs.py CSC --limit 12

Entries carry the cache TTL, so re-run it before a demo when desired.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("school", help="NTU programme code, e.g. CSC")
    parser.add_argument("--programme-type", default="GEMX", help="GEMX or SUSEP")
    parser.add_argument("--country", default=None, help="Optional country filter")
    parser.add_argument("--limit", type=int, default=12, help="How many universities to warm")
    parser.add_argument(
        "--names", nargs="*", default=None,
        help="Warm these university names instead of a shortlist",
    )
    args = parser.parse_args()

    from api.chat import _briefing_cost
    from data import coursefinder_db as cf
    from services import store

    backend = store.backend_name()
    print(f"cache backend : {backend}")
    if backend == "sqlite":
        print("  NOTE  writing to the local SQLite cache. To fill the deployed")
        print("        cache, set STORAGE_BACKEND=dynamodb and supply AWS credentials.")

    if args.names:
        targets = [(name, args.country) for name in args.names]
    else:
        cards, total, _ = cf.eligible_universities(
            school_code=args.school.strip().upper(),
            programme_type=args.programme_type.strip().upper(),
            country=args.country,
            offset=0,
            limit=args.limit,
        )
        targets = [(c.name, c.country) for c in cards]
        print(f"shortlist     : {len(targets)} of {total} for {args.school.upper()}")

    print()
    warmed = failed = 0
    for index, (name, country) in enumerate(targets, start=1):
        started = time.perf_counter()
        try:
            cost = _briefing_cost(name, country)
        except Exception as exc:  # noqa: BLE001 - one bad university must not stop the run
            print(f"{index:>3}. {name[:44]:<46} ERROR {exc.__class__.__name__}")
            failed += 1
            continue
        elapsed = time.perf_counter() - started
        if cost.single_person_monthly:
            print(f"{index:>3}. {name[:44]:<46} {cost.single_person_monthly:>10}  "
                  f"{(cost.city or '')[:18]:<18} {elapsed:4.1f}s")
            warmed += 1
        else:
            reason = (cost.error or "no estimate")[:52]
            print(f"{index:>3}. {name[:44]:<46} {'—':>10}  {reason}")
            failed += 1

    print()
    print(f"warmed {warmed}, unavailable {failed}")
    if warmed and backend == "dynamodb":
        print("\nThe deployed app will now serve these from cache. Entries expire")
        print("after 7 days - re-run before a demo if it has been longer.")
    return 0 if warmed else 1


if __name__ == "__main__":
    raise SystemExit(main())
