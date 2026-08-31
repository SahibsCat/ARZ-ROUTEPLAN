import re
from math import asin, cos, radians, sin, sqrt
from statistics import median
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from app.distance_service import get_distances_from_point, prime_route_cache, route_distance_time

# ARZ Food Ventures Private Limited - 8/49, Indira Gandhi Nagar, Velachery,
# Chennai, Tamil Nadu 600042 (dispatch depot; exact coordinates supplied
# directly via Google Maps since free geocoders can't pinpoint this street).
VELOCHERY_DEPOT = {"lat": 12.989953044885272, "lng": 80.21804157624011}
BIKE_CAPACITY = 3
CAR_CAPACITY = 6

# A vehicle's *base* capacity (6 for a car, 3 for a bike, above) is what
# auto-generate fills to, and the ceiling for every ordinary add - from
# the Unassigned Orders pool, via "Move to...", or in bulk. *Max* capacity
# only comes into play through the one deliberate "Add Address from
# Another Route" admin action (crud.move_orders_between_routes), which is
# allowed to push a route ONE delivery point past its base - never
# further, and never more than once per route (that "once" is enforced by
# Route.manual_extra_order_id, not by capacity math alone - see
# move_orders_between_routes' own comment for why). Conceptually: Normal
# Capacity + Admin Override (this one extra slot) = Temporary Route
# Capacity - never a change to the normal capacity itself, which is why
# CAR_CAPACITY/BIKE_CAPACITY above stay exactly 6/3 and every automatic
# and ordinary-add path keeps using them, not these.
MANUAL_OVERRIDE_EXTRA = 1
CAR_MAX_CAPACITY = CAR_CAPACITY + MANUAL_OVERRIDE_EXTRA
BIKE_MAX_CAPACITY = BIKE_CAPACITY + MANUAL_OVERRIDE_EXTRA

SERVICE_TIME_MINUTES = 3.0
LATE_GRACE_MINUTES = 0.0
SWAP_MIN_SAVINGS_MINUTES = 8.0
DEFAULT_ROUTE_START_MINUTES = 480.0
ROUTE_START_LEAD_MINUTES = 60.0
# 2-opt (_improve_route) can uncross one crossing per sweep and then
# reveal another it can now see, so this gets more sweeps than
# _relocate_across_routes's cross-route moves (RELOCATE_SWEEPS below)
# typically need to settle.
IMPROVEMENT_SWEEPS = 6
RELOCATE_SWEEPS = 2
# Minimum net distance a stop must save (its removal savings on the
# source route, minus its insertion cost on the target route) before
# _relocate_across_routes actually moves it - stops thrashing a stop back
# and forth for a few hundred meters of "improvement" that isn't real
# given how approximate road-distance estimates already are.
RELOCATE_MIN_SAVINGS_KM = 1.5

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

# --- Geographic outlier detection (route SEPARATION - which route a stop
# belongs to - not route ORDERING, which _improve_route already handles).
# See _extract_geographic_outliers for the full explanation. Every
# threshold here is deliberately RELATIVE to each route's own scale, not
# a fixed km value - a rural route's stops are naturally more spread out
# than a dense urban one, and a fixed threshold would flag half of one and
# none of the other for exactly the same real-world "does this stop
# actually belong here" answer. -------------------------------------------

# A route needs at least this many stops before "outlier" is even a
# meaningful question - a 1-2 stop route has no internal structure to be
# an outlier relative to.
MIN_ROUTE_SIZE_FOR_OUTLIER_CHECK = 3
# A stop is a candidate outlier once its nearest OTHER stop on the same
# route is farther than this many times the route's own median
# nearest-neighbor spacing (a robust "how tight is this route normally"
# baseline - median, not mean, so the outlier itself can't drag its own
# threshold up).
OUTLIER_DISTANCE_FACTOR = 3.0
# ...but never for a genuinely small gap - even 3x a very tight route's
# spacing can be under a km, which isn't worth reacting to. This absolute
# floor (straight-line) is what actually stops "5 close together, 1
# slightly farther" (spec Test Case 5) from being flagged at all.
MIN_OUTLIER_DETOUR_KM = 3.0
# Once a stop IS flagged, it's only actually moved to an existing route if
# that insertion costs no more than this much real (road-distance) detour
# - past this, no existing route is a good enough fit and a fresh vehicle
# is spawned for it instead (see _extract_geographic_outliers).
MAX_OUTLIER_RELOCATE_DETOUR_KM = 6.0


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


