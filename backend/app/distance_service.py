import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

import httpx

OSRM_TABLE_URL = "http://router.project-osrm.org/table/v1/driving"
OSRM_ROUTE_URL = "http://router.project-osrm.org/route/v1/driving"
TIMEOUT_SECONDS = 10.0
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 0.5
TABLE_CHUNK_SIZE = 90
MAX_CONCURRENT_OSRM_REQUESTS = 3


def _fetch_osrm_json(url: str, params: Dict[str, str]) -> Optional[Dict[str, object]]:
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=True) as client:
                response = client.get(url, params=params)
                response.raise_for_status()
                return response.json()
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
            last_error = exc
            if attempt == MAX_RETRIES:
                break
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    return None


_ROUTE_CACHE: Dict[Tuple[float, float, float, float], Dict[str, Optional[float]]] = {}


def clear_route_cache() -> None:
    _ROUTE_CACHE.clear()


def _cache_key(lat1: float, lng1: float, lat2: float, lng2: float) -> Tuple[float, float, float, float]:
    return (round(lat1, 5), round(lng1, 5), round(lat2, 5), round(lng2, 5))


def route_distance_time(lat1: float, lng1: float, lat2: float, lng2: float) -> Dict[str, Optional[float]]:
    key = _cache_key(lat1, lng1, lat2, lng2)
    if key in _ROUTE_CACHE:
        return _ROUTE_CACHE[key]

    coords = f"{lng1},{lat1};{lng2},{lat2}"
    params = {"overview": "false", "alternatives": "false", "steps": "false"}
    data = _fetch_osrm_json(f"{OSRM_ROUTE_URL}/{coords}", params)
    if not data:
        result = {"distance_km": None, "time_minutes": None}
        return result

    route_list = data.get("routes")
    if not route_list or not isinstance(route_list, list):
        result = {"distance_km": None, "time_minutes": None}
        return result

    route_info = route_list[0]
    distance = route_info.get("distance")
    duration = route_info.get("duration")
    if distance is None or duration is None:
        result = {"distance_km": None, "time_minutes": None}
        return result

    result = {
        "distance_km": round(distance / 1000.0, 2),
        "time_minutes": round(duration / 60.0, 1),
    }
    _ROUTE_CACHE[key] = result
    return result


def build_route_geometry(depot: Dict[str, float], stops: List[Dict[str, float]]) -> Optional[List[Dict[str, float]]]:
    """The actual road-following path for depot -> every stop, in delivery
    order, as a list of {lat, lng} points - one multi-waypoint OSRM request
    returns the whole route's shape in a single call, via the same free
    OSRM server route_distance_time already relies on above. This exists
    for the admin live-tracking map's "planned route" line: the Maps key
    only has the JavaScript API enabled, not the (separately-gated)
    Directions/Routes API, so drawing the road-following line server-side
    with what's already proven reliable here avoids needing that turned on.
    """
    if not stops:
        return None
    points = [depot] + list(stops)
    coordinates = ";".join(f"{p['lng']},{p['lat']}" for p in points)
    params = {"overview": "full", "geometries": "geojson"}
    data = _fetch_osrm_json(f"{OSRM_ROUTE_URL}/{coordinates}", params)
    if not data:
        return None
    route_list = data.get("routes")
    if not route_list or not isinstance(route_list, list):
        return None
    geometry = route_list[0].get("geometry") or {}
    raw_coords = geometry.get("coordinates")
    if not raw_coords:
        return None
    # GeoJSON coordinates are [lng, lat] - flip to the {lat, lng} shape
    # used everywhere else in this codebase and by the frontend.
    return [{"lat": c[1], "lng": c[0]} for c in raw_coords]


def _table_one_to_many(
    origin: Tuple[float, float],
    destinations: List[Tuple[float, float]],
) -> List[Dict[str, Optional[float]]]:
    points = [origin] + destinations
    coordinates = ";".join(f"{lng},{lat}" for lat, lng in points)
    params = {"annotations": "distance,duration", "sources": "0"}
    data = _fetch_osrm_json(f"{OSRM_TABLE_URL}/{coordinates}", params)

    if data:
        distances = data.get("distances")
        durations = data.get("durations")
        if (
            isinstance(distances, list) and distances
            and isinstance(durations, list) and durations
        ):
            distance_row = distances[0]
            duration_row = durations[0]
            results: List[Dict[str, Optional[float]]] = []
            for index in range(len(destinations)):
                distance_value = distance_row[index + 1] if index + 1 < len(distance_row) else None
                duration_value = duration_row[index + 1] if index + 1 < len(duration_row) else None
                results.append({
                    "distance_km": round(distance_value / 1000.0, 2) if isinstance(distance_value, (int, float)) else None,
                    "time_minutes": round(duration_value / 60.0, 1) if isinstance(duration_value, (int, float)) else None,
                })
            return results

    return [{"distance_km": None, "time_minutes": None} for _ in destinations]


