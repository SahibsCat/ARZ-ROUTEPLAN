from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.orm import relationship

from app.database import Base


class UploadBatch(Base):
    """Upload History - one row per Excel upload."""
    __tablename__ = "upload_batches"

    id = Column(Integer, primary_key=True)
    file_name = Column(String, nullable=True)
    uploaded_at = Column(DateTime(timezone=True), server_default=func.now())
    total_orders = Column(Integer, default=0)
    generated_routes = Column(Integer, default=0)
    pending_orders_count = Column(Integer, default=0)
    failed_orders_count = Column(Integer, default=0)
    generated_by = Column(String, nullable=True)
    is_valid = Column(Boolean, default=True)
    errors = Column(JSON, default=list)
    # The uploaded sheet's exact left-to-right column layout - [{"label":
    # <original header text>, "field": <canonical field name, or None for
    # an extra column>}, ...]. Lets "Download all" rebuild the upload's
    # exact columns/order/wording later, including after a refresh.
    column_order = Column(JSON, default=list)

    orders = relationship("Order", back_populates="batch", cascade="all, delete-orphan")
    route_plans = relationship("RoutePlan", back_populates="batch", cascade="all, delete-orphan")
    failed_addresses = relationship("FailedAddress", back_populates="batch", cascade="all, delete-orphan")


