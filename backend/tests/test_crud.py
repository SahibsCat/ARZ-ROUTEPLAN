from unittest import mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import crud
from app.database import Base


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


def test_save_upload_batch_persists_batch_and_orders(db_session):
    geocoded_orders = [
        {"order_id": "1", "customer_name": "Alice", "address": "1 Main", "delivery_time": "10:00", "lat": 13.0, "lng": 80.2, "geocoded_address": "1 Main, Chennai"},
        {"order_id": "2", "customer_name": "Bob", "address": "2 Main", "delivery_time": "11:00", "lat": None, "lng": None, "geocode_error": "failed"},
    ]

    batch = crud.save_upload_batch(db_session, "orders.xlsx", 2, True, [], geocoded_orders)

    assert batch.id is not None
    assert batch.file_name == "orders.xlsx"
    assert batch.total_orders == 2
    assert len(batch.orders) == 2
    assert batch.orders[0].order_id == "1"
    assert batch.orders[0].lat == 13.0
    assert batch.orders[0].status == "pending"
    assert batch.orders[1].geocode_error == "failed"
    assert batch.orders[1].status == "failed"
    assert batch.failed_orders_count == 1


def test_save_upload_batch_creates_failed_address_rows(db_session):
    geocoded_orders = [
        {"order_id": "2", "customer_name": "Bob", "address": "2 Main", "delivery_time": "11:00", "lat": None, "lng": None, "geocode_error": "failed"},
    ]

    batch = crud.save_upload_batch(db_session, "orders.xlsx", 1, True, [], geocoded_orders)

    failed = crud.list_failed_addresses(db_session, batch_id=batch.id)
    assert len(failed) == 1
    assert failed[0].order_id == "2"
    assert failed[0].status == "pending"
    assert failed[0].failure_reason == "failed"


def test_save_route_plan_persists_normalized_routes_and_stops(db_session):
    batch = crud.save_upload_batch(db_session, "orders.xlsx", 1, True, [], [
        {"order_id": "1", "customer_name": "Alice", "address": "1 Main", "delivery_time": "10:00", "lat": 13.0, "lng": 80.2},
    ])

    plan = {
        "route_count": 1,
        "routes": [{
            "route_name": "Route 1",
            "vehicle_type": "bike",
            "orders": [{"order_id": "1", "eta": "10:05", "is_late": False}],
            "route_distance_km": 2.0,
            "route_time_minutes": 5.0,
            "route_segments": [{"from": "Depot", "to": "1", "distance_km": 2.0, "time_minutes": 5.0}],
        }],
        "pending_orders": [],
        "warnings": [],
    }

    route_plan = crud.save_route_plan(db_session, batch.id, plan, available_cars=1, available_bikes=2)

    assert route_plan.id is not None
    assert route_plan.batch_id == batch.id
    assert route_plan.route_count == 1
    assert route_plan.available_cars == 1
    assert route_plan.available_bikes == 2
    assert len(route_plan.routes) == 1
    assert route_plan.routes[0].route_name == "Route 1"
    assert len(route_plan.routes[0].stops) == 1
    assert route_plan.routes[0].stops[0].order_id == "1"
    assert route_plan.routes[0].stops[0].travel_distance_km == 2.0

    # Order status/assigned_vehicle sync with the plan just generated.
    db_session.refresh(batch)
    assert batch.orders[0].status == "assigned"
    assert batch.orders[0].assigned_vehicle == "Route 1"


def test_save_route_plan_allows_no_batch(db_session):
    plan = {"route_count": 0, "routes": [], "pending_orders": [], "warnings": []}

    route_plan = crud.save_route_plan(db_session, None, plan, available_cars=0, available_bikes=0)

    assert route_plan.id is not None
    assert route_plan.batch_id is None


def test_list_upload_batches_orders_newest_first(db_session):
    first = crud.save_upload_batch(db_session, "first.xlsx", 1, True, [], [])
    second = crud.save_upload_batch(db_session, "second.xlsx", 1, True, [], [])

    batches = crud.list_upload_batches(db_session)

    assert [b.id for b in batches][:2] == [second.id, first.id]


