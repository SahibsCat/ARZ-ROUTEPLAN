"""One-off, run-by-hand fix: re-checks every Google-geocoded address this
install has ever cached against the corrected precision check now in
app/geocoding/google_geocoder.py (see that file's _score_result docstring
for the bug this closes: before this fix, ANY result Google returned - even
a city/postal-code-level guess with location_type=APPROXIMATE and no real
street/building match - was accepted as a fully-precise pin, identical to a
real rooftop match. Google still returns a clean, correctly-formatted
`formatted_address` for whatever broader area it matched, which is exactly
why the address TEXT looked right while the MAP PIN was nowhere near the
real place).

Every `geocoding_cache` row the Google provider ever wrote before this fix
has confidence = NULL (Google never set one at all) - that NULL is the
marker this script uses to find rows that were never actually
confidence-checked, as opposed to rows already produced by the fixed code.

For each one, re-geocodes the same address fresh (bypassing the cache) and:
  - Fresh result is still confident (OK) and lands close (<150m) to the old
    point: just stamps the row with its real confidence number - nothing
    was actually wrong, only unverified. No other data touched.
  - Fresh result is OK but lands meaningfully far from the old point: the
    old point really was wrong. Corrects the cache row, every `orders` row
    that used this exact address, and - for an order currently on a route -
    that route's one RouteStop snapshot (or a PendingOrder snapshot, if it's
    unrouted-but-planned), so an already-generated route's map pin is fixed
    too, not just future uploads.
  - Fresh result is low-confidence again: the address genuinely can't be
    pinned precisely. The unusable cache row is removed (so nothing reuses
    a known-bad point again) and every affected order is listed so a human
    can verify it - manually check the map, or ask the customer - exactly
    what the app already does for a fresh low-confidence address, applied
    to old data. Existing lat/lng on any such order is
    deliberately left untouched: a driver may be mid-delivery to that pin
    right now, and replacing an uncertain point with an equally uncertain
    new one is not obviously safer than leaving the last known point in
    place while flagging it.

Deliberately does NOT touch route metrics (distance/time/ETA/sequence) or
delivery_status/delivered_at - those belong to route generation
(recompute_route_metrics/_persist_route_stops in app/crud.py), and
re-running that machinery on an ACTIVE route (one a driver may be
mid-delivery on right now) risks wiping delivery_status/delivered_at, since
_persist_route_stops always rebuilds every stop from scratch. This script
only ever patches lat/lng/geocoded_address on an existing row/snapshot in
place - the actual fix for "the pin is in the wrong place", nothing else
about the route changes.

Spends live Google Geocoding API quota - one request per unverified cached
address - which is exactly why this is a run-by-hand script, not something
that runs automatically.

Safe to re-run - once every cached Google address has a real confidence
number, there's nothing left with confidence IS NULL to re-check.

Usage (from backend/):
    venv\\Scripts\\python.exe scripts\\revalidate_google_geocodes.py
        Dry run (default) - reports what WOULD change, changes nothing.
    venv\\Scripts\\python.exe scripts\\revalidate_google_geocodes.py --execute
        Actually applies the corrections/flags described above.
"""

import math
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

# Windows' console defaults to the cp1252 codepage, which can't encode the
# unicode markers (checkmark/warning/arrow) this script prints - reconfigure
# stdout to UTF-8 so the report doesn't crash partway through on Windows.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

# This script imports app.database directly, never app.main - main.py is
# what normally calls load_dotenv() at import time, so without this
# DATABASE_URL/GOOGLE_MAPS_API_KEY would never be loaded here (same gotcha
# as scripts/migrate_sqlite_to_postgres.py and
# scripts/cleanup_stray_location_pings.py).
load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

from app.database import DATABASE_URL, SessionLocal  # noqa: E402
from app.geocode_service import _cache_key, clean_address  # noqa: E402
from app.geocoding.base import STATUS_OK, GeocodeResult  # noqa: E402
from app.geocoding.google_geocoder import GoogleGeocoder  # noqa: E402
from app.models import GeocodingCache, Order, PendingOrder, RouteStop  # noqa: E402

# A fresh result landing within this many meters of the old (unverified)
# cached point counts as "same place, just now confidence-checked" - not a
# real correction. Beyond it, the old point is treated as genuinely wrong.
# 150m is roughly "wrong side of the block" for dense urban delivery areas -
# comfortably past ordinary rooftop-vs-street GPS noise.
MOVED_THRESHOLD_METERS = 150.0

_EARTH_RADIUS_METERS = 6371000.0


def _haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_METERS * math.asin(math.sqrt(a))


