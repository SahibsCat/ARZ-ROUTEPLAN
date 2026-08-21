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
