"""Repository layer - every database read/write in the app goes through a
function here. API routes call these; they never build SQLAlchemy queries
themselves."""

import re
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

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
from app.route_service import (
    insert_orders_into_route,
    recompute_route_metrics,
    single_stop_maps_link,
    vehicle_capacity,
    vehicle_max_capacity,
)

SETTINGS_ROW_ID = 1

_CITY_NOISE_WORDS = ("chennai", "tamil nadu", "tamilnadu", "tn", "india")
# Building/flat/apartment-complex descriptors and landmark references - real
# parts of the address, just never the area name itself, and they tend to
# sit in the same trailing position an area would (right before the city),
# which was throwing off "the area is the last usable segment" for
# addresses like "Adyar, Independent House, near CSI church, Chennai - 600020"
# or "..., F2 Krish Homes, Chennai - 600028".
_BUILDING_NOISE_WORDS = {"independent house", "flat", "ground floor", "first floor", "second floor"}
_BUILDING_NOISE_SUBSTRINGS = ("homes", "apartment", "apartments", "towers", "tower", "residency", "enclave", "heights", "flats", "nest", "block")
_LANDMARK_PREFIXES = ("opp", "opposite", "near", "behind", "beside", "next to", "backside", "above", "below", "adjacent", "close to", "front of")
_DOOR_NUMBER_PREFIX = re.compile(r"^(plot|door|flat|house)\b\s*(no\.?)?\s*[:\-]?\s*\d", re.IGNORECASE)
# "S2", "F2", "A1" ... - a block/unit code at the start of a segment
# ("S2 Probity Shantha") reliably marks it as a building name, not a locality.
_BLOCK_CODE_PREFIX = re.compile(r"^[A-Za-z]{1,2}\d{1,3}\b")
_TRAILING_PINCODE = re.compile(r"\s*-?\s*\d{6}\b")


def derive_area(address: Optional[str]) -> Optional[str]:
    """Best-effort area/locality extraction from a full delivery address,
    for the UI's AREA hierarchy and Excel export - never fabricated, always
    a literal substring of the address the admin actually uploaded.
    Chennai delivery addresses are almost always comma-separated
    "door/street, AREA, city, pincode" (with the occasional building name,
    phone number, or "near <landmark>" thrown in too, and sometimes the
    city/pincode sharing a segment with the real area - "Velachery
    600042"), so the area is reliably the last usable segment once the
    city, pincode, phone number, and those descriptors are stripped *from*
    each segment (not the whole segment discarded - "Velachery 600042"
    keeps "Velachery"). Returns None if the address is too sparse to say
    anything useful (single-segment addresses just show as-is in the UI
    instead of a wrong guess)."""
    if not address:
        return None
    raw_segments = [s.strip() for s in re.split(r"[,\n]", address) if s.strip()]
    if len(raw_segments) < 2:
        return None

    def clean(segment: str) -> str:
        s = _TRAILING_PINCODE.sub("", segment).strip(" ,-")
        for city in _CITY_NOISE_WORDS:
            s = re.sub(rf"\b{re.escape(city)}\b\.?", "", s, flags=re.IGNORECASE).strip(" ,-")
        return s.strip()

    def is_noise(cleaned: str) -> bool:
        if not cleaned:
            return True
        low = cleaned.lower()
        if low in _BUILDING_NOISE_WORDS:
            return True
        if low.startswith(_LANDMARK_PREFIXES):
            return True
        if any(word in low for word in _BUILDING_NOISE_SUBSTRINGS):
            return True
        if _BLOCK_CODE_PREFIX.match(cleaned) or _DOOR_NUMBER_PREFIX.match(cleaned):
            return True
        if re.fullmatch(r"[+\d][\d\s-]*", cleaned):  # phone number / country code fragment
            return True
        return False

    candidates = []
    for segment in raw_segments:
        cleaned = clean(segment)
        if not is_noise(cleaned):
            candidates.append(cleaned)
    if not candidates:
        return None
    return candidates[-1]


_LOCATION_FIELD_NAMES = ("location", "area", "zone")


def resolve_location(data: Dict[str, object]) -> Optional[str]:
    """The order's location, preferring whatever the uploaded Excel already
    said. If the source file had its own LOCATION/AREA/ZONE column (kept
    under `extra_fields` under its original header - see
    excel_service.validate_excel_file), that value is real, admin-entered
    data and always wins - it's never overwritten or second-guessed here.
    derive_area()'s heuristic parse of the address is only a fallback for
    when no such column existed in the upload."""
    extra_fields = data.get("extra_fields") or {}
    for label, value in extra_fields.items():
        if str(label).strip().lower() in _LOCATION_FIELD_NAMES:
            text = str(value).strip() if value is not None else ""
            if text:
                return text
    return derive_area(data.get("address"))


# --------------------------------------------------------------------------
# Manual route editing errors - raised by the functions in the "Manual route
# editing" section below and translated to HTTP responses in main.py.
# --------------------------------------------------------------------------

class RootplanError(Exception):
    """Base class for crud-layer errors the API layer knows how to turn into
    a clear HTTP response, instead of a raw 500."""


