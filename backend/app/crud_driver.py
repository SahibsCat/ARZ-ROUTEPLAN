"""Driver management, assignment, and live-tracking data access - kept
separate from crud.py (which is entirely about orders/routes/route plans)
since this is a distinct concern with its own auth story. Reuses crud.py's
exception hierarchy (RootplanError/RouteNotFoundError) and route helpers
(route_summary, recompute_route_metrics) so a driver's "my route" view and
the admin's route view are always built from the exact same data."""

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload

from app import crud
from app.crud import RootplanError, RouteNotFoundError
from app.driver_auth import create_session, generate_driver_code, hash_password, revoke_driver_sessions, verify_password
from app.models import Driver, DriverLocationPing, DriverSession, Route

# A ping newer than this reads as genuinely live; older than that but
# within the delayed window reads as "location delayed"; anything older
# (or no ping at all) reads as offline. Matches the driver app's ~5-10s
# ping interval with headroom for a couple of missed pings before
# downgrading the admin's status badge.
LIVE_WINDOW_SECONDS = 30
DELAYED_WINDOW_SECONDS = 300


class DriverNotFoundError(RootplanError):
    pass


class DriverConflictError(RootplanError):
    """Username already taken, or similar uniqueness violation."""
    pass


class InvalidCredentialsError(RootplanError):
    pass


class InactiveDriverError(RootplanError):
    pass


class RouteAlreadyStartedError(RootplanError):
    pass


# --------------------------------------------------------------------------
# Driver roster (admin side)
# --------------------------------------------------------------------------

def create_driver(
    db: Session, name: str, username: str, password: str,
    mobile: Optional[str] = None, vehicle_number: Optional[str] = None, notes: Optional[str] = None,
) -> Driver:
    existing = db.query(Driver).filter(Driver.username == username).first()
    if existing is not None:
        raise DriverConflictError(f"Username '{username}' is already in use")

    existing_codes = [d.driver_code for d in db.query(Driver.driver_code).all()]
    # .all() on a single-column query returns tuples in older-style access;
    # be explicit about pulling the string out either way.
    existing_codes = [c[0] if isinstance(c, tuple) else c for c in existing_codes]
    driver_code = generate_driver_code(existing_codes)

    password_hash, password_salt = hash_password(password)
    driver = Driver(
        driver_code=driver_code,
        name=name,
        mobile=mobile,
        username=username,
        password_hash=password_hash,
        password_salt=password_salt,
        vehicle_number=vehicle_number,
        notes=notes,
        status="active",
    )
    db.add(driver)
    db.commit()
    db.refresh(driver)
    return driver


def list_drivers(db: Session) -> List[Driver]:
    return db.query(Driver).order_by(Driver.created_at.desc()).all()


def get_driver(db: Session, driver_id: int) -> Optional[Driver]:
    return db.query(Driver).filter(Driver.id == driver_id).first()


def update_driver(
    db: Session, driver_id: int, name: Optional[str] = None, mobile: Optional[str] = None,
    vehicle_number: Optional[str] = None, notes: Optional[str] = None,
) -> Driver:
    driver = get_driver(db, driver_id)
    if driver is None:
        raise DriverNotFoundError("Driver not found")
    if name is not None:
        driver.name = name
    if mobile is not None:
        driver.mobile = mobile
    if vehicle_number is not None:
        driver.vehicle_number = vehicle_number
    if notes is not None:
        driver.notes = notes
    db.commit()
    db.refresh(driver)
    return driver


def set_driver_status(db: Session, driver_id: int, status: str) -> Driver:
    """Deactivating revokes every session the driver is holding - an
    inactive driver's phone, if still logged in, is locked out on its very
    next request, not just on its next login."""
    if status not in ("active", "inactive"):
        raise RootplanError("status must be 'active' or 'inactive'")
    driver = get_driver(db, driver_id)
    if driver is None:
        raise DriverNotFoundError("Driver not found")
    driver.status = status
    if status == "inactive":
        revoke_driver_sessions(db, driver.id)
    db.commit()
    db.refresh(driver)
    return driver


def delete_driver(db: Session, driver_id: int) -> None:
    """Hard delete. Any route currently assigned to this driver just loses
    its driver (routes.driver_id is ON DELETE SET NULL) rather than being
    touched otherwise, and the driver's sessions/location pings go with
    them (ON DELETE CASCADE on both) - nothing about the route plan or its
    stops changes."""
    driver = get_driver(db, driver_id)
    if driver is None:
        raise DriverNotFoundError("Driver not found")
    db.delete(driver)
    db.commit()


