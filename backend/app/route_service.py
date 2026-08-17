import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from app.distance_service import get_distances_from_point, route_distance_time

# ARZ Food Ventures Private Limited - 8/49, Indira Gandhi Nagar, Velachery,
# Chennai, Tamil Nadu 600042 (dispatch depot; exact coordinates supplied
# directly via Google Maps since free geocoders can't pinpoint this street).
VELOCHERY_DEPOT = {"lat": 12.989953044885272, "lng": 80.21804157624011}
BIKE_CAPACITY = 3
CAR_CAPACITY = 6

SERVICE_TIME_MINUTES = 3.0
LATE_GRACE_MINUTES = 0.0
SWAP_MIN_SAVINGS_MINUTES = 8.0
DEFAULT_ROUTE_START_MINUTES = 480.0
ROUTE_START_LEAD_MINUTES = 60.0
IMPROVEMENT_SWEEPS = 2
IMPROVEMENT_WINDOW = 4

# Consolidation bias: picking a vehicle that's still sitting at the depot
# (never assigned a stop yet) is treated as if it were this many km farther
# away than it really is, when compared against a vehicle that's already
# out on the road with room to spare. Without this, every vehicle starts at
# the exact same depot point, so a fresh idle vehicle looks "just as close"
# as one already near the order - and the greedy search happily spreads
# orders across every vehicle configured instead of filling the ones
# already in use. A real detour bigger than this margin still wins on its
# own merits, so a genuinely isolated order (nothing else anywhere near it)
# still correctly gets its own dedicated vehicle - this only kills *close*
# calls in favor of consolidating, it never forces a bad detour.
VEHICLE_ACTIVATION_PENALTY_KM = 4.0


def parse_delivery_time(value: object) -> float:
    if value is None:
        return float("inf")
    text = str(value).strip().lower()
    if not text:
        return float("inf")

    text = text.replace("sharp", "").replace("hrs", "").replace("hours", "")
    text = text.replace(".", ":").replace("-", ":").replace("/", ":")
    text = text.replace("noon", "12:00").replace("midnight", "00:00")

    time_match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or "0")
        meridian = time_match.group(3)
        if meridian == "pm" and hour != 12:
            hour += 12
        if meridian == "am" and hour == 12:
            hour = 0
        return hour + minute / 60.0

    return float("inf")


def parse_delivery_slot_minutes(order: Dict[str, object]) -> float:
    hours = parse_delivery_time(order.get("delivery_time", ""))
    if hours == float("inf"):
        return float("inf")
    return hours * 60.0


def format_minutes_as_clock(minutes: Optional[float]) -> Optional[str]:
    if minutes is None or minutes == float("inf"):
        return None
    total_minutes = int(round(minutes)) % (24 * 60)
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def compute_route_start_minutes(orders: List[Dict[str, object]]) -> float:
    finite_slots = [
        slot for slot in (parse_delivery_slot_minutes(order) for order in orders)
        if slot != float("inf")
    ]
    if not finite_slots:
        return DEFAULT_ROUTE_START_MINUTES
    return max(min(finite_slots) - ROUTE_START_LEAD_MINUTES, DEFAULT_ROUTE_START_MINUTES)


def build_vehicle_specs(available_cars: int, available_bikes: int) -> List[Dict[str, object]]:
    specs: List[Dict[str, object]] = []
    for _ in range(available_cars):
        specs.append({"vehicle_type": "car", "capacity": CAR_CAPACITY})
    for _ in range(available_bikes):
        specs.append({"vehicle_type": "bike", "capacity": BIKE_CAPACITY})
    return specs


def has_coordinates(order: Dict[str, object]) -> bool:
    return order.get("lat") is not None and order.get("lng") is not None


def _road_distance_time(source: Dict[str, float], destination: Dict[str, float]) -> Dict[str, Optional[float]]:
    return route_distance_time(source["lat"], source["lng"], destination["lat"], destination["lng"])


def _is_feasible(eta: Optional[float], deadline: float) -> bool:
    if eta is None or deadline == float("inf"):
        return True
    return eta <= deadline + LATE_GRACE_MINUTES