class RouteNotFoundError(RootplanError):
    pass


class OrderNotFoundError(RootplanError):
    pass


class CapacityError(RootplanError):
    def __init__(self, capacity: int, available: int, requested: int):
        self.capacity = capacity
        self.available = available
        self.requested = requested
        super().__init__(
            f"This route has only {available} available space(s) (capacity {capacity}). "
            f"{requested} order(s) were selected."
        )


class GeocodeFailedError(RootplanError):
    """A manually-entered address couldn't be geocoded precisely enough to
    place it on the map - see add_manual_address. Distinct from
    RouteNotFoundError/OrderNotFoundError (nothing is missing; the address
    itself is the problem) so the frontend can show address-specific
    guidance instead of a generic error."""


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
            geocode_confidence=order.get("confidence"),
            location_source="geocoded" if has_coords else None,
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
    """Deletes the upload and everything derived from it. Also permanently
    removes any geocoding_cache row that was used ONLY by this batch's
    orders - once no other batch's order references that same normalized
    address, there's no reason to keep remembering its coordinates. An
    address another batch's order still uses is left alone - its cache
    entry still speeds up (and stays consistent for) that other batch."""
    # Local import - geocode_service imports crud at module level, so a
    # top-of-file import here would be circular.
    from app.geocode_service import _cache_key, clean_address

    batch = get_upload_batch(db, batch_id)
    if batch is None:
        return False

    address_keys = {
        _cache_key(clean_address(order.address)) for order in batch.orders if order.address
    }
    address_keys.discard("")

    db.delete(batch)  # cascades to orders, route plans (+ routes/stops), failed addresses
    db.flush()

    if address_keys:
        still_used = {
            _cache_key(clean_address(row[0]))
            for row in db.query(Order.address).filter(Order.address.isnot(None)).all()
        }
        orphaned_keys = address_keys - still_used
        if orphaned_keys:
            db.query(GeocodingCache).filter(
                GeocodingCache.address_key.in_(orphaned_keys)
            ).delete(synchronize_session=False)

    # None for plan_id: any route plan(s) this batch had are gone with it,
    # whichever id(s) they were - clearing by batch_id alone is enough.
    clear_current_session_if_matches(db, batch_id=batch_id, plan_id=None)
    db.commit()
    return True


def sync_orders_with_route_plan(db: Session, batch_id: Optional[int], route_plan: "RoutePlan", plan: Dict[str, object]) -> None:
    """After a route plan is generated for a real batch, update each
    persisted Order's status/assigned_vehicle/route_id/sequence_position so
    the dashboard - and the manual add/remove/reorder endpoints below - can
    be driven from `orders` alone without re-reading the plan's JSON.
    `route_plan` must already be committed (real Route ids) - called right
    after db.refresh(route_plan) in save_route_plan."""
    if batch_id is None:
        return

    route_id_by_name = {route.route_name: route.id for route in route_plan.routes}

    assigned_info_by_order: Dict[str, Dict[str, object]] = {}
    for route in plan.get("routes", []):
        route_name = route.get("route_name", "")
        route_id = route_id_by_name.get(route_name)
        for index, order in enumerate(route.get("orders", [])):
            assigned_info_by_order[str(order.get("order_id"))] = {
                "route_name": route_name,
                "route_id": route_id,
                "sequence_position": index + 1,
            }

    pending_ids = {str(o.get("order_id")) for o in plan.get("pending_orders", [])}

    orders = db.query(Order).filter(Order.batch_id == batch_id).all()
    for order in orders:
        info = assigned_info_by_order.get(order.order_id)
        if info is not None:
            order.status = "assigned"
            order.assigned_vehicle = info["route_name"]
            order.route_id = info["route_id"]
            order.sequence_position = info["sequence_position"]
            order.unassigned_at = None
            order.previous_route_name = None
            order.previous_vehicle_type = None
        elif order.order_id in pending_ids:
            order.status = "pending"
            order.assigned_vehicle = None
            order.route_id = None
            order.sequence_position = None
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
        # Same hard invariant as _persist_route_stops below, applied to the
        # auto-generate/regenerate write path - route_service.build_routes
        # already caps every vehicle at its capacity while assigning stops,
        # so this never actually fires; it's here so a future bug in that
        # assignment logic gets caught before a plan is ever saved, instead
        # of silently writing a route with more stops than its vehicle
        # (6 for a car, 3 for a bike) can hold.
        route_orders = route_data.get("orders", [])
        route_capacity = vehicle_capacity(route_data.get("vehicle_type", ""))
        if route_capacity and len(route_orders) > route_capacity:
            raise CapacityError(capacity=route_capacity, available=0, requested=len(route_orders))

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
    sync_orders_with_route_plan(db, batch_id, route_plan, plan)
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
# Manual route editing - Unassigned Orders pool, add/remove/reorder, create
# route, bulk-assign. Everything here mutates the batch's current *draft*
# route plan directly and keeps Order.route_id/sequence_position/status and
# the corresponding RouteStop rows in sync in the same transaction, per the
# "update the existing order row, never delete-and-recreate" rule - this is
# what the manual editing surface is built on. Requires a real batch_id
# (persisted Order rows) - the legacy no-upload/manual-orders-body route
# generation path (batch_id=None) isn't editable order-by-order because
# there's no Order table row to update.
# --------------------------------------------------------------------------