def test_list_upload_batches_paginates(db_session):
    for i in range(5):
        crud.save_upload_batch(db_session, f"{i}.xlsx", 1, True, [], [])

    page = crud.list_upload_batches(db_session, limit=2, offset=0)
    assert len(page) == 2
    assert crud.count_upload_batches(db_session) == 5


def test_batch_summary_and_detail_shapes(db_session):
    batch = crud.save_upload_batch(db_session, "orders.xlsx", 1, True, [], [
        {"order_id": "1", "customer_name": "Alice", "address": "1 Main", "delivery_time": "10:00", "lat": 13.0, "lng": 80.2},
    ])
    crud.save_route_plan(db_session, batch.id, {"route_count": 1, "routes": [], "pending_orders": [], "warnings": []}, 1, 2)

    summary = crud.batch_summary(crud.get_upload_batch(db_session, batch.id))
    assert summary["route_plan_count"] == 1
    assert summary["total_orders"] == 1

    detail = crud.batch_detail(crud.get_upload_batch(db_session, batch.id))
    assert len(detail["orders"]) == 1
    assert len(detail["route_plans"]) == 1


def test_delete_upload_batch_cascades(db_session):
    batch = crud.save_upload_batch(db_session, "orders.xlsx", 1, True, [], [
        {"order_id": "1", "customer_name": "Alice", "address": "1 Main", "delivery_time": "10:00", "lat": None, "lng": None, "geocode_error": "bad"},
    ])
    batch_id = batch.id

    assert crud.delete_upload_batch(db_session, batch_id) is True
    assert crud.get_upload_batch(db_session, batch_id) is None
    assert crud.list_failed_addresses(db_session, batch_id=batch_id) == []
    assert crud.delete_upload_batch(db_session, batch_id) is False


def test_delete_upload_batch_removes_geocode_cache_no_longer_used(db_session):
    """An address used only by the deleted batch's orders is forgotten with
    it - no cached geocode should outlive every order that referenced it."""
    from app.geocode_service import _cache_key, clean_address
    from app.models import GeocodingCache

    address = "1 Main St, Chennai"
    key = _cache_key(clean_address(address))
    db_session.add(GeocodingCache(
        address_key=key, address=address, formatted_address=address,
        lat=13.0, lng=80.2, provider="google", confidence=0.95,
    ))
    db_session.commit()

    batch = crud.save_upload_batch(db_session, "orders.xlsx", 1, True, [], [
        {"order_id": "1", "customer_name": "Alice", "address": address, "delivery_time": "10:00", "lat": 13.0, "lng": 80.2, "geocoded_address": address},
    ])

    crud.delete_upload_batch(db_session, batch.id)

    assert crud.get_cached_geocode(db_session, key) is None


def test_delete_upload_batch_keeps_geocode_cache_still_used_by_another_batch(db_session):
    """The same address on a SECOND, still-existing batch must keep its
    cached geocode - deleting one batch must never break another's cache."""
    from app.geocode_service import _cache_key, clean_address
    from app.models import GeocodingCache

    address = "1 Main St, Chennai"
    key = _cache_key(clean_address(address))
    db_session.add(GeocodingCache(
        address_key=key, address=address, formatted_address=address,
        lat=13.0, lng=80.2, provider="google", confidence=0.95,
    ))
    db_session.commit()

    order_fields = {"customer_name": "Alice", "address": address, "delivery_time": "10:00", "lat": 13.0, "lng": 80.2, "geocoded_address": address}
    batch_one = crud.save_upload_batch(db_session, "orders1.xlsx", 1, True, [], [{"order_id": "1", **order_fields}])
    crud.save_upload_batch(db_session, "orders2.xlsx", 1, True, [], [{"order_id": "2", **order_fields}])

    crud.delete_upload_batch(db_session, batch_one.id)

    assert crud.get_cached_geocode(db_session, key) is not None


