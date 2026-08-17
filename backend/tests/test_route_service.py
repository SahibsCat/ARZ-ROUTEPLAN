from app.route_service import generate_routes, BIKE_CAPACITY


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
    monkeypatch.setattr("app.route_service.VELOCHERY_DEPOT", {"lat": 0.0, "lng": 0.0})

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
    monkeypatch.setattr("app.route_service.VELOCHERY_DEPOT", depot)

    result = generate_routes(orders, available_cars=0, available_bikes=5)

    assert result["route_count"] == 2
    far_route = next(r for r in result["routes"] if any(o["order_id"] == "far" for o in r["orders"]))
    assert far_route["number_of_stops"] == 1


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