def get_or_create_draft_route_plan(db: Session, batch_id: int) -> RoutePlan:
    """The plan any manual edit (add/remove/reorder/create-route) attaches
    to - the batch's current unsaved draft, same one Generate/Regenerate
    writes to. Created empty on first use if the admin creates a route by
    hand before ever clicking Generate."""
    existing = (
        db.query(RoutePlan)
        .filter(RoutePlan.batch_id == batch_id, RoutePlan.is_saved.is_(False))
        .order_by(RoutePlan.id.desc())
        .first()
    )
    if existing is not None:
        return existing

    route_plan = RoutePlan(batch_id=batch_id, is_saved=False, route_count=0)
    db.add(route_plan)
    set_current_session(db, batch_id=batch_id, plan_id=None)
    db.flush()
    set_current_session(db, batch_id=batch_id, plan_id=route_plan.id)
    db.commit()
    db.refresh(route_plan)
    return route_plan


def _next_route_number(route_names: List[str]) -> int:
    numbers = []
    for name in route_names:
        match = re.search(r"(\d+)\s*$", name or "")
        if match:
            numbers.append(int(match.group(1)))
    return (max(numbers) + 1) if numbers else 1


def unassigned_order_summary(order: Order) -> Dict[str, object]:
    return {
        **order_summary(order),
        "map_link": single_stop_maps_link(order_summary(order)),
        "previous_route_name": order.previous_route_name,
        "previous_vehicle_type": order.previous_vehicle_type,
        "unassigned_at": order.unassigned_at.isoformat() if order.unassigned_at else None,
    }


def list_unassigned_orders(
    db: Session,
    batch_id: Optional[int],
    search: Optional[str] = None,
    previous_route_name: Optional[str] = None,
    order_status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[Order], int]:
    """Everything not currently sitting on a route: newly-imported orders
    that were never routed ("pending") and orders removed from a route
    ("unassigned") - both belong in the same pool from the admin's point of
    view. Scoped to one batch since this is a single-session tool (see
    AppSettings.current_session_batch_id)."""
    query = db.query(Order).filter(Order.status.in_(["pending", "unassigned"]))
    if batch_id is not None:
        query = query.filter(Order.batch_id == batch_id)
    else:
        query = query.filter(Order.batch_id.is_(None))  # no active session -> no results, not "everything ever uploaded"
    if search:
        like = f"%{search.strip()}%"
        query = query.filter(
            (Order.customer_name.ilike(like))
            | (Order.order_id.ilike(like))
            | (Order.address.ilike(like))
        )
    if previous_route_name:
        query = query.filter(Order.previous_route_name == previous_route_name)
    if order_status:
        query = query.filter(Order.status == order_status)

    total = query.count()
    rows = (
        query.order_by(Order.id.desc())
        .offset(max(offset, 0))
        .limit(max(min(limit, 200), 1))
        .all()
    )
    return rows, total


def count_unassigned_orders(db: Session, batch_id: Optional[int]) -> int:
    query = db.query(Order).filter(Order.status.in_(["pending", "unassigned"]))
    query = query.filter(Order.batch_id == batch_id) if batch_id is not None else query.filter(Order.batch_id.is_(None))
    return query.count()


def _persist_route_stops(db: Session, route: Route, batch_id: Optional[int], metrics: Dict[str, object]) -> None:
    """The one place that writes a route's stop list. Replaces this route's
    RouteStop rows wholesale (cheap - at most a vehicle's capacity in rows)
    and, in the same transaction, updates every order in the new list's
    route_id/sequence_position/status - so RouteStop and Order can never
    disagree about which orders are on this route or in what order.

    Hard invariant, not just a caller-side check: every call site above
    (add/remove/reorder/vehicle-toggle/move-between-routes) already
    validates capacity before it gets here, but this is the single choke
    point every one of them funnels through, so it's where a future bug in
    any of those call sites - or a new one added later - gets caught
    before bad data ever reaches the database. Checked against each
    vehicle's *max* capacity (10 for a car, 3 for a bike) - the true hard
    ceiling once the "Add Address from Another Route" flow is allowed to
    push a route past its base capacity - not the lower base capacity that
    ordinary adds are capped at."""
    incoming_orders = metrics.get("orders") or []
    max_capacity = vehicle_max_capacity(route.vehicle_type)
    if max_capacity and len(incoming_orders) > max_capacity:
        raise CapacityError(capacity=max_capacity, available=0, requested=len(incoming_orders))

    # Safety net, not just move_orders_between_routes' own bookkeeping:
    # whichever path got this route's stop list here (an ordinary remove,
    # a vehicle-type change, anything), if the one stop the manual
    # override was recorded against is no longer actually on the route,
    # the flag would otherwise go stale - claiming the override is used
    # up when the route no longer has anything to show for it. Since this
    # is the one choke point every stop-list write funnels through (see
    # this function's own docstring), it's the one place that's
    # guaranteed to catch every path, not just the dedicated Undo action.
    if route.manual_extra_order_id is not None:
        incoming_ids = {str(o.get("order_id")) for o in incoming_orders}
        if route.manual_extra_order_id not in incoming_ids:
            route.manual_extra_order_id = None

    for stop in list(route.stops):
        db.delete(stop)
    db.flush()

    segments = metrics.get("route_segments") or []
    for index, order_dict in enumerate(metrics["orders"]):
        order_id = str(order_dict.get("order_id"))
        segment = segments[index] if index < len(segments) else {}
        db.add(RouteStop(
            route_id=route.id,
            order_id=order_id,
            sequence=index + 1,
            travel_distance_km=segment.get("distance_km"),
            travel_time_minutes=segment.get("time_minutes"),
            eta=order_dict.get("eta"),
            status="late" if order_dict.get("is_late") else "on_time",
            order_snapshot=order_dict,
        ))
        if batch_id is not None:
            order_row = (
                db.query(Order)
                .filter(Order.batch_id == batch_id, Order.order_id == order_id)
                .first()
            )
            if order_row is not None:
                order_row.status = "assigned"
                order_row.assigned_vehicle = route.route_name
                order_row.route_id = route.id
                order_row.sequence_position = index + 1
                order_row.unassigned_at = None
                order_row.previous_route_name = None
                order_row.previous_vehicle_type = None