def _patch_snapshot(obj, lat: float, lng: float, formatted_address: str) -> None:
    """Reassigns the whole order_snapshot dict rather than mutating it in
    place - SQLAlchemy only detects a JSON column as changed on
    reassignment, not on mutating the dict object it already handed back."""
    snapshot = dict(obj.order_snapshot or {})
    snapshot["lat"] = lat
    snapshot["lng"] = lng
    if formatted_address:
        snapshot["geocoded_address"] = formatted_address
    obj.order_snapshot = snapshot


def _orders_by_address_key(db) -> Dict[str, List[Order]]:
    """One pass over every geocoded order, grouped by the same normalized
    cache key geocode_service.py itself uses - so "which orders used this
    cached address" matches the app's own cache semantics exactly, instead
    of an approximate lat/lng match that could accidentally group unrelated
    addresses that happened to resolve to the same point."""
    by_key: Dict[str, List[Order]] = {}
    for order in db.query(Order).filter(Order.lat.isnot(None), Order.lng.isnot(None)).all():
        key = _cache_key(clean_address(order.address or ""))
        if not key:
            continue
        by_key.setdefault(key, []).append(order)
    return by_key


def _apply_correction(db, row: GeocodingCache, fresh: GeocodeResult, orders: List[Order]) -> None:
    row.lat, row.lng = fresh.lat, fresh.lng
    row.formatted_address = fresh.formatted_address
    row.confidence = fresh.confidence

    for order in orders:
        order.lat, order.lng = fresh.lat, fresh.lng
        order.formatted_address = fresh.formatted_address
        if order.route_id is not None:
            stop = (
                db.query(RouteStop)
                .filter(RouteStop.route_id == order.route_id, RouteStop.order_id == order.order_id)
                .first()
            )
            if stop is not None:
                _patch_snapshot(stop, fresh.lat, fresh.lng, fresh.formatted_address)
        for pending in db.query(PendingOrder).filter(PendingOrder.order_id == order.order_id).all():
            _patch_snapshot(pending, fresh.lat, fresh.lng, fresh.formatted_address)


def main() -> None:
    execute = "--execute" in sys.argv
    dialect = "sqlite" if DATABASE_URL.startswith("sqlite") else "postgresql"
    print(
        f"Connected to: {dialect}"
        + (f" ({DATABASE_URL.split('@')[-1]})" if dialect == "postgresql" else " (local file - not production)")
    )

    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not api_key:
        print("GOOGLE_MAPS_API_KEY is not set - nothing to re-check.")
        return

    db = SessionLocal()
    try:
        stale_rows = (
            db.query(GeocodingCache)
            .filter(GeocodingCache.provider == "google")
            .filter(GeocodingCache.confidence.is_(None))
            .all()
        )
        print(f"Unverified Google cache entries (predate the precision fix): {len(stale_rows)}")
        if not stale_rows:
            print("Nothing to re-check.")
            return

        orders_by_key = _orders_by_address_key(db)

        confirmed = moved = flagged = unresolved = 0

        with GoogleGeocoder(api_key=api_key) as geocoder:
            for row in stale_rows:
                address_key = row.address_key
                orders = orders_by_key.get(address_key, [])
                fresh = geocoder.geocode(row.address or "")

                if fresh is None:
                    unresolved += 1
                    print(f"  ? '{row.address}' - no longer resolves at all; leaving as-is for manual review")
                    continue

                if fresh.status != STATUS_OK:
                    flagged += 1
                    order_ids = ", ".join(o.order_id for o in orders) or "no current orders"
                    print(
                        f"  ⚠ FLAG '{row.address}' -> confidence={fresh.confidence:.2f}, "
                        f"below the accuracy threshold. Affected order(s): {order_ids}"
                    )
                    if execute:
                        db.query(GeocodingCache).filter(GeocodingCache.id == row.id).delete()
                    continue

                distance = _haversine_meters(row.lat, row.lng, fresh.lat, fresh.lng)
                if distance <= MOVED_THRESHOLD_METERS:
                    confirmed += 1
                    print(f"  ✓ '{row.address}' - confirmed in place (confidence={fresh.confidence:.2f})")
                    if execute:
                        row.confidence = fresh.confidence
                else:
                    moved += 1
                    order_ids = ", ".join(o.order_id for o in orders) or "no current orders"
                    print(
                        f"  → CORRECT '{row.address}' - {distance:.0f}m from the old (unverified) point, "
                        f"confidence={fresh.confidence:.2f}. Affected order(s): {order_ids}"
                    )
                    if execute:
                        _apply_correction(db, row, fresh, orders)

        print("\n======================================")
        print(f"Confirmed (unchanged) : {confirmed}")
        print(f"Corrected (moved)     : {moved}")
        print(f"Flagged (manual check): {flagged}")
        print(f"Unresolved            : {unresolved}")
        print("======================================\n")

        if not execute:
            print("Dry run - nothing written. Re-run with --execute to apply the corrections/flags above.")
            return

        db.commit()
        print("Done.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
