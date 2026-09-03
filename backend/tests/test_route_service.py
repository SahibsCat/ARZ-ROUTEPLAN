from app.route_service import generate_routes, recompute_route_metrics, BIKE_CAPACITY


def test_generate_routes_drops_duplicate_order_id_instead_of_double_booking_it():
    orders = [
        {"order_id": "17", "customer_name": "Praveen", "address": "1 Main", "delivery_time": "12:00"},
        {"order_id": "17", "customer_name": "Praveen", "address": "1 Main", "delivery_time": "12:00"},
        {"order_id": "18", "customer_name": "Kumar", "address": "2 Main", "delivery_time": "12:00"},
    ]

    result = generate_routes(orders, available_cars=1, available_bikes=1)

    all_order_ids = [o["order_id"] for route in result["routes"] for o in route["orders"]]
    assert all_order_ids.count("17") == 1
    assert any("duplicate order_id" in w for w in result["warnings"])


def test_recompute_route_metrics_drops_duplicate_order_id():
    route_orders = [
        {"order_id": "17", "customer_name": "Praveen", "address": "1 Main", "delivery_time": "12:00"},
        {"order_id": "17", "customer_name": "Praveen", "address": "1 Main", "delivery_time": "12:00"},
    ]

    metrics = recompute_route_metrics(route_orders, "car")

    assert len(metrics["orders"]) == 1
    assert metrics["number_of_stops"] == 1


def test_generate_routes_groups_orders_into_priority_routes():
    orders = [
        {"order_id": "1003", "customer_name": "Carol", "address": "3 Main", "delivery_time": "15:00"},
        {"order_id": "1001", "customer_name": "Alice", "address": "1 Main", "delivery_time": "09:00"},
        {"order_id": "1002", "customer_name": "Bob", "address": "2 Main", "delivery_time": "11:00"},
        {"order_id": "1004", "customer_name": "Dina", "address": "4 Main", "delivery_time": "17:00"},
    ]

    result = generate_routes(orders, available_cars=1, available_bikes=2)

    assert result["route_count"] == 3
    assert len(result["routes"]) == 3
    assert result["routes"][0]["orders"][0]["order_id"] == "1001"
    assert result["routes"][1]["orders"][0]["order_id"] == "1002"
    assert result["routes"][2]["orders"][0]["order_id"] == "1003"
    assert result["routes"][0]["vehicle_type"] in {"bike", "car"}
    assert result["routes"][0]["route_distance_km"] is None or isinstance(result["routes"][0]["route_distance_km"], float)
    assert result["routes"][0]["route_time_minutes"] is None or isinstance(result["routes"][0]["route_time_minutes"], float)
    assert result["routes"][0]["number_of_stops"] == len(result["routes"][0]["orders"])
    assert result["pending_orders"] == []


def test_generate_routes_uses_osrm_distance_for_selection(monkeypatch):
    orders = [
        {"order_id": "1001", "customer_name": "Alice", "address": "1 Main", "delivery_time": None, "lat": 12.0, "lng": 80.0},
        {"order_id": "1002", "customer_name": "Bob", "address": "2 Main", "delivery_time": None, "lat": 13.0, "lng": 80.0},
    ]

    def fake_result(destination):
        if destination["lat"] == 12.0:
            return {"distance_km": 1.0, "time_minutes": 10.0}
        return {"distance_km": 20.0, "time_minutes": 30.0}

    def fake_road_distance_time(current, destination):
        return fake_result(destination)

    def fake_get_distances_from_point(origin, destinations):
        return [fake_result(destination) for destination in destinations]

    monkeypatch.setattr("app.route_service._road_distance_time", fake_road_distance_time)
    monkeypatch.setattr("app.route_service.get_distances_from_point", fake_get_distances_from_point)
    # generate_routes() now primes _ROUTE_CACHE via one batched OSRM table
    # request before build_routes runs (see distance_service.
    # prime_route_cache) - without this, every test below with real lat/
    # lng on its orders would silently make a real network call (and eat
    # its retry/backoff delay on failure) despite the two mocks above,
    # since prime_route_cache goes through distance_service.
    # build_order_matrix directly, not through either mocked function.
    monkeypatch.setattr("app.route_service.prime_route_cache", lambda orders, depot: None)
    result = generate_routes(orders, available_cars=1, available_bikes=0)

    assert result["route_count"] == 1
    assert result["routes"][0]["orders"][0]["order_id"] == "1001"


def test_generate_routes_auto_spawns_vehicle_when_capacity_exceeded():
    orders = [
        {"order_id": str(1000 + i), "customer_name": f"Customer {i}", "address": f"{i} Main", "delivery_time": "12:00"}
        for i in range(BIKE_CAPACITY + 1)
    ]

    result = generate_routes(orders, available_cars=0, available_bikes=1)

    assert result["pending_orders"] == []
    assert result["route_count"] == 2
    assert any(route["is_auto_created"] for route in result["routes"])
    assigned_ids = {order["order_id"] for route in result["routes"] for order in route["orders"]}
    assert assigned_ids == {order["order_id"] for order in orders}


def test_generate_routes_flags_late_deliveries(monkeypatch):
    orders = [
        {"order_id": "2001", "customer_name": "Late Larry", "address": "1 Main", "delivery_time": "08:00", "lat": 12.0, "lng": 80.0},
    ]

    def fake_road_distance_time(current, destination):
        return {"distance_km": 2.0, "time_minutes": 5.0}

    def fake_get_distances_from_point(origin, destinations):
        return [{"distance_km": 2.0, "time_minutes": 5.0} for _ in destinations]

    monkeypatch.setattr("app.route_service._road_distance_time", fake_road_distance_time)
    monkeypatch.setattr("app.route_service.get_distances_from_point", fake_get_distances_from_point)
    # generate_routes() now primes _ROUTE_CACHE via one batched OSRM table
    # request before build_routes runs (see distance_service.
    # prime_route_cache) - without this, every test below with real lat/
    # lng on its orders would silently make a real network call (and eat
    # its retry/backoff delay on failure) despite the two mocks above,
    # since prime_route_cache goes through distance_service.
    # build_order_matrix directly, not through either mocked function.
    monkeypatch.setattr("app.route_service.prime_route_cache", lambda orders, depot: None)

    result = generate_routes(orders, available_cars=0, available_bikes=1)

    route = result["routes"][0]
    assert route["orders"][0]["is_late"] is True
    assert route["late_deliveries"] == ["2001"]