def _apply_route_metrics(route: Route, metrics: Dict[str, object]) -> None:
    route.total_distance_km = metrics.get("route_distance_km")
    route.total_duration_minutes = metrics.get("route_time_minutes")
    route.estimated_finish_time = metrics.get("estimated_finish_time")
    route.utilization_percent = metrics.get("utilization_percent")
    route.google_maps_url = metrics.get("google_maps_url")
    route.status = "manually_edited"


def create_route(
    db: Session, route_plan_id: int, vehicle_type: str, order_ids: Optional[List[str]] = None,
) -> Route:
    """Manual "Add Route" - spins up an empty route with a chosen vehicle
    type (car/bike), optionally pre-populated with selected unassigned
    orders in one step (the "create new route from selection" flow). Also
    how a genuinely isolated order gets its own single-delivery route - no
    minimum stop count is enforced here."""
    route_plan = db.query(RoutePlan).filter(RoutePlan.id == route_plan_id).first()
    if route_plan is None:
        raise RouteNotFoundError("Route plan not found")
    if vehicle_type not in ("car", "bike"):
        raise RootplanError("vehicle_type must be 'car' or 'bike'")

    existing_names = [r.route_name for r in route_plan.routes]
    next_number = _next_route_number(existing_names)
    route = Route(
        route_plan_id=route_plan.id,
        route_name=f"Route {next_number}",
        vehicle_type=vehicle_type,
        status="planned",
        is_auto_created=False,
    )
    db.add(route)
    route_plan.route_count = len(existing_names) + 1
    db.flush()

    if order_ids:
        return add_orders_to_route(db, route.id, order_ids)

    db.commit()
    db.refresh(route)
    return route


def add_orders_to_route(db: Session, route_id: int, order_ids: List[str]) -> Route:
    """Adds one or more currently-unassigned orders to an existing route -
    powers both the single-order "Assign" action and bulk assignment.
    Capacity is validated for the whole batch before anything is written,
    so a batch that doesn't fit is rejected atomically rather than
    partially applied."""
    route = db.query(Route).filter(Route.id == route_id).first()
    if route is None:
        raise RouteNotFoundError("Route not found")
    route_plan = route.route_plan
    batch_id = route_plan.batch_id if route_plan else None
    if batch_id is None:
        raise RootplanError("This route belongs to a session with no upload behind it and can't be edited order-by-order.")

    existing_stops = sorted(route.stops, key=lambda s: s.sequence)
    capacity = vehicle_capacity(route.vehicle_type)
    available = capacity - len(existing_stops)
    if len(order_ids) > available:
        raise CapacityError(capacity=capacity, available=max(available, 0), requested=len(order_ids))

    orders_to_add: List[Order] = []
    seen_ids = set()
    for order_id in order_ids:
        key = str(order_id)
        if key in seen_ids:
            continue
        seen_ids.add(key)
        order = db.query(Order).filter(Order.batch_id == batch_id, Order.order_id == key).first()
        if order is None:
            raise OrderNotFoundError(f"Order {key} not found in the current session")
        if order.status == "assigned":
            raise RootplanError(f"Order {key} is already assigned to a route")
        orders_to_add.append(order)

    existing_order_dicts = [dict(stop.order_snapshot or {}) for stop in existing_stops]
    new_order_dicts = [order_summary(order) for order in orders_to_add]
    full_orders = existing_order_dicts + new_order_dicts

    metrics = recompute_route_metrics(full_orders, route.vehicle_type)
    _apply_route_metrics(route, metrics)
    _persist_route_stops(db, route, batch_id, metrics)

    db.commit()
    db.refresh(route)
    return route


