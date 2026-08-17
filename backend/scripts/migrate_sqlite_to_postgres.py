"""One-off, run-once-by-hand script: copies the data in the local
./rootplan.db SQLite file (the current normalized schema - the same one
`alembic upgrade head` builds) into the Postgres database pointed at by
DATABASE_URL, e.g. Neon.

This is for migrating an existing local install's real data (orders, route
history, failed addresses, geocoding cache, ...) the first time you switch
that install over to Postgres. A brand-new install with an empty
./rootplan.db has nothing to migrate and doesn't need this.

Not an Alembic revision on purpose - this is a single historical data import
for one local SQLite file, not a repeatable schema change (compare
scripts/migrate_legacy_data.py, the same pattern for the pre-Alembic
database). Safe to re-run: every table is imported idempotently, skipping
any row whose id already exists on the Postgres side.

Usage (from backend/, with DATABASE_URL set to your Neon connection string):
    1. alembic upgrade head          # creates the schema on Postgres
    2. venv\\Scripts\\python.exe scripts\\migrate_sqlite_to_postgres.py
"""

import json
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# This script is invoked directly (not through app/main.py), so .env would
# otherwise never be loaded and DATABASE_URL would silently fall back to
# SQLite - same load_dotenv() call app/main.py and alembic/env.py make.
load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

from app.database import DATABASE_URL, SessionLocal, engine  # noqa: E402
from app.models import (  # noqa: E402
    AppSettings,
    FailedAddress,
    GeocodingCache,
    Order,
    PendingOrder,
    Route,
    RoutePlan,
    RouteStop,
    UploadBatch,
)

SQLITE_DB_PATH = Path(__file__).resolve().parent.parent / "rootplan.db"


def _rows(cursor) -> list:
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _json(value, default):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    return json.loads(value)


def _existing_ids(db, model) -> set:
    return {row[0] for row in db.query(model.id).all()}


def _copy_table(db, source_cur, table: str, model, row_to_kwargs) -> int:
    existing = _existing_ids(db, model)
    imported = 0
    for row in _rows(source_cur.execute(f'SELECT * FROM "{table}" ORDER BY id ASC')):
        if row["id"] in existing:
            continue
        db.add(model(**row_to_kwargs(row)))
        imported += 1
    db.commit()
    return imported


def _reset_sequences(db) -> None:
    """Explicit-PK inserts (above) don't advance Postgres's auto-increment
    sequences, so the next plain INSERT (a normal app write) would collide
    with an id we just imported. Bump every sequence to max(id) once,
    after all data is in."""
    tables = [
        "upload_batches", "orders", "route_plans", "routes",
        "route_stops", "pending_orders", "failed_addresses",
        "geocoding_cache", "app_settings",
    ]
    for table in tables:
        db.execute(
            text(
                "SELECT setval("
                "  pg_get_serial_sequence(:table, 'id'),"
                f'  COALESCE((SELECT MAX(id) FROM "{table}"), 1),'
                f'  (SELECT MAX(id) FROM "{table}") IS NOT NULL'
                ")"
            ),
            {"table": table},
        )
    db.commit()