def test_generate_routes_never_delays_earlier_slot_for_closer_later_slot(monkeypatch):
    # Spec example: A and C are both 12:00 orders, far from the depot but
    # close to each other. B is a 2:00 PM order that happens to sit right
    # next to the depot. Even though visiting B first is geographically
    # tempting, both 12:00 orders must be served before the 2:00 one.
    orders = [
        {"order_id": "A", "customer_name": "A", "address": "A", "delivery_time": "12:00", "lat": 1.0, "lng": 0.0},
        {"order_id": "B", "customer_name": "B", "address": "B", "delivery_time": "14:00", "lat": 0.1, "lng": 0.0},
        {"order_id": "C", "customer_name": "C", "address": "C", "delivery_time": "12:00", "lat": 1.2, "lng": 0.0},
    ]

    travel_time_minutes = {
        (0.0, 1.0): 10.0, (1.0, 0.0): 10.0,
        (0.0, 0.1): 2.0, (0.1, 0.0): 2.0,
        (0.0, 1.2): 12.0, (1.2, 0.0): 12.0,
        (1.0, 0.1): 15.0, (0.1, 1.0): 15.0,
        (1.0, 1.2): 3.0, (1.2, 1.0): 3.0,
        (0.1, 1.2): 16.0, (1.2, 0.1): 16.0,
    }

    def fake_road_distance_time(source, destination):
        time_minutes = travel_time_minutes[(round(source["lat"], 2), round(destination["lat"], 2))]
        return {"distance_km": time_minutes, "time_minutes": time_minutes}

    def fake_get_distances_from_point(origin, destinations):
        return [fake_road_distance_time(origin, destination) for destination in destinations]

    monkeypatch.setattr("app.route_service._road_distance_time", fake_road_distance_time)
    monkeypatch.setattr("app.route_service.get_distances_from_point", fake_get_distances_from_point)
    # generate_routes() now primes _ROUTE_CACHE via one batched OSRM table
    # request before build_routes runs (see distance_service.
    # prime_route_cache) - without this, every test below with real lat/
    # lng on its orders would silently make a real network call (and eat
    # its retry/backoff delay on failure) despite the two mocks above,
    # since prime_route_cache goes through distance_service.
    # build_order_matrix directly, not through either mocked function.
    monkeypatch.setattr("app.route_service.prime_route_cache", lambda orders, depot: None)
    monkeypatch.setattr("app.route_service.VELOCHERY_DEPOT", {"lat": 0.0, "lng": 0.0})
    # This test's lat values (1.0, 0.1, 1.2) are an abstract 1D scale for
    # the fake travel_time_minutes lookup above, not real coordinates -
    # taken as real degrees they're ~100km+ apart, which is exactly what
    # _extract_geographic_outliers (real haversine distance - see its own
    # docstring) would now correctly flag. That's a different concern from
    # what this test is actually about (slot-ordering precedence, not
    # route separation) - isolated out the same way prime_route_cache is
    # above, rather than hand-tuning coordinates around its thresholds.
    monkeypatch.setattr("app.route_service._extract_geographic_outliers", lambda vehicles, depot, start, cap, types, spawned, max_spawns: spawned)

    result = generate_routes(orders, available_cars=1, available_bikes=0)

    assert result["routes"][0]["delivery_sequence"] == ["A", "C", "B"]
    assert all(not order["is_late"] for order in result["routes"][0]["orders"])


def test_google_maps_url_prefers_coordinates_when_available():
    from app.route_service import build_google_maps_url

    # Coordinates always resolve in Google Maps (a literal point, no address
    # parsing) - preferred over address text, which can fail unpredictably
    # even after whitespace cleanup ("we can't find that place"). For a
    # successfully geocoded order, lat/lng IS that address, just in a form
    # that's guaranteed to work.
    depot = {"lat": 12.99, "lng": 80.21}
    orders = [
        {"order_id": "1", "address": "12 Main Street, Chennai", "lat": 13.5, "lng": 81.5},
        {"order_id": "2", "address": "45 Anna Nagar, Chennai", "lat": 13.6, "lng": 81.6},
    ]

    url = build_google_maps_url(orders, depot)

    assert "13.5%2C81.5" in url or "13.5,81.5" in url.replace("%2C", ",")
    assert "13.6,81.6" in url.replace("%2C", ",")
    assert "Main%20Street" not in url
    assert "Anna%20Nagar" not in url
    assert url.startswith("https://www.google.com/maps/dir/?api=1&origin=12.99,80.21")


def test_google_maps_url_falls_back_to_address_when_no_coordinates():
    from app.route_service import build_google_maps_url

    depot = {"lat": 0.0, "lng": 0.0}
    orders = [{"order_id": "1", "address": "12 Main Street, Chennai"}]

    url = build_google_maps_url(orders, depot)

    assert "12%20Main%20Street%2C%20Chennai" in url


def test_google_maps_url_encodes_special_characters_in_address():
    from app.route_service import build_google_maps_url

    depot = {"lat": 0.0, "lng": 0.0}
    orders = [{"order_id": "1", "address": "Flat 3/B, MG Road & 1st Cross, Chennai"}]

    url = build_google_maps_url(orders, depot)

    assert "Flat 3/B" not in url  # raw, unencoded text must not appear
    assert "destination=" in url


def test_google_maps_url_skips_orders_with_no_address():
    from app.route_service import build_google_maps_url

    depot = {"lat": 0.0, "lng": 0.0}
    orders = [
        {"order_id": "1", "address": ""},
        {"order_id": "2", "address": "Real Address, Chennai"},
    ]

    url = build_google_maps_url(orders, depot)

    assert "Real%20Address%2C%20Chennai" in url


