from fastapi.testclient import TestClient

from app import crud, crud_driver, push_service
from app.main import app
from tests.test_driver_management import _route, db_session  # noqa: F401 - fixture reuse

TOKEN = "ExponentPushToken[abc123]"


def test_save_push_token_upserts_and_moves_between_drivers(db_session):
    a = crud_driver.create_driver(db_session, "A", "a", "pass123")
    b = crud_driver.create_driver(db_session, "B", "b", "pass123")
    push_service.save_push_token(db_session, a.id, TOKEN, "android")
    push_service.save_push_token(db_session, b.id, TOKEN, "android")
    assert push_service.tokens_for_driver(db_session, a.id) == []
    assert push_service.tokens_for_driver(db_session, b.id) == [TOKEN]


def test_route_assigned_message_has_sound_and_channel():
    [msg] = push_service.build_route_assigned_messages([TOKEN, "garbage"], "Route 3", 5, 42)
    assert msg["sound"] == "default"
    assert msg["channelId"] == push_service.ROUTE_CHANNEL_ID
    assert msg["priority"] == "high"
    assert "5 stops" in msg["body"]
    assert msg["data"] == {"type": "route_assigned", "route_id": 42}


def test_notify_route_assigned_sends_to_registered_phone(db_session, monkeypatch):
    sent = []
    monkeypatch.setattr(push_service, "_send", lambda messages: sent.extend(messages))

    class SyncThread:
        def __init__(self, target, args, daemon):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(push_service.threading, "Thread", SyncThread)
    driver = crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123")
    route = _route(db_session, n=3)
    push_service.save_push_token(db_session, driver.id, TOKEN)

    count = push_service.notify_route_assigned(db_session, driver.id, route.route_name, len(route.stops), route.id)
    assert count == 1 and sent[0]["to"] == TOKEN and "3 stops" in sent[0]["body"]


def test_push_token_endpoint_rejects_non_expo_token():
    from app.driver_auth import get_current_driver

    client = TestClient(app)
    created = client.post("/api/drivers", json={"name": "P", "username": "pushy", "password": "pass123"})
    assert created.status_code in (200, 201), created.text
    driver_id = created.json()["driver"]["id"]

    class _Driver:
        id = driver_id

    app.dependency_overrides[get_current_driver] = lambda: _Driver()
    try:
        assert client.post("/api/driver/me/push-token", json={"token": "nope"}).status_code == 400
        assert client.post("/api/driver/me/push-token", json={"token": TOKEN}).json() == {"ok": True}
    finally:
        app.dependency_overrides.pop(get_current_driver, None)


def test_login_accepts_mobile_number_and_any_case(db_session):
    crud_driver.create_driver(db_session, "Kumar", "kumar", "pass123", mobile="98000 00001")
    assert crud_driver.find_driver_for_login(db_session, "Kumar ").username == "kumar"
    assert crud_driver.find_driver_for_login(db_session, "+91 9800000001").username == "kumar"
    assert crud_driver.find_driver_for_login(db_session, "9800000002") is None
    driver, _ = crud_driver.authenticate_driver(db_session, "9800000001", "pass123")
    assert driver.username == "kumar"


def test_force_delete_in_progress_run(db_session):
    import pytest
    driver = crud_driver.create_driver(db_session, "Stuck", "stuck", "pass123")
    route = _route(db_session)
    crud_driver.assign_driver_to_route(db_session, route.id, driver.id)
    route.route_run_status = "in_progress"
    db_session.commit()
    with pytest.raises(crud.RootplanError):
        crud_driver.delete_driver_history_record(db_session, route.id)
    crud_driver.delete_driver_history_record(db_session, route.id, force=True)
    from app.models import Route
    assert db_session.query(Route).filter(Route.id == route.id).first() is None


def test_retry_all_routes_every_failed_order(monkeypatch):
    from app.geocoding.base import GeocodeResult, STATUS_NEEDS_MANUAL_VERIFICATION

    class Fake:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def geocode(self, q):
            return GeocodeResult(13.0, 80.2, "x", STATUS_NEEDS_MANUAL_VERIFICATION, "google", 0.3, "Street name mismatch")

    monkeypatch.setattr("app.geocode_service._build_geocoder", lambda: Fake())
    monkeypatch.setattr("app.main.generate_routes", lambda orders, available_cars, available_bikes: {"route_count": 0, "routes": [], "pending_orders": [], "warnings": []})
    client = TestClient(app)
    orders = [
        {"order_id": "1", "address": "12 Bad St, Velachery", "lat": None, "lng": None, "geocode_error": "x"},
        {"order_id": "2", "address": "", "lat": None, "lng": None},
        {"order_id": "3", "address": "ok", "lat": 13.0, "lng": 80.0},
    ]
    data = client.post("/api/geocode/retry-all", json={"orders": orders, "available_cars": 1, "available_bikes": 1}).json()
    assert data["failed_orders"] == []
    by_id = {o["order_id"]: o for o in data["orders"]}
    assert by_id["1"]["location_source"] == "approximate"
    assert by_id["2"]["location_source"] == "unlocated"
    assert data["fixed_count"] == 2
