"""One-off, run-once-by-hand script: copies data out of the pre-Alembic
rootplan.db (3 tables, JSON-blob routes) into the new normalized schema
(rootplan.db, built by `alembic upgrade head`).

Not an Alembic revision on purpose - this is a single historical import for
this one legacy file, not a repeatable schema change. Safe to re-run: it
skips any legacy batch whose id was already imported.

Usage (from backend/):
    ..\\..\\.venv\\Scripts\\python.exe scripts\\migrate_legacy_data.py
"""

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import crud  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import FailedAddress, Order, PendingOrder, Route, RoutePlan, RouteStop, UploadBatch  # noqa: E402

LEGACY_DB_PATH = Path(__file__).resolve().parent.parent / "rootplan.db.legacy"


def _rows(cursor) -> list:
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def main() -> None:
    if not LEGACY_DB_PATH.exists():
        print(f"No legacy database at {LEGACY_DB_PATH} - nothing to migrate.")
        return

    legacy = sqlite3.connect(str(LEGACY_DB_PATH))
    legacy.row_factory = sqlite3.Row
    db = SessionLocal()

    try:
        already_imported = {b.id for b in db.query(UploadBatch.id).all()}

        batch_id_map: dict[int, int] = {}
        cur = legacy.execute("SELECT * FROM upload_batches ORDER BY id ASC")
        legacy_batches = _rows(cur)
        imported_batches = 0

        for row in legacy_batches:
            if row["id"] in already_imported:
                continue

            batch = UploadBatch(
                file_name=row["file_name"],
                uploaded_at=_parse_dt(row["uploaded_at"]),
                total_orders=row["total_orders"] or 0,
                is_valid=bool(row["is_valid"]),
                errors=json.loads(row["errors"]) if row["errors"] else [],
                generated_by="legacy-import",
            )
            db.add(batch)
            db.flush()
            batch_id_map[row["id"]] = batch.id
            imported_batches += 1

            order_cur = legacy.execute(
                "SELECT * FROM orders WHERE batch_id = ? ORDER BY id ASC", (row["id"],)
            )
            failed_count = 0
            for order_row in _rows(order_cur):
                has_coords = order_row["lat"] is not None and order_row["lng"] is not None
                db.add(Order(
                    batch_id=batch.id,
                    order_id=str(order_row["order_id"]),
                    customer_name=order_row["customer_name"],
                    address=order_row["address"],
                    formatted_address=order_row["geocoded_address"],
                    lat=order_row["lat"],
                    lng=order_row["lng"],
                    delivery_slot=order_row["delivery_time"],
                    geocode_error=order_row["geocode_error"],
                    status="pending" if has_coords else "failed",
                ))
                if not has_coords:
                    failed_count += 1
                    db.add(FailedAddress(
                        batch_id=batch.id,
                        order_id=str(order_row["order_id"]),
                        customer_name=order_row["customer_name"],
                        entered_address=order_row["address"],
                        failure_reason=order_row["geocode_error"],
                        status="resolved" if order_row["geocode_error"] is None else "pending",
                    ))
            batch.failed_orders_count = failed_count

        db.commit()

        plan_cur = legacy.execute("SELECT * FROM route_plans ORDER BY id ASC")
        imported_plans = 0
        for plan_row in _rows(plan_cur):
            legacy_batch_id = plan_row["batch_id"]
            new_batch_id = batch_id_map.get(legacy_batch_id) if legacy_batch_id is not None else None
            if legacy_batch_id is not None and new_batch_id is None:
                # This plan's batch wasn't (re-)imported this run - skip it
                # rather than orphan it under the wrong batch.
                continue

            route_plan = RoutePlan(
                batch_id=new_batch_id,
                created_at=_parse_dt(plan_row["created_at"]),
                available_cars=plan_row["available_cars"] or 0,
                available_bikes=plan_row["available_bikes"] or 0,
                route_count=plan_row["route_count"] or 0,
                warnings=json.loads(plan_row["warnings"]) if plan_row["warnings"] else [],
            )
            db.add(route_plan)
            db.flush()

            routes_data = json.loads(plan_row["routes"]) if plan_row["routes"] else []
            for route_data in routes_data:
                route = Route(
                    route_plan_id=route_plan.id,
                    route_name=route_data.get("route_name", ""),
                    vehicle_type=route_data.get("vehicle_type", ""),
                    total_distance_km=route_data.get("route_distance_km"),
                    total_duration_minutes=route_data.get("route_time_minutes"),
                    estimated_finish_time=route_data.get("estimated_finish_time"),
                    utilization_percent=route_data.get("utilization_percent"),
                    google_maps_url=route_data.get("google_maps_url"),
                    is_auto_created=bool(route_data.get("is_auto_created", False)),
                )
                db.add(route)
                db.flush()

                segments = route_data.get("route_segments") or []
                for index, order in enumerate(route_data.get("orders", [])):
                    segment = segments[index] if index < len(segments) else {}
                    db.add(RouteStop(
                        route_id=route.id,
                        order_id=str(order.get("order_id")),
                        sequence=index + 1,
                        travel_distance_km=segment.get("distance_km"),
                        travel_time_minutes=segment.get("time_minutes"),
                        eta=order.get("eta"),
                        status="late" if order.get("is_late") else "on_time",
                        order_snapshot=order,
                    ))

            pending_data = json.loads(plan_row["pending_orders"]) if plan_row["pending_orders"] else []
            for order in pending_data:
                db.add(PendingOrder(
                    route_plan_id=route_plan.id,
                    order_id=str(order.get("order_id")),
                    order_snapshot=order,
                ))

            imported_plans += 1

        db.commit()
        print(f"Imported {imported_batches} upload batch(es) and {imported_plans} route plan(s) from the legacy database.")
    finally:
        db.close()
        legacy.close()


if __name__ == "__main__":
    main()