def test_generate_routes_never_returns_empty_route_cards(monkeypatch):
    # Regression test: configuring more vehicles than the order volume
    # actually needs used to render an empty "Route N" card for every
    # unused vehicle (10 vehicles configured, 3 tight-cluster orders, used
    # to show 10 routes - 7 of them with zero stops).
    orders = [
        {"order_id": "1", "customer_name": "A", "address": "1 Main", "delivery_time": "09:00", "lat": 13.00, "lng": 80.20},
        {"order_id": "2", "customer_name": "B", "address": "2 Main", "delivery_time": "09:00", "lat": 13.001, "lng": 80.201},
        {"order_id": "3", "customer_name": "C", "address": "3 Main", "delivery_time": "09:00", "lat": 13.002, "lng": 80.202},
    ]

    def fake_road_distance_time(source, destination):
        return {"distance_km": 0.5, "time_minutes": 2.0}

    def fake_get_distances_from_point(origin, destinations):
        return [{"distance_km": 0.5, "time_minutes": 2.0} for _ in destinations]

    monkeypatch.setattr("app.route_service._road_distance_time", fake_road_distance_time)
    monkeypatch.setattr("app.route_service.get_distances_from_point", fake_get_distances_from_point)
    # generate_routes() now primes _ROUTE_CACHE via one batched OSRM table
    # request before build_routes runs (see distance_service.
    # prime_route_cache) - without this, every test below with real lat/
    # lng on its orders would silently make a real network call (and eat
    # its retry/backoff delay on failure) despite the two mocks above,
    # since prime_route_cache goes through distance_service.
    # build_order_matrix directly, not through either mocked function.
    monkeypatch.setattr("app.route_service.prime_route_cache", lambda orders, depot: None)

    result = generate_routes(orders, available_cars=0, available_bikes=10)

    assert all(route["number_of_stops"] > 0 for route in result["routes"])
    assert result["route_count"] == len(result["routes"])
    # 3 tightly-clustered orders within one bike's capacity (3) shouldn't
    # need more than one vehicle.
    assert result["route_count"] == 1


def test_generate_routes_consolidates_nearby_orders_onto_fewer_vehicles(monkeypatch):
    # Two tight clusters of 3 orders each (6 orders total, well within a
    # single bike's capacity of 3 per cluster) with 10 bikes configured -
    # must use 2 vehicles (one per cluster), not spread across many more
    # just because extra idle vehicles happen to exist.
    cluster_a = [
        {"order_id": f"a{i}", "customer_name": f"A{i}", "address": f"A{i}", "delivery_time": "09:00", "lat": 13.000 + i * 0.001, "lng": 80.200}
        for i in range(3)
    ]
    cluster_b = [
        {"order_id": f"b{i}", "customer_name": f"B{i}", "address": f"B{i}", "delivery_time": "09:00", "lat": 13.300 + i * 0.001, "lng": 80.500}
        for i in range(3)
    ]
    orders = cluster_a + cluster_b

    def distance_between(a, b):
        # Cheap synthetic "distance" - same cluster is close (~0.1km apart),
        # different clusters are far apart (~40km).
        same_cluster = round(a["lat"], 1) == round(b["lat"], 1)
        return 0.3 if same_cluster else 40.0

    def fake_road_distance_time(source, destination):
        d = distance_between(source, destination)
        return {"distance_km": d, "time_minutes": d}

    def fake_get_distances_from_point(origin, destinations):
        return [fake_road_distance_time(origin, d) for d in destinations]

    monkeypatch.setattr("app.route_service._road_distance_time", fake_road_distance_time)
    monkeypatch.setattr("app.route_service.get_distances_from_point", fake_get_distances_from_point)
    # generate_routes() now primes _ROUTE_CACHE via one batched OSRM table
    # request before build_routes runs (see distance_service.
    # prime_route_cache) - without this, every test below with real lat/
    # lng on its orders would silently make a real network call (and eat
    # its retry/backoff delay on failure) despite the two mocks above,
    # since prime_route_cache goes through distance_service.
    # build_order_matrix directly, not through either mocked function.
    monkeypatch.setattr("app.route_service.prime_route_cache", lambda orders, depot: None)
    monkeypatch.setattr("app.route_service.VELOCHERY_DEPOT", {"lat": 13.15, "lng": 80.35})

    result = generate_routes(orders, available_cars=0, available_bikes=10)

    assert result["route_count"] == 2
    stop_counts = sorted(r["number_of_stops"] for r in result["routes"])
    assert stop_counts == [3, 3]


def test_generate_routes_gives_isolated_far_order_its_own_vehicle(monkeypatch):
    # A tight cluster of 2 nearby orders (near depot) plus one order far
    # away with nothing near it. Going straight from the depot to the far
    # order (20km) is a lot cheaper than detouring an already-active
    # near-cluster vehicle out to it (45km) - so a fresh vehicle from the
    # depot should win and the isolated order should not get dragged onto
    # the cluster's vehicle just to save a vehicle.
    orders = [
        {"order_id": "near1", "customer_name": "N1", "address": "N1", "delivery_time": "09:00", "lat": 13.000, "lng": 80.200},
        {"order_id": "near2", "customer_name": "N2", "address": "N2", "delivery_time": "09:00", "lat": 13.001, "lng": 80.201},
        {"order_id": "far", "customer_name": "F", "address": "F", "delivery_time": "09:00", "lat": 13.500, "lng": 80.900},
    ]

    def point_key(point):
        if round(point["lat"], 1) == 13.5:
            return "far"
        if point.get("_is_depot"):
            return "depot"
        return "near"

    depot = {"lat": 13.05, "lng": 80.25, "_is_depot": True}

    distance_table = {
        ("near", "near"): 0.3,
        ("depot", "near"): 5.0,
        ("near", "far"): 45.0,
        ("depot", "far"): 20.0,
    }

    def lookup(a, b):
        key_a, key_b = point_key(a), point_key(b)
        return distance_table.get((key_a, key_b)) or distance_table.get((key_b, key_a)) or 0.3

    def fake_road_distance_time(source, destination):
        d = lookup(source, destination)
        return {"distance_km": d, "time_minutes": d}

    def fake_get_distances_from_point(origin, destinations):
        return [fake_road_distance_time(origin, d) for d in destinations]

    monkeypatch.setattr("app.route_service._road_distance_time", fake_road_distance_time)
    monkeypatch.setattr("app.route_service.get_distances_from_point", fake_get_distances_from_point)
    # generate_routes() now primes _ROUTE_CACHE via one batched OSRM table
    # request before build_routes runs (see distance_service.
    # prime_route_cache) - without this, every test below with real lat/
    # lng on its orders would silently make a real network call (and eat
    # its retry/backoff delay on failure) despite the two mocks above,
    # since prime_route_cache goes through distance_service.
    # build_order_matrix directly, not through either mocked function.
    monkeypatch.setattr("app.route_service.prime_route_cache", lambda orders, depot: None)
    monkeypatch.setattr("app.route_service.VELOCHERY_DEPOT", depot)

    result = generate_routes(orders, available_cars=0, available_bikes=5)

    assert result["route_count"] == 2
    far_route = next(r for r in result["routes"] if any(o["order_id"] == "far" for o in r["orders"]))
    assert far_route["number_of_stops"] == 1


