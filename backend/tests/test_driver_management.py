from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import crud, crud_driver
from app.database import Base
from app.driver_auth import hash_password, verify_password, generate_driver_code


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _orders(n):
    return [
        {
            "order_id": str(i), "customer_name": f"Customer {i}", "address": f"{i} Main St",
            "delivery_time": "18:00", "lat": 13.0 + i * 0.01, "lng": 80.2 + i * 0.01,
        }
        for i in range(1, n + 1)
    ]


def _route(db_session, n=2):
    batch = crud.save_upload_batch(db_session, "orders.xlsx", n, True, [], _orders(n))
    plan = crud.get_or_create_draft_route_plan(db_session, batch.id)
    return crud.create_route(db_session, plan.id, "bike", order_ids=[str(i) for i in range(1, n + 1)])


# --------------------------------------------------------------------------
# Password hashing
# --------------------------------------------------------------------------

def test_hash_password_round_trips():
    password_hash, salt = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", password_hash, salt) is True
    assert verify_password("wrong password", password_hash, salt) is False


def test_hash_password_uses_a_fresh_salt_each_time():
    hash1, salt1 = hash_password("same password")
    hash2, salt2 = hash_password("same password")
    assert salt1 != salt2
    assert hash1 != hash2  # different salt -> different hash for the same password


def test_generate_driver_code_is_sequential():
    assert generate_driver_code([]) == "DRV-0001"
    assert generate_driver_code(["DRV-0001"]) == "DRV-0002"
    assert generate_driver_code(["DRV-0001", "DRV-0003"]) == "DRV-0004"


# --------------------------------------------------------------------------
# Driver CRUD
# --------------------------------------------------------------------------

def test_create_driver_assigns_sequential_codes(db_session):
    d1 = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123", mobile="9800000001")
    d2 = crud_driver.create_driver(db_session, "Suresh", "suresh", "pass123", mobile="9800000002")
    assert d1.driver_code == "DRV-0001"
    assert d2.driver_code == "DRV-0002"
    assert d1.status == "active"
    # Password is never stored in plain text.
    assert d1.password_hash != "pass123"


def test_create_driver_rejects_duplicate_username(db_session):
    crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    with pytest.raises(crud_driver.DriverConflictError):
        crud_driver.create_driver(db_session, "Someone Else", "kumar", "otherpass")


def test_deactivate_driver_revokes_existing_sessions(db_session):
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    _, session = crud_driver.authenticate_driver(db_session, "kumar", "pass123")
    assert session.revoked_at is None

    crud_driver.set_driver_status(db_session, driver.id, "inactive")
    db_session.refresh(session)
    assert session.revoked_at is not None


def test_delete_driver_removes_them(db_session):
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    crud_driver.delete_driver(db_session, driver.id)
    assert crud_driver.get_driver(db_session, driver.id) is None


def test_delete_driver_unassigns_their_route_without_touching_it(db_session):
    route = _route(db_session)
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    crud_driver.assign_driver_to_route(db_session, route.id, driver.id)

    crud_driver.delete_driver(db_session, driver.id)

    db_session.refresh(route)
    assert route.driver_id is None
    assert len(route.stops) == 2  # the route and its stops are untouched


def test_delete_unknown_driver_raises(db_session):
    with pytest.raises(crud_driver.DriverNotFoundError):
        crud_driver.delete_driver(db_session, 999)


def test_reset_password_revokes_existing_sessions(db_session):
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    _, session = crud_driver.authenticate_driver(db_session, "kumar", "pass123")

    crud_driver.reset_driver_password(db_session, driver.id, "newpass456")
    db_session.refresh(session)
    assert session.revoked_at is not None

    # Old password no longer works; new one does.
    with pytest.raises(crud_driver.InvalidCredentialsError):
        crud_driver.authenticate_driver(db_session, "kumar", "pass123")
    driver2, _ = crud_driver.authenticate_driver(db_session, "kumar", "newpass456")
    assert driver2.id == driver.id


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------

def test_authenticate_driver_rejects_wrong_password(db_session):
    crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    with pytest.raises(crud_driver.InvalidCredentialsError):
        crud_driver.authenticate_driver(db_session, "kumar", "wrongpass")


def test_authenticate_driver_rejects_unknown_username(db_session):
    with pytest.raises(crud_driver.InvalidCredentialsError):
        crud_driver.authenticate_driver(db_session, "nobody", "pass123")


def test_authenticate_inactive_driver_is_rejected(db_session):
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    crud_driver.set_driver_status(db_session, driver.id, "inactive")
    with pytest.raises(crud_driver.InactiveDriverError):
        crud_driver.authenticate_driver(db_session, "kumar", "pass123")


# --------------------------------------------------------------------------
# Assignment
# --------------------------------------------------------------------------