def _generate_manual_order_id(db: Session, batch_id: int) -> str:
    """MANUAL-<6 hex chars> - visually distinct from any real business
    order_id an Excel upload would ever contain, so a hand-entered address
    can never collide with (or be mistaken for) a real order number."""
    for _ in range(20):
        candidate = f"MANUAL-{uuid.uuid4().hex[:6].upper()}"
        clash = db.query(Order).filter(Order.batch_id == batch_id, Order.order_id == candidate).first()
        if clash is None:
            return candidate
    raise RootplanError("Could not generate a unique order id - please try again.")


def add_manual_address(
    db: Session,
    batch_id: int,
    address: str,
    customer_name: Optional[str],
    delivery_time: Optional[str],
    fallback_vehicle_type: str,
) -> Dict[str, object]:
    """Powers the admin panel's Create Route "manual address entry" - types
    one address in directly, no Excel row needed (a phone-in order, or a
    customer added after the day's upload already happened). Looks for an
    existing route in the current draft plan that's already serving the SAME
    area (the identical derive_area() heuristic every uploaded order's AREA
    already comes from - see resolve_location) with room left under its base
    capacity, and adds the new stop there; only spins up a brand new route
    (fallback_vehicle_type) if no route covers that area yet, or every route
    that does is already full. Among several same-area candidates, picks the
    one with the MOST existing stops in that area - the most "established"
    route for it, not just the first one found.

    Geocoding reuses the exact same pipeline (confidence scoring, landmark
    retry, Places fallback) as a bulk Excel upload - a low-confidence/failed
    match raises GeocodeFailedError immediately instead of creating an order
    with a silently wrong pin; the admin fixes the address text and
    resubmits, the same experience as retrying a Failed Order."""
    # Local import - geocode_service imports crud at module level, so a
    # top-of-file import here would be circular.
    from app.geocode_service import geocode_address

    if fallback_vehicle_type not in ("car", "bike"):
        raise RootplanError("vehicle_type must be 'car' or 'bike'")

    cleaned_address = (address or "").strip()
    if not cleaned_address:
        raise RootplanError("Address is required.")

    geocoded = geocode_address(cleaned_address, db=db)
    if geocoded is None:
        raise GeocodeFailedError(
            "Could not geocode this address precisely enough to place it on the map. "
            "Check for a missing house/flat number or a landmark-only description, "
            "then try again."
        )

    order_id = _generate_manual_order_id(db, batch_id)
    order = Order(
        batch_id=batch_id,
        order_id=order_id,
        customer_name=(customer_name or "").strip() or None,
        address=cleaned_address,
        formatted_address=geocoded.get("display_name"),
        lat=geocoded["lat"],
        lng=geocoded["lng"],
        delivery_slot=(delivery_time or "").strip() or None,
        status="pending",
        extra_fields={},
    )
    db.add(order)
    db.flush()

    route_plan = get_or_create_draft_route_plan(db, batch_id)

    new_area = derive_area(cleaned_address)
    matched_route: Optional[Route] = None
    matched_count = 0
    if new_area:
        new_area_key = new_area.strip().lower()
        for candidate_route in route_plan.routes:
            stops = candidate_route.stops
            if len(stops) >= vehicle_capacity(candidate_route.vehicle_type):
                continue
            area_count = sum(
                1 for stop in stops
                if str((stop.order_snapshot or {}).get("area") or "").strip().lower() == new_area_key
            )
            if area_count > matched_count:
                matched_count = area_count
                matched_route = candidate_route

    if matched_route is not None:
        route = add_orders_to_route(db, matched_route.id, [order_id])
        return {"route": route, "order_id": order_id, "matched_area": new_area, "created_new_route": False}

    route = create_route(db, route_plan.id, fallback_vehicle_type, order_ids=[order_id])
    return {"route": route, "order_id": order_id, "matched_area": new_area, "created_new_route": True}


def set_manual_location(
    db: Session, batch_id: int, order_id: str, lat: float, lng: float,
) -> Order:
    """An admin drags/places a pin themselves - the one case explicitly
    trusted over anything a geocoder could produce (a human confirmed
    against the real place). Marks the order location_source="manual" at
    full confidence, and if it's currently on a route, patches that route's
    one RouteStop snapshot to match - deliberately a direct field patch,
    not a call into recompute_route_metrics/_persist_route_stops: this
    only ever needs to move where ONE pin is drawn, and re-running that
    machinery on a route a driver may be mid-delivery on risks wiping
    delivery_status/delivered_at (see revalidate_google_geocodes.py's own
    reasoning for the same thing this session already established).

    Callers must never auto-re-geocode an order once location_source is
    "manual" - see the guard in main.py's retry_single_geocode."""
    order = db.query(Order).filter(Order.batch_id == batch_id, Order.order_id == str(order_id)).first()
    if order is None:
        raise OrderNotFoundError(f"Order {order_id} not found in the current session")

    order.lat = lat
    order.lng = lng
    order.geocode_confidence = 1.0
    order.location_source = "manual"
    order.geocode_error = None
    if order.status == "failed":
        order.status = "pending"

    if order.route_id is not None:
        stop = (
            db.query(RouteStop)
            .filter(RouteStop.route_id == order.route_id, RouteStop.order_id == order.order_id)
            .first()
        )
        if stop is not None:
            snapshot = dict(stop.order_snapshot or {})
            snapshot["lat"] = lat
            snapshot["lng"] = lng
            snapshot["geocode_confidence"] = 1.0
            snapshot["location_source"] = "manual"
            stop.order_snapshot = snapshot  # reassign whole dict - JSON columns only pick up changes on reassignment

    db.commit()
    db.refresh(order)
    return order