def _spawn_vehicle(vehicle_type: str, capacity: int, route_start_minutes: float, depot: Dict[str, float]) -> Dict[str, object]:
    return {
        "vehicle_type": vehicle_type,
        "capacity": capacity,
        "orders": [],
        "current_time": route_start_minutes,
        "current_location": dict(depot),
        "is_auto_created": False,
    }


def _simulate_route(
    route_orders: List[Dict[str, object]],
    depot: Dict[str, float],
    start_time: float,
) -> Tuple[List[Optional[float]], Optional[float], Optional[float], bool]:
    etas: List[Optional[float]] = []
    current_time = start_time
    current_location = depot
    total_distance = 0.0
    total_time = 0.0
    feasible_all = True
    any_unknown_leg = False

    for order in route_orders:
        if has_coordinates(order) and has_coordinates(current_location):
            travel = _road_distance_time(current_location, {"lat": order["lat"], "lng": order["lng"]})
            distance = travel.get("distance_km")
            time_minutes = travel.get("time_minutes")
        else:
            distance = None
            time_minutes = None

        if time_minutes is None:
            any_unknown_leg = True
            eta = None
        else:
            current_time = current_time + time_minutes + SERVICE_TIME_MINUTES
            eta = current_time
            total_time += time_minutes

        if distance is not None:
            total_distance += distance

        if not _is_feasible(eta, parse_delivery_slot_minutes(order)):
            feasible_all = False

        etas.append(eta)
        if has_coordinates(order):
            current_location = {"lat": order["lat"], "lng": order["lng"]}

    if any_unknown_leg:
        return etas, None, None, feasible_all
    return etas, round(total_distance, 2), round(total_time, 1), feasible_all


def _improve_route(vehicle: Dict[str, object], depot: Dict[str, float], route_start_minutes: float) -> None:
    route_orders = vehicle["orders"]
    if len(route_orders) < 2:
        return
    if not all(has_coordinates(order) for order in route_orders):
        return

    best_orders = list(route_orders)
    _, best_distance, best_time, _ = _simulate_route(best_orders, depot, route_start_minutes)
    if best_time is None:
        return

    for _ in range(IMPROVEMENT_SWEEPS):
        improved = False
        for i in range(len(best_orders) - 1):
            for j in range(i + 1, min(i + IMPROVEMENT_WINDOW, len(best_orders))):
                # Distance optimization only happens *inside* a delivery
                # slot - never reorder a later-slot stop ahead of an
                # earlier-slot one just to save travel time.
                if parse_delivery_slot_minutes(best_orders[i]) != parse_delivery_slot_minutes(best_orders[j]):
                    continue
                candidate = list(best_orders)
                candidate[i], candidate[j] = candidate[j], candidate[i]
                _, candidate_distance, candidate_time, feasible_all = _simulate_route(candidate, depot, route_start_minutes)
                if candidate_time is None or not feasible_all:
                    continue
                if best_time - candidate_time >= SWAP_MIN_SAVINGS_MINUTES:
                    best_orders = candidate
                    best_time = candidate_time
                    best_distance = candidate_distance
                    improved = True
        if not improved:
            break

    vehicle["orders"] = best_orders


