import httpx
import pytest
from app.distance_service import (
    build_distance_matrix,
    build_order_matrix,
    clear_route_cache,
    get_distances_from_point,
    prime_route_cache,
    route_distance_time,
)


@pytest.fixture(autouse=True)
def reset_cache():
    clear_route_cache()
    yield
    clear_route_cache()


class DummyResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class DummyClient:
    def __init__(self, response_data, raise_error=False):
        self._response_data = response_data
        self._raise_error = raise_error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, params=None):
        if self._raise_error:
            raise httpx.RequestError("Failed to connect")
        return DummyResponse(self._response_data)


def test_route_distance_time_returns_converted_osrm_values(monkeypatch):
    response_data = {"routes": [{"distance": 12345.0, "duration": 900.0}]}
    monkeypatch.setattr("app.distance_service.httpx.Client", lambda timeout, follow_redirects: DummyClient(response_data))

    result = route_distance_time(12.0, 80.0, 13.0, 80.0)

    assert result["distance_km"] == 12.35
    assert result["time_minutes"] == 15.0


def test_route_distance_time_returns_none_on_osrm_failure(monkeypatch):
    monkeypatch.setattr("app.distance_service.httpx.Client", lambda timeout, follow_redirects: DummyClient({}, raise_error=True))

    result = route_distance_time(12.0, 80.0, 13.0, 80.0)

    assert result["distance_km"] is None
    assert result["time_minutes"] is None


def test_build_order_matrix_converts_osrm_table_units(monkeypatch):
    response_data = {
        "distances": [[0.0, 1000.0], [1000.0, 0.0]],
        "durations": [[0.0, 60.0], [60.0, 0.0]],
    }
    monkeypatch.setattr("app.distance_service.httpx.Client", lambda timeout, follow_redirects: DummyClient(response_data))

    distances, durations = build_order_matrix(
        orders=[{"lat": 13.0, "lng": 80.0}], depot={"lat": 12.0, "lng": 80.0}
    )

    assert distances == [[0.0, 1.0], [1.0, 0.0]]
    assert durations == [[0.0, 1.0], [1.0, 0.0]]


def test_build_distance_matrix_returns_none_for_unroutable_orders(monkeypatch):
    response_data = {"routes": [{"distance": 2000.0, "duration": 300.0}]}
    monkeypatch.setattr("app.distance_service.httpx.Client", lambda timeout, follow_redirects: DummyClient(response_data))

    matrix = build_distance_matrix(
        orders=[
            {"order_id": "A", "lat": 13.0, "lng": 80.0},
            {"order_id": "B", "lat": None, "lng": None},
        ],
        depot={"lat": 12.0, "lng": 80.0},
    )

    assert matrix[0]["distance_km"] == 2.0
    assert matrix[0]["time_minutes"] == 5.0
    assert matrix[1]["distance_km"] is None
    assert matrix[1]["time_minutes"] is None


class CountingTableClient(DummyClient):
    def __init__(self, call_counter):
        super().__init__(response_data=None)
        self._call_counter = call_counter

    def get(self, url, params=None):
        self._call_counter["count"] += 1
        num_destinations = url.rsplit("/", 1)[-1].count(";")
        distances = [[0.0] + [1000.0 * (i + 1) for i in range(num_destinations)]]
        durations = [[0.0] + [60.0 * (i + 1) for i in range(num_destinations)]]
        return DummyResponse({"distances": distances, "durations": durations})


def test_get_distances_from_point_batches_and_caches(monkeypatch):
    call_counter = {"count": 0}
    monkeypatch.setattr(
        "app.distance_service.httpx.Client",
        lambda timeout, follow_redirects: CountingTableClient(call_counter),
    )

    origin = {"lat": 12.0, "lng": 80.0}
    destinations = [{"lat": 13.0 + i * 0.01, "lng": 80.0} for i in range(5)]

    first = get_distances_from_point(origin, destinations)
    assert call_counter["count"] == 1
    assert len(first) == 5
    assert all(result["distance_km"] is not None for result in first)

    second = get_distances_from_point(origin, destinations)
    assert call_counter["count"] == 1
    assert second == first


def test_get_distances_from_point_chunks_large_requests(monkeypatch):
    call_counter = {"count": 0}
    monkeypatch.setattr(
        "app.distance_service.httpx.Client",
        lambda timeout, follow_redirects: CountingTableClient(call_counter),
    )

    origin = {"lat": 12.0, "lng": 80.0}
    destinations = [{"lat": 13.0 + i * 0.01, "lng": 80.0} for i in range(5)]

    results = get_distances_from_point(origin, destinations, chunk_size=2)

    assert call_counter["count"] == 3
    assert len(results) == 5