def reset_driver_password(db: Session, driver_id: int, new_password: str) -> Driver:
    driver = get_driver(db, driver_id)
    if driver is None:
        raise DriverNotFoundError("Driver not found")
    driver.password_hash, driver.password_salt = hash_password(new_password)
    revoke_driver_sessions(db, driver.id)
    db.commit()
    db.refresh(driver)
    return driver


def driver_summary(driver: Driver, assigned_route: Optional[Route] = None) -> Dict[str, object]:
    return {
        "id": driver.id,
        "driver_code": driver.driver_code,
        "name": driver.name,
        "mobile": driver.mobile,
        "username": driver.username,
        "vehicle_number": driver.vehicle_number,
        "status": driver.status,
        "notes": driver.notes,
        "assigned_route_id": assigned_route.id if assigned_route else None,
        "assigned_route_name": assigned_route.route_name if assigned_route else None,
        "created_at": driver.created_at.isoformat() if driver.created_at else None,
    }


def list_drivers_with_assignment(db: Session) -> List[Dict[str, object]]:
    drivers = list_drivers(db)
    # One query for every currently-assigned route rather than N+1 -
    # active routes (not soft-deleted, driver_id set) keyed by driver_id.
    routes_by_driver = {
        r.driver_id: r
        for r in db.query(Route).filter(Route.driver_id.isnot(None)).all()
    }
    return [driver_summary(d, routes_by_driver.get(d.id)) for d in drivers]


# --------------------------------------------------------------------------
# Driver login
# --------------------------------------------------------------------------

def authenticate_driver(db: Session, username: str, password: str) -> Tuple[Driver, DriverSession]:
    driver = db.query(Driver).filter(Driver.username == username).first()
    if driver is None or not verify_password(password, driver.password_hash, driver.password_salt):
        raise InvalidCredentialsError("Invalid username or password.")
    if driver.status != "active":
        raise InactiveDriverError("Your account has been deactivated. Please contact the administrator.")
    session = create_session(db, driver)
    db.commit()
    db.refresh(driver)
    return driver, session


# --------------------------------------------------------------------------
# Assignment
# --------------------------------------------------------------------------

def get_driver_current_route(db: Session, driver_id: int) -> Optional[Route]:
    return db.query(Route).filter(Route.driver_id == driver_id).first()


def assign_driver_to_route(db: Session, route_id: int, driver_id: int, force: bool = False) -> Dict[str, object]:
    """Assigns a driver to a route. If that driver is already assigned to a
    *different* route, this refuses unless `force=True` - the caller
    (frontend) shows the "Kumar is already assigned to Route #103. Move
    Kumar to Route #104?" confirmation first, then retries with force=True.
    A route that's already in progress can't have its driver swapped out
    from under a live delivery without an explicit force too."""
    route = db.query(Route).filter(Route.id == route_id).first()
    if route is None:
        raise RouteNotFoundError("Route not found")
    driver = get_driver(db, driver_id)
    if driver is None:
        raise DriverNotFoundError("Driver not found")
    if driver.status != "active":
        raise RootplanError(f"{driver.name} is inactive and can't be assigned to a route")

    previous_route = get_driver_current_route(db, driver_id)
    if previous_route is not None and previous_route.id != route.id and not force:
        return {
            "conflict": True,
            "driver": driver,
            "previous_route": previous_route,
        }

    if route.route_run_status == "in_progress" and route.driver_id and route.driver_id != driver_id and not force:
        return {
            "conflict": True,
            "in_progress": True,
            "driver": driver,
            "previous_route": route,
        }

    if previous_route is not None and previous_route.id != route.id:
        previous_route.driver_id = None
        previous_route.driver = None

    route.driver_id = driver.id
    route.driver = driver.name
    # Every (re)assignment starts the route fresh for whoever's driving it
    # now, even if it's the same driver being assigned to the same route
    # again - that's the admin's way of resetting a route stuck mid-run
    # (e.g. the driver's app crashed before End Route) back to a clean
    # "not started yet" state without a separate reset action. A route
    # that's still genuinely in progress under a different driver already
    # returned a conflict above unless force=True was passed, so reaching
    # this line means the admin explicitly meant this as a fresh start.
    route.route_run_status = "planned"
    route.started_at = None
    route.completed_at = None
    db.commit()
    db.refresh(route)
    return {"conflict": False, "route": route, "driver": driver}


def unassign_driver_from_route(db: Session, route_id: int) -> Route:
    route = db.query(Route).filter(Route.id == route_id).first()
    if route is None:
        raise RouteNotFoundError("Route not found")
    route.driver_id = None
    route.driver = None
    db.commit()
    db.refresh(route)
    return route


# --------------------------------------------------------------------------
# Route lifecycle (start/end) - driver-authenticated
# --------------------------------------------------------------------------

