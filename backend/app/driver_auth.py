"""Driver authentication - password hashing and opaque bearer-token
sessions for the driver app. Deliberately not a JWT: DriverSession rows
are revocable (deactivate a driver or reset their password and every
session they're holding stops working immediately), which a stateless
signed token can't do without a separate denylist. No new dependency
either - PBKDF2-HMAC-SHA256 via the standard library's hashlib is a
well-regarded password hash, not a hand-rolled scheme."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Header, HTTPException, Depends
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.models import Driver, DriverSession

PBKDF2_ITERATIONS = 260_000
SESSION_LIFETIME_DAYS = 30


def hash_password(password: str, salt: Optional[str] = None) -> tuple:
    """Returns (hash_hex, salt_hex). Pass the stored salt back in to verify
    a login attempt against it; omit it to generate a fresh salt for a new
    password."""
    salt_bytes = bytes.fromhex(salt) if salt else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, PBKDF2_ITERATIONS)
    return digest.hex(), salt_bytes.hex()


def verify_password(password: str, password_hash: str, password_salt: str) -> bool:
    candidate, _ = hash_password(password, password_salt)
    return secrets.compare_digest(candidate, password_hash)


def generate_driver_code(existing_codes: list) -> str:
    """DRV-0001, DRV-0002, ... - next number after the highest one already
    issued, so codes stay sequential even if a driver in the middle was
    never deactivated/removed."""
    numbers = []
    for code in existing_codes:
        if code and code.upper().startswith("DRV-"):
            tail = code[4:]
            if tail.isdigit():
                numbers.append(int(tail))
    next_number = (max(numbers) + 1) if numbers else 1
    return f"DRV-{next_number:04d}"


def create_session(db: Session, driver: Driver) -> DriverSession:
    token = secrets.token_urlsafe(32)
    session = DriverSession(
        driver_id=driver.id,
        token=token,
        expires_at=datetime.now(timezone.utc) + timedelta(days=SESSION_LIFETIME_DAYS),
    )
    db.add(session)
    db.flush()
    return session


def revoke_driver_sessions(db: Session, driver_id: int) -> None:
    """Called on deactivate and on password reset - every existing session
    for this driver stops authenticating immediately."""
    now = datetime.now(timezone.utc)
    (
        db.query(DriverSession)
        .filter(DriverSession.driver_id == driver_id, DriverSession.revoked_at.is_(None))
        .update({"revoked_at": now}, synchronize_session=False)
    )


def get_current_driver(
    authorization: Optional[str] = Header(None), db: Session = Depends(get_db),
) -> Driver:
    """Every driver-app endpoint depends on this instead of trusting a
    driver_id passed in the request body/query - the authenticated
    identity always comes from the token, never from client-supplied
    input, so driver A can never act as driver B by editing a request."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    session = db.query(DriverSession).filter(DriverSession.token == token).first()
    if session is None or session.revoked_at is not None:
        raise HTTPException(status_code=401, detail="Session expired or revoked - please log in again")
    if session.expires_at is not None and session.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Session expired - please log in again")

    driver = db.query(Driver).filter(Driver.id == session.driver_id).first()
    if driver is None:
        raise HTTPException(status_code=401, detail="Session expired or revoked - please log in again")
    if driver.status != "active":
        raise HTTPException(status_code=403, detail="Your account has been deactivated. Please contact the administrator.")
    return driver