class FullMatrixTableClient(DummyClient):
    """Unlike CountingTableClient (shaped for the one-to-many /table
    endpoint - a single source row), this returns a full N x N matrix, the
    shape build_order_matrix (and so prime_route_cache) actually expects
    back from a table request with no `sources`/`destinations` restriction."""

    def __init__(self, call_counter):
        super().__init__(response_data=None)
        self._call_counter = call_counter

    def get(self, url, params=None):
        self._call_counter["count"] += 1
        n = url.rsplit("/", 1)[-1].count(";") + 1
        distances = [[0.0 if i == j else 1000.0 * (i + j + 1) for j in range(n)] for i in range(n)]
        durations = [[0.0 if i == j else 60.0 * (i + j + 1) for j in range(n)] for i in range(n)]
        return DummyResponse({"distances": distances, "durations": durations})


def test_prime_route_cache_fills_every_leg_from_one_request(monkeypatch):
    # Regression for a real reproduced hang: Generate/Regenerate Routes'
    # optimizer (route_service._improve_route/_relocate_across_routes)
    # calls route_distance_time for one leg at a time as it evaluates
    # candidate stop orderings - dozens of real, uncached network round
    # trips for a genuine batch of orders. Priming the cache up front from
    # a single OSRM table request means every one of those per-leg calls
    # below is served from the cache instead, with zero further requests.
    call_counter = {"count": 0}
    monkeypatch.setattr(
        "app.distance_service.httpx.Client",
        lambda timeout, follow_redirects: FullMatrixTableClient(call_counter),
    )

    depot = {"lat": 12.0, "lng": 80.0}
    orders = [
        {"order_id": "A", "lat": 13.0, "lng": 80.0},
        {"order_id": "B", "lat": 13.01, "lng": 80.0},
        {"order_id": "C", "lat": 13.02, "lng": 80.0},
    ]

    prime_route_cache(orders, depot)
    assert call_counter["count"] == 1  # the one batched table request

    # Every depot<->order and order<->order leg is now servable from the
    # cache alone - route_distance_time must not make a single further
    # request for any of them.
    route_distance_time(depot["lat"], depot["lng"], orders[0]["lat"], orders[0]["lng"])
    route_distance_time(orders[0]["lat"], orders[0]["lng"], orders[1]["lat"], orders[1]["lng"])
    route_distance_time(orders[1]["lat"], orders[1]["lng"], orders[2]["lat"], orders[2]["lng"])
    route_distance_time(orders[2]["lat"], orders[2]["lng"], depot["lat"], depot["lng"])
    assert call_counter["count"] == 1


def test_prime_route_cache_skips_orders_with_no_coordinates(monkeypatch):
    call_counter = {"count": 0}
    monkeypatch.setattr(
        "app.distance_service.httpx.Client",
        lambda timeout, follow_redirects: CountingTableClient(call_counter),
    )

    depot = {"lat": 12.0, "lng": 80.0}
    orders = [{"order_id": "A", "lat": None, "lng": None}]

    prime_route_cache(orders, depot)  # nothing geocoded to prime with

    assert call_counter["count"] == 0


def test_prime_route_cache_skips_batches_over_the_size_cap(monkeypatch):
    from app import distance_service

    call_counter = {"count": 0}
    monkeypatch.setattr(
        "app.distance_service.httpx.Client",
        lambda timeout, follow_redirects: CountingTableClient(call_counter),
    )
    monkeypatch.setattr(distance_service, "PRIME_CACHE_MAX_ORDERS", 2)

    depot = {"lat": 12.0, "lng": 80.0}
    orders = [{"order_id": str(i), "lat": 13.0 + i * 0.01, "lng": 80.0} for i in range(3)]

    prime_route_cache(orders, depot)  # over the (patched) cap - must not fire a request

    assert call_counter["count"] == 0


def test_prime_route_cache_is_silently_a_no_op_when_osrm_fails(monkeypatch):
    monkeypatch.setattr(
        "app.distance_service.httpx.Client",
        lambda timeout, follow_redirects: DummyClient({}, raise_error=True),
    )

    depot = {"lat": 12.0, "lng": 80.0}
    orders = [{"order_id": "A", "lat": 13.0, "lng": 80.0}]

    prime_route_cache(orders, depot)  # must not raise

    # Cache stays empty - the individual per-leg fallback (also broken in
    # this test, deliberately) is what every caller still gets, same as
    # if prime_route_cache didn't exist at all.
    result = route_distance_time(depot["lat"], depot["lng"], orders[0]["lat"], orders[0]["lng"])
    assert result["distance_km"] is None