def get_route_for_driver(db: Session, driver: Driver, route_id: int) -> Route:
    """Every driver-facing route action goes through this - the route must
    both exist AND belong to the authenticated driver, so driver A editing
    a request to reference driver B's route_id gets 404, not access."""
    route = db.query(Route).filter(Route.id == route_id).first()
    if route is None or route.driver_id != driver.id:
        raise RouteNotFoundError("Route not found")
    return route


def start_route(db: Session, driver: Driver, route_id: int) -> Route:
    route = get_route_for_driver(db, driver, route_id)
    if route.route_run_status == "completed":
        raise RouteAlreadyStartedError("This route has already been completed")
    route.route_run_status = "in_progress"
    route.started_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(route)
    return route


def end_route(db: Session, driver: Driver, route_id: int) -> Route:
    route = get_route_for_driver(db, driver, route_id)
    route.route_run_status = "completed"
    route.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(route)
    return route


# --------------------------------------------------------------------------
# Live location
# --------------------------------------------------------------------------

def record_location(
    db: Session, driver: Driver, route_id: int,
    lat: float, lng: float, speed: Optional[float] = None,
    heading: Optional[float] = None, accuracy: Optional[float] = None,
) -> DriverLocationPing:
    route = get_route_for_driver(db, driver, route_id)
    ping = DriverLocationPing(
        driver_id=driver.id, route_id=route.id,
        lat=lat, lng=lng, speed=speed, heading=heading, accuracy=accuracy,
    )
    db.add(ping)
    db.commit()
    db.refresh(ping)
    return ping


def _as_aware_utc(value: datetime) -> datetime:
    """Postgres (production) returns tz-aware datetimes for a
    DateTime(timezone=True) column; SQLite (local dev/tests) silently
    drops the tzinfo and returns a naive one - assume UTC for those rather
    than let the subtraction below crash with "can't subtract offset-naive
    and offset-aware datetimes"."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _tracking_status(last_ping: Optional[DriverLocationPing]) -> str:
    if last_ping is None:
        return "offline"
    age = (datetime.now(timezone.utc) - _as_aware_utc(last_ping.recorded_at)).total_seconds()
    if age <= LIVE_WINDOW_SECONDS:
        return "live"
    if age <= DELAYED_WINDOW_SECONDS:
        return "delayed"
    return "offline"


def get_route_tracking(db: Session, route_id: int, path_limit: int = 200) -> Dict[str, object]:
    route = db.query(Route).options(joinedload(Route.driver_ref)).filter(Route.id == route_id).first()
    if route is None:
        raise RouteNotFoundError("Route not found")

    last_ping = (
        db.query(DriverLocationPing)
        .filter(DriverLocationPing.route_id == route_id)
        .order_by(DriverLocationPing.recorded_at.desc())
        .first()
    )
    status = _tracking_status(last_ping) if route.route_run_status == "in_progress" else (
        "completed" if route.route_run_status == "completed" else "not_started"
    )

    path = []
    if path_limit:
        recent = (
            db.query(DriverLocationPing)
            .filter(DriverLocationPing.route_id == route_id)
            .order_by(DriverLocationPing.recorded_at.desc())
            .limit(path_limit)
            .all()
        )
        path = [
            {"lat": p.lat, "lng": p.lng, "recorded_at": p.recorded_at.isoformat()}
            for p in reversed(recent)
        ]

    return {
        "route_id": route.id,
        "route_run_status": route.route_run_status,
        "driver": driver_summary(route.driver_ref) if route.driver_ref else None,
        "tracking_status": status,
        "last_location": (
            {
                "lat": last_ping.lat, "lng": last_ping.lng,
                "speed": last_ping.speed, "heading": last_ping.heading,
                "accuracy": last_ping.accuracy,
                "recorded_at": last_ping.recorded_at.isoformat(),
                "maps_url": f"https://www.google.com/maps/search/?api=1&query={last_ping.lat}%2C{last_ping.lng}",
            }
            if last_ping else None
        ),
        "path": path,
    }


# --------------------------------------------------------------------------
# Driver's own route view (driver app)
# --------------------------------------------------------------------------

def get_driver_active_route(db: Session, driver: Driver) -> Optional[Dict[str, object]]:
    """The driver's one currently-assigned route, in the same shape
    crud.route_summary() gives the admin - same stop order, same ETAs,
    same map link, so the driver app and admin panel never disagree about
    what the route actually is."""
    route = get_driver_current_route(db, driver.id)
    if route is None:
        return None
    # route_run_status/started_at/completed_at are on crud.route_summary()
    # itself now - see its docstring-adjacent comment for why (this used to
    # patch them in ad hoc here, which the /start and /end endpoints never
    # did, so the driver app's UI would only show "in progress" after a
    # manual refresh that happened to come through this function).
    return crud.route_summary(route)