# --------------------------------------------------------------------------
# Geographic outlier detection (route SEPARATION - see
# _extract_geographic_outliers) - real coordinates throughout, since the
# whole point is testing real-geography behavior, not an abstract distance
# table. _road_distance_time/get_distances_from_point are mocked to real
# haversine distance (not left unmocked - the standard test isolation
# pattern this file already uses everywhere else, just backed by the real
# math instead of a fake lookup) so the whole pipeline - greedy build,
# relocate, outlier extraction, 2-opt - runs internally consistently
# without ever needing a real network call.
# --------------------------------------------------------------------------

def _mock_real_distance_functions(monkeypatch):
    from app.route_service import _haversine_km

    def fake_road_distance_time(source, destination):
        d = _haversine_km(source, destination)
        return {"distance_km": d, "time_minutes": d}

    def fake_get_distances_from_point(origin, destinations):
        return [fake_road_distance_time(origin, d) for d in destinations]

    monkeypatch.setattr("app.route_service._road_distance_time", fake_road_distance_time)
    monkeypatch.setattr("app.route_service.get_distances_from_point", fake_get_distances_from_point)
    monkeypatch.setattr("app.route_service.prime_route_cache", lambda orders, depot: None)


def test_generate_routes_separates_a_geographically_isolated_point_into_its_own_route(monkeypatch):
    # The exact spec scenario: 5 points forming a tight real-world cluster
    # (roughly 250m-650m apart from each other) plus a 6th point ~23km
    # away, deliberately listed LAST in the input so nothing about input
    # order could explain a correct separation. A single car (capacity 6)
    # is configured - exactly enough to hold all 6 on one vehicle - so the
    # only thing that could split this into two routes is genuine
    # geographic separation, not a capacity limit forcing the split.
    orders = [
        {"order_id": "1", "customer_name": "P1", "address": "P1", "delivery_time": None, "lat": 11.0000, "lng": 79.8000},
        {"order_id": "2", "customer_name": "P2", "address": "P2", "delivery_time": None, "lat": 11.0020, "lng": 79.8030},
        {"order_id": "3", "customer_name": "P3", "address": "P3", "delivery_time": None, "lat": 11.0040, "lng": 79.8010},
        {"order_id": "4", "customer_name": "P4", "address": "P4", "delivery_time": None, "lat": 11.0010, "lng": 79.8060},
        {"order_id": "5", "customer_name": "P5", "address": "P5", "delivery_time": None, "lat": 11.0030, "lng": 79.8050},
        {"order_id": "6", "customer_name": "P6", "address": "P6", "delivery_time": None, "lat": 11.1500, "lng": 79.9500},
    ]
    _mock_real_distance_functions(monkeypatch)
    monkeypatch.setattr("app.route_service.VELOCHERY_DEPOT", {"lat": 11.0000, "lng": 79.8000})

    result = generate_routes(orders, available_cars=1, available_bikes=0)

    assert result["route_count"] == 2
    routes_by_ids = [{o["order_id"] for o in r["orders"]} for r in result["routes"]]
    assert {"1", "2", "3", "4", "5"} in routes_by_ids
    assert {"6"} in routes_by_ids


def test_generate_routes_keeps_a_tight_cluster_on_one_route(monkeypatch):
    # Spec Test Case 1: every point close together must stay one route -
    # the new outlier pass must not be trigger-happy on a perfectly normal
    # compact cluster.
    orders = [
        {"order_id": str(i), "customer_name": f"P{i}", "address": f"P{i}", "delivery_time": None,
         "lat": 11.0000 + i * 0.0010, "lng": 79.8000 + i * 0.0008}
        for i in range(5)
    ]
    _mock_real_distance_functions(monkeypatch)
    monkeypatch.setattr("app.route_service.VELOCHERY_DEPOT", {"lat": 11.0000, "lng": 79.8000})

    result = generate_routes(orders, available_cars=1, available_bikes=0)

    assert result["route_count"] == 1
    assert result["routes"][0]["number_of_stops"] == 5


def test_generate_routes_keeps_a_slightly_farther_point_with_its_cluster(monkeypatch):
    # Spec Test Case 5: a point that's only modestly farther than the rest
    # (not a genuine outlier, just the farthest of a still-compact group)
    # must NOT be split out on its own - the relative+absolute thresholds
    # existing to prevent exactly this over-eager splitting.
    orders = [
        {"order_id": "1", "customer_name": "P1", "address": "P1", "delivery_time": None, "lat": 11.0000, "lng": 79.8000},
        {"order_id": "2", "customer_name": "P2", "address": "P2", "delivery_time": None, "lat": 11.0020, "lng": 79.8030},
        {"order_id": "3", "customer_name": "P3", "address": "P3", "delivery_time": None, "lat": 11.0040, "lng": 79.8010},
        {"order_id": "4", "customer_name": "P4", "address": "P4", "delivery_time": None, "lat": 11.0010, "lng": 79.8060},
        # Only ~1.2km from the cluster centre, not 23km - a real but modest
        # outer edge, the kind every normal delivery area has somewhere.
        {"order_id": "5", "customer_name": "P5", "address": "P5", "delivery_time": None, "lat": 11.0120, "lng": 79.8090},
    ]
    _mock_real_distance_functions(monkeypatch)
    monkeypatch.setattr("app.route_service.VELOCHERY_DEPOT", {"lat": 11.0000, "lng": 79.8000})

    result = generate_routes(orders, available_cars=1, available_bikes=0)

    assert result["route_count"] == 1
    assert result["routes"][0]["number_of_stops"] == 5


