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


def _orders(n):
    return [
        {
            "order_id": str(i), "customer_name": f"Customer {i}", "address": f"{i} Main St",
            "delivery_time": "18:00", "lat": 13.0 + i * 0.01, "lng": 80.2 + i * 0.01,
        }
        for i in range(1, n + 1)
    ]


def _batch(db_session, n=3):
    return crud.save_upload_batch(db_session, "orders.xlsx", n, True, [], _orders(n))


def test_create_route_starts_empty_and_numbers_sequentially(db_session):
    batch = _batch(db_session, 0)
    plan = crud.get_or_create_draft_route_plan(db_session, batch.id)

    route1 = crud.create_route(db_session, plan.id, "bike")
    route2 = crud.create_route(db_session, plan.id, "car")

    assert route1.route_name == "Route 1"
    assert route2.route_name == "Route 2"
    assert route1.vehicle_type == "bike"
    assert len(route1.stops) == 0


def test_create_route_rejects_invalid_vehicle_type(db_session):
    batch = _batch(db_session, 0)
    plan = crud.get_or_create_draft_route_plan(db_session, batch.id)

    with pytest.raises(crud.RootplanError):
        crud.create_route(db_session, plan.id, "truck")


def test_add_orders_to_route_moves_orders_out_of_unassigned_pool(db_session):
    batch = _batch(db_session, 2)
    plan = crud.get_or_create_draft_route_plan(db_session, batch.id)
    route = crud.create_route(db_session, plan.id, "bike")

    before, before_total = crud.list_unassigned_orders(db_session, batch.id)
    assert before_total == 2

    updated = crud.add_orders_to_route(db_session, route.id, ["1", "2"])

    assert updated.stops[0].order_id in {"1", "2"}
    assert len(updated.stops) == 2

    after, after_total = crud.list_unassigned_orders(db_session, batch.id)
    assert after_total == 0

    order1 = db_session.query(crud.Order).filter_by(batch_id=batch.id, order_id="1").first()
    assert order1.status == "assigned"
    assert order1.route_id == route.id
    assert order1.sequence_position == 1


def test_add_orders_to_route_enforces_bike_capacity(db_session):
    batch = _batch(db_session, 5)  # BIKE_CAPACITY == 3
    plan = crud.get_or_create_draft_route_plan(db_session, batch.id)
    route = crud.create_route(db_session, plan.id, "bike")

    with pytest.raises(crud.CapacityError):
        crud.add_orders_to_route(db_session, route.id, ["1", "2", "3", "4"])

    # Nothing was written - the failed batch must not partially apply.
    remaining, total = crud.list_unassigned_orders(db_session, batch.id)
    assert total == 5


def test_add_orders_to_route_rejects_unknown_order(db_session):
    batch = _batch(db_session, 1)
    plan = crud.get_or_create_draft_route_plan(db_session, batch.id)
    route = crud.create_route(db_session, plan.id, "bike")

    with pytest.raises(crud.OrderNotFoundError):
        crud.add_orders_to_route(db_session, route.id, ["does-not-exist"])


def test_remove_order_from_route_returns_it_to_unassigned_with_history(db_session):
    batch = _batch(db_session, 2)
    plan = crud.get_or_create_draft_route_plan(db_session, batch.id)
    route = crud.create_route(db_session, plan.id, "bike", order_ids=["1", "2"])

    result = crud.remove_order_from_route(db_session, route.id, "1")

    assert result["order"].status == "unassigned"
    assert result["order"].previous_route_name == route.route_name
    assert result["order"].previous_vehicle_type == "bike"
    assert result["order"].unassigned_at is not None
    assert result["order"].route_id is None
    assert len(result["route"].stops) == 1
    assert result["route"].stops[0].order_id == "2"
    assert result["route"].stops[0].sequence == 1  # re-sequenced, no gap

    _, total = crud.list_unassigned_orders(db_session, batch.id)
    assert total == 1


def test_remove_order_never_deletes_the_order_row(db_session):
    batch = _batch(db_session, 1)
    plan = crud.get_or_create_draft_route_plan(db_session, batch.id)
    route = crud.create_route(db_session, plan.id, "bike", order_ids=["1"])

    crud.remove_order_from_route(db_session, route.id, "1")

    order = db_session.query(crud.Order).filter_by(batch_id=batch.id, order_id="1").first()
    assert order is not None


def test_remove_order_not_on_route_raises(db_session):
    batch = _batch(db_session, 1)
    plan = crud.get_or_create_draft_route_plan(db_session, batch.id)
    route = crud.create_route(db_session, plan.id, "bike")

    with pytest.raises(crud.OrderNotFoundError):
        crud.remove_order_from_route(db_session, route.id, "1")


def test_reorder_route_persists_new_sequence(db_session):
    batch = _batch(db_session, 3)
    plan = crud.get_or_create_draft_route_plan(db_session, batch.id)
    route = crud.create_route(db_session, plan.id, "bike", order_ids=["1", "2", "3"])

    updated = crud.reorder_route(db_session, route.id, ["3", "1", "2"])

    ordered_ids = [stop.order_id for stop in sorted(updated.stops, key=lambda s: s.sequence)]
    assert ordered_ids == ["3", "1", "2"]

    order3 = db_session.query(crud.Order).filter_by(batch_id=batch.id, order_id="3").first()
    assert order3.sequence_position == 1