def _slot_order_preserved(route_orders: List[Dict[str, object]]) -> bool:
    """True when this route never serves a later delivery slot before an
    earlier one - the actual business rule ("both 12:00 orders must be
    served before the 2:00 one, even though visiting the 2:00 one first is
    geographically tempting"), which is stricter than mere deadline
    feasibility: a stop can still make its own deadline while jumping
    ahead of an earlier-slot stop it has no business preceding. Stops with
    no parseable delivery time (slot is inf) are unconstrained - they can
    sit anywhere. Two stops that share the same slot can be reordered
    freely relative to each other (that's the whole point of letting 2-opt
    resequence within a time window for distance)."""
    finite_slots_in_order = [
        slot for slot in (parse_delivery_slot_minutes(order) for order in route_orders)
        if slot != float("inf")
    ]
    return all(
        finite_slots_in_order[i] <= finite_slots_in_order[i + 1]
        for i in range(len(finite_slots_in_order) - 1)
    )


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


def dedupe_orders_by_id(orders: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """Drops any order whose order_id has already been seen, keeping the
    first occurrence and its position. excel_service.validate_excel_file
    now rejects a duplicate order_id at upload time, but this is the
    choke point every route computation - auto-generate and every manual
    add/remove/reorder/vehicle-toggle - actually runs through, so it's
    where an already-duplicated order (from data uploaded before that
    fix, or from any other caller of these functions) gets caught before
    it can turn into the same address appearing twice on the same route."""
    seen = set()
    deduped: List[Dict[str, object]] = []
    for order in orders:
        key = str(order.get("order_id"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(order)
    return deduped


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
    """2-opt local search: the standard fix for a route whose path
    crosses itself - a stop near the end that's actually much closer to
    the *start* of the route than to its neighbors, which reads as the
    line zig-zagging back across ground it already covered. A plain
    position swap (this function's previous approach) can't fix that in
    general - swapping two stops leaves everything *between* them in its
    original order, so a crossing spanning more than one stop apart
    often can't be undone by any single swap. 2-opt instead reverses the
    whole segment between two edges, which is exactly the move that
    untangles a crossing: reverse the segment between wherever the path
    crosses, and the crossing edges become two non-crossing ones.
    Checks every pair of edges in the route (cheap for the route sizes
    here - a handful to ~20 stops - and cache-backed distance lookups
    after the first pass), not just nearby ones, since a crossing can
    span the whole route."""
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
        n = len(best_orders)
        for i in range(n - 1):
            for j in range(i + 1, n):
                segment = best_orders[i:j + 1]
                candidate = best_orders[:i] + list(reversed(segment)) + best_orders[j + 1:]
                # Reversing this segment reorders every stop inside it
                # relative to every other stop inside it, and that can
                # violate the delivery-slot precedence rule (serve earlier
                # slots before later ones - see _slot_order_preserved)
                # even when every stop still individually makes its own
                # deadline, so deadline feasibility (checked via
                # _simulate_route below) alone isn't enough to guard this.
                # An earlier version instead required every stop in the
                # segment to share one identical delivery slot before even
                # trying the reversal - simple, but far too blunt: it
                # blocked a reversal the instant the segment touched *any*
                # two different slots, even ones the reversal wouldn't
                # actually invert the order of, which in practice blocked
                # almost every reversal on real data (orders almost never
                # share one exact delivery time). Checking the real
                # invariant directly, on the whole candidate route, allows
                # every reversal that doesn't actually break slot order.
                if not _slot_order_preserved(candidate):
                    continue
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


def _relocate_across_routes(vehicles: List[Dict[str, object]], depot: Dict[str, float], route_start_minutes: float) -> None:
    """Fixes the class of mistake build_routes' bucket-by-time,
    nearest-available-vehicle greedy pass can make under capacity
    pressure: by the time it reaches a given stop, every vehicle that's
    actually near it may already be full, so the stop lands on whichever
    vehicle still had a free slot - however far away that vehicle's
    route actually is. _improve_route (above) can't catch this - it only
    reorders stops *within* one route, it never moves a stop to a
    *different* one. This does: for every stop on every route, it checks
    whether removing it and re-inserting it (at its best position) on
    some other route with spare capacity would reduce total distance
    across both routes, and if a real improvement is found, moves it.
    Classic VRP "relocate"/or-opt local search - runs after the initial
    build and after _improve_route's within-route pass."""
    routable = [v for v in vehicles if v["orders"] and all(has_coordinates(o) for o in v["orders"])]
    if len(routable) < 2:
        return

    for _ in range(RELOCATE_SWEEPS):
        moved_any = False
        for source in routable:
            i = 0
            while i < len(source["orders"]):
                order = source["orders"][i]
                without = source["orders"][:i] + source["orders"][i + 1:]

                _, dist_with, _, _ = _simulate_route(source["orders"], depot, route_start_minutes)
                _, dist_without, _, source_feasible_without = _simulate_route(without, depot, route_start_minutes)
                if dist_with is None or dist_without is None or not source_feasible_without:
                    i += 1
                    continue
                removal_savings = dist_with - dist_without

                best_target = None
                best_insert_at = None
                best_insertion_cost = None

                for target in routable:
                    if target is source or len(target["orders"]) >= target["capacity"]:
                        continue
                    _, target_dist_before, _, _ = _simulate_route(target["orders"], depot, route_start_minutes)
                    if target_dist_before is None:
                        continue
                    for insert_at in range(len(target["orders"]) + 1):
                        candidate = target["orders"][:insert_at] + [order] + target["orders"][insert_at:]
                        if not _slot_order_preserved(candidate):
                            continue
                        _, cand_dist, _, cand_feasible = _simulate_route(candidate, depot, route_start_minutes)
                        if cand_dist is None or not cand_feasible:
                            continue
                        insertion_cost = cand_dist - target_dist_before
                        if best_insertion_cost is None or insertion_cost < best_insertion_cost:
                            best_insertion_cost = insertion_cost
                            best_target = target
                            best_insert_at = insert_at

                if best_target is not None and (removal_savings - best_insertion_cost) >= RELOCATE_MIN_SAVINGS_KM:
                    source["orders"] = without
                    best_target["orders"] = best_target["orders"][:best_insert_at] + [order] + best_target["orders"][best_insert_at:]
                    moved_any = True
                    continue  # re-check this same index - a different stop has shifted into it

                i += 1
        if not moved_any:
            break


def _haversine_km(a: Dict[str, float], b: Dict[str, float]) -> float:
    """Straight-line distance, not a road-distance API call - exactly what
    outlier DETECTION should use (see the block comment above the
    OUTLIER_* constants and generate_routes' own reasoning: GPS distance
    for clustering/filtering, road distance only for the final cost check
    on a stop that's already been flagged). Finding "which stop is
    geographically disconnected" for a handful of stops per route, many
    times over as routes settle, would otherwise mean a real network call
    every time - this is instant and free."""
    r_km = 6371.0
    lat1, lat2 = radians(a["lat"]), radians(b["lat"])
    dlat = radians(b["lat"] - a["lat"])
    dlng = radians(b["lng"] - a["lng"])
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlng / 2) ** 2
    return 2 * r_km * asin(sqrt(h))


def _smallest_disconnected_group(route_orders: List[Dict[str, object]]) -> Optional[Tuple[List[int], float, float]]:
    """Finds whether this route is actually more than one geographical
    group stitched together, and if so returns the smallest of those
    groups to peel off - (indexes, that group's nearest gap to the rest,
    the route's own typical spacing) - or None if the route is too small
    to judge, has stops without coordinates, or is genuinely one
    connected cluster.

    A single "this one point's nearest neighbor is far" check (an earlier
    version of this function) catches a lone outlier fine, but silently
    misses two real multi-point clusters stitched into one route: every
    point in a 3-and-3 split still has a *close* neighbor - the other two
    points in its own half - so a per-point nearest-neighbor check alone
    finds nothing wrong. What's actually needed is single-linkage
    clustering: connect any two stops closer than the route's own
    relative threshold (same OUTLIER_DISTANCE_FACTOR/MIN_OUTLIER_DETOUR_KM
    reasoning as before, just applied as an edge cutoff instead of a
    per-point check), take the connected components under that cutoff,
    and treat any component other than the largest as disconnected. This
    is a standard technique (equivalent to cutting a minimum spanning
    tree's longest edges) - reliable for both a single stray point AND
    genuinely separate clusters, since either way it's really asking the
    same question: "does removing one gap split this into pieces that
    were never actually one group?"

    Only the SMALLEST disconnected group is ever returned in one call -
    the caller (_extract_geographic_outliers) handles one group at a
    time, across repeated passes, the same conservative one-at-a-time
    approach the single-outlier version used, rather than restructuring a
    whole route's grouping in one shot from a single scan.
    """
    n = len(route_orders)
    if n < MIN_ROUTE_SIZE_FOR_OUTLIER_CHECK or not all(has_coordinates(o) for o in route_orders):
        return None

    pairwise_km = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = _haversine_km(route_orders[i], route_orders[j])
            pairwise_km[i][j] = pairwise_km[j][i] = d

    nearest_neighbor_km = [min(pairwise_km[i][j] for j in range(n) if j != i) for i in range(n)]
    typical_spacing_km = median(nearest_neighbor_km)
    threshold_km = max(MIN_OUTLIER_DETOUR_KM, typical_spacing_km * OUTLIER_DISTANCE_FACTOR)

    # Union-find single-linkage clustering: two stops end up in the same
    # component iff there's a chain of stops between them where every
    # consecutive link is within threshold_km - exactly cutting every
    # inter-component gap larger than the route's own relative scale.
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if pairwise_km[i][j] <= threshold_km:
                root_i, root_j = find(i), find(j)
                if root_i != root_j:
                    parent[root_i] = root_j

    components: Dict[int, List[int]] = {}
    for i in range(n):
        components.setdefault(find(i), []).append(i)

    if len(components) < 2:
        return None  # one connected group - nothing to split

    smallest = min(components.values(), key=len)
    others = [idx for group in components.values() if group is not smallest for idx in group]
    gap_km = min(pairwise_km[i][j] for i in smallest for j in others)
    return smallest, gap_km, typical_spacing_km


def _extract_geographic_outliers(
    vehicles: List[Dict[str, object]],
    depot: Dict[str, float],
    route_start_minutes: float,
    capacity_by_type: Dict[str, int],
    configured_types: List[str],
    spawn_count: int,
    max_auto_spawns: int,
) -> int:
    """Pass 2 of route separation (Pass 1 is build_routes' own greedy
    assignment + _relocate_across_routes above, both already run by the
    time this is called): catches the one thing neither of those can -
    a stop (or a whole tight cluster of stops) that's genuinely
    geographically disconnected from every route it could plausibly be
    on, including the one it's currently stuck on. _relocate_across_routes
    already moves a *single* stop to a *better existing* route when one
    exists; what nothing before this does is (a) notice a stitched-
    together multi-cluster route at all, or (b) create a *new* route for
    a group that has no good existing home - the literal Point-6 scenario
    (5 tightly clustered stops sharing one time slot, a 6th far away with
    nowhere reasonable to relocate to), and its multi-point cousin (two
    real 3-stop clusters that ended up sharing one route/time-slot bucket).

    For every route, repeatedly: find its smallest disconnected group
    (see _smallest_disconnected_group - single-linkage clustering
    relative to that route's OWN typical spacing, never a fixed km
    value; a lone outlier is just the size-1 case of the same check).
    Try relocating the WHOLE group onto whichever OTHER existing route
    has spare capacity for all of it at the lowest real (road-distance)
    insertion cost; if that cost is within MAX_OUTLIER_RELOCATE_DETOUR_KM,
    do that - no new vehicle needed. Otherwise, if there's still spawn
    budget, a configured vehicle type to spawn as, and that vehicle type
    actually has room for the whole group (the exact spawn mechanism
    build_routes already uses for capacity/deadline overflow - same
    is_auto_created flag), give the group a fresh dedicated route of its
    own. If none of that is possible, the group is left exactly where it
    was - never lost, never duplicated, just not improved this round.

    Repeats across every route until a full sweep makes no change, or
    until a safety cap on total moves - bounded by how many stops exist,
    since each successful move/spawn strictly reduces how much work is
    left to do, but an infinite loop here would be a genuinely bad place
    to have one.
    """
    max_iterations = sum(len(v["orders"]) for v in vehicles) + 1
    for _ in range(max_iterations):
        moved = False

        for source in vehicles:
            if not source["orders"]:
                continue
            found = _smallest_disconnected_group(source["orders"])
            if found is None:
                continue
            indexes, gap_km, typical_spacing_km = found
            index_set = set(indexes)
            group = [order for idx, order in enumerate(source["orders"]) if idx in index_set]
            without = [order for idx, order in enumerate(source["orders"]) if idx not in index_set]
            _, dist_without, _, source_feasible_without = _simulate_route(without, depot, route_start_minutes) if without else (None, 0.0, 0.0, True)
            if dist_without is None or not source_feasible_without:
                continue  # can't cleanly remove it (an unroutable leg elsewhere) - leave it

            group_ids = [order.get("order_id") for order in group]
            print(
                f"[route-separation] disconnected group candidate: order_ids={group_ids} "
                f"gap_from_rest_km={gap_km:.2f} route_typical_spacing_km={typical_spacing_km:.2f}"
            )

            best_target = None
            best_insert_at = None
            best_insertion_cost = None
            for target in vehicles:
                if target is source or target["capacity"] - len(target["orders"]) < len(group):
                    continue
                _, target_dist_before, _, _ = _simulate_route(target["orders"], depot, route_start_minutes)
                if target_dist_before is None:
                    continue
                # Only the two simplest insertion points for the whole
                # group as one block (start, end) - not every position, to
                # keep this to a handful of simulations even for a
                # multi-stop group. Final ordering doesn't depend on this
                # choice anyway: _improve_route (2-opt) resequences the
                # whole route again right after this pass.
                for candidate in (
                    group + target["orders"],
                    target["orders"] + group,
                ):
                    if not _slot_order_preserved(candidate):
                        continue
                    _, cand_dist, _, cand_feasible = _simulate_route(candidate, depot, route_start_minutes)
                    if cand_dist is None or not cand_feasible:
                        continue
                    insertion_cost = cand_dist - target_dist_before
                    if best_insertion_cost is None or insertion_cost < best_insertion_cost:
                        best_insertion_cost = insertion_cost
                        best_target = target
                        best_insert_at = candidate

            if best_target is not None and best_insertion_cost <= MAX_OUTLIER_RELOCATE_DETOUR_KM:
                source["orders"] = without
                best_target["orders"] = best_insert_at
                print(f"[route-separation] decision: relocated group of {len(group)} to an existing route (detour {best_insertion_cost:.2f} km)")
                moved = True
                break

            spawn_type = source["vehicle_type"] if source["vehicle_type"] in capacity_by_type else (configured_types[0] if configured_types else None)
            if spawn_type and capacity_by_type.get(spawn_type, 0) >= len(group) and spawn_count < max_auto_spawns:
                fresh_vehicle = _spawn_vehicle(spawn_type, capacity_by_type[spawn_type], route_start_minutes, depot)
                fresh_vehicle["is_auto_created"] = True
                fresh_vehicle["orders"] = group
                vehicles.append(fresh_vehicle)
                spawn_count += 1
                source["orders"] = without
                print(f"[route-separation] decision: spawned a new route for {len(group)} stop(s) (no existing route within {MAX_OUTLIER_RELOCATE_DETOUR_KM} km detour)")
                moved = True
                break

            print("[route-separation] decision: left in place (no relocation target, no spawn budget/capacity for a new route)")

        if not moved:
            break

    return spawn_count


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

    _relocate_across_routes(vehicles, depot, route_start_minutes)

    # Route SEPARATION's own second pass - catches a stop (or a whole
    # tight cluster of stops) that's geographically disconnected from
    # every route it could be on, including the one the greedy build +
    # relocate above left it on (relocate only ever moves a stop to a
    # *better existing* route on a bare "does this reduce total distance"
    # basis; this is the one thing that can give a genuine outlier group
    # a route of its own). See _extract_geographic_outliers' own
    # docstring - deliberately NOT followed by another plain
    # _relocate_across_routes pass: that pass has no notion of "these two
    # groups were just separated on purpose" and, on a real batch, was
    # observed merging two newly-separated outlier groups straight back
    # together whenever doing so happened to shave a bit off total
    # distance - undoing this pass's whole point. Any consolidation worth
    # doing was already considered *inside* extraction itself, under its
    # own bounded, geography-aware detour cap.
    spawn_count = _extract_geographic_outliers(
        vehicles, depot, route_start_minutes, capacity_by_type, configured_types, spawn_count, max_auto_spawns,
    )

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


def single_stop_maps_link(order: Dict[str, object]) -> str:
    """A precise, single-pin Google Maps link for one delivery - lat/lng
    when the order has coordinates (a literal point, always resolves to
    exactly that spot), sanitized address text as a fallback for orders
    that failed geocoding. Distinct from build_google_maps_url below, which
    is a full multi-stop *directions* link for an entire route - this one
    is for "View on Map" on a single delivery card, where the admin needs
    to see one clear dropped pin, not turn-by-turn directions."""
    target = _stop_link_target(order)
    if not target:
        return ""
    return f"https://www.google.com/maps/search/?api=1&query={quote(target)}"


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


VEHICLE_CAPACITIES = {"car": CAR_CAPACITY, "bike": BIKE_CAPACITY}
VEHICLE_MAX_CAPACITIES = {"car": CAR_MAX_CAPACITY, "bike": BIKE_MAX_CAPACITY}


def vehicle_capacity(vehicle_type: str) -> int:
    """Base capacity - what auto-generate fills to and what every ordinary
    add (Unassigned pool, "Move to...", bulk-assign) is capped at."""
    return VEHICLE_CAPACITIES.get(vehicle_type, BIKE_CAPACITY)


def vehicle_max_capacity(vehicle_type: str) -> int:
    """Hard structural ceiling - what a route can never exceed even
    through the flex "Add Address from Another Route" action."""
    return VEHICLE_MAX_CAPACITIES.get(vehicle_type, BIKE_MAX_CAPACITY)


def recompute_route_metrics(route_orders: List[Dict[str, object]], vehicle_type: str) -> Dict[str, object]:
    """Recomputes everything derived from a route's stop list - distance,
    duration, per-stop ETA/lateness, segments, Maps link, utilization - from
    scratch given the (possibly just-edited) ordered list of order dicts.
    Called after every manual add/remove/reorder so a route's cached
    aggregate fields are never left stale relative to its current stops;
    mirrors the per-route math generate_routes() does for the auto-build
    path so both stay consistent with each other."""
    route_orders = dedupe_orders_by_id(route_orders)
    depot = VELOCHERY_DEPOT
    capacity = vehicle_capacity(vehicle_type)
    route_start_minutes = compute_route_start_minutes(route_orders)
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

    return {
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
        "utilization_percent": round(stops / capacity * 100, 1) if capacity else None,
        "capacity": capacity,
    }


def insert_orders_into_route(
    existing_orders: List[Dict[str, object]], new_orders: List[Dict[str, object]], vehicle_type: str,
) -> List[Dict[str, object]]:
    """Adds `new_orders` into `existing_orders` at each one's actual best
    position - not appended to the end - then runs the same 2-opt local
    search (_improve_route) the automatic build already relies on, for
    any further polish (e.g. a crossing the insertion itself introduced).
    Used by the admin's manual "Add Address from Another Route" action
    (crud.move_orders_between_routes) so a newly-added stop lands where
    it geographically belongs.

    2-opt alone isn't enough for this: it only reverses contiguous
    segments, which can fix a route that crosses itself but can't relocate
    one stop from the end of the list into the middle while leaving every
    other stop's relative order untouched - that's a different move
    (insertion/relocation), the same "try every position, keep the
    cheapest" search _relocate_across_routes and _extract_geographic_
    outliers already use elsewhere in this file, reused here rather than
    a third, separate implementation of the same idea. New orders are
    inserted one at a time (in the order given) so each one sees the
    route as it stands after the previous insertion, not the original."""
    depot = VELOCHERY_DEPOT
    sequence = list(existing_orders)

    for order in new_orders:
        route_start_minutes = compute_route_start_minutes(sequence + [order])
        best_at = len(sequence)  # falls back to "append" only if nothing scores better
        best_distance = None
        for insert_at in range(len(sequence) + 1):
            candidate = sequence[:insert_at] + [order] + sequence[insert_at:]
            if not _slot_order_preserved(candidate):
                continue
            _, candidate_distance, candidate_time, _ = _simulate_route(candidate, depot, route_start_minutes)
            if candidate_time is None:
                continue
            if best_distance is None or candidate_distance < best_distance:
                best_distance = candidate_distance
                best_at = insert_at
        sequence = sequence[:best_at] + [order] + sequence[best_at:]

    route_start_minutes = compute_route_start_minutes(sequence)
    vehicle = {
        "vehicle_type": vehicle_type,
        "capacity": vehicle_max_capacity(vehicle_type),
        "orders": sequence,
        "current_time": route_start_minutes,
        "current_location": dict(depot),
        "is_auto_created": False,
    }
    _improve_route(vehicle, depot, route_start_minutes)
    return vehicle["orders"]


def generate_routes(orders: List[Dict[str, object]], available_cars: int, available_bikes: int) -> Dict[str, object]:
    if not orders:
        return {
            "route_count": 0,
            "routes": [],
            "pending_orders": [],
            "warnings": [],
        }

    deduped_orders = dedupe_orders_by_id(orders)
    duplicate_count = len(orders) - len(deduped_orders)
    orders = deduped_orders

    depot = VELOCHERY_DEPOT
    route_start_minutes = compute_route_start_minutes(orders)
    # One batched OSRM call up front instead of build_routes' own
    # optimization sweeps (_improve_route/_relocate_across_routes)
    # discovering each depot<->order and order<->order leg one at a time
    # as they evaluate candidate stop orderings - see prime_route_cache's
    # own docstring for why this is the actual fix for Generate/Regenerate
    # Routes hanging on a real batch of orders. Best-effort: every existing
    # call still works exactly as before if this can't run (too many
    # orders, or OSRM itself unavailable) - it just won't be as fast.
    prime_route_cache(orders, depot)
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
    if duplicate_count:
        warnings.append(
            f"{duplicate_count} order(s) had a duplicate order_id and were skipped - "
            "only the first occurrence of each was routed."
        )
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