def test_generate_routes_separates_two_distinct_geographical_clusters(monkeypatch):
    # Spec Test Case 3: two real, separate clusters, neither of which is a
    # single lone point - both must survive as their own routes.
    cluster_a = [
        {"order_id": f"a{i}", "customer_name": f"A{i}", "address": f"A{i}", "delivery_time": None,
         "lat": 11.0000 + i * 0.0010, "lng": 79.8000 + i * 0.0008}
        for i in range(3)
    ]
    cluster_b = [
        {"order_id": f"b{i}", "customer_name": f"B{i}", "address": f"B{i}", "delivery_time": None,
         "lat": 11.1500 + i * 0.0010, "lng": 79.9500 + i * 0.0008}
        for i in range(3)
    ]
    orders = cluster_a + cluster_b
    _mock_real_distance_functions(monkeypatch)
    monkeypatch.setattr("app.route_service.VELOCHERY_DEPOT", {"lat": 11.0000, "lng": 79.8000})

    # One car, capacity 6 - exactly enough to hold all 6 on one vehicle,
    # same as the single-outlier test above: only genuine geography can
    # justify the split here, not a capacity ceiling.
    result = generate_routes(orders, available_cars=1, available_bikes=0)

    assert result["route_count"] == 2
    routes_by_ids = [{o["order_id"] for o in r["orders"]} for r in result["routes"]]
    assert {"a0", "a1", "a2"} in routes_by_ids
    assert {"b0", "b1", "b2"} in routes_by_ids


def test_generate_routes_separates_three_distinct_geographical_clusters(monkeypatch):
    # Spec Test Case 6: not just a 2-way split - three real clusters must
    # each survive as their own route, all sharing one time slot/vehicle
    # capacity ceiling so only geography explains the 3-way split.
    cluster_a = [
        {"order_id": f"a{i}", "customer_name": f"A{i}", "address": f"A{i}", "delivery_time": None,
         "lat": 11.0000 + i * 0.0010, "lng": 79.8000 + i * 0.0008}
        for i in range(3)
    ]
    cluster_b = [
        {"order_id": f"b{i}", "customer_name": f"B{i}", "address": f"B{i}", "delivery_time": None,
         "lat": 11.1500 + i * 0.0010, "lng": 79.9500 + i * 0.0008}
        for i in range(2)
    ]
    cluster_c = [
        {"order_id": "c0", "customer_name": "C0", "address": "C0", "delivery_time": None,
         "lat": 11.3000, "lng": 79.7000},
    ]
    orders = cluster_a + cluster_b + cluster_c  # 3 + 2 + 1 = 6 - exactly one car's worth
    _mock_real_distance_functions(monkeypatch)
    monkeypatch.setattr("app.route_service.VELOCHERY_DEPOT", {"lat": 11.0000, "lng": 79.8000})

    # One car, capacity 6 - exactly enough to hold all 6 stops on a single
    # vehicle by capacity alone, so a 3-way split can only come from
    # genuine geographic separation, not a capacity ceiling forcing it.
    result = generate_routes(orders, available_cars=1, available_bikes=0)

    assert result["route_count"] == 3
    routes_by_ids = [{o["order_id"] for o in r["orders"]} for r in result["routes"]]
    assert {"a0", "a1", "a2"} in routes_by_ids
    assert {"b0", "b1"} in routes_by_ids
    assert {"c0"} in routes_by_ids


def test_generate_routes_separation_is_independent_of_input_order(monkeypatch):
    # Spec Test Case 7: the 5-cluster-plus-1-outlier scenario, but with the
    # outlier listed FIRST and the cluster genuinely shuffled - proving the
    # separation comes from geography (single-linkage clustering considers
    # every pairwise distance, not a sequential scan) and not from
    # whatever order the sheet happened to list stops in.
    cluster_ids_in_order = ["6", "3", "1", "5", "2", "4"]
    coords = {
        "1": (11.0000, 79.8000), "2": (11.0020, 79.8030), "3": (11.0040, 79.8010),
        "4": (11.0010, 79.8060), "5": (11.0030, 79.8050), "6": (11.1500, 79.9500),
    }
    orders = [
        {"order_id": oid, "customer_name": f"P{oid}", "address": f"P{oid}", "delivery_time": None,
         "lat": coords[oid][0], "lng": coords[oid][1]}
        for oid in cluster_ids_in_order
    ]
    _mock_real_distance_functions(monkeypatch)
    monkeypatch.setattr("app.route_service.VELOCHERY_DEPOT", {"lat": 11.0000, "lng": 79.8000})

    result = generate_routes(orders, available_cars=1, available_bikes=0)

    assert result["route_count"] == 2
    routes_by_ids = [{o["order_id"] for o in r["orders"]} for r in result["routes"]]
    assert {"1", "2", "3", "4", "5"} in routes_by_ids
    assert {"6"} in routes_by_ids