def build_routes(
    orders: List[Dict[str, object]],
    available_cars: int,
    available_bikes: int,
) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
    depot = VELOCHERY_DEPOT
    base_specs = build_vehicle_specs(available_cars, available_bikes)
    if not base_specs:
        return [], list(orders)

    route_start_minutes = compute_route_start_minutes(orders)
    vehicles: List[Dict[str, object]] = [
        _spawn_vehicle(spec["vehicle_type"], spec["capacity"], route_start_minutes, depot)
        for spec in base_specs
    ]
    capacity_by_type = {spec["vehicle_type"]: spec["capacity"] for spec in base_specs}
    configured_types = list(capacity_by_type.keys())

    remaining = list(orders)
    max_auto_spawns = max(len(orders), 1)
    spawn_count = 0

    # Per-vehicle cache of "vehicle's current location -> bucket order" travel
    # results, refreshed with one batched OSRM table call per vehicle instead
    # of one call per (vehicle, order) pair. Cleared for a vehicle whenever it
    # moves (gets assigned a stop), since its current_location changed.
    vehicle_distance_cache: Dict[int, Dict[object, Dict[str, Optional[float]]]] = {}

    def vehicle_rows_for_bucket(vehicle: Dict[str, object], bucket_orders_with_coords: List[Dict[str, object]]) -> Dict[object, Dict[str, Optional[float]]]:
        cache = vehicle_distance_cache.setdefault(id(vehicle), {})
        if not bucket_orders_with_coords or not has_coordinates(vehicle["current_location"]):
            return cache
        missing = [order for order in bucket_orders_with_coords if order.get("order_id") not in cache]
        if missing:
            travel_results = get_distances_from_point(
                vehicle["current_location"],
                [{"lat": order["lat"], "lng": order["lng"]} for order in missing],
            )
            for order, result in zip(missing, travel_results):
                cache[order.get("order_id")] = result
        return cache

    while remaining:
        remaining.sort(key=parse_delivery_slot_minutes)
        target_slot = parse_delivery_slot_minutes(remaining[0])
        bucket = [order for order in remaining if parse_delivery_slot_minutes(order) == target_slot]

        while bucket:
            best_vehicle = None
            best_order = None
            best_eta: Optional[float] = None
            best_feasible = False
            best_cost = None
            best_time_minutes: Optional[float] = None

            bucket_orders_with_coords = [order for order in bucket if has_coordinates(order)]

            for vehicle in vehicles:
                if len(vehicle["orders"]) >= vehicle["capacity"]:
                    continue

                vehicle_cache = vehicle_rows_for_bucket(vehicle, bucket_orders_with_coords)

                for order in bucket:
                    if has_coordinates(order) and has_coordinates(vehicle["current_location"]):
                        travel = vehicle_cache.get(order.get("order_id"), {"distance_km": None, "time_minutes": None})
                        distance = travel.get("distance_km")
                        time_minutes = travel.get("time_minutes")
                    else:
                        distance = None
                        time_minutes = None

                    eta = vehicle["current_time"] + time_minutes + SERVICE_TIME_MINUTES if time_minutes is not None else None
                    feasible = _is_feasible(eta, target_slot)
                    cost = distance if distance is not None else float(len(vehicle["orders"]))
                    if distance is not None and len(vehicle["orders"]) == 0:
                        cost += VEHICLE_ACTIVATION_PENALTY_KM

                    is_better = (
                        best_vehicle is None
                        or (feasible and not best_feasible)
                        or (feasible == best_feasible and cost < best_cost)
                    )
                    if is_better:
                        best_vehicle = vehicle
                        best_order = order
                        best_eta = eta
                        best_feasible = feasible
                        best_cost = cost
                        best_time_minutes = time_minutes

            if best_vehicle is None:
                # Every vehicle is already at capacity - spawn a new one
                # rather than stranding the rest of this bucket as pending.
                if not configured_types or spawn_count >= max_auto_spawns:
                    break

                reference_order = bucket[0]
                spawn_type = configured_types[0]
                if has_coordinates(reference_order) and vehicles:
                    nearest_vehicle = min(
                        vehicles,
                        key=lambda v: (
                            _road_distance_time(v["current_location"], {"lat": reference_order["lat"], "lng": reference_order["lng"]}).get("distance_km")
                            if has_coordinates(v["current_location"]) else None
                        ) or float("inf"),
                    )
                    spawn_type = nearest_vehicle["vehicle_type"]

                fresh_vehicle = _spawn_vehicle(spawn_type, capacity_by_type[spawn_type], route_start_minutes, depot)
                fresh_vehicle["is_auto_created"] = True
                vehicles.append(fresh_vehicle)
                spawn_count += 1
                continue

            if not best_feasible and spawn_count < max_auto_spawns and configured_types:
                spawn_type = best_vehicle["vehicle_type"] if best_vehicle["vehicle_type"] in capacity_by_type else configured_types[0]
                fresh_vehicle = _spawn_vehicle(spawn_type, capacity_by_type[spawn_type], route_start_minutes, depot)

                if has_coordinates(best_order):
                    fresh_travel = _road_distance_time(fresh_vehicle["current_location"], {"lat": best_order["lat"], "lng": best_order["lng"]})
                    fresh_time_minutes = fresh_travel.get("time_minutes")
                else:
                    fresh_time_minutes = None

                fresh_eta = fresh_vehicle["current_time"] + fresh_time_minutes + SERVICE_TIME_MINUTES if fresh_time_minutes is not None else None
                fresh_feasible = _is_feasible(fresh_eta, target_slot)

                if fresh_eta is not None and (best_eta is None or fresh_eta < best_eta) and (fresh_feasible or not best_feasible):
                    fresh_vehicle["is_auto_created"] = True
                    vehicles.append(fresh_vehicle)
                    best_vehicle = fresh_vehicle
                    best_eta = fresh_eta
                    best_feasible = fresh_feasible
                    best_time_minutes = fresh_time_minutes
                    spawn_count += 1

            best_vehicle["orders"].append(best_order)
            if has_coordinates(best_order):
                best_vehicle["current_location"] = {"lat": best_order["lat"], "lng": best_order["lng"]}
                vehicle_distance_cache.pop(id(best_vehicle), None)
            if best_eta is not None:
                best_vehicle["current_time"] = best_eta
            elif best_time_minutes is not None:
                best_vehicle["current_time"] += best_time_minutes + SERVICE_TIME_MINUTES

            bucket.remove(best_order)
            remaining.remove(best_order)

    for vehicle in vehicles:
        _improve_route(vehicle, depot, route_start_minutes)

    return vehicles, remaining