class Order(Base):
    """Normalized order rows for an upload. `status`/`assigned_vehicle` are
    kept in sync with the most recent route plan generated for this batch so
    the dashboard can be rebuilt from the DB alone after a refresh."""
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("upload_batches.id"), nullable=False)
    order_id = Column(String, nullable=False, index=True)
    customer_name = Column(String, nullable=True)
    address = Column(String, nullable=True)
    formatted_address = Column(String, nullable=True)
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    delivery_slot = Column(String, nullable=True)
    # pending (not yet routed) | assigned (on a route) | unassigned (was on a
    # route, removed by the admin - distinct from "pending" so the
    # Unassigned Orders screen can show where it came from) | failed
    # (geocode failed)
    status = Column(String, default="pending", nullable=False)
    assigned_vehicle = Column(String, nullable=True)
    geocode_error = Column(String, nullable=True)
    # Which route (if any) this order currently sits on. Nullable - null
    # whenever status is pending/unassigned/failed. This plus
    # sequence_position is the single source of truth for route membership;
    # RouteStop rows are kept in sync with these in the same transaction
    # (see crud.add_orders_to_route / remove_order_from_route /
    # reorder_route) rather than being an independently-drifting copy.
    # ondelete="SET NULL": a Regenerate replaces the whole unsaved draft
    # plan (cascade-deletes its Route/RouteStop rows) - without this, that
    # delete would fail with a foreign-key violation on Postgres the moment
    # any order had been manually assigned to one of those routes.
    # sync_orders_with_route_plan() then re-derives the correct route_id
    # for the new plan in the same call.
    route_id = Column(Integer, ForeignKey("routes.id", ondelete="SET NULL"), nullable=True)
    sequence_position = Column(Integer, nullable=True)
    # Set the moment an order is removed from a route (status -> unassigned)
    # so the Unassigned Orders screen can filter/sort by it. Cleared again if
    # the order is reassigned to a route.
    unassigned_at = Column(DateTime(timezone=True), nullable=True)
    # Informational only, per the brief's "Previously: Route 1 / bike" note
    # on the Unassigned Orders screen - not a live FK (the route it names may
    # itself have since been deleted), so it survives that.
    previous_route_name = Column(String, nullable=True)
    previous_vehicle_type = Column(String, nullable=True)
    # Columns from the upload that don't map to order_id/customer_name/
    # address/delivery_time - contact number, amount, payment mode,
    # remarks, box counts, whatever else a business tracks - kept under
    # their original spreadsheet header instead of being dropped on
    # upload. Surfaced back out in the "Download all" export.
    extra_fields = Column(JSON, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    batch = relationship("UploadBatch", back_populates="orders")


class RoutePlan(Base):
    """One route-generation event. Holds generation-level data (fleet size,
    warnings) that doesn't belong to any single route; the routes it
    produced are normalized rows in `Route`/`RouteStop` below."""
    __tablename__ = "route_plans"

    id = Column(Integer, primary_key=True)
    # Nullable: a route plan can be generated straight from an orders list
    # without a known upload batch to link to.
    batch_id = Column(Integer, ForeignKey("upload_batches.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    available_cars = Column(Integer, default=0)
    available_bikes = Column(Integer, default=0)
    route_count = Column(Integer, default=0)
    warnings = Column(JSON, default=list)
    # Every Generate/Regenerate/Retry writes a plan here so a refresh never
    # loses in-progress work, but is_saved stays False until the user
    # explicitly clicks "Save to history" - only saved plans show up on the
    # Route History screen, and only one draft per batch is kept at a time
    # (see crud.save_route_plan) so drafts never pile up.
    is_saved = Column(Boolean, default=False, nullable=False)
    label = Column(String, nullable=True)
    saved_at = Column(DateTime(timezone=True), nullable=True)

    batch = relationship("UploadBatch", back_populates="route_plans")
    routes = relationship("Route", back_populates="route_plan", cascade="all, delete-orphan")
    pending_stops = relationship("PendingOrder", back_populates="route_plan", cascade="all, delete-orphan")


class Route(Base):
    __tablename__ = "routes"

    id = Column(Integer, primary_key=True)
    route_plan_id = Column(Integer, ForeignKey("route_plans.id"), nullable=False)
    route_name = Column(String, nullable=False)
    vehicle_type = Column(String, nullable=False)
    driver = Column(String, nullable=True)  # legacy free-text name, unused now that driver_id exists
    total_distance_km = Column(Float, nullable=True)
    total_duration_minutes = Column(Float, nullable=True)
    estimated_finish_time = Column(String, nullable=True)
    utilization_percent = Column(Float, nullable=True)
    google_maps_url = Column(Text, nullable=True)
    # planned | manually_edited
    status = Column(String, default="planned", nullable=False)
    is_auto_created = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # ondelete="SET NULL": deactivating/deleting driver data must never take
    # a route down with it.
    driver_id = Column(Integer, ForeignKey("drivers.id", ondelete="SET NULL"), nullable=True)
    # planned (not yet started by the driver) | in_progress (Start Route
    # tapped, GPS tracking live) | completed (End Route tapped). Distinct
    # from `status` above, which is about admin edits (planned/manually_
    # edited), not delivery execution.
    route_run_status = Column(String, default="planned", nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    route_plan = relationship("RoutePlan", back_populates="routes")
    stops = relationship(
        "RouteStop", back_populates="route", cascade="all, delete-orphan",
        order_by="RouteStop.sequence",
    )
    driver_ref = relationship("Driver", back_populates="routes")
    location_pings = relationship(
        "DriverLocationPing", back_populates="route", cascade="all, delete-orphan",
    )


class RouteStop(Base):
    """order_id is the business order_id (matches Order.order_id / the
    upload's order_id column), not a foreign key to `orders.id` - route
    generation runs off in-memory order dicts (including ones from a
    manual/no-upload flow) that don't always have a persisted Order row."""
    __tablename__ = "route_stops"

    id = Column(Integer, primary_key=True)
    route_id = Column(Integer, ForeignKey("routes.id"), nullable=False)
    order_id = Column(String, nullable=False)
    sequence = Column(Integer, nullable=False)
    travel_distance_km = Column(Float, nullable=True)
    travel_time_minutes = Column(Float, nullable=True)
    eta = Column(String, nullable=True)
    # on_time | late
    status = Column(String, default="on_time", nullable=False)
    # Full order dict (customer name, address, slot, ...) as it looked at
    # generation time, so a route card can be rendered from this table alone.
    order_snapshot = Column(JSON, default=dict)

    route = relationship("Route", back_populates="stops")


class PendingOrder(Base):
    """Orders a route plan couldn't place on any vehicle. Persisted so the
    Pending board survives a refresh."""
    __tablename__ = "pending_orders"

    id = Column(Integer, primary_key=True)
    route_plan_id = Column(Integer, ForeignKey("route_plans.id"), nullable=False)
    order_id = Column(String, nullable=False)
    order_snapshot = Column(JSON, default=dict)

    route_plan = relationship("RoutePlan", back_populates="pending_stops")


class FailedAddress(Base):
    """Orders whose address couldn't be geocoded. Kept independent of
    RoutePlan/RouteStop (a failed address never made it onto a route) so it
    survives regenerates and remains editable/retryable after a refresh."""
    __tablename__ = "failed_addresses"

    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("upload_batches.id"), nullable=True)
    order_id = Column(String, nullable=False, index=True)
    customer_name = Column(String, nullable=True)
    entered_address = Column(String, nullable=True)
    edited_address = Column(String, nullable=True)
    failure_reason = Column(String, nullable=True)
    retry_count = Column(Integer, default=0)
    # pending | resolved
    status = Column(String, default="pending", nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    batch = relationship("UploadBatch", back_populates="failed_addresses")


class GeocodingCache(Base):
    """Looked up by normalized address before any provider call - a repeat
    address (common across uploads for regular customers) never spends a
    billed Google request twice."""
    __tablename__ = "geocoding_cache"

    id = Column(Integer, primary_key=True)
    address_key = Column(String, nullable=False, unique=True, index=True)
    address = Column(String, nullable=True)
    formatted_address = Column(String, nullable=True)
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    provider = Column(String, nullable=True)
    confidence = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Driver(Base):
    """A delivery driver/partner - login identity for the driver app, plus
    the roster fields the admin manages from the Drivers page. Never
    hard-deleted while any route/tracking history references it (see
    crud_driver.deactivate_driver) - `status` is the soft-delete."""
    __tablename__ = "drivers"

    id = Column(Integer, primary_key=True)
    # DRV-0001, DRV-0002, ... - assigned once at creation, never reused.
    driver_code = Column(String, nullable=False, unique=True, index=True)
    name = Column(String, nullable=False)
    mobile = Column(String, nullable=True)
    username = Column(String, nullable=False, unique=True, index=True)
    password_hash = Column(String, nullable=False)
    password_salt = Column(String, nullable=False)
    vehicle_number = Column(String, nullable=True)
    # active | inactive
    status = Column(String, default="active", nullable=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    routes = relationship("Route", back_populates="driver_ref")
    sessions = relationship("DriverSession", back_populates="driver", cascade="all, delete-orphan")
    location_pings = relationship("DriverLocationPing", back_populates="driver", cascade="all, delete-orphan")


class DriverSession(Base):
    """An opaque bearer token issued on driver login - not a stateless JWT,
    specifically so deactivating a driver or resetting their password can
    immediately revoke every session they're holding (revoked_at), which a
    self-contained JWT can't do without extra denylist infrastructure."""
    __tablename__ = "driver_sessions"

    id = Column(Integer, primary_key=True)
    driver_id = Column(Integer, ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False)
    token = Column(String, nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    driver = relationship("Driver", back_populates="sessions")


class DriverLocationPing(Base):
    """One GPS sample. Every ping is kept (not just the latest) so the
    admin can optionally see the travelled path for the active route, not
    only the current dot - `route_id` scopes history to one run of one
    route rather than mixing pings across different days/routes for the
    same driver."""
    __tablename__ = "driver_location_pings"

    id = Column(Integer, primary_key=True)
    driver_id = Column(Integer, ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False)
    route_id = Column(Integer, ForeignKey("routes.id", ondelete="CASCADE"), nullable=False)
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    speed = Column(Float, nullable=True)
    heading = Column(Float, nullable=True)
    accuracy = Column(Float, nullable=True)
    # Server-assigned, deliberately not client-supplied - a driver's phone
    # clock isn't a trustworthy source for "how recent is this", and
    # Live/Delayed/Offline is computed from this.
    recorded_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    driver = relationship("Driver", back_populates="location_pings")
    route = relationship("Route", back_populates="location_pings")


class AppSettings(Base):
    """Single fixed row (id=1) - process-wide preferences. A key/value table
    would be overkill for the handful of settings this app has."""
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True)
    default_car_count = Column(Integer, default=1)
    default_bike_count = Column(Integer, default=2)
    theme = Column(String, default="system")
    preferences = Column(JSON, default=dict)
    # What /api/dashboard restores on page load - deliberately NOT "whichever
    # upload/plan happens to be newest in the whole table". That inferred
    # "latest" approach was the bug: deleting your active session just fell
    # through to an older historical upload (real file name, real order
    # count, real Regenerate button) instead of an empty dashboard. These
    # are explicit pointers instead: set when an upload/route plan is
    # created, cleared the moment that specific upload/plan is deleted -
    # never inferred from "whatever's left over."
    current_session_batch_id = Column(Integer, nullable=True)
    current_session_plan_id = Column(Integer, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