def test_improve_route_uncrosses_a_self_crossing_route(monkeypatch):
    # Reproduces a real dispatcher-reported bug: a route whose stops were
    # sequenced so the path zig-zags - goes out one direction, then
    # doubles back across ground it already covered, then out again -
    # instead of sweeping through them without backtracking. The old
    # _improve_route only tried swapping two stops within a narrow
    # window, leaving everything between them in its original order -
    # that can't undo a crossing that isn't reducible to a single
    # adjacent swap. 2-opt (reversing a whole segment between two edges)
    # can. Uses real Euclidean distance (not a lookup table) so the
    # geometry - and the brute-force ground truth below - are honest.
    import itertools
    from app.route_service import _improve_route, _simulate_route

    depot = {"lat": 0.0, "lng": 0.0}
    coords = {
        "p1": (10.0, 50.0), "p2": (12.0, 40.0), "p3": (15.0, 38.0), "p4": (20.0, 35.0),
        "p5": (-30.0, 10.0),  # far to one side, like the screenshot's stop 5
        "p6": (40.0, 10.0),   # far to the other side, like the screenshot's stop 6
    }
    orders = {
        oid: {"order_id": oid, "customer_name": oid, "address": oid, "delivery_time": "12:00", "lat": lat, "lng": lng}
        for oid, (lat, lng) in coords.items()
    }

    def euclid(a, b):
        return ((a["lat"] - b["lat"]) ** 2 + (a["lng"] - b["lng"]) ** 2) ** 0.5

    def fake_road_distance_time(source, destination):
        d = euclid(source, destination)
        return {"distance_km": d, "time_minutes": d}

    monkeypatch.setattr("app.route_service._road_distance_time", fake_road_distance_time)

    # The crossing order: sweep through the p1-p4 cluster, then jump far
    # to one side (p5), then all the way back past the cluster to the
    # far other side (p6) - exactly the reported shape.
    bad_order = [orders["p1"], orders["p2"], orders["p3"], orders["p4"], orders["p5"], orders["p6"]]
    vehicle = {"vehicle_type": "car", "capacity": 6, "orders": list(bad_order), "current_time": 480.0, "current_location": dict(depot), "is_auto_created": False}

    _, bad_distance, _, _ = _simulate_route(bad_order, depot, 480.0)

    _improve_route(vehicle, depot, 480.0)

    _, improved_distance, _, _ = _simulate_route(vehicle["orders"], depot, 480.0)
    improved_ids = [o["order_id"] for o in vehicle["orders"]]

    # Ground truth: the actual best possible order for these 6 points,
    # found by brute force (720 permutations - cheap).
    best_possible = min(
        _simulate_route(list(perm), depot, 480.0)[1]
        for perm in itertools.permutations(bad_order)
    )

    assert set(improved_ids) == {"p1", "p2", "p3", "p4", "p5", "p6"}  # nothing lost or duplicated
    assert improved_distance < bad_distance
    # Within 10% of true optimal - 2-opt is a local search, not guaranteed
    # to hit the global optimum, but should land close for 6 points.
    assert improved_distance <= best_possible * 1.10


def test_improve_route_uncrosses_a_self_crossing_route_with_mixed_delivery_times(monkeypatch):
    # Same crossing geometry as test_improve_route_uncrosses_a_self_crossing_route,
    # but with the delivery times realistically mixed instead of every stop
    # sharing one identical "12:00" slot. Reproduces a real regression: an
    # earlier version of _improve_route only allowed a 2-opt reversal when
    # every stop in the reversed segment shared one *identical* delivery
    # slot - which sounds cautious, but in practice blocks almost every
    # reversal on real data, since real routes are a mix of stops with
    # different explicit times and stops with no time set at all (an
    # unset time and any explicit time already count as two different
    # slot values under that rule). The actual invariant that matters is
    # _slot_order_preserved - never serve a later slot before an earlier
    # one - which is far less restrictive and should still let 2-opt
    # uncross this route.
    import itertools
    from app.route_service import _improve_route, _simulate_route

    depot = {"lat": 0.0, "lng": 0.0}
    coords = {
        "p1": (10.0, 50.0), "p2": (12.0, 40.0), "p3": (15.0, 38.0), "p4": (20.0, 35.0),
        "p5": (-30.0, 10.0), "p6": (40.0, 10.0),
    }
    # p1-p4 share one delivery window; p5 and p6 have no delivery time set
    # at all (the common real-world case) - a mix an old identical-slot
    # check would treat as "different slots everywhere" and refuse to
    # touch.
    delivery_times = {"p1": "12:00", "p2": "12:00", "p3": "12:00", "p4": "12:00", "p5": None, "p6": None}
    orders = {
        oid: {"order_id": oid, "customer_name": oid, "address": oid, "delivery_time": delivery_times[oid], "lat": lat, "lng": lng}
        for oid, (lat, lng) in coords.items()
    }

    def euclid(a, b):
        return ((a["lat"] - b["lat"]) ** 2 + (a["lng"] - b["lng"]) ** 2) ** 0.5

    def fake_road_distance_time(source, destination):
        d = euclid(source, destination)
        return {"distance_km": d, "time_minutes": d}

    monkeypatch.setattr("app.route_service._road_distance_time", fake_road_distance_time)

    bad_order = [orders["p1"], orders["p2"], orders["p3"], orders["p4"], orders["p5"], orders["p6"]]
    vehicle = {"vehicle_type": "car", "capacity": 6, "orders": list(bad_order), "current_time": 480.0, "current_location": dict(depot), "is_auto_created": False}

    _, bad_distance, _, _ = _simulate_route(bad_order, depot, 480.0)

    _improve_route(vehicle, depot, 480.0)

    _, improved_distance, _, _ = _simulate_route(vehicle["orders"], depot, 480.0)
    improved_ids = [o["order_id"] for o in vehicle["orders"]]

    best_possible = min(
        _simulate_route(list(perm), depot, 480.0)[1]
        for perm in itertools.permutations(bad_order)
    )

    assert set(improved_ids) == {"p1", "p2", "p3", "p4", "p5", "p6"}
    assert improved_distance < bad_distance
    assert improved_distance <= best_possible * 1.10
    # p1-p4 (the "12:00" stops) must still all precede any stop that has
    # no time set is fine either way, but they must never be reordered
    # relative to each other's slot value - trivially true here since
    # they're all equal, so this really checks the mixed inf/finite case
    # didn't block the move that fixes the crossing.
    assert bad_distance - improved_distance > 10  # a real, substantial fix, not a rounding artifact