def remove_order_from_route(db: Session, route_id: int, order_id: str) -> Dict[str, object]:
    """The single atomic "remove from route" operation (brief §4/§9): moves
    the order to Unassigned - never deletes it - and returns both the
    updated route and the now-unassigned order so the caller can refresh
    every dependent screen from one confirmed response."""
    route = db.query(Route).filter(Route.id == route_id).first()
    if route is None:
        raise RouteNotFoundError("Route not found")
    route_plan = route.route_plan
    batch_id = route_plan.batch_id if route_plan else None
    if batch_id is None:
        raise RootplanError("This route belongs to a session with no upload behind it and can't be edited order-by-order.")

    stops = sorted(route.stops, key=lambda s: s.sequence)
    target_stop = next((s for s in stops if str(s.order_id) == str(order_id)), None)
    if target_stop is None:
        raise OrderNotFoundError(f"Order {order_id} is not on this route")

    remaining_order_dicts = [
        dict(stop.order_snapshot or {}) for stop in stops if stop.id != target_stop.id
    ]
    metrics = recompute_route_metrics(remaining_order_dicts, route.vehicle_type)
    _apply_route_metrics(route, metrics)
    _persist_route_stops(db, route, batch_id, metrics)

    order = db.query(Order).filter(Order.batch_id == batch_id, Order.order_id == str(order_id)).first()
    if order is not None:
        order.status = "unassigned"
        order.previous_route_name = route.route_name
        order.previous_vehicle_type = route.vehicle_type
        order.unassigned_at = datetime.now(timezone.utc)
        order.route_id = None
        order.sequence_position = None
        order.assigned_vehicle = None

    db.commit()
    db.refresh(route)
    if order is not None:
        db.refresh(order)
    return {"route": route, "order": order}


def change_route_vehicle_type(db: Session, route_id: int, vehicle_type: str) -> Route:
    """Toggles a route between car/bike after the fact. Rejected if the
    route currently carries more stops than the new vehicle type's
    capacity - the admin has to remove some deliveries first rather than
    silently having some knocked off."""
    route = db.query(Route).filter(Route.id == route_id).first()
    if route is None:
        raise RouteNotFoundError("Route not found")
    if vehicle_type not in ("car", "bike"):
        raise RootplanError("vehicle_type must be 'car' or 'bike'")
    if vehicle_type == route.vehicle_type:
        return route

    route_plan = route.route_plan
    batch_id = route_plan.batch_id if route_plan else None

    stops = sorted(route.stops, key=lambda stop: stop.sequence)
    new_capacity = vehicle_capacity(vehicle_type)
    if len(stops) > new_capacity:
        raise RootplanError(
            f"This route has {len(stops)} deliveries - more than a {vehicle_type}'s capacity "
            f"of {new_capacity}. Remove some deliveries before switching vehicle type."
        )

    order_dicts = [dict(stop.order_snapshot or {}) for stop in stops]
    route.vehicle_type = vehicle_type
    metrics = recompute_route_metrics(order_dicts, vehicle_type)
    _apply_route_metrics(route, metrics)
    _persist_route_stops(db, route, batch_id, metrics)

    db.commit()
    db.refresh(route)
    return route


def delete_route(db: Session, route_id: int) -> List[Order]:
    """Deletes a single route (not the whole plan - see crud.delete_route_plan
    for that). Every order on it goes back to Unassigned first, with
    previous-route history recorded, exactly like removing them one at a
    time - the route itself is what's discarded, never an order."""
    route = db.query(Route).filter(Route.id == route_id).first()
    if route is None:
        raise RouteNotFoundError("Route not found")
    route_plan = route.route_plan
    batch_id = route_plan.batch_id if route_plan else None

    freed_orders: List[Order] = []
    if batch_id is not None:
        for stop in route.stops:
            order = db.query(Order).filter(Order.batch_id == batch_id, Order.order_id == str(stop.order_id)).first()
            if order is not None:
                order.status = "unassigned"
                order.previous_route_name = route.route_name
                order.previous_vehicle_type = route.vehicle_type
                order.unassigned_at = datetime.now(timezone.utc)
                order.route_id = None
                order.sequence_position = None
                order.assigned_vehicle = None
                freed_orders.append(order)

    db.delete(route)  # cascades to its RouteStop rows
    db.flush()
    if route_plan is not None:
        route_plan.route_count = len(route_plan.routes)

    db.commit()
    for order in freed_orders:
        db.refresh(order)
    return freed_orders


