import io

from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.main import app

client = TestClient(app)


# Real, specific, house-numbered Chennai addresses that geocode cleanly to
# STATUS_OK - NOT the "{i} Main Street, Chennai" template this used to be:
# "Main Street"/"Main Road" repeats across dozens of unrelated Chennai
# neighbourhoods, so Google's top match for it is itself ambiguous and
# genuinely inconsistent about which one it picks - once geocoding
# accuracy validation got strict enough to check the matched STREET NAME
# itself (not just PIN/locality/house number), that ambiguity started
# tripping the very check it exists to test, independent of anything
# these tests are actually about (route-editing mechanics, not geocoding).
_REAL_TEST_ADDRESSES = [
    "21 TTK Road, Alwarpet, Chennai",
    "15 Sardar Patel Road, Guindy, Chennai",
    "20 Kutchery Road, Mylapore, Chennai",
    "8 Bazaar Road, Mylapore, Chennai",
]


def _upload_batch(order_ids):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["order_id", "customer_name", "address", "delivery_time"])
    for i, order_id in enumerate(order_ids):
        sheet.append([order_id, f"Customer {order_id}", _REAL_TEST_ADDRESSES[i % len(_REAL_TEST_ADDRESSES)], "18:00"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)

    response = client.post(
        "/api/orders/upload",
        files={"file": ("orders.xlsx", buffer, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert response.status_code == 200
    return response.json()["batch_id"]


def test_full_manual_editing_flow_add_remove_reorder():
    batch_id = _upload_batch(["a1", "a2", "a3"])

    unassigned = client.get("/api/orders/unassigned").json()
    assert unassigned["total"] == 3

    created = client.post("/api/routes", json={"vehicle_type": "bike", "order_ids": ["a1", "a2"]})
    assert created.status_code == 200
    route = created.json()["route"]
    assert route["vehicle_type"] == "bike"
    assert route["number_of_stops"] == 2
    assert created.json()["unassigned_total"] == 1
    route_id = route["route_id"]

    reordered = client.patch(f"/api/routes/{route_id}/reorder", json={"order_ids": ["a2", "a1"]})
    assert reordered.status_code == 200
    assert reordered.json()["route"]["delivery_sequence"] == ["a2", "a1"]

    removed = client.delete(f"/api/routes/{route_id}/orders/a2")
    assert removed.status_code == 200
    body = removed.json()
    assert body["order"]["status"] == "unassigned"
    assert body["order"]["previous_route_name"] == "Route 1"
    assert body["route"]["number_of_stops"] == 1
    assert body["unassigned_total"] == 2

    final_unassigned = client.get("/api/orders/unassigned").json()
    assert final_unassigned["total"] == 2
    assert {o["order_id"] for o in final_unassigned["orders"]} == {"a2", "a3"}


def test_add_orders_over_capacity_returns_409():
    batch_id = _upload_batch(["b1", "b2", "b3", "b4"])
    route = client.post("/api/routes", json={"vehicle_type": "bike"}).json()["route"]

    response = client.post(f"/api/routes/{route['route_id']}/orders", json={"order_ids": ["b1", "b2", "b3", "b4"]})

    assert response.status_code == 409
    assert "available space" in response.json()["detail"]


def test_remove_from_nonexistent_route_returns_404():
    response = client.delete("/api/routes/999999/orders/whatever")
    assert response.status_code == 404


def test_bulk_assign_endpoint():
    batch_id = _upload_batch(["c1", "c2"])
    route = client.post("/api/routes", json={"vehicle_type": "car"}).json()["route"]

    response = client.post("/api/orders/assign", json={"order_ids": ["c1", "c2"], "route_id": route["route_id"]})

    assert response.status_code == 200
    assert response.json()["route"]["number_of_stops"] == 2


def test_search_filters_unassigned_orders_by_customer_name():
    _upload_batch(["d1"])

    response = client.get("/api/orders/unassigned", params={"search": "Customer d1"})

    assert response.status_code == 200
    assert response.json()["total"] == 1


def _mark_delivered_directly(route_id, order_id):
    from app.dependencies import get_db
    from app.models import RouteStop
    gen = app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        stop = db.query(RouteStop).filter(RouteStop.route_id == route_id, RouteStop.order_id == order_id).first()
        stop.delivery_status = "delivered"
        db.commit()
    finally:
        db.close()


def test_reorder_keeps_delivered_status_and_reopen_resets_it():
    _upload_batch(["r1", "r2"])
    route_id = client.post("/api/routes", json={"vehicle_type": "bike", "order_ids": ["r1", "r2"]}).json()["route"]["route_id"]
    _mark_delivered_directly(route_id, "r1")

    reordered = client.patch(f"/api/routes/{route_id}/reorder", json={"order_ids": ["r2", "r1"]}).json()["route"]
    by_id = {o["order_id"]: o for o in reordered["orders"]}
    assert by_id["r1"]["is_delivered"] is True
    assert by_id["r2"]["is_delivered"] is False

    reopened = client.post(f"/api/routes/{route_id}/orders/r1/reopen")
    assert reopened.status_code == 200
    assert all(not o["is_delivered"] for o in reopened.json()["route"]["orders"])

    assert client.post(f"/api/routes/{route_id}/orders/nope/reopen").status_code == 404