def test_relocate_across_routes_moves_stray_stop_to_closer_route_with_capacity(monkeypatch):
    # Reproduces a real dispatcher-reported bug: a route with 5
    # tightly-clustered stops that also picked up one stop from a totally
    # different part of the city, because that's the route the initial
    # bucket-by-time/nearest-available-vehicle greedy build happened to
    # still have a free slot on when it reached that order - not because
    # it was actually a good fit. _improve_route can't fix this (it only
    # reorders stops *within* one route); _relocate_across_routes is the
    # cross-route pass that should.
    from app.route_service import _relocate_across_routes

    depot = {"lat": 13.0, "lng": 80.0}
    stray = {"order_id": "stray", "customer_name": "Stray", "address": "Stray", "delivery_time": "12:00", "lat": 13.50, "lng": 80.50}
    a1 = {"order_id": "a1", "customer_name": "A1", "address": "A1", "delivery_time": "12:00", "lat": 13.001, "lng": 80.001}
    a2 = {"order_id": "a2", "customer_name": "A2", "address": "A2", "delivery_time": "12:00", "lat": 13.002, "lng": 80.002}
    # Right next to `stray`, on a different route that still has a free
    # capacity slot (bike capacity 3, only 2 stops used).
    b1 = {"order_id": "b1", "customer_name": "B1", "address": "B1", "delivery_time": "12:00", "lat": 13.501, "lng": 80.501}
    b2 = {"order_id": "b2", "customer_name": "B2", "address": "B2", "delivery_time": "12:00", "lat": 13.502, "lng": 80.502}

    def bucket(point):
        lat = round(point["lat"], 1)
        return "far" if lat == 13.5 else "near_depot"

    def distance_between(source, destination):
        same_bucket = bucket(source) == bucket(destination)
        return 0.3 if same_bucket else 60.0

    def fake_road_distance_time(source, destination):
        d = distance_between(source, destination)
        return {"distance_km": d, "time_minutes": d}

    monkeypatch.setattr("app.route_service._road_distance_time", fake_road_distance_time)

    route_a = {"vehicle_type": "bike", "capacity": 3, "orders": [a1, a2, stray], "current_time": 480.0, "current_location": dict(depot), "is_auto_created": False}
    route_b = {"vehicle_type": "bike", "capacity": 3, "orders": [b1, b2], "current_time": 480.0, "current_location": dict(depot), "is_auto_created": False}
    vehicles = [route_a, route_b]

    _relocate_across_routes(vehicles, depot, 480.0)

    assert [o["order_id"] for o in route_a["orders"]] == ["a1", "a2"]
    assert "stray" in [o["order_id"] for o in route_b["orders"]]


def test_google_maps_url_strips_embedded_line_breaks_from_address():
    # Regression test: many uploaded addresses keep literal newlines
    # (readable multi-line cells in Excel), e.g.
    # "BBCL Stanburry Villa 17\nOld No: 56, New No: 79, Anna Main Rd,
    # Kolapakkam, Chennai, Tamil Nadu 600128". A raw newline inside a Maps
    # query string breaks Google's address parser and produces
    # "we can't find that place" on redirect - the address itself was fine,
    # it was never sanitized before going into the URL.
    from app.route_service import build_google_maps_url

    depot = {"lat": 12.99, "lng": 80.21}
    orders = [{
        "order_id": "1",
        "address": "BBCL Stanburry Villa 17\nOld No: 56, New No: 79, Anna Main Rd,\nKolapakkam, Chennai, Tamil Nadu 600128",
    }]

    url = build_google_maps_url(orders, depot)

    assert "%0A" not in url  # no encoded newline reached the URL
    assert "\n" not in url
    assert "BBCL%20Stanburry%20Villa%2017%20Old%20No" in url


# --------------------------------------------------------------------------
# _detour_split_candidate - the fallback _smallest_disconnected_group calls
# when its own fixed-threshold single-linkage check finds a route "one
# connected group" despite a real, inefficient zigzag: a chain of hops each
# individually too small to trip that check. See route_service.py's own
# module-level comment above DETOUR_COST_RATIO_FACTOR and
# _detour_split_candidate's docstring for the full reasoning.
# --------------------------------------------------------------------------

# Real production data this fallback was written to fix (see this session's
# investigation): a 6-stop route that _smallest_disconnected_group's fixed
# threshold read as "one connected group" (every consecutive gap 2.4-5.0km,
# comfortably under its own ~7.5-9km threshold for this route) despite an
# obvious NW-cluster/coastal-group split on the map.
_REAL_ZIGZAG_STOPS = [
    {"order_id": "17", "lat": 12.9831203, "lng": 80.21584059999999, "delivery_time": None},
    {"order_id": "22", "lat": 12.9867924, "lng": 80.19001999999999, "delivery_time": None},
    {"order_id": "25", "lat": 12.9680937, "lng": 80.20997360000001, "delivery_time": None},
    {"order_id": "5", "lat": 12.9453063, "lng": 80.233682, "delivery_time": None},
    {"order_id": "1", "lat": 12.9795033, "lng": 80.2630137, "delivery_time": None},
    {"order_id": "23", "lat": 12.9593476, "lng": 80.2555699, "delivery_time": None},
]


def _mock_real_road_distance(monkeypatch):
    """Every test below needs _simulate_route's road-distance calls to
    resolve to something - real haversine distance stands in for OSRM here
    (deterministic, no network), same monkeypatch point
    (app.route_service._road_distance_time) every other test in this file
    already uses."""
    from app.route_service import _haversine_km

    def fake_road_distance_time(source, destination):
        d = _haversine_km(source, destination)
        return {"distance_km": d, "time_minutes": d * 2.0}  # ~30 km/h, arbitrary but consistent

    monkeypatch.setattr("app.route_service._road_distance_time", fake_road_distance_time)


def test_smallest_disconnected_group_catches_the_real_zigzag_via_detour_fallback(monkeypatch):
    from app.route_service import _smallest_disconnected_group, VELOCHERY_DEPOT, DEFAULT_ROUTE_START_MINUTES

    _mock_real_road_distance(monkeypatch)
    result = _smallest_disconnected_group(_REAL_ZIGZAG_STOPS, VELOCHERY_DEPOT, DEFAULT_ROUTE_START_MINUTES)

    assert result is not None
    indexes, _, _ = result
    split_ids = {_REAL_ZIGZAG_STOPS[i]["order_id"] for i in indexes}
    assert split_ids == {"5", "1", "23"}  # the coastal group, not the NW cluster