def _fake_geocode(lat=13.05, lng=80.24, display_name="42 Fake St, Adyar, Chennai, Tamil Nadu 600020, India"):
    return {"address": display_name, "lat": lat, "lng": lng, "display_name": display_name, "confidence": 0.9}


def test_add_manual_address_creates_new_route_when_nothing_matches(db_session):
    """No existing route at all - the fallback vehicle type is used."""
    batch = crud.save_upload_batch(db_session, "orders.xlsx", 0, True, [], [])

    with mock.patch("app.geocode_service.geocode_address", return_value=_fake_geocode()):
        result = crud.add_manual_address(
            db_session, batch.id,
            address="42 Fake St, Adyar, Chennai", customer_name="Walk-in Customer",
            delivery_time=None, fallback_vehicle_type="bike",
        )

    assert result["created_new_route"] is True
    assert result["matched_area"] == "Adyar"
    assert result["route"].vehicle_type == "bike"
    assert len(result["route"].stops) == 1
    assert result["route"].stops[0].order_id == result["order_id"]


def test_add_manual_address_joins_existing_route_in_the_same_area_with_room(db_session):
    """An existing route already serving Adyar, with room left - the new
    address should land there instead of spawning a new route."""
    batch = crud.save_upload_batch(db_session, "orders.xlsx", 1, True, [], [
        {"order_id": "1", "customer_name": "Alice", "address": "1 Main St, Adyar, Chennai", "delivery_time": "10:00", "lat": 13.0, "lng": 80.25, "geocoded_address": "1 Main St, Adyar, Chennai"},
    ])
    route_plan = crud.get_or_create_draft_route_plan(db_session, batch.id)
    existing_route = crud.create_route(db_session, route_plan.id, "car", order_ids=["1"])  # capacity 6, 1 used

    with mock.patch("app.geocode_service.geocode_address", return_value=_fake_geocode()):
        result = crud.add_manual_address(
            db_session, batch.id,
            address="42 Fake St, Adyar, Chennai", customer_name=None,
            delivery_time=None, fallback_vehicle_type="bike",
        )

    assert result["created_new_route"] is False
    assert result["route"].id == existing_route.id
    assert len(result["route"].stops) == 2


def test_add_manual_address_skips_a_full_same_area_route(db_session):
    """The same-area route exists but is already at capacity - a new route
    is created instead of overfilling it."""
    order_fields = [
        {"order_id": str(i), "customer_name": f"Cust {i}", "address": f"{i} Main St, Adyar, Chennai", "delivery_time": "10:00", "lat": 13.0, "lng": 80.25, "geocoded_address": f"{i} Main St, Adyar, Chennai"}
        for i in range(1, 4)
    ]
    batch = crud.save_upload_batch(db_session, "orders.xlsx", 3, True, [], order_fields)
    route_plan = crud.get_or_create_draft_route_plan(db_session, batch.id)
    full_route = crud.create_route(db_session, route_plan.id, "bike", order_ids=["1", "2", "3"])  # bike capacity 3, now full

    with mock.patch("app.geocode_service.geocode_address", return_value=_fake_geocode()):
        result = crud.add_manual_address(
            db_session, batch.id,
            address="42 Fake St, Adyar, Chennai", customer_name=None,
            delivery_time=None, fallback_vehicle_type="car",
        )

    assert result["created_new_route"] is True
    assert result["route"].id != full_route.id
    assert result["route"].vehicle_type == "car"


def test_add_manual_address_raises_on_failed_geocode(db_session):
    batch = crud.save_upload_batch(db_session, "orders.xlsx", 0, True, [], [])

    with mock.patch("app.geocode_service.geocode_address", return_value=None):
        with pytest.raises(crud.GeocodeFailedError):
            crud.add_manual_address(
                db_session, batch.id,
                address="not a real place", customer_name=None,
                delivery_time=None, fallback_vehicle_type="bike",
            )