def reorder_route(db: Session, route_id: int, ordered_order_ids: List[str]) -> Route:
    """Persists a drag-and-drop reorder: `ordered_order_ids` must be exactly
    this route's current stops, just in a new order. Recomputes ETAs/
    distance/lateness for the new sequence, since reordering can change all
    of them."""
    route = db.query(Route).filter(Route.id == route_id).first()
    if route is None:
        raise RouteNotFoundError("Route not found")
    route_plan = route.route_plan
    batch_id = route_plan.batch_id if route_plan else None
    if batch_id is None:
        raise RootplanError("This route belongs to a session with no upload behind it and can't be edited order-by-order.")

    stops_by_order_id = {str(stop.order_id): stop for stop in route.stops}
    requested_ids = [str(order_id) for order_id in ordered_order_ids]
    if set(requested_ids) != set(stops_by_order_id.keys()) or len(requested_ids) != len(stops_by_order_id):
        raise RootplanError("The reordered list must contain exactly this route's current stops")

    ordered_dicts = [dict(stops_by_order_id[order_id].order_snapshot or {}) for order_id in requested_ids]
    metrics = recompute_route_metrics(ordered_dicts, route.vehicle_type)
    _apply_route_metrics(route, metrics)
    _persist_route_stops(db, route, batch_id, metrics)

    db.commit()
    db.refresh(route)
    return route


def move_orders_between_routes(
    db: Session, source_route_id: int, target_route_id: int, order_ids: List[str],
) -> Dict[str, Route]:
    """"Add Address from Another Route" - moves one or more stops directly
    from one route to another. Distinct from the ordinary Unassigned-pool
    add (add_orders_to_route), which is capped at each vehicle type's base
    capacity (6 for a car, 3 for a bike): a move that stays within the
    destination's base capacity is unrestricted (any number of stops, same
    as an ordinary add), but a move that would push the destination PAST
    its base capacity is the one deliberate admin override - exactly one
    delivery point, and only once per route. That "once" is tracked
    explicitly (Route.manual_extra_order_id - not inferred from
    len(stops) > capacity, which can't tell an override apart from a
    route that simply has more stops for some other reason) rather than
    left to capacity math alone, so the UI can show which stop it was and
    offer Undo (see remove_manual_extra_stop). Capacity/override rules are
    validated before anything is written, and both routes are recomputed
    and persisted together, so a route plan can never be left with an
    order missing from both, or on both."""
    if source_route_id == target_route_id:
        raise RootplanError("Source and destination route must be different")

    source_route = db.query(Route).filter(Route.id == source_route_id).first()
    if source_route is None:
        raise RouteNotFoundError("Source route not found")
    target_route = db.query(Route).filter(Route.id == target_route_id).first()
    if target_route is None:
        raise RouteNotFoundError("Destination route not found")

    route_plan = target_route.route_plan
    batch_id = route_plan.batch_id if route_plan else None
    if batch_id is None:
        raise RootplanError("This route belongs to a session with no upload behind it and can't be edited order-by-order.")

    source_stops_by_id = {str(stop.order_id): stop for stop in source_route.stops}
    requested_ids: List[str] = []
    seen = set()
    for order_id in order_ids:
        key = str(order_id)
        if key in seen:
            continue
        seen.add(key)
        if key not in source_stops_by_id:
            raise OrderNotFoundError(f"Order {key} is not on {source_route.route_name}")
        requested_ids.append(key)
    if not requested_ids:
        raise RootplanError("No addresses were selected to move")

    target_stops = sorted(target_route.stops, key=lambda s: s.sequence)
    base_capacity = vehicle_capacity(target_route.vehicle_type)
    max_capacity = vehicle_max_capacity(target_route.vehicle_type)
    final_count = len(target_stops) + len(requested_ids)

    uses_override = final_count > base_capacity
    if uses_override:
        if target_route.manual_extra_order_id is not None:
            raise CapacityError(capacity=base_capacity, available=0, requested=len(requested_ids))
        if len(requested_ids) != 1:
            raise RootplanError(
                "Only one delivery point can be added past this route's normal capacity at a time - "
                "select just one, or select up to its normal capacity."
            )
        if final_count > max_capacity:
            raise CapacityError(capacity=max_capacity, available=max(max_capacity - len(target_stops), 0), requested=len(requested_ids))

    moved_order_dicts = [dict(source_stops_by_id[oid].order_snapshot or {}) for oid in requested_ids]
    remaining_source_dicts = [
        dict(stop.order_snapshot or {})
        for stop in sorted(source_route.stops, key=lambda s: s.sequence)
        if str(stop.order_id) not in seen
    ]
    # Inserted at its actual best position, not simply appended to the
    # end - a manually moved stop still deserves a sensible position in
    # the route, same as every automatically-placed one.
    target_order_dicts = insert_orders_into_route(
        [dict(stop.order_snapshot or {}) for stop in target_stops],
        moved_order_dicts,
        target_route.vehicle_type,
    )

    source_metrics = recompute_route_metrics(remaining_source_dicts, source_route.vehicle_type)
    target_metrics = recompute_route_metrics(target_order_dicts, target_route.vehicle_type)
    if uses_override:
        target_route.manual_extra_order_id = requested_ids[0]

    _apply_route_metrics(source_route, source_metrics)
    _apply_route_metrics(target_route, target_metrics)
    # Target first: _persist_route_stops' hard capacity guard checks against
    # vehicle_max_capacity, which target_order_dicts is validated against
    # above: writing it first means a bug that somehow slipped past the
    # check above still fails loudly before the source route is touched,
    # rather than leaving an order removed from source with nowhere to land.
    _persist_route_stops(db, target_route, batch_id, target_metrics)
    _persist_route_stops(db, source_route, batch_id, source_metrics)

    db.commit()
    db.refresh(source_route)
    db.refresh(target_route)
    return {"source_route": source_route, "target_route": target_route}