def build_route_segments(route_orders: List[Dict[str, object]], depot: Dict[str, float]) -> List[Dict[str, object]]:
    segments: List[Dict[str, object]] = []
    current = depot
    from_label = "Depot"

    for order in route_orders:
        if has_coordinates(order) and has_coordinates(current):
            estimate = _road_distance_time(current, {"lat": order["lat"], "lng": order["lng"]})
            distance_km = estimate["distance_km"]
            time_minutes = estimate["time_minutes"]
        else:
            distance_km = None
            time_minutes = None

        to_label = f"{order.get('order_id')}"
        segments.append({
            "from": from_label,
            "to": to_label,
            "distance_km": distance_km,
            "time_minutes": time_minutes,
        })
        current = order
        from_label = to_label

    return segments


def _sanitize_address_for_link(address: object) -> str:
    """Collapses embedded line breaks and duplicate whitespace before an
    address goes into a Maps URL. Many uploaded addresses keep literal
    newlines on purpose (readable multi-line cells in Excel), but a raw
    newline inside a Maps query string breaks Google's address parser -
    that's what was causing "we can't find that place" on redirect, not
    anything about which address field was used. This only touches
    whitespace; not one word of the address changes (clean_address() in
    geocode_service.py already does the same collapsing before geocoding -
    this mirrors it for the link, which never went through that path)."""
    return " ".join(str(address or "").split())


def _stop_link_target(order: Dict[str, object]) -> str:
    """A `lat,lng` pair always resolves in Google Maps - it's a literal
    point, no address parsing involved, so it can never fail with "we can't
    find that place". Preferred whenever the order has coordinates, which
    for a successfully geocoded order are exactly what Google's own
    Geocoding API resolved that address to - not a stand-in for the
    address, the same location under a different representation. Address
    text is only used as a fallback for orders with no coordinates at all
    (failed/pending geocodes), where it's the only thing available; even
    then it's whitespace-sanitized (see _sanitize_address_for_link) since a
    raw newline in the query breaks Google's parser regardless of how good
    the address text otherwise is."""
    lat, lng = order.get("lat"), order.get("lng")
    if lat is not None and lng is not None:
        return f"{lat},{lng}"
    return _sanitize_address_for_link(order.get("address"))