def test_set_manual_location_marks_order_manually_verified(db_session):
    batch = crud.save_upload_batch(db_session, "orders.xlsx", 1, True, [], [
        {"order_id": "1", "customer_name": "Alice", "address": "1 Main, Chennai", "delivery_time": "10:00",
         "lat": 13.0, "lng": 80.2, "geocoded_address": "1 Main, Chennai", "confidence": 0.2},
    ])

    order = crud.set_manual_location(db_session, batch.id, "1", lat=13.05, lng=80.25)

    assert order.lat == 13.05
    assert order.lng == 80.25
    assert order.location_source == "manual"
    assert order.geocode_confidence == 1.0
    assert order.geocode_error is None


def test_set_manual_location_promotes_a_failed_order_to_pending(db_session):
    batch = crud.save_upload_batch(db_session, "orders.xlsx", 1, True, [], [
        {"order_id": "1", "customer_name": "Alice", "address": "bad address", "delivery_time": "10:00",
         "lat": None, "lng": None, "geocode_error": "Address could not be geocoded"},
    ])

    order = crud.set_manual_location(db_session, batch.id, "1", lat=13.05, lng=80.25)

    assert order.status == "pending"


def test_set_manual_location_patches_the_live_route_stop_snapshot(db_session):
    """The order is already on a route when corrected - the route's own
    stop snapshot (what the map/driver app actually reads) must reflect
    the corrected pin too, not just the underlying Order row."""
    batch = crud.save_upload_batch(db_session, "orders.xlsx", 1, True, [], [
        {"order_id": "1", "customer_name": "Alice", "address": "1 Main, Chennai", "delivery_time": "10:00",
         "lat": 13.0, "lng": 80.2, "geocoded_address": "1 Main, Chennai"},
    ])
    route_plan = crud.get_or_create_draft_route_plan(db_session, batch.id)
    route = crud.create_route(db_session, route_plan.id, "bike", order_ids=["1"])

    crud.set_manual_location(db_session, batch.id, "1", lat=13.05, lng=80.25)

    db_session.refresh(route)
    stop = route.stops[0]
    assert stop.order_snapshot["lat"] == 13.05
    assert stop.order_snapshot["lng"] == 80.25
    assert stop.order_snapshot["location_source"] == "manual"
    # Nothing else about the stop was disturbed - no route-metrics
    # recompute, no wholesale stop replacement (see the function's own
    # docstring for why that matters on a route a driver may be mid-
    # delivery on).
    assert stop.delivery_status == "pending"


def test_set_manual_location_raises_for_an_unknown_order(db_session):
    batch = crud.save_upload_batch(db_session, "orders.xlsx", 0, True, [], [])

    with pytest.raises(crud.OrderNotFoundError):
        crud.set_manual_location(db_session, batch.id, "does-not-exist", lat=13.0, lng=80.2)


def test_delete_route_plan(db_session):
    plan = crud.save_route_plan(db_session, None, {"route_count": 0, "routes": [], "pending_orders": [], "warnings": []}, 0, 0)
    assert crud.delete_route_plan(db_session, plan.id) is True
    assert crud.get_route_plan(db_session, plan.id) is None
    assert crud.delete_route_plan(db_session, plan.id) is False


def test_record_retry_attempt_resolves_failed_address_and_updates_order(db_session):
    batch = crud.save_upload_batch(db_session, "orders.xlsx", 1, True, [], [
        {"order_id": "1", "customer_name": "Alice", "address": "Bad Addr", "delivery_time": "10:00", "lat": None, "lng": None, "geocode_error": "not found"},
    ])

    crud.record_retry_attempt(db_session, batch.id, "1", "Fixed Addr, Chennai", success=True)

    failed = crud.get_failed_address(db_session, batch.id, "1")
    assert failed.status == "resolved"
    assert failed.retry_count == 1
    assert failed.edited_address == "Fixed Addr, Chennai"

    db_session.refresh(batch)
    order = batch.orders[0]
    assert order.status == "pending"
    assert order.address == "Fixed Addr, Chennai"
    assert order.geocode_error is None