def test_assign_driver_to_route(db_session):
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    route = _route(db_session)

    result = crud_driver.assign_driver_to_route(db_session, route.id, driver.id)

    assert result["conflict"] is False
    assert result["route"].driver_id == driver.id
    assert crud_driver.get_driver_current_route(db_session, driver.id).id == route.id


def test_assign_driver_already_on_another_route_requires_confirmation(db_session):
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    route1 = _route(db_session)
    route2 = _route(db_session)
    crud_driver.assign_driver_to_route(db_session, route1.id, driver.id)

    result = crud_driver.assign_driver_to_route(db_session, route2.id, driver.id)
    assert result["conflict"] is True
    assert result["previous_route"].id == route1.id
    # Nothing changed yet - still on route1.
    assert crud_driver.get_driver_current_route(db_session, driver.id).id == route1.id


def test_assign_driver_with_force_moves_them(db_session):
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    route1 = _route(db_session)
    route2 = _route(db_session)
    crud_driver.assign_driver_to_route(db_session, route1.id, driver.id)

    result = crud_driver.assign_driver_to_route(db_session, route2.id, driver.id, force=True)

    assert result["conflict"] is False
    assert crud_driver.get_driver_current_route(db_session, driver.id).id == route2.id
    db_session.refresh(route1)
    assert route1.driver_id is None  # freed from the old route


def test_reassigning_same_driver_to_same_route_resets_it_to_planned(db_session):
    # The bug report this covers: a route stuck "in progress" (e.g. the
    # driver's app crashed before End Route) - re-assigning the same
    # driver to the same route is the admin's way of resetting it back to
    # a clean, startable state, without a separate "reset route" action.
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    route = _route(db_session)
    crud_driver.assign_driver_to_route(db_session, route.id, driver.id)
    crud_driver.start_route(db_session, driver, route.id)
    db_session.refresh(route)
    assert route.route_run_status == "in_progress"
    assert route.started_at is not None

    result = crud_driver.assign_driver_to_route(db_session, route.id, driver.id)

    assert result["conflict"] is False
    db_session.refresh(route)
    assert route.route_run_status == "planned"
    assert route.started_at is None
    assert route.completed_at is None


def test_force_reassigning_a_different_driver_resets_the_route_to_planned(db_session):
    driver1 = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    driver2 = crud_driver.create_driver(db_session, "Suresh", "suresh", "pass123")
    route = _route(db_session)
    crud_driver.assign_driver_to_route(db_session, route.id, driver1.id)
    crud_driver.start_route(db_session, driver1, route.id)

    result = crud_driver.assign_driver_to_route(db_session, route.id, driver2.id, force=True)

    assert result["conflict"] is False
    db_session.refresh(route)
    assert route.driver_id == driver2.id
    assert route.route_run_status == "planned"
    assert route.started_at is None


def test_assigning_a_driver_to_a_fresh_route_leaves_it_planned(db_session):
    # Not a regression by itself (a brand new route is already "planned"),
    # but locks in that assignment never *starts* a route on its own -
    # only the driver's own Start Route action does.
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    route = _route(db_session)

    result = crud_driver.assign_driver_to_route(db_session, route.id, driver.id)

    assert result["route"].route_run_status == "planned"


def test_assign_inactive_driver_is_rejected(db_session):
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    crud_driver.set_driver_status(db_session, driver.id, "inactive")
    route = _route(db_session)
    with pytest.raises(crud.RootplanError):
        crud_driver.assign_driver_to_route(db_session, route.id, driver.id)


def test_unassign_driver_from_route(db_session):
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    route = _route(db_session)
    crud_driver.assign_driver_to_route(db_session, route.id, driver.id)

    updated = crud_driver.unassign_driver_from_route(db_session, route.id)

    assert updated.driver_id is None
    assert crud_driver.get_driver_current_route(db_session, driver.id) is None


# --------------------------------------------------------------------------
# Route lifecycle + security (driver A can never touch driver B's route)
# --------------------------------------------------------------------------

def test_start_and_end_route(db_session):
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    route = _route(db_session)
    crud_driver.assign_driver_to_route(db_session, route.id, driver.id)

    started = crud_driver.start_route(db_session, driver, route.id)
    assert started.route_run_status == "in_progress"
    assert started.started_at is not None

    ended = crud_driver.end_route(db_session, driver, route.id)
    assert ended.route_run_status == "completed"
    assert ended.completed_at is not None


def test_driver_cannot_start_a_route_not_assigned_to_them(db_session):
    driver_a = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    driver_b = crud_driver.create_driver(db_session, "Suresh", "suresh", "pass123")
    route = _route(db_session)
    crud_driver.assign_driver_to_route(db_session, route.id, driver_a.id)

    with pytest.raises(crud.RouteNotFoundError):
        crud_driver.start_route(db_session, driver_b, route.id)