def get_distances_from_point(
    origin: Dict[str, float],
    destinations: List[Dict[str, float]],
    chunk_size: int = TABLE_CHUNK_SIZE,
) -> List[Dict[str, Optional[float]]]:
    """One-to-many distance/time lookup. Cache-first, chunked OSRM table
    calls for whatever isn't cached, run with modest concurrency so this
    stays polite to the public OSRM demo server while still cutting the
    total request count from O(n) single-pair calls down to O(n / chunk)."""
    if not destinations:
        return []

    results: List[Optional[Dict[str, Optional[float]]]] = [None] * len(destinations)
    uncached_indexes: List[int] = []
    uncached_points: List[Tuple[float, float]] = []

    for index, destination in enumerate(destinations):
        key = _cache_key(origin["lat"], origin["lng"], destination["lat"], destination["lng"])
        cached = _ROUTE_CACHE.get(key)
        if cached is not None:
            results[index] = cached
        else:
            uncached_indexes.append(index)
            uncached_points.append((destination["lat"], destination["lng"]))

    if uncached_points:
        origin_point = (origin["lat"], origin["lng"])
        chunks = [
            (uncached_indexes[start:start + chunk_size], uncached_points[start:start + chunk_size])
            for start in range(0, len(uncached_points), chunk_size)
        ]

        def resolve_chunk(chunk: Tuple[List[int], List[Tuple[float, float]]]):
            indexes, points = chunk
            return indexes, _table_one_to_many(origin_point, points)

        with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_OSRM_REQUESTS, len(chunks))) as executor:
            for indexes, chunk_results in executor.map(resolve_chunk, chunks):
                for offset, index in enumerate(indexes):
                    result = chunk_results[offset]
                    results[index] = result
                    destination = destinations[index]
                    key = _cache_key(origin["lat"], origin["lng"], destination["lat"], destination["lng"])
                    _ROUTE_CACHE[key] = result

    return [result if result is not None else {"distance_km": None, "time_minutes": None} for result in results]


def build_distance_matrix(orders: List[Dict[str, object]], depot: Dict[str, float]) -> List[Dict[str, object]]:
    matrix: List[Dict[str, object]] = []
    for order in orders:
        if order.get("lat") is None or order.get("lng") is None:
            matrix.append({"order_id": order.get("order_id"), "distance_km": None, "time_minutes": None})
            continue
        result = route_distance_time(depot["lat"], depot["lng"], order["lat"], order["lng"])
        matrix.append({"order_id": order.get("order_id"), **result})
    return matrix


def build_order_matrix(orders: List[Dict[str, object]], depot: Dict[str, float]) -> Tuple[List[List[Optional[float]]], List[List[Optional[float]]]]:
    points = [(depot["lat"], depot["lng"])]
    for order in orders:
        lat = order.get("lat")
        lng = order.get("lng")
        if lat is None or lng is None:
            n = len(orders) + 1
            return ([[None] * n for _ in range(n)], [[None] * n for _ in range(n)])
        points.append((lat, lng))

    if len(points) <= 1:
        return [[0.0]], [[0.0]]

    coordinates = ";".join(f"{lng},{lat}" for lat, lng in points)
    params = {"annotations": "distance,duration"}
    data = _fetch_osrm_json(f"{OSRM_TABLE_URL}/{coordinates}", params)

    if data:
        distances = data.get("distances")
        durations = data.get("durations")
        if isinstance(distances, list) and isinstance(durations, list):
            normalized_distances: List[List[Optional[float]]] = []
            normalized_durations: List[List[Optional[float]]] = []
            for distance_row, duration_row in zip(distances, durations):
                normalized_distances.append([
                    round(value / 1000.0, 2) if isinstance(value, (int, float)) else None
                    for value in distance_row
                ])
                normalized_durations.append([
                    round(value / 60.0, 1) if isinstance(value, (int, float)) else None
                    for value in duration_row
                ])
            return normalized_distances, normalized_durations

    n = len(points)
    empty_distances: List[List[Optional[float]]] = [[None] * n for _ in range(n)]
    empty_durations: List[List[Optional[float]]] = [[None] * n for _ in range(n)]
    return empty_distances, empty_durations
