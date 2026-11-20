"""Direction-aware route splitting: a rider should never be sent to the
opposite side of the depot between two stops that lie on the same line."""
from app import route_service as rs

DEPOT = {"lat": 12.99, "lng": 80.22}
KM = 1 / 111.0  # ~1 km in degrees of latitude


def _fake_travel(monkeypatch):
    def road(src, dst):
        km = rs._haversine_km(src, dst) * 1.3
        return {"distance_km": km, "time_minutes": km * 3}

    monkeypatch.setattr(rs, "_road_distance_time", road)
    monkeypatch.setattr(rs, "get_distances_from_point", lambda o, ds: [road(o, d) for d in ds])
    monkeypatch.setattr(rs, "prime_route_cache", lambda orders, depot: None)
    monkeypatch.setattr(rs, "VELOCHERY_DEPOT", DEPOT)


def _order(oid, north_km, east_km=0.0, slot="10:00"):
    return {"order_id": oid, "customer_name": oid, "address": oid, "delivery_time": slot,
            "lat": DEPOT["lat"] + north_km * KM, "lng": DEPOT["lng"] + east_km * KM}


def test_opposite_direction_stop_is_not_sandwiched_between_same_line_stops(monkeypatch):
    _fake_travel(monkeypatch)
    # Isolate stop ORDERING from route separation (which could otherwise
    # just give B its own vehicle and hide the ordering bug).
    monkeypatch.setattr(rs, "_extract_geographic_outliers", lambda v, d, s, c, t, sp, m: sp)
    # A and C are north on one line; B is south. Slots are close together
    # (and the original strict slot order would have forced A -> B -> C).
    orders = [_order("A", 3, slot="12:00"), _order("B", -2, slot="12:15"), _order("C", 5, slot="12:30")]
    vehicles, pending = rs.build_routes(orders, available_cars=1, available_bikes=0)
    assert not pending
    sequence = [o["order_id"] for v in vehicles for o in v["orders"]]
    assert sorted(sequence) == ["A", "B", "C"]
    a, b, c = sequence.index("A"), sequence.index("B"), sequence.index("C")
    assert not (min(a, c) < b < max(a, c)), sequence


def test_two_bikes_each_take_one_direction(monkeypatch):
    _fake_travel(monkeypatch)
    orders = [
        _order("N1", 3), _order("S1", -3), _order("N2", 5), _order("S2", -5),
        _order("N3", 6.5, 0.5), _order("S3", -6.5, -0.5),
    ]
    vehicles, pending = rs.build_routes(orders, available_cars=0, available_bikes=2)
    assert not pending
    for v in vehicles:
        ids = {o["order_id"][0] for o in v["orders"]}
        assert len(ids) <= 1, [o["order_id"] for o in v["orders"]]


def test_far_apart_slots_still_keep_their_order():
    early = {"delivery_time": "10:00"}
    late = {"delivery_time": "12:00"}
    close = {"delivery_time": "10:30"}
    assert rs._slot_order_preserved([early, late])
    assert not rs._slot_order_preserved([late, early])
    assert rs._slot_order_preserved([close, early])  # within tolerance