def test_geocoding_cache_round_trips(db_session):
    assert crud.get_cached_geocode(db_session, "12 main st, chennai") is None

    crud.save_geocode_cache(
        db_session, "12 main st, chennai", "12 Main St, Chennai",
        "12 Main Street, Chennai, India", 13.0, 80.2, "google", None,
    )

    cached = crud.get_cached_geocode(db_session, "12 main st, chennai")
    assert cached is not None
    assert cached.lat == 13.0
    assert cached.lng == 80.2


def test_save_route_plan_replaces_prior_unsaved_draft_for_same_batch(db_session):
    batch = crud.save_upload_batch(db_session, "orders.xlsx", 1, True, [], [
        {"order_id": "1", "customer_name": "Alice", "address": "1 Main", "delivery_time": "10:00", "lat": 13.0, "lng": 80.2},
    ])
    plan = {"route_count": 0, "routes": [], "pending_orders": [], "warnings": []}

    crud.save_route_plan(db_session, batch.id, plan, 1, 2)
    crud.save_route_plan(db_session, batch.id, plan, 2, 1)

    # The first draft was replaced, not kept alongside the second - exactly
    # one row for this batch, and it reflects the *second* call's fleet
    # numbers, not the first's. (Deliberately not asserting on row ids: a
    # freshly-emptied SQLite table can reuse a deleted row's id for the next
    # insert, which would make an id-equality check pass or fail by
    # accident rather than by testing the real invariant.)
    remaining = db_session.query(crud.RoutePlan).filter(crud.RoutePlan.batch_id == batch.id).all()
    assert len(remaining) == 1
    assert remaining[0].available_cars == 2
    assert remaining[0].available_bikes == 1


def test_save_route_plan_never_replaces_an_already_saved_plan(db_session):
    batch = crud.save_upload_batch(db_session, "orders.xlsx", 1, True, [], [])
    plan = {"route_count": 0, "routes": [], "pending_orders": [], "warnings": []}

    saved_plan = crud.save_route_plan(db_session, batch.id, plan, 1, 2)
    crud.promote_route_plan_to_saved(db_session, saved_plan.id)

    new_draft = crud.save_route_plan(db_session, batch.id, plan, 3, 0)

    assert crud.get_route_plan(db_session, saved_plan.id) is not None
    assert crud.get_route_plan(db_session, new_draft.id) is not None
    remaining = db_session.query(crud.RoutePlan).filter(crud.RoutePlan.batch_id == batch.id).all()
    assert len(remaining) == 2


def test_promote_route_plan_to_saved_sets_default_label(db_session):
    batch = crud.save_upload_batch(db_session, "orders.xlsx", 1, True, [], [])
    plan = crud.save_route_plan(db_session, batch.id, {"route_count": 0, "routes": [], "pending_orders": [], "warnings": []}, 1, 2)

    promoted = crud.promote_route_plan_to_saved(db_session, plan.id)

    assert promoted.is_saved is True
    assert promoted.saved_at is not None
    assert "orders.xlsx" in promoted.label


def test_promote_route_plan_to_saved_with_custom_label(db_session):
    plan = crud.save_route_plan(db_session, None, {"route_count": 0, "routes": [], "pending_orders": [], "warnings": []}, 0, 0)

    promoted = crud.promote_route_plan_to_saved(db_session, plan.id, label="Monday morning run")

    assert promoted.label == "Monday morning run"


def test_promote_route_plan_to_saved_returns_none_for_unknown_id(db_session):
    assert crud.promote_route_plan_to_saved(db_session, 999999) is None


def test_list_saved_route_plans_only_returns_saved(db_session):
    batch = crud.save_upload_batch(db_session, "orders.xlsx", 1, True, [], [])
    plan_data = {"route_count": 0, "routes": [], "pending_orders": [], "warnings": []}

    draft = crud.save_route_plan(db_session, batch.id, plan_data, 1, 2)
    saved = crud.save_route_plan(db_session, None, plan_data, 1, 0)
    crud.promote_route_plan_to_saved(db_session, saved.id)

    results = crud.list_saved_route_plans(db_session)
    assert [p.id for p in results] == [saved.id]
    assert draft.id not in [p.id for p in results]
    assert crud.count_saved_route_plans(db_session) == 1


