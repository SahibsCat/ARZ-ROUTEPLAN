import httpx
import pytest
from app.distance_service import (
    build_distance_matrix,
    build_order_matrix,
    clear_route_cache,
    get_distances_from_point,
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