def test_driver_cannot_record_location_on_another_drivers_route(db_session):
    driver_a = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    driver_b = crud_driver.create_driver(db_session, "Suresh", "suresh", "pass123")
    route = _route(db_session)
    crud_driver.assign_driver_to_route(db_session, route.id, driver_a.id)

    with pytest.raises(crud.RouteNotFoundError):
        crud_driver.record_location(db_session, driver_b, route.id, lat=13.0, lng=80.2)


def test_set_stop_delivered_and_undo(db_session):
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    route = _route(db_session)
    crud_driver.assign_driver_to_route(db_session, route.id, driver.id)

    stop = crud_driver.set_stop_delivered(db_session, driver, route.id, "1", delivered=True)
    assert stop.delivery_status == "delivered"
    assert stop.delivered_at is not None

    summary = crud.route_summary(route)
    assert summary["delivered_count"] == 1
    delivered_order = next(o for o in summary["orders"] if o["order_id"] == "1")
    assert delivered_order["is_delivered"] is True

    # A mis-tap is undoable, not a one-way door.
    stop = crud_driver.set_stop_delivered(db_session, driver, route.id, "1", delivered=False)
    assert stop.delivery_status == "pending"
    assert stop.delivered_at is None


def test_set_stop_delivered_unknown_order_raises(db_session):
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    route = _route(db_session)
    crud_driver.assign_driver_to_route(db_session, route.id, driver.id)

    with pytest.raises(crud.OrderNotFoundError):
        crud_driver.set_stop_delivered(db_session, driver, route.id, "not-a-real-order", delivered=True)


def test_driver_cannot_mark_delivered_on_another_drivers_route(db_session):
    driver_a = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    driver_b = crud_driver.create_driver(db_session, "Suresh", "suresh", "pass123")
    route = _route(db_session)
    crud_driver.assign_driver_to_route(db_session, route.id, driver_a.id)

    with pytest.raises(crud.RouteNotFoundError):
        crud_driver.set_stop_delivered(db_session, driver_b, route.id, "1", delivered=True)


def test_driver_active_route_matches_admin_route_summary(db_session):
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    route = _route(db_session)
    crud_driver.assign_driver_to_route(db_session, route.id, driver.id)

    driver_view = crud_driver.get_driver_active_route(db_session, driver)
    admin_view = crud.route_summary(route)

    assert driver_view["route_id"] == admin_view["route_id"]
    assert [o["order_id"] for o in driver_view["orders"]] == [o["order_id"] for o in admin_view["orders"]]
    assert driver_view["route_run_status"] == "planned"


def test_driver_with_no_assigned_route_gets_none(db_session):
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    assert crud_driver.get_driver_active_route(db_session, driver) is None


# --------------------------------------------------------------------------
# Live tracking status (Live / Delayed / Offline)
# --------------------------------------------------------------------------

def test_tracking_status_not_started_before_route_begins(db_session):
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    route = _route(db_session)
    crud_driver.assign_driver_to_route(db_session, route.id, driver.id)

    tracking = crud_driver.get_route_tracking(db_session, route.id)
    assert tracking["tracking_status"] == "not_started"
    assert tracking["last_location"] is None


def test_tracking_status_live_right_after_a_fresh_ping(db_session):
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    route = _route(db_session)
    crud_driver.assign_driver_to_route(db_session, route.id, driver.id)
    crud_driver.start_route(db_session, driver, route.id)
    crud_driver.record_location(db_session, driver, route.id, lat=13.05, lng=80.25, speed=12.5, heading=90)

    tracking = crud_driver.get_route_tracking(db_session, route.id)
    assert tracking["tracking_status"] == "live"
    assert tracking["last_location"]["lat"] == 13.05
    assert "maps_url" in tracking["last_location"]


def test_tracking_status_delayed_between_the_live_and_offline_windows(db_session):
    # Locks in the real-world-driven thresholds (crud_driver.LIVE_WINDOW_
    # SECONDS / DELAYED_WINDOW_SECONDS) - Android throttles the driver
    # app's requested 8s ping interval down to ~70-110s once the phone is
    # idle/stationary, confirmed via real device testing, so a ping this
    # old is normal mid-route behavior, not a dropped connection.
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    route = _route(db_session)
    crud_driver.assign_driver_to_route(db_session, route.id, driver.id)
    crud_driver.start_route(db_session, driver, route.id)
    ping = crud_driver.record_location(db_session, driver, route.id, lat=13.05, lng=80.25)

    # Rewinds the ping to simulate time having passed since a real ping -
    # route.started_at has to move back with it (get_route_tracking now
    # scopes pings to route.started_at..completed_at), or this old ping
    # reads as predating the route's own start and gets filtered out.
    aged_recorded_at = datetime.now(timezone.utc) - timedelta(seconds=300)  # past "live", within "delayed"
    ping.recorded_at = aged_recorded_at
    route.started_at = aged_recorded_at - timedelta(seconds=10)
    db_session.commit()

    tracking = crud_driver.get_route_tracking(db_session, route.id)
    assert tracking["tracking_status"] == "delayed"


