"""One-off, run-by-hand cleanup: deletes DriverLocationPing rows that fall
outside the route's own active run (before Start Route was tapped, after
End Route was tapped, or left over from an earlier run of the same
route_id before a reassignment reset it - see get_route_tracking's and
record_location's own comments in app/crud_driver.py for the full story).
Those gaps existed before this session's fix closed them going forward;
this is what actually removes what already got stored through them - a
real privacy concern (it can mean showing where a driver personally went
outside their tracked hours), not just clutter.

Uses the exact same window logic get_route_tracking now uses, so "what
this script would delete" and "what the admin panel already refuses to
show" are guaranteed to agree.

Safe to re-run - matching nothing to delete on a second run is the
expected, successful outcome once the database is clean.

Usage (from backend/):
    ..\\..\\.venv\\Scripts\\python.exe scripts\\cleanup_stray_location_pings.py
        Dry run (default) - reports what WOULD be deleted, changes nothing.
    ..\\..\\.venv\\Scripts\\python.exe scripts\\cleanup_stray_location_pings.py --execute
        Actually deletes the rows reported above.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

# This script imports app.database directly, never app.main - main.py is
# what normally calls load_dotenv() at import time, so without this
# DATABASE_URL would never be loaded here and would silently fall back to
# the local sqlite:///./rootplan.db instead of the real (Neon) database.
# Same gotcha, same fix, as scripts/migrate_sqlite_to_postgres.py.
load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

from app.database import DATABASE_URL, SessionLocal  # noqa: E402
from app.models import DriverLocationPing, Route  # noqa: E402


def _as_aware_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def find_stray_ping_ids(db) -> list:
    """Same window logic as crud_driver.get_route_tracking, inverted -
    every ping id that does NOT fall within its own route's
    started_at..completed_at window (or whose route was never started at
    all)."""
    stray_ids = []
    routes = db.query(Route).all()
    for route in routes:
        pings = db.query(DriverLocationPing).filter(DriverLocationPing.route_id == route.id).all()
        if not pings:
            continue
        if route.started_at is None:
            stray_ids.extend(p.id for p in pings)
            continue
        run_start = _as_aware_utc(route.started_at)
        run_end = _as_aware_utc(route.completed_at) if route.completed_at is not None else None
        for p in pings:
            recorded = _as_aware_utc(p.recorded_at)
            if recorded < run_start or (run_end is not None and recorded > run_end):
                stray_ids.append(p.id)
    return stray_ids


def main() -> None:
    execute = "--execute" in sys.argv
    dialect = "sqlite" if DATABASE_URL.startswith("sqlite") else "postgresql"
    print(f"Connected to: {dialect}" + (f" ({DATABASE_URL.split('@')[-1]})" if dialect == "postgresql" else " (local file - not production)"))
    db = SessionLocal()
    try:
        stray_ids = find_stray_ping_ids(db)
        total_pings = db.query(DriverLocationPing).count()
        print(f"Total location pings in the database: {total_pings}")
        print(f"Stray pings (outside their route's active run): {len(stray_ids)}")

        if not stray_ids:
            print("Nothing to clean up.")
            return

        if not execute:
            print("\nDry run - nothing deleted. Re-run with --execute to actually delete these rows.")
            return

        deleted = (
            db.query(DriverLocationPing)
            .filter(DriverLocationPing.id.in_(stray_ids))
            .delete(synchronize_session=False)
        )
        db.commit()
        print(f"\nDeleted {deleted} stray location ping(s).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