def remove_manual_extra_stop(db: Session, route_id: int) -> Dict[str, object]:
    """Undoes the admin's one-time "Add Address from Another Route"
    override on this route (brief's optional "Remove Manual Delivery" /
    Undo) - removes exactly the stop that used it
    (Route.manual_extra_order_id) back to Unassigned, via the same
    remove_order_from_route every ordinary removal already goes through.
    That path's own _persist_route_stops call clears the flag as a side
    effect (the stop is no longer among the route's incoming orders), so
    the override becomes available again with no separate bookkeeping
    needed here."""
    route = db.query(Route).filter(Route.id == route_id).first()
    if route is None:
        raise RouteNotFoundError("Route not found")
    if not route.manual_extra_order_id:
        raise RootplanError("This route has no manually-added delivery to undo")
    return remove_order_from_route(db, route_id, route.manual_extra_order_id)


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
    confidence: Optional[float] = None,
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
                order.geocode_confidence = confidence
                order.location_source = "geocoded"
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
    data = {
        "order_id": order.order_id,
        "customer_name": order.customer_name,
        "address": order.address,
        "delivery_time": order.delivery_slot,
        "lat": order.lat,
        "lng": order.lng,
        "geocoded_address": order.formatted_address,
        "geocode_error": order.geocode_error,
        "geocode_confidence": order.geocode_confidence,
        # "manual" (admin-corrected pin - see set_manual_location) or
        # "geocoded" (whatever the geocoding pipeline last produced) -
        # None for an order from before this field existed. Surfaced so the
        # UI can show a status badge and gate "Adjust Location" separately
        # from "this needs geocoding at all".
        "location_source": order.location_source,
        "status": order.status,
        "assigned_vehicle": order.assigned_vehicle,
        "extra_fields": order.extra_fields or {},
    }
    data["map_link"] = single_stop_maps_link(data)
    data["area"] = resolve_location(data)
    return data


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
    data["map_link"] = single_stop_maps_link(data)
    data["area"] = resolve_location(data)
    data["delivery_status"] = stop.delivery_status
    data["is_delivered"] = stop.delivery_status == "delivered"
    data["delivered_at"] = stop.delivered_at.isoformat() if stop.delivered_at else None
    return data


def route_summary(route: Route) -> Dict[str, object]:
    stops = sorted(route.stops, key=lambda s: s.sequence)
    orders = [_route_stop_to_order_dict(stop) for stop in stops]
    capacity = vehicle_capacity(route.vehicle_type)
    max_capacity = vehicle_max_capacity(route.vehicle_type)
    # De-duplicated, in-order list of areas this route touches - "Velachery,
    # Adambakkam, Guindy" for the route sidebar/summary panel. Falls back to
    # the order's address when derive_area() couldn't isolate one, so an
    # address-only order still contributes something rather than a blank.
    seen_areas: List[str] = []
    seen_areas_lower = set()
    for order in orders:
        label = order.get("area") or order.get("address")
        if label and label.lower() not in seen_areas_lower:
            seen_areas.append(label)
            seen_areas_lower.add(label.lower())

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
        "route_id": route.id,
        "route_name": route.route_name,
        "vehicle_type": route.vehicle_type,
        "driver": route.driver,
        "driver_id": route.driver_id,
        "route_run_status": route.route_run_status,
        "started_at": route.started_at.isoformat() if route.started_at else None,
        "completed_at": route.completed_at.isoformat() if route.completed_at else None,
        "orders": orders,
        "route_distance_km": route.total_distance_km,
        "route_time_minutes": route.total_duration_minutes,
        "number_of_stops": len(stops),
        "capacity": capacity,
        "available_capacity": max(capacity - len(stops), 0),
        "is_full": len(stops) >= capacity,
        "max_capacity": max_capacity,
        "available_max_capacity": max(max_capacity - len(stops), 0),
        "is_at_max_capacity": len(stops) >= max_capacity,
        # Which stop (if any) is here via the admin's one-time "Add
        # Address from Another Route" override - lets the UI badge it and
        # offer Undo, and tells it whether that override is still
        # available on this route at all.
        "manual_extra_order_id": route.manual_extra_order_id,
        "areas": seen_areas,
        "route_segments": segments,
        "google_maps_url": route.google_maps_url,
        "estimated_finish_time": route.estimated_finish_time,
        "average_stop_time": (
            round(route.total_duration_minutes / len(stops), 1)
            if route.total_duration_minutes is not None and stops else None
        ),
        "delivery_sequence": [stop.order_id for stop in stops],
        "late_deliveries": [stop.order_id for stop in stops if stop.status == "late"],
        "delivered_count": sum(1 for stop in stops if stop.delivery_status == "delivered"),
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