def test_route_plan_list_item_shape(db_session):
    plan = crud.save_route_plan(db_session, None, {
        "route_count": 1,
        "routes": [{
            "route_name": "Route 1", "vehicle_type": "bike",
            "orders": [{"order_id": "1"}, {"order_id": "2"}],
            "route_distance_km": 5.0, "route_time_minutes": 10.0,
        }],
        "pending_orders": [{"order_id": "3"}],
        "warnings": [],
    }, 1, 1)
    crud.promote_route_plan_to_saved(db_session, plan.id, label="Test run")

    item = crud.route_plan_list_item(crud.get_route_plan(db_session, plan.id))
    assert item["label"] == "Test run"
    assert item["route_count"] == 1
    assert item["total_stops"] == 2
    assert item["pending_count"] == 1
    assert item["total_distance_km"] == 5.0


def test_settings_defaults_and_update(db_session):
    settings = crud.get_settings(db_session)
    assert settings.default_car_count == 1
    assert settings.default_bike_count == 2

    updated = crud.update_settings(db_session, default_car_count=3, theme="dark")
    assert updated.default_car_count == 3
    assert updated.default_bike_count == 2
    assert updated.theme == "dark"


def test_save_upload_batch_sets_current_session(db_session):
    batch = crud.save_upload_batch(db_session, "orders.xlsx", 1, True, [], [])
    settings = crud.get_settings(db_session)
    assert settings.current_session_batch_id == batch.id
    assert settings.current_session_plan_id is None


def test_save_route_plan_sets_current_session(db_session):
    batch = crud.save_upload_batch(db_session, "orders.xlsx", 1, True, [], [])
    plan = crud.save_route_plan(db_session, batch.id, {"route_count": 0, "routes": [], "pending_orders": [], "warnings": []}, 1, 2)

    settings = crud.get_settings(db_session)
    assert settings.current_session_batch_id == batch.id
    assert settings.current_session_plan_id == plan.id


def test_deleting_current_session_route_plan_clears_the_pointer(db_session):
    batch = crud.save_upload_batch(db_session, "orders.xlsx", 1, True, [], [
        {"order_id": "1", "customer_name": "A", "address": "1 Main", "delivery_time": "10:00", "lat": 13.0, "lng": 80.2},
    ])
    plan = crud.save_route_plan(db_session, batch.id, {"route_count": 0, "routes": [], "pending_orders": [], "warnings": []}, 1, 2)

    crud.delete_route_plan(db_session, plan.id)

    settings = crud.get_settings(db_session)
    assert settings.current_session_batch_id is None
    assert settings.current_session_plan_id is None


def test_deleting_a_non_current_route_plan_leaves_the_pointer_alone(db_session):
    batch_a = crud.save_upload_batch(db_session, "a.xlsx", 1, True, [], [])
    plan_a = crud.save_route_plan(db_session, batch_a.id, {"route_count": 0, "routes": [], "pending_orders": [], "warnings": []}, 1, 0)
    crud.promote_route_plan_to_saved(db_session, plan_a.id)

    batch_b = crud.save_upload_batch(db_session, "b.xlsx", 1, True, [], [])
    plan_b = crud.save_route_plan(db_session, batch_b.id, {"route_count": 0, "routes": [], "pending_orders": [], "warnings": []}, 1, 0)

    # b is the current session now; deleting a's plan must not touch it.
    crud.delete_route_plan(db_session, plan_a.id)

    settings = crud.get_settings(db_session)
    assert settings.current_session_batch_id == batch_b.id
    assert settings.current_session_plan_id == plan_b.id


def test_deleting_upload_batch_directly_clears_matching_session_pointer(db_session):
    batch = crud.save_upload_batch(db_session, "orders.xlsx", 1, True, [], [])
    crud.save_route_plan(db_session, batch.id, {"route_count": 0, "routes": [], "pending_orders": [], "warnings": []}, 1, 0)

    crud.delete_upload_batch(db_session, batch.id)

    settings = crud.get_settings(db_session)
    assert settings.current_session_batch_id is None
    assert settings.current_session_plan_id is None