def main() -> None:
    if not SQLITE_DB_PATH.exists():
        print(f"No local database at {SQLITE_DB_PATH} - nothing to migrate.")
        return

    if DATABASE_URL.startswith("sqlite"):
        print(
            "DATABASE_URL is still pointing at SQLite. Set it to your Neon/"
            "Postgres connection string before running this script (see "
            ".env.example), then run `alembic upgrade head` to create the "
            "schema there first."
        )
        return

    if not engine.dialect.name.startswith("postgresql"):
        print(f"DATABASE_URL resolves to '{engine.dialect.name}', not postgresql - aborting.")
        return

    source = sqlite3.connect(str(SQLITE_DB_PATH))
    source.row_factory = sqlite3.Row
    db = SessionLocal()

    try:
        counts = {}

        counts["upload_batches"] = _copy_table(
            db, source, "upload_batches", UploadBatch, lambda r: dict(
                id=r["id"], file_name=r["file_name"], uploaded_at=r["uploaded_at"],
                total_orders=r["total_orders"] or 0, generated_routes=r["generated_routes"] or 0,
                pending_orders_count=r["pending_orders_count"] or 0,
                failed_orders_count=r["failed_orders_count"] or 0,
                generated_by=r["generated_by"], is_valid=bool(r["is_valid"]),
                errors=_json(r["errors"], []), column_order=_json(r["column_order"], []),
            ),
        )

        counts["orders"] = _copy_table(
            db, source, "orders", Order, lambda r: dict(
                id=r["id"], batch_id=r["batch_id"], order_id=r["order_id"],
                customer_name=r["customer_name"], address=r["address"],
                formatted_address=r["formatted_address"], lat=r["lat"], lng=r["lng"],
                delivery_slot=r["delivery_slot"], status=r["status"],
                assigned_vehicle=r["assigned_vehicle"], geocode_error=r["geocode_error"],
                extra_fields=_json(r["extra_fields"], {}),
                created_at=r["created_at"], updated_at=r["updated_at"],
            ),
        )

        counts["route_plans"] = _copy_table(
            db, source, "route_plans", RoutePlan, lambda r: dict(
                id=r["id"], batch_id=r["batch_id"], created_at=r["created_at"],
                available_cars=r["available_cars"] or 0, available_bikes=r["available_bikes"] or 0,
                route_count=r["route_count"] or 0, warnings=_json(r["warnings"], []),
                is_saved=bool(r["is_saved"]), label=r["label"], saved_at=r["saved_at"],
            ),
        )

        counts["routes"] = _copy_table(
            db, source, "routes", Route, lambda r: dict(
                id=r["id"], route_plan_id=r["route_plan_id"], route_name=r["route_name"],
                vehicle_type=r["vehicle_type"], driver=r["driver"],
                total_distance_km=r["total_distance_km"], total_duration_minutes=r["total_duration_minutes"],
                estimated_finish_time=r["estimated_finish_time"], utilization_percent=r["utilization_percent"],
                google_maps_url=r["google_maps_url"], status=r["status"],
                is_auto_created=bool(r["is_auto_created"]), created_at=r["created_at"],
            ),
        )

        counts["route_stops"] = _copy_table(
            db, source, "route_stops", RouteStop, lambda r: dict(
                id=r["id"], route_id=r["route_id"], order_id=r["order_id"], sequence=r["sequence"],
                travel_distance_km=r["travel_distance_km"], travel_time_minutes=r["travel_time_minutes"],
                eta=r["eta"], status=r["status"], order_snapshot=_json(r["order_snapshot"], {}),
            ),
        )

        counts["pending_orders"] = _copy_table(
            db, source, "pending_orders", PendingOrder, lambda r: dict(
                id=r["id"], route_plan_id=r["route_plan_id"], order_id=r["order_id"],
                order_snapshot=_json(r["order_snapshot"], {}),
            ),
        )

        counts["failed_addresses"] = _copy_table(
            db, source, "failed_addresses", FailedAddress, lambda r: dict(
                id=r["id"], batch_id=r["batch_id"], order_id=r["order_id"],
                customer_name=r["customer_name"], entered_address=r["entered_address"],
                edited_address=r["edited_address"], failure_reason=r["failure_reason"],
                retry_count=r["retry_count"] or 0, status=r["status"],
                created_at=r["created_at"], updated_at=r["updated_at"],
            ),
        )

        counts["geocoding_cache"] = _copy_table(
            db, source, "geocoding_cache", GeocodingCache, lambda r: dict(
                id=r["id"], address_key=r["address_key"], address=r["address"],
                formatted_address=r["formatted_address"], lat=r["lat"], lng=r["lng"],
                provider=r["provider"], confidence=r["confidence"], created_at=r["created_at"],
            ),
        )

        counts["app_settings"] = _copy_table(
            db, source, "app_settings", AppSettings, lambda r: dict(
                id=r["id"], default_car_count=r["default_car_count"] or 1,
                default_bike_count=r["default_bike_count"] or 2, theme=r["theme"] or "system",
                preferences=_json(r["preferences"], {}),
                current_session_batch_id=r["current_session_batch_id"],
                current_session_plan_id=r["current_session_plan_id"],
                updated_at=r["updated_at"],
            ),
        )

        _reset_sequences(db)

        print("Imported into Postgres:")
        for table, count in counts.items():
            print(f"  {table}: {count}")
        print("Done. Existing rows (by id) were left untouched, so this is safe to re-run.")
    finally:
        db.close()
        source.close()


if __name__ == "__main__":
    main()