def build_google_maps_url(route_orders: List[Dict[str, object]], depot: Dict[str, float]) -> str:
    """One stop per waypoint, each resolved via coordinates when available
    (always reliable) or sanitized address text as a fallback. See
    _stop_link_target for why coordinates are preferred now - an earlier
    version of this function used address text exclusively and that turned
    out to fail unpredictably ("address not found") for any address Google's
    web search can't parse well, even after whitespace cleanup."""
    if not route_orders:
        return ""
    origin = f"{depot['lat']},{depot['lng']}"
    targets = [_stop_link_target(order) for order in route_orders]
    targets = [target for target in targets if target]
    if not targets:
        return ""
    destination = quote(targets[-1])
    waypoints = "|".join(quote(target) for target in targets[:-1])
    url = f"https://www.google.com/maps/dir/?api=1&origin={origin}&destination={destination}"
    if waypoints:
        url += f"&waypoints={waypoints}"
    return url


def generate_routes(orders: List[Dict[str, object]], available_cars: int, available_bikes: int) -> Dict[str, object]:
    if not orders:
        return {
            "route_count": 0,
            "routes": [],
            "pending_orders": [],
            "warnings": [],
        }

    depot = VELOCHERY_DEPOT
    route_start_minutes = compute_route_start_minutes(orders)
    vehicles, leftover = build_routes(orders, available_cars, available_bikes)
    # A configured vehicle that never actually got a stop assigned isn't a
    # route - showing it anyway was a real bug: ask for 10 vehicles when the
    # order volume only needs 8, and this used to render 10 route cards (2
    # of them empty) instead of 8. Only vehicles that were actually used
    # become routes, and "Route N" numbering stays contiguous.
    used_vehicles = [vehicle for vehicle in vehicles if vehicle["orders"]]
    routes: List[Dict[str, object]] = []
    pending_orders: List[Dict[str, object]] = leftover.copy()

    for index, vehicle in enumerate(used_vehicles):
        route_orders = vehicle["orders"]
        etas, distance_km, time_minutes, _ = _simulate_route(route_orders, depot, route_start_minutes)

        annotated_orders: List[Dict[str, object]] = []
        late_order_ids: List[object] = []
        for order, eta in zip(route_orders, etas):
            annotated = dict(order)
            annotated["eta"] = format_minutes_as_clock(eta)
            deadline = parse_delivery_slot_minutes(order)
            is_late = eta is not None and deadline != float("inf") and eta > deadline + LATE_GRACE_MINUTES
            annotated["is_late"] = is_late
            if is_late:
                late_order_ids.append(order.get("order_id"))
            annotated_orders.append(annotated)

        segments = build_route_segments(route_orders, depot)
        maps_url = build_google_maps_url(route_orders, depot)
        stops = len(route_orders)
        finish_eta = etas[-1] if etas else None

        routes.append({
            "route_name": f"Route {index + 1}",
            "vehicle_type": vehicle["vehicle_type"],
            "orders": annotated_orders,
            "route_distance_km": distance_km,
            "route_time_minutes": time_minutes,
            "number_of_stops": stops,
            "route_segments": segments,
            "google_maps_url": maps_url,
            "estimated_finish_time": format_minutes_as_clock(finish_eta),
            "average_stop_time": round(time_minutes / stops, 1) if time_minutes is not None and stops else None,
            "delivery_sequence": [order.get("order_id") for order in route_orders],
            "late_deliveries": late_order_ids,
            "utilization_percent": round(stops / vehicle["capacity"] * 100, 1) if vehicle["capacity"] else None,
            "is_auto_created": vehicle.get("is_auto_created", False),
        })

    warnings: List[str] = []
    if available_cars <= 0 and available_bikes <= 0 and pending_orders:
        warnings.append(
            "No vehicles configured - add at least one car or bike to generate routes. "
            f"{len(pending_orders)} order(s) could not be assigned."
        )

    return {
        "route_count": len(routes),
        "routes": routes,
        "pending_orders": pending_orders,
        "warnings": warnings,
    }