def test_reorder_route_rejects_mismatched_order_ids(db_session):
    batch = _batch(db_session, 2)
    plan = crud.get_or_create_draft_route_plan(db_session, batch.id)
    route = crud.create_route(db_session, plan.id, "bike", order_ids=["1", "2"])

    with pytest.raises(crud.RootplanError):
        crud.reorder_route(db_session, route.id, ["1", "999"])


def test_get_or_create_draft_route_plan_reuses_existing_draft(db_session):
    batch = _batch(db_session, 0)
    first = crud.get_or_create_draft_route_plan(db_session, batch.id)
    second = crud.get_or_create_draft_route_plan(db_session, batch.id)
    assert first.id == second.id


def test_delete_route_unassigns_its_orders_without_deleting_them(db_session):
    batch = _batch(db_session, 2)
    plan = crud.get_or_create_draft_route_plan(db_session, batch.id)
    route = crud.create_route(db_session, plan.id, "bike", order_ids=["1", "2"])

    freed = crud.delete_route(db_session, route.id)

    assert {o.order_id for o in freed} == {"1", "2"}
    assert all(o.status == "unassigned" for o in freed)
    assert all(o.previous_route_name == route.route_name for o in freed)
    assert db_session.query(crud.Route).filter_by(id=route.id).first() is None
    order1 = db_session.query(crud.Order).filter_by(batch_id=batch.id, order_id="1").first()
    assert order1 is not None  # never deleted


def test_delete_route_rejects_unknown_route(db_session):
    with pytest.raises(crud.RouteNotFoundError):
        crud.delete_route(db_session, 999999)


def test_derive_area_extracts_locality_from_comma_separated_address():
    assert crud.derive_area("12, 4th Main Road, Velachery, Chennai - 600042") == "Velachery"
    assert crud.derive_area("5th Street, Adambakkam, Chennai, Tamil Nadu 600088") == "Adambakkam"
    assert crud.derive_area("1 Main St") is None
    assert crud.derive_area("") is None
    assert crud.derive_area(None) is None


def test_derive_area_skips_building_and_landmark_descriptors():
    assert crud.derive_area("Adyar, Independent House, Chennai - 600020") == "Adyar"
    assert crud.derive_area("9th Cross Street, Adyar, near CSI St.Lukes church, Chennai - 600020") == "Adyar"
    assert crud.derive_area("RA Puram, Opp to CSI St.Lukes church, Chennai - 600028") == "RA Puram"


def test_order_summary_and_route_summary_include_area(db_session):
    batch = crud.save_upload_batch(db_session, "orders.xlsx", 1, True, [], [
        {"order_id": "1", "customer_name": "Alice", "address": "12, 4th Main Road, Velachery, Chennai - 600042", "delivery_time": "18:00", "lat": 13.0, "lng": 80.2},
    ])
    plan = crud.get_or_create_draft_route_plan(db_session, batch.id)
    route = crud.create_route(db_session, plan.id, "bike", order_ids=["1"])

    summary = crud.route_summary(route)
    assert summary["orders"][0]["area"] == "Velachery"
    assert summary["areas"] == ["Velachery"]


def test_change_route_vehicle_type_updates_capacity(db_session):
    batch = _batch(db_session, 2)
    plan = crud.get_or_create_draft_route_plan(db_session, batch.id)
    route = crud.create_route(db_session, plan.id, "bike", order_ids=["1", "2"])

    updated = crud.change_route_vehicle_type(db_session, route.id, "car")

    assert updated.vehicle_type == "car"
    order1 = db_session.query(crud.Order).filter_by(batch_id=batch.id, order_id="1").first()
    assert order1.route_id == route.id  # still on the route, just a different vehicle now


def test_change_route_vehicle_type_rejects_when_over_new_capacity(db_session):
    batch = _batch(db_session, 4)
    plan = crud.get_or_create_draft_route_plan(db_session, batch.id)
    route = crud.create_route(db_session, plan.id, "car", order_ids=["1", "2", "3", "4"])

    with pytest.raises(crud.RootplanError):
        crud.change_route_vehicle_type(db_session, route.id, "bike")  # BIKE_CAPACITY == 3


def test_list_unassigned_orders_search_filters_by_customer_name(db_session):
    batch = crud.save_upload_batch(db_session, "orders.xlsx", 2, True, [], [
        {"order_id": "1", "customer_name": "Alice Kumar", "address": "1 Main", "delivery_time": "18:00", "lat": 13.0, "lng": 80.2},
        {"order_id": "2", "customer_name": "Bob Rao", "address": "2 Main", "delivery_time": "18:00", "lat": 13.1, "lng": 80.3},
    ])

    rows, total = crud.list_unassigned_orders(db_session, batch.id, search="alice")

    assert total == 1
    assert rows[0].order_id == "1"
