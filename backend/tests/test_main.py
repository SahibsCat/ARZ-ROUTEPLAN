import io
from unittest import mock

from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.main import app

client = TestClient(app)


def _build_test_workbook():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["order_id", "customer_name", "address", "delivery_time"])
    sheet.append(["1", "Alice", "1 Main Street, Chennai", "10:00"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer


def test_generate_routes_endpoint_returns_route_plan():
    payload = {
        "orders": [
            {"order_id": "1001", "customer_name": "Alice", "address": "1 Main", "delivery_time": "09:00"},
            {"order_id": "1002", "customer_name": "Bob", "address": "2 Main", "delivery_time": "11:00"},
            {"order_id": "1003", "customer_name": "Carol", "address": "3 Main", "delivery_time": "15:00"},
        ],
        "available_cars": 1,
        "available_bikes": 2,
    }

    response = client.post("/generate-routes", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["route_count"] == 3
    assert len(body["routes"]) == 3
    # Regression: routes returned here must carry a real route_id (the
    # persisted Route row's id) so the frontend can immediately call the
    # manual add/remove/reorder endpoints against a just-generated route
    # without a reload. Before this was fixed, these came straight from
    # route_service.generate_routes()'s raw in-memory computation, which has
    # no route_id at all - every manual edit call built a URL like
    # "/api/routes/undefined/orders" and 422'd.
    for route in body["routes"]:
        assert route["route_id"] is not None
        assert route["capacity"] is not None


def test_api_routes_generate_alias_matches_generate_route_plan():
    payload = {
        "orders": [
            {"order_id": "1001", "customer_name": "Alice", "address": "1 Main", "delivery_time": "09:00"},
            {"order_id": "1002", "customer_name": "Bob", "address": "2 Main", "delivery_time": "11:00"},
            {"order_id": "1003", "customer_name": "Carol", "address": "3 Main", "delivery_time": "15:00"},
        ],
        "available_cars": 1,
        "available_bikes": 2,
    }

    response = client.post("/api/routes/generate", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["route_count"] == 3
    assert len(body["routes"]) == 3
    assert "pending_orders" in body
    assert body["warnings"] == []


def _retry_payload(order_id, available_cars, available_bikes):
    orders = [
        {"order_id": "1", "customer_name": "Alice", "address": "A Street", "delivery_time": "10:00", "lat": 13.0, "lng": 80.2},
        {"order_id": "2", "customer_name": "Bob", "address": "B Street", "delivery_time": "11:00", "lat": 13.01, "lng": 80.21},
        {"order_id": "3", "customer_name": "Carol", "address": "Bad Address", "delivery_time": "12:00", "lat": None, "lng": None, "geocode_error": "failed"},
    ]
    return {
        "order_id": order_id,
        "updated_address": "Fixed Address, Chennai",
        "orders": orders,
        "available_cars": available_cars,
        "available_bikes": available_bikes,
    }


def test_retry_geocode_with_zero_vehicles_returns_warning_not_silent_blank():
    fake_geocode = lambda address, db=None: {"lat": 12.99, "lng": 80.21, "display_name": "Fixed Address, Chennai"}

    with mock.patch("app.geocode_service.geocode_single_address", fake_geocode):
        response = client.post("/api/geocode/retry", json=_retry_payload("3", 0, 0))

    assert response.status_code == 200
    body = response.json()
    assert body["routes"] == []
    assert body["successful_orders"]
    assert len(body["pending_orders"]) == len(body["successful_orders"])
    assert any("no vehicles configured" in w.lower() for w in body["warnings"])


def test_retry_geocode_with_vehicles_configured_produces_full_route_and_no_warning():
    fake_geocode = lambda address, db=None: {"lat": 12.99, "lng": 80.21, "display_name": "Fixed Address, Chennai"}
    fake_distance = lambda source, destination: {"distance_km": 2.0, "time_minutes": 5.0}
    fake_matrix = lambda origin, destinations: [fake_distance(origin, d) for d in destinations]

    with mock.patch("app.geocode_service.geocode_single_address", fake_geocode), \
         mock.patch("app.route_service._road_distance_time", fake_distance), \
         mock.patch("app.route_service.get_distances_from_point", fake_matrix):
        response = client.post("/api/geocode/retry", json=_retry_payload("3", 1, 2))

    assert response.status_code == 200
    body = response.json()
    assert body["pending_orders"] == []
    assert len(body["routes"]) >= 1
    assert sum(r["number_of_stops"] for r in body["routes"]) == 3
    assert body["warnings"] == []


def test_upload_persists_batch_and_history_endpoints_return_it():
    fake_geocoded = [
        {
            "order_id": "1",
            "customer_name": "Alice",
            "address": "1 Main Street, Chennai",
            "delivery_time": "10:00",
            "lat": 13.0,
            "lng": 80.2,
            "geocoded_address": "1 Main Street, Chennai",
        },
    ]

    with mock.patch("app.main.geocode_orders", return_value=(fake_geocoded, None)):
        upload_response = client.post(
            "/api/orders/upload",
            files={
                "file": (
                    "orders.xlsx",
                    _build_test_workbook(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )

    assert upload_response.status_code == 200
    upload_body = upload_response.json()
    batch_id = upload_body["batch_id"]
    assert batch_id is not None

    history_response = client.get("/api/history")
    assert history_response.status_code == 200
    batches = history_response.json()["batches"]
    assert any(batch["id"] == batch_id for batch in batches)

    detail_response = client.get(f"/api/history/{batch_id}")
    assert detail_response.status_code == 200
    detail_body = detail_response.json()
    assert detail_body["total_orders"] == 1
    assert len(detail_body["orders"]) == 1
    assert detail_body["orders"][0]["order_id"] == "1"


def test_history_detail_returns_404_for_unknown_batch():
    response = client.get("/api/history/999999")
    assert response.status_code == 404


def test_dashboard_reports_no_data_before_any_upload():
    # A completely fresh database (this test's isolated in-memory one) has
    # nothing to restore - the frontend should show the empty state, not an
    # error.
    response = client.get("/api/dashboard")
    assert response.status_code == 200
    body = response.json()
    assert "settings" in body
    # has_data may be true if an earlier test in this run already uploaded
    # something into the shared test DB - only settings is guaranteed here.


def test_dashboard_restores_latest_upload_and_routes():
    fake_geocoded = [
        {
            "order_id": "d1",
            "customer_name": "Dana",
            "address": "9 Dashboard Street, Chennai",
            "delivery_time": "10:00",
            "lat": 13.0,
            "lng": 80.2,
            "geocoded_address": "9 Dashboard Street, Chennai",
        },
    ]

    with mock.patch("app.main.geocode_orders", return_value=(fake_geocoded, None)):
        upload_response = client.post(
            "/api/orders/upload",
            files={
                "file": (
                    "dashboard.xlsx",
                    _build_test_workbook(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
    batch_id = upload_response.json()["batch_id"]
    assert batch_id is not None

    route_response = client.post(
        "/api/routes/generate",
        json={
            "orders": fake_geocoded,
            "available_cars": 1,
            "available_bikes": 0,
            "batch_id": batch_id,
        },
    )
    assert route_response.status_code == 200

    dashboard_response = client.get("/api/dashboard")
    assert dashboard_response.status_code == 200
    body = dashboard_response.json()
    assert body["has_data"] is True
    assert body["batch_id"] == batch_id
    assert body["total_orders"] == 1
    assert len(body["orders"]) == 1
    assert body["orders"][0]["order_id"] == "d1"


def test_settings_round_trip():
    response = client.put("/api/settings", json={"default_car_count": 4, "theme": "dark"})
    assert response.status_code == 200
    body = response.json()
    assert body["default_car_count"] == 4
    assert body["theme"] == "dark"

    get_response = client.get("/api/settings")
    assert get_response.json()["default_car_count"] == 4


def test_delete_upload_removes_it_from_history():
    fake_geocoded = [
        {"order_id": "x1", "customer_name": "X", "address": "1 X St, Chennai", "delivery_time": "10:00", "lat": 13.0, "lng": 80.2},
    ]
    with mock.patch("app.main.geocode_orders", return_value=(fake_geocoded, None)):
        upload_response = client.post(
            "/api/orders/upload",
            files={
                "file": (
                    "todelete.xlsx",
                    _build_test_workbook(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
    batch_id = upload_response.json()["batch_id"]

    delete_response = client.delete(f"/api/history/{batch_id}")
    assert delete_response.status_code == 200

    assert client.get(f"/api/history/{batch_id}").status_code == 404
    assert client.delete(f"/api/history/{batch_id}").status_code == 404


def test_delete_route_plan_endpoint():
    route_response = client.post(
        "/api/routes/generate",
        json={
            "orders": [{"order_id": "r1", "customer_name": "R", "address": "1 R St", "delivery_time": "09:00", "lat": 13.0, "lng": 80.2}],
            "available_cars": 1,
            "available_bikes": 0,
        },
    )
    plan_id = route_response.json()["plan_id"]
    assert plan_id is not None

    delete_response = client.delete(f"/api/routes/{plan_id}")
    assert delete_response.status_code == 200
    assert client.delete(f"/api/routes/{plan_id}").status_code == 404


def test_generated_route_plan_is_unsaved_draft_by_default():
    response = client.post(
        "/api/routes/generate",
        json={
            "orders": [{"order_id": "s1", "customer_name": "S", "address": "1 S St", "delivery_time": "09:00", "lat": 13.0, "lng": 80.2}],
            "available_cars": 1,
            "available_bikes": 0,
        },
    )
    body = response.json()
    assert body["is_saved"] is False

    history = client.get("/api/routes/history").json()
    assert body["plan_id"] not in [p["plan_id"] for p in history["plans"]]


def test_save_route_plan_endpoint_promotes_it_to_history():
    route_response = client.post(
        "/api/routes/generate",
        json={
            "orders": [{"order_id": "sv1", "customer_name": "S", "address": "1 S St", "delivery_time": "09:00", "lat": 13.0, "lng": 80.2}],
            "available_cars": 1,
            "available_bikes": 0,
        },
    )
    plan_id = route_response.json()["plan_id"]

    save_response = client.post(f"/api/routes/{plan_id}/save", json={"label": "Morning batch"})
    assert save_response.status_code == 200
    assert save_response.json()["is_saved"] is True
    assert save_response.json()["label"] == "Morning batch"

    history = client.get("/api/routes/history").json()
    assert plan_id in [p["plan_id"] for p in history["plans"]]
    saved_entry = next(p for p in history["plans"] if p["plan_id"] == plan_id)
    assert saved_entry["label"] == "Morning batch"
    assert saved_entry["route_count"] == 1

    detail_response = client.get(f"/api/routes/history/{plan_id}")
    assert detail_response.status_code == 200
    assert detail_response.json()["is_saved"] is True


def test_save_route_plan_endpoint_404_for_unknown_plan():
    response = client.post("/api/routes/999999/save", json={})
    assert response.status_code == 404


def test_route_history_detail_404_for_unknown_plan():
    assert client.get("/api/routes/history/999999").status_code == 404


def test_regenerating_a_saved_plan_creates_a_new_draft_without_touching_the_saved_one():
    fake_geocoded = [
        {"order_id": "rg1", "customer_name": "RG", "address": "1 RG St, Chennai", "delivery_time": "09:00", "lat": 13.0, "lng": 80.2},
    ]
    with mock.patch("app.main.geocode_orders", return_value=(fake_geocoded, None)):
        upload_response = client.post(
            "/api/orders/upload",
            files={
                "file": (
                    "regen.xlsx",
                    _build_test_workbook(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
    batch_id = upload_response.json()["batch_id"]

    first_route_response = client.post(
        "/api/routes/generate",
        json={"orders": fake_geocoded, "available_cars": 1, "available_bikes": 0, "batch_id": batch_id},
    )
    first_plan_id = first_route_response.json()["plan_id"]
    client.post(f"/api/routes/{first_plan_id}/save")

    second_route_response = client.post(
        "/api/routes/generate",
        json={"orders": fake_geocoded, "available_cars": 0, "available_bikes": 1, "batch_id": batch_id},
    )
    second_plan_id = second_route_response.json()["plan_id"]
    assert second_plan_id != first_plan_id

    # The saved plan survives untouched...
    assert client.get(f"/api/routes/history/{first_plan_id}").json()["is_saved"] is True
    # ...and the dashboard now points at the fresh draft, not the saved one.
    dashboard = client.get("/api/dashboard").json()
    assert dashboard["plan_id"] == second_plan_id


def test_deleting_current_session_route_plan_deletes_its_upload_and_shows_empty_dashboard():
    # Deleting a route plan must clean up the whole upload it came from
    # (file name, orders, failed addresses) too - not leave it behind as
    # orphaned storage with no route pointing at it. And deleting the
    # session you're actually in must show a genuinely empty dashboard
    # afterward - not silently fall through to some older, unrelated
    # upload still sitting in history (that read exactly like "the delete
    # didn't work": a real file name and order count reappeared after
    # refresh, just from a different upload).
    fake_geocoded = [
        {"order_id": "del1", "customer_name": "Del", "address": "1 Del St, Chennai", "delivery_time": "09:00", "lat": 13.0, "lng": 80.2},
    ]

    with mock.patch("app.main.geocode_orders", return_value=(fake_geocoded, None)):
        older_upload = client.post(
            "/api/orders/upload",
            files={"file": ("older.xlsx", _build_test_workbook(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
    older_batch_id = older_upload.json()["batch_id"]
    older_route_response = client.post(
        "/api/routes/generate",
        json={"orders": fake_geocoded, "available_cars": 1, "available_bikes": 0, "batch_id": older_batch_id},
    )
    older_plan_id = older_route_response.json()["plan_id"]

    with mock.patch("app.main.geocode_orders", return_value=(fake_geocoded, None)):
        newer_upload = client.post(
            "/api/orders/upload",
            files={"file": ("newer.xlsx", _build_test_workbook(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
    newer_batch_id = newer_upload.json()["batch_id"]
    newer_route_response = client.post(
        "/api/routes/generate",
        json={"orders": fake_geocoded, "available_cars": 1, "available_bikes": 0, "batch_id": newer_batch_id},
    )
    newer_plan_id = newer_route_response.json()["plan_id"]

    # Sanity check: dashboard currently shows the newer upload/plan.
    dashboard_before = client.get("/api/dashboard").json()
    assert dashboard_before["plan_id"] == newer_plan_id
    assert dashboard_before["batch_id"] == newer_batch_id
    assert dashboard_before["file_name"] == "newer.xlsx"
    assert dashboard_before["total_orders"] == 1

    delete_response = client.delete(f"/api/routes/{newer_plan_id}")
    assert delete_response.status_code == 200

    # The newer upload (file name, orders, everything) is gone, not just
    # its route - and the dashboard shows a plain empty state, not the
    # older upload that's still sitting in history.
    dashboard_after_delete = client.get("/api/dashboard").json()
    assert dashboard_after_delete["has_data"] is False
    assert dashboard_after_delete["plan_id"] is None
    assert dashboard_after_delete["batch_id"] is None
    assert not dashboard_after_delete["file_name"]
    assert dashboard_after_delete["total_orders"] == 0

    # Really gone from the database, not just hidden.
    assert client.get(f"/api/routes/history/{newer_plan_id}").status_code == 404
    assert client.get(f"/api/history/{newer_batch_id}").status_code == 404

    # The older upload/plan was never touched - it's just no longer what
    # the dashboard auto-restores, since it was never the current session.
    assert client.get(f"/api/history/{older_batch_id}").status_code == 200
    assert client.get(f"/api/routes/history/{older_plan_id}").status_code == 200


def test_deleting_route_plan_keeps_batch_alive_if_another_plan_still_uses_it():
    # If an upload has more than one route plan against it (e.g. a
    # regenerate created a fresh draft after an earlier plan from the same
    # upload was already saved to history), deleting just one of them must
    # not destroy the upload out from under the other still-referencing
    # plan.
    fake_geocoded = [
        {"order_id": "keep1", "customer_name": "Keep", "address": "1 Keep St, Chennai", "delivery_time": "09:00", "lat": 13.0, "lng": 80.2},
    ]
    with mock.patch("app.main.geocode_orders", return_value=(fake_geocoded, None)):
        upload_response = client.post(
            "/api/orders/upload",
            files={"file": ("keep.xlsx", _build_test_workbook(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
    batch_id = upload_response.json()["batch_id"]

    first_route_response = client.post(
        "/api/routes/generate",
        json={"orders": fake_geocoded, "available_cars": 1, "available_bikes": 0, "batch_id": batch_id},
    )
    first_plan_id = first_route_response.json()["plan_id"]
    client.post(f"/api/routes/{first_plan_id}/save")  # now protected from draft replacement

    second_route_response = client.post(
        "/api/routes/generate",
        json={"orders": fake_geocoded, "available_cars": 0, "available_bikes": 1, "batch_id": batch_id},
    )
    second_plan_id = second_route_response.json()["plan_id"]
    assert second_plan_id != first_plan_id

    delete_response = client.delete(f"/api/routes/{second_plan_id}")
    assert delete_response.status_code == 200

    # The upload survives because the saved plan still references it.
    assert client.get(f"/api/history/{batch_id}").status_code == 200
    assert client.get(f"/api/routes/history/{first_plan_id}").status_code == 200