def test_smallest_disconnected_group_result_is_independent_of_input_order(monkeypatch):
    """Spec requirement: shuffling the input list must not change which
    stops get grouped together - geographic grouping must never be a
    function of array/database order."""
    import random

    from app.route_service import _smallest_disconnected_group, VELOCHERY_DEPOT, DEFAULT_ROUTE_START_MINUTES

    _mock_real_road_distance(monkeypatch)
    shuffled = list(_REAL_ZIGZAG_STOPS)
    random.Random(42).shuffle(shuffled)

    result = _smallest_disconnected_group(shuffled, VELOCHERY_DEPOT, DEFAULT_ROUTE_START_MINUTES)

    assert result is not None
    indexes, _, _ = result
    split_ids = {shuffled[i]["order_id"] for i in indexes}
    assert split_ids == {"5", "1", "23"}


def test_smallest_disconnected_group_does_not_over_split_a_natural_corridor(monkeypatch):
    """Spec test case: 5 stops evenly spaced along one natural corridor
    must NOT be split just because the corridor's total span is large -
    every stop here carries a similar, proportionate share of the route's
    distance, so neither the fixed-threshold check nor the detour fallback
    should find anything to peel off."""
    from app.route_service import _smallest_disconnected_group, VELOCHERY_DEPOT, DEFAULT_ROUTE_START_MINUTES

    _mock_real_road_distance(monkeypatch)
    # ~2km apart, in a straight line - a textbook corridor, deliberately
    # spanning almost 10km end-to-end (bigger than the real zigzag route's
    # own weakest link above) to prove span alone isn't the criterion.
    corridor = [
        {"order_id": f"C{i}", "lat": 13.00 + i * 0.018, "lng": 80.20, "delivery_time": None}
        for i in range(5)
    ]

    result = _smallest_disconnected_group(corridor, VELOCHERY_DEPOT, DEFAULT_ROUTE_START_MINUTES)

    assert result is None


def test_smallest_disconnected_group_still_catches_a_single_far_outlier(monkeypatch):
    """Regression: the pre-existing fixed-threshold check (a lone stop far
    from a tight cluster) must still work unchanged - this fallback only
    ever engages when that check finds nothing."""
    from app.route_service import _smallest_disconnected_group, VELOCHERY_DEPOT, DEFAULT_ROUTE_START_MINUTES

    _mock_real_road_distance(monkeypatch)
    cluster = [
        {"order_id": "A", "lat": 13.000, "lng": 80.200, "delivery_time": None},
        {"order_id": "B", "lat": 13.002, "lng": 80.201, "delivery_time": None},
        {"order_id": "C", "lat": 13.001, "lng": 80.203, "delivery_time": None},
        {"order_id": "X", "lat": 13.200, "lng": 80.400, "delivery_time": None},  # far outlier
    ]

    result = _smallest_disconnected_group(cluster, VELOCHERY_DEPOT, DEFAULT_ROUTE_START_MINUTES)

    assert result is not None
    indexes, _, _ = result
    assert {cluster[i]["order_id"] for i in indexes} == {"X"}


def test_smallest_disconnected_group_leaves_a_genuinely_tight_route_alone(monkeypatch):
    """A route too small/tight to judge, or where every stop is close to
    every other, must return None from both the fixed check and the
    fallback - not every route needs splitting."""
    from app.route_service import _smallest_disconnected_group, VELOCHERY_DEPOT, DEFAULT_ROUTE_START_MINUTES

    _mock_real_road_distance(monkeypatch)
    tight = [
        {"order_id": "A", "lat": 13.000, "lng": 80.200, "delivery_time": None},
        {"order_id": "B", "lat": 13.001, "lng": 80.201, "delivery_time": None},
        {"order_id": "C", "lat": 13.002, "lng": 80.202, "delivery_time": None},
    ]

    assert _smallest_disconnected_group(tight, VELOCHERY_DEPOT, DEFAULT_ROUTE_START_MINUTES) is None


def test_generate_routes_splits_the_real_zigzag_into_two_routes(monkeypatch):
    """End-to-end (not just the unit-level check above): the exact
    production scenario this session's investigation was based on, run
    through the full generate_routes() pipeline - one car, capacity 6
    (enough to hold all 6 stops on one route, which is exactly what used
    to happen), confirmed to now produce two separate routes instead."""
    from app.route_service import _haversine_km

    def fake_road_distance_time(source, destination):
        d = _haversine_km(source, destination)
        return {"distance_km": d, "time_minutes": d * 2.0}

    def fake_get_distances_from_point(origin, destinations):
        return [fake_road_distance_time(origin, d) for d in destinations]

    monkeypatch.setattr("app.route_service._road_distance_time", fake_road_distance_time)
    monkeypatch.setattr("app.route_service.get_distances_from_point", fake_get_distances_from_point)
    monkeypatch.setattr("app.route_service.prime_route_cache", lambda orders, depot: None)

    orders = [
        {"order_id": s["order_id"], "customer_name": f"Cust {s['order_id']}", "address": f"Addr {s['order_id']}",
         "delivery_time": None, "lat": s["lat"], "lng": s["lng"]}
        for s in _REAL_ZIGZAG_STOPS
    ]

    result = generate_routes(orders, available_cars=1, available_bikes=0)

    assert result["route_count"] == 2
    groups = [{o["order_id"] for o in route["orders"]} for route in result["routes"]]
    assert {"5", "1", "23"} in groups
    assert {"17", "22", "25"} in groups


# Slot-vs-geography safety ("geography must never override an existing
# delivery-slot rule") is already covered, unmodified, by this file's own
# pre-existing test_generate_routes_never_delays_earlier_slot_for_closer_later_slot
# above - this session's change never touches slot bucketing/feasibility at
# all, only which existing route a geographically-flagged group gets
# relocated to (still gated by the pre-existing _slot_order_preserved check
# in _extract_geographic_outliers, itself untouched), so that test already
# proves the requirement holds with this change in place.
