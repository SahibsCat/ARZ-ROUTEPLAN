"""Repository layer - every database read/write in the app goes through a
function here. API routes call these; they never build SQLAlchemy queries
themselves."""

from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy.orm import Session, joinedload

from app.models import (
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

SETTINGS_ROW_ID = 1


# --------------------------------------------------------------------------
# Upload batches / orders
# --------------------------------------------------------------------------

def _get_or_create_settings_row(db: Session) -> AppSettings:
    settings = db.query(AppSettings).filter(AppSettings.id == SETTINGS_ROW_ID).first()
    if settings is None:
        settings = AppSettings(id=SETTINGS_ROW_ID)
        db.add(settings)
        db.flush()
    return settings


def set_current_session(db: Session, batch_id: Optional[int], plan_id: Optional[int]) -> None:
    """Marks what /api/dashboard restores on the next page load. Always sets
    both fields explicitly (pass None for whichever doesn't apply) - this is
    the single source of truth for "what session is the user in", replacing
    the old approach of inferring it from whatever upload happened to be
    newest in the whole table (which is what let a deleted session's data
    resurface as an older, unrelated one after a refresh)."""
    settings = _get_or_create_settings_row(db)
    settings.current_session_batch_id = batch_id
    settings.current_session_plan_id = plan_id


def clear_current_session_if_matches(
    db: Session, batch_id: Optional[int] = None, plan_id: Optional[int] = None,
) -> None:
    """Called after a delete: un-points the session from whatever was just
    removed, rather than leaving it pointing at a dangling id (which would
    make /api/dashboard 404-hunt) or silently drifting to some other record.
    If the batch matches, the plan pointer is cleared too regardless of its
    exact id - deleting a batch takes every route plan under it with it, so
    the whole session context is gone, not just half of it."""
    settings = db.query(AppSettings).filter(AppSettings.id == SETTINGS_ROW_ID).first()
    if settings is None:
        return
    if batch_id is not None and settings.current_session_batch_id == batch_id:
        settings.current_session_batch_id = None
        settings.current_session_plan_id = None
    elif plan_id is not None and settings.current_session_plan_id == plan_id:
        settings.current_session_plan_id = None


def save_upload_batch(
    db: Session,
    file_name: Optional[str],
    total_orders: int,
    is_valid: bool,
    errors: List[str],
    geocoded_orders: List[Dict[str, object]],
    generated_by: Optional[str] = None,
    column_order: Optional[List[Dict[str, Optional[str]]]] = None,
) -> UploadBatch:
    batch = UploadBatch(
        file_name=file_name,
        total_orders=total_orders,
        is_valid=is_valid,
        errors=errors or [],
        generated_by=generated_by,
        column_order=column_order or [],
    )
    db.add(batch)
    db.flush()

    failed_count = 0
    for order in geocoded_orders:
        has_coords = order.get("lat") is not None and order.get("lng") is not None
        db.add(Order(
            batch_id=batch.id,
            order_id=str(order.get("order_id", "")),
            customer_name=order.get("customer_name"),
            address=order.get("address"),
            delivery_slot=str(order["delivery_time"]) if order.get("delivery_time") is not None else None,
            lat=order.get("lat"),
            lng=order.get("lng"),
            formatted_address=order.get("geocoded_address"),
            geocode_error=order.get("geocode_error"),
            status="pending" if has_coords else "failed",
            extra_fields=order.get("extra_fields") or {},
        ))
        if not has_coords:
            failed_count += 1
            db.add(FailedAddress(
                batch_id=batch.id,
                order_id=str(order.get("order_id", "")),
                customer_name=order.get("customer_name"),
                entered_address=order.get("address"),
                failure_reason=order.get("geocode_error"),
            ))

    batch.failed_orders_count = failed_count
    # A fresh upload becomes the current session - no route plan yet.
    set_current_session(db, batch_id=batch.id, plan_id=None)
    db.commit()
    db.refresh(batch)
    return batch


def list_upload_batches(db: Session, limit: int = 20, offset: int = 0) -> List[UploadBatch]:
    # Order by id, not uploaded_at: SQLite's CURRENT_TIMESTAMP only has
    # second resolution, so batches created within the same second (common
    # in tests, and possible in real bursts) would otherwise tie.
    return (
        db.query(UploadBatch)
        .order_by(UploadBatch.id.desc())
        .offset(max(offset, 0))
        .limit(max(min(limit, 200), 1))
        .all()
    )


def count_upload_batches(db: Session) -> int:
    return db.query(UploadBatch).count()


def get_upload_batch(db: Session, batch_id: int) -> Optional[UploadBatch]:
    return db.query(UploadBatch).filter(UploadBatch.id == batch_id).first()


def delete_upload_batch(db: Session, batch_id: int) -> bool:
    batch = get_upload_batch(db, batch_id)
    if batch is None:
        return False
    db.delete(batch)  # cascades to orders, route plans (+ routes/stops), failed addresses
    db.flush()
    # None for plan_id: any route plan(s) this batch had are gone with it,
    # whichever id(s) they were - clearing by batch_id alone is enough.
    clear_current_session_if_matches(db, batch_id=batch_id, plan_id=None)
    db.commit()
    return True


def sync_orders_with_route_plan(db: Session, batch_id: Optional[int], plan: Dict[str, object]) -> None:
    """After a route plan is generated for a real batch, update each
    persisted Order's status/assigned_vehicle so the dashboard can be
    rebuilt from `orders` alone without re-reading the plan's JSON."""
    if batch_id is None:
        return

    assigned_vehicle_by_order: Dict[str, str] = {}
    for route in plan.get("routes", []):
        for order in route.get("orders", []):
            assigned_vehicle_by_order[str(order.get("order_id"))] = route.get("route_name", "")

    pending_ids = {str(o.get("order_id")) for o in plan.get("pending_orders", [])}

    orders = db.query(Order).filter(Order.batch_id == batch_id).all()
    for order in orders:
        if order.order_id in assigned_vehicle_by_order:
            order.status = "assigned"
            order.assigned_vehicle = assigned_vehicle_by_order[order.order_id]
        elif order.order_id in pending_ids:
            order.status = "pending"
            order.assigned_vehicle = None
    db.commit()


# --------------------------------------------------------------------------
# Route plans / routes / stops / pending orders
# --------------------------------------------------------------------------

def save_route_plan(
    db: Session,
    batch_id: Optional[int],
    plan: Dict[str, object],
    available_cars: int,
    available_bikes: int,
) -> RoutePlan:
    """Called by Generate/Regenerate/Retry - always writes a plan (so a
    refresh never loses in-progress work) but always as an unsaved draft.
    Only one draft is kept per batch: the previous unsaved draft for this
    batch (if any) is replaced rather than left to pile up, so Route History
    - which only ever lists is_saved=True rows - stays exactly what the user
    chose to keep, no matter how many times they hit Regenerate."""
    if batch_id is not None:
        stale_drafts = (
            db.query(RoutePlan)
            .filter(RoutePlan.batch_id == batch_id, RoutePlan.is_saved.is_(False))
            .all()
        )
        for stale in stale_drafts:
            db.delete(stale)
        db.flush()

    route_plan = RoutePlan(
        batch_id=batch_id,
        available_cars=available_cars,
        available_bikes=available_bikes,
        route_count=plan.get("route_count", 0),
        warnings=plan.get("warnings", []),
        is_saved=False,
    )
    db.add(route_plan)
    db.flush()

    for route_data in plan.get("routes", []):
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

    for order in plan.get("pending_orders", []):
        db.add(PendingOrder(
            route_plan_id=route_plan.id,
            order_id=str(order.get("order_id")),
            order_snapshot=order,
        ))

    if batch_id is not None:
        batch = get_upload_batch(db, batch_id)
        if batch is not None:
            batch.generated_routes = plan.get("route_count", 0)
            batch.pending_orders_count = len(plan.get("pending_orders", []))

    # This plan becomes the current session, whether or not it's tied to an
    # upload (a no-upload/manual route generation is still "a session").
    set_current_session(db, batch_id=batch_id, plan_id=route_plan.id)
    db.commit()
    db.refresh(route_plan)
    sync_orders_with_route_plan(db, batch_id, plan)
    return route_plan


def get_latest_route_plan(db: Session, batch_id: Optional[int] = None) -> Optional[RoutePlan]:
    query = db.query(RoutePlan).options(
        joinedload(RoutePlan.routes).joinedload(Route.stops),
        joinedload(RoutePlan.pending_stops),
    )
    if batch_id is not None:
        query = query.filter(RoutePlan.batch_id == batch_id)
    return query.order_by(RoutePlan.id.desc()).first()


def get_route_plan(db: Session, route_plan_id: int) -> Optional[RoutePlan]:
    return (
        db.query(RoutePlan)
        .options(joinedload(RoutePlan.routes).joinedload(Route.stops), joinedload(RoutePlan.pending_stops))
        .filter(RoutePlan.id == route_plan_id)
        .first()
    )


def delete_route_plan(db: Session, route_plan_id: int) -> bool:
    """Deletes the route plan (cascades to its routes -> stops, and pending
    stops). If that was the only route plan tied to its upload, the upload
    itself - file name, orders, failed addresses - is deleted too, rather
    than left behind as an orphaned record with no route pointing at it.
    An upload that still has another route plan referencing it (e.g. a
    separately saved plan from the same upload) is left alone.

    If the deleted plan (or its now-deleted upload) was the current
    session, the session pointer is cleared too - so /api/dashboard shows
    the normal empty state on the next refresh instead of falling through
    to whatever other upload happens to be sitting in history."""
    route_plan = db.query(RoutePlan).filter(RoutePlan.id == route_plan_id).first()
    if route_plan is None:
        return False

    batch_id = route_plan.batch_id
    db.delete(route_plan)
    db.flush()

    deleted_batch_id: Optional[int] = None
    if batch_id is not None:
        remaining = db.query(RoutePlan).filter(RoutePlan.batch_id == batch_id).count()
        if remaining == 0:
            batch = db.query(UploadBatch).filter(UploadBatch.id == batch_id).first()
            if batch is not None:
                db.delete(batch)  # cascades to orders and failed_addresses
                deleted_batch_id = batch_id

    clear_current_session_if_matches(db, batch_id=deleted_batch_id, plan_id=route_plan_id)
    db.commit()
    return True


def _default_route_plan_label(route_plan: RoutePlan) -> str:
    base = route_plan.batch.file_name if route_plan.batch else None
    when = route_plan.created_at.strftime("%d %b %Y, %I:%M %p") if route_plan.created_at else ""
    return f"{base or 'Manual route plan'} — {when}".strip(" —")


def promote_route_plan_to_saved(
    db: Session, route_plan_id: int, label: Optional[str] = None,
) -> Optional[RoutePlan]:
    """The Save-to-history action: flips a plan from draft to saved. Doesn't
    touch its routes/stops - it's the same data, just no longer subject to
    being replaced by the next Regenerate (see save_route_plan above)."""
    route_plan = get_route_plan(db, route_plan_id)
    if route_plan is None:
        return None
    route_plan.is_saved = True
    route_plan.saved_at = datetime.now(timezone.utc)
    route_plan.label = label.strip() if label and label.strip() else (
        route_plan.label or _default_route_plan_label(route_plan)
    )
    db.commit()
    db.refresh(route_plan)
    return route_plan


def list_saved_route_plans(db: Session, limit: int = 20, offset: int = 0) -> List[RoutePlan]:
    return (
        db.query(RoutePlan)
        .options(
            joinedload(RoutePlan.batch),
            joinedload(RoutePlan.routes).joinedload(Route.stops),
            joinedload(RoutePlan.pending_stops),
        )
        .filter(RoutePlan.is_saved.is_(True))
        .order_by(RoutePlan.saved_at.desc())
        .offset(max(offset, 0))
        .limit(max(min(limit, 200), 1))
        .all()
    )


def count_saved_route_plans(db: Session) -> int:
    return db.query(RoutePlan).filter(RoutePlan.is_saved.is_(True)).count()


def route_plan_list_item(route_plan: RoutePlan) -> Dict[str, object]:
    """Lightweight shape for the Route History list screen - enough to tell
    plans apart and decide what to open, without the full nested
    routes/stops payload route_plan_summary() returns."""
    routes = route_plan.routes
    total_stops = sum(len(route.stops) for route in routes)
    total_distance_km = round(sum((route.total_distance_km or 0) for route in routes), 2) if routes else 0.0
    return {
        "plan_id": route_plan.id,
        "label": route_plan.label,
        "saved_at": route_plan.saved_at.isoformat() if route_plan.saved_at else None,
        "created_at": route_plan.created_at.isoformat() if route_plan.created_at else None,
        "batch_id": route_plan.batch_id,
        "file_name": route_plan.batch.file_name if route_plan.batch else None,
        "available_cars": route_plan.available_cars,
        "available_bikes": route_plan.available_bikes,
        "route_count": route_plan.route_count,
        "total_stops": total_stops,
        "pending_count": len(route_plan.pending_stops),
        "total_distance_km": total_distance_km,
    }


# --------------------------------------------------------------------------
# Failed addresses
# --------------------------------------------------------------------------

def list_failed_addresses(
    db: Session, batch_id: Optional[int] = None, status: Optional[str] = "pending",
) -> List[FailedAddress]:
    query = db.query(FailedAddress)
    if batch_id is not None:
        query = query.filter(FailedAddress.batch_id == batch_id)
    if status is not None:
        query = query.filter(FailedAddress.status == status)
    return query.order_by(FailedAddress.id.desc()).all()


def get_failed_address(db: Session, batch_id: Optional[int], order_id: str) -> Optional[FailedAddress]:
    query = db.query(FailedAddress).filter(FailedAddress.order_id == str(order_id))
    if batch_id is not None:
        query = query.filter(FailedAddress.batch_id == batch_id)
    return query.order_by(FailedAddress.id.desc()).first()


def record_retry_attempt(
    db: Session,
    batch_id: Optional[int],
    order_id: str,
    edited_address: Optional[str],
    success: bool,
    failure_reason: Optional[str] = None,
) -> None:
    """Called after every retry (success or failure) so retry_count and
    status stay accurate. On success the row is marked resolved rather than
    deleted, and the matching Order row is updated - both survive a refresh."""
    failed = get_failed_address(db, batch_id, order_id)
    if failed is not None:
        failed.retry_count = (failed.retry_count or 0) + 1
        if edited_address:
            failed.edited_address = edited_address
        if success:
            failed.status = "resolved"
        else:
            failed.failure_reason = failure_reason

    if batch_id is not None:
        order = (
            db.query(Order)
            .filter(Order.batch_id == batch_id, Order.order_id == str(order_id))
            .first()
        )
        if order is not None:
            if edited_address:
                order.address = edited_address
            if success:
                order.status = "pending"
                order.geocode_error = None
            else:
                order.status = "failed"
                order.geocode_error = failure_reason

    db.commit()


# --------------------------------------------------------------------------
# Geocoding cache
# --------------------------------------------------------------------------

def get_cached_geocode(db: Session, address_key: str) -> Optional[GeocodingCache]:
    if not address_key:
        return None
    return db.query(GeocodingCache).filter(GeocodingCache.address_key == address_key).first()


def save_geocode_cache(
    db: Session,
    address_key: str,
    address: str,
    formatted_address: Optional[str],
    lat: float,
    lng: float,
    provider: Optional[str],
    confidence: Optional[float],
) -> None:
    if not address_key:
        return
    existing = get_cached_geocode(db, address_key)
    if existing is not None:
        existing.address = address
        existing.formatted_address = formatted_address
        existing.lat = lat
        existing.lng = lng
        existing.provider = provider
        existing.confidence = confidence
    else:
        db.add(GeocodingCache(
            address_key=address_key,
            address=address,
            formatted_address=formatted_address,
            lat=lat,
            lng=lng,
            provider=provider,
            confidence=confidence,
        ))
    db.commit()


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def get_settings(db: Session) -> AppSettings:
    settings = db.query(AppSettings).filter(AppSettings.id == SETTINGS_ROW_ID).first()
    if settings is None:
        settings = AppSettings(id=SETTINGS_ROW_ID)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def update_settings(db: Session, **fields: object) -> AppSettings:
    settings = get_settings(db)
    for key, value in fields.items():
        if value is not None and hasattr(settings, key):
            setattr(settings, key, value)
    db.commit()
    db.refresh(settings)
    return settings


# --------------------------------------------------------------------------
# Response-shape helpers (DB rows -> the plain dicts routes/tests expect)
# --------------------------------------------------------------------------

def order_summary(order: Order) -> Dict[str, object]:
    return {
        "order_id": order.order_id,
        "customer_name": order.customer_name,
        "address": order.address,
        "delivery_time": order.delivery_slot,
        "lat": order.lat,
        "lng": order.lng,
        "geocoded_address": order.formatted_address,
        "geocode_error": order.geocode_error,
        "status": order.status,
        "assigned_vehicle": order.assigned_vehicle,
        "extra_fields": order.extra_fields or {},
    }


def failed_address_summary(failed: FailedAddress) -> Dict[str, object]:
    return {
        "order_id": failed.order_id,
        "customer_name": failed.customer_name,
        "address": failed.edited_address or failed.entered_address,
        "entered_address": failed.entered_address,
        "edited_address": failed.edited_address,
        "geocode_error": failed.failure_reason,
        "retry_count": failed.retry_count,
        "status": failed.status,
    }


def _route_stop_to_order_dict(stop: RouteStop) -> Dict[str, object]:
    data = dict(stop.order_snapshot or {})
    data["eta"] = stop.eta
    data["is_late"] = stop.status == "late"
    return data


def route_summary(route: Route) -> Dict[str, object]:
    stops = sorted(route.stops, key=lambda s: s.sequence)
    orders = [_route_stop_to_order_dict(stop) for stop in stops]

    segments: List[Dict[str, object]] = []
    from_label = "Depot"
    for stop in stops:
        to_label = str(stop.order_id)
        segments.append({
            "from": from_label,
            "to": to_label,
            "distance_km": stop.travel_distance_km,
            "time_minutes": stop.travel_time_minutes,
        })
        from_label = to_label

    return {
        "route_name": route.route_name,
        "vehicle_type": route.vehicle_type,
        "driver": route.driver,
        "orders": orders,
        "route_distance_km": route.total_distance_km,
        "route_time_minutes": route.total_duration_minutes,
        "number_of_stops": len(stops),
        "route_segments": segments,
        "google_maps_url": route.google_maps_url,
        "estimated_finish_time": route.estimated_finish_time,
        "average_stop_time": (
            round(route.total_duration_minutes / len(stops), 1)
            if route.total_duration_minutes is not None and stops else None
        ),
        "delivery_sequence": [stop.order_id for stop in stops],
        "late_deliveries": [stop.order_id for stop in stops if stop.status == "late"],
        "utilization_percent": route.utilization_percent,
        "is_auto_created": route.is_auto_created,
        "status": route.status,
    }


def route_plan_summary(route_plan: RoutePlan) -> Dict[str, object]:
    routes = sorted(route_plan.routes, key=lambda r: r.id)
    pending = [dict(p.order_snapshot or {}) for p in route_plan.pending_stops]
    return {
        "plan_id": route_plan.id,
        "id": route_plan.id,
        "created_at": route_plan.created_at.isoformat() if route_plan.created_at else None,
        "available_cars": route_plan.available_cars,
        "available_bikes": route_plan.available_bikes,
        "route_count": route_plan.route_count,
        "routes": [route_summary(route) for route in routes],
        "pending_orders": pending,
        "warnings": route_plan.warnings or [],
        "is_saved": route_plan.is_saved,
        "label": route_plan.label,
        "saved_at": route_plan.saved_at.isoformat() if route_plan.saved_at else None,
        "batch_id": route_plan.batch_id,
    }


def batch_summary(batch: UploadBatch) -> Dict[str, object]:
    return {
        "id": batch.id,
        "file_name": batch.file_name,
        "uploaded_at": batch.uploaded_at.isoformat() if batch.uploaded_at else None,
        "total_orders": batch.total_orders,
        "generated_routes": batch.generated_routes,
        "pending_orders_count": batch.pending_orders_count,
        "failed_orders_count": batch.failed_orders_count,
        "generated_by": batch.generated_by,
        "is_valid": batch.is_valid,
        "errors": batch.errors or [],
        "route_plan_count": len(batch.route_plans),
        "column_order": batch.column_order or [],
    }


def batch_detail(batch: UploadBatch) -> Dict[str, object]:
    return {
        **batch_summary(batch),
        "orders": [order_summary(order) for order in batch.orders],
        "route_plans": [route_plan_summary(route_plan) for route_plan in batch.route_plans],
    }