def test_tracking_status_offline_with_no_pings_while_in_progress(db_session):
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    route = _route(db_session)
    crud_driver.assign_driver_to_route(db_session, route.id, driver.id)
    crud_driver.start_route(db_session, driver, route.id)

    tracking = crud_driver.get_route_tracking(db_session, route.id)
    assert tracking["tracking_status"] == "offline"


def test_tracking_status_completed_after_route_ends(db_session):
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    route = _route(db_session)
    crud_driver.assign_driver_to_route(db_session, route.id, driver.id)
    crud_driver.start_route(db_session, driver, route.id)
    crud_driver.record_location(db_session, driver, route.id, lat=13.05, lng=80.25)
    crud_driver.end_route(db_session, driver, route.id)

    tracking = crud_driver.get_route_tracking(db_session, route.id)
    assert tracking["tracking_status"] == "completed"


def test_tracking_path_returns_pings_oldest_first(db_session):
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    route = _route(db_session)
    crud_driver.assign_driver_to_route(db_session, route.id, driver.id)
    crud_driver.start_route(db_session, driver, route.id)
    crud_driver.record_location(db_session, driver, route.id, lat=13.00, lng=80.20)
    crud_driver.record_location(db_session, driver, route.id, lat=13.01, lng=80.21)
    crud_driver.record_location(db_session, driver, route.id, lat=13.02, lng=80.22)

    tracking = crud_driver.get_route_tracking(db_session, route.id)
    assert [p["lat"] for p in tracking["path"]] == [13.00, 13.01, 13.02]


# --------------------------------------------------------------------------
# Location recording is scoped to "while the driver is actually on the
# clock" - reproduces a real privacy/accuracy bug: route_id alone doesn't
# say whether a ping happened before Start Route, after End Route, or
# during an entirely different (earlier) run of the same route_id under a
# different driver.
# --------------------------------------------------------------------------

def test_record_location_rejected_before_route_is_started(db_session):
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    route = _route(db_session)
    crud_driver.assign_driver_to_route(db_session, route.id, driver.id)
    # Deliberately no start_route() call - this is the "phone pings before
    # Start Route is tapped" case.

    with pytest.raises(crud_driver.RouteNotActiveError):
        crud_driver.record_location(db_session, driver, route.id, lat=13.0, lng=80.2)


def test_record_location_rejected_after_route_ends(db_session):
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    route = _route(db_session)
    crud_driver.assign_driver_to_route(db_session, route.id, driver.id)
    crud_driver.start_route(db_session, driver, route.id)
    crud_driver.end_route(db_session, driver, route.id)
    # The "background task outlives End Route" case - a stray ping arrives
    # after the route is already marked completed.

    with pytest.raises(crud_driver.RouteNotActiveError):
        crud_driver.record_location(db_session, driver, route.id, lat=13.0, lng=80.2)


def test_route_tracking_excludes_pings_from_a_previous_run(db_session):
    # Reassigning a route (even to the same driver, e.g. recovering from a
    # crashed app) resets route_run_status/started_at/completed_at to a
    # clean slate (see assign_driver_to_route's own comment) but never
    # deletes the previous run's DriverLocationPing rows - they're still
    # sitting there under the same route_id. A fresh run's tracking view
    # must not show that old breadcrumb trail.
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    route = _route(db_session)
    crud_driver.assign_driver_to_route(db_session, route.id, driver.id)
    crud_driver.start_route(db_session, driver, route.id)
    crud_driver.record_location(db_session, driver, route.id, lat=13.00, lng=80.20)
    crud_driver.end_route(db_session, driver, route.id)

    # Reassign (same driver, simulating "admin resets a stuck route") -
    # this is what actually clears started_at/completed_at back to None.
    crud_driver.assign_driver_to_route(db_session, route.id, driver.id, force=True)

    tracking = crud_driver.get_route_tracking(db_session, route.id)
    assert tracking["tracking_status"] == "not_started"
    assert tracking["last_location"] is None
    assert tracking["path"] == []

    # And once the new run actually starts and pings, only *its* pings
    # show up - not the old run's.
    crud_driver.start_route(db_session, driver, route.id)
    crud_driver.record_location(db_session, driver, route.id, lat=14.00, lng=81.00)
    tracking = crud_driver.get_route_tracking(db_session, route.id)
    assert [p["lat"] for p in tracking["path"]] == [14.00]
