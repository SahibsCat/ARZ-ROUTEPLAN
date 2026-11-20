"""Push notifications to the driver app via Expo's push service.

Used for one thing today: telling a delivery partner, with sound, that a
route has just been assigned to them - even when the app is closed or the
phone is locked. Sending is always best-effort and off the request thread:
a slow or failing push must never slow down or break the admin's
assignment action itself.
"""
import threading
from typing import Dict, List, Optional

import httpx
from sqlalchemy.orm import Session

from app.models import DriverPushToken

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
# Must match the Android notification channel the driver app creates
# (driver-app/src/notifications.js) - that channel is what carries the
# sound + high importance on Android 8+.
ROUTE_CHANNEL_ID = "route-assigned"


def is_expo_token(token: str) -> bool:
    return bool(token) and (token.startswith("ExponentPushToken[") or token.startswith("ExpoPushToken["))


def save_push_token(db: Session, driver_id: int, token: str, platform: Optional[str] = None) -> DriverPushToken:
    """Upserts by token: a phone that changes hands (another driver logs in
    on it) moves to the new driver instead of notifying both."""
    row = db.query(DriverPushToken).filter(DriverPushToken.token == token).first()
    if row is None:
        row = DriverPushToken(driver_id=driver_id, token=token, platform=platform)
        db.add(row)
    else:
        row.driver_id = driver_id
        row.platform = platform or row.platform
    db.commit()
    db.refresh(row)
    return row


def delete_push_token(db: Session, token: str) -> None:
    db.query(DriverPushToken).filter(DriverPushToken.token == token).delete()
    db.commit()


def tokens_for_driver(db: Session, driver_id: int) -> List[str]:
    return [r.token for r in db.query(DriverPushToken).filter(DriverPushToken.driver_id == driver_id).all()]


def build_route_assigned_messages(tokens: List[str], route_name: str, stop_count: int, route_id: int) -> List[Dict[str, object]]:
    stops = f"{stop_count} stop{'s' if stop_count != 1 else ''}"
    return [
        {
            "to": token,
            "title": "🚚 New route assigned",
            "body": f"{route_name} · {stops}. Open RoutePlan to start.",
            "sound": "default",
            "priority": "high",
            "channelId": ROUTE_CHANNEL_ID,
            "data": {"type": "route_assigned", "route_id": route_id},
        }
        for token in tokens
        if is_expo_token(token)
    ]


def _send(messages: List[Dict[str, object]]) -> None:
    try:
        response = httpx.post(
            EXPO_PUSH_URL,
            json=messages,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=10.0,
        )
        print(f"Push: sent {len(messages)} notification(s), HTTP {response.status_code}")
    except Exception as exc:  # noqa: BLE001 - best-effort, never fatal
        print(f"Push: failed to send route notification: {exc}")


def notify_route_assigned(db: Session, driver_id: int, route_name: str, stop_count: int, route_id: int) -> int:
    """Queues the notification on a background thread; returns how many
    phones it was sent to (0 if the driver has never opened the new app
    version, i.e. no token registered yet)."""
    messages = build_route_assigned_messages(tokens_for_driver(db, driver_id), route_name, stop_count, route_id)
    if messages:
        threading.Thread(target=_send, args=(messages,), daemon=True).start()
    return len(messages)
