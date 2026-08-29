"""In-process WebSocket fan-out for live driver location updates.

Single-process design, matching this app's actual deployment (one uvicorn
worker - see the Procfile): every connected admin browser tab holds one
WebSocket to /ws/tracking, and the moment a driver's phone posts a fresh
location (driver_location_ping_endpoint in main.py), that same location is
pushed to every connected tab immediately - no polling round-trip, no
waiting out the next few-second refresh. This is what actually closes the
gap to "looks like Swiggy/Rapido": the phone already pings every ~5s (see
driver-app/src/locationTask.js), so the old bottleneck was never the
driver's GPS - it was the admin side waiting out its own poll interval on
top of that.

Broadcasting to every connected client regardless of which route it's
looking at, rather than a per-route subscribe/unsubscribe protocol: this
fleet is small enough (a handful of vehicles a day) that the extra
messages are free, and it keeps both ends of this protocol trivially
simple - the client just filters to whatever route_ids it cares about,
exactly like it already does with the REST tracking payloads.

If this backend is ever run with more than one worker process, this
in-memory set stops being enough (a ping handled by worker A never reaches
a client connected to worker B) - a real pub/sub (Redis, etc.) would be
needed then. Not attempting to solve for that now since the Procfile runs
a single worker (`uvicorn app.main:app ...`, no --workers flag).
"""
import asyncio
import logging
from typing import Optional, Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class TrackingConnectionManager:
    def __init__(self) -> None:
        self._connections: Set[WebSocket] = set()
        # Captured once, from the main event loop thread, via bind_loop()
        # in main.py's lifespan startup - broadcast_threadsafe (below) is
        # what lets a *sync* endpoint (driver_location_ping_endpoint runs
        # in FastAPI's threadpool, not the event loop thread, since it's a
        # plain `def` not `async def`) safely hand a broadcast back to the
        # loop that actually owns these WebSocket connections.
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def broadcast(self, message: dict) -> None:
        if not self._connections:
            return
        dead = []
        for ws in list(self._connections):
            try:
                await ws.send_json(message)
            except Exception:
                # A closed/broken socket shouldn't take the rest of the
                # broadcast down with it - drop it and keep going.
                dead.append(ws)
        for ws in dead:
            self._connections.discard(ws)

    def broadcast_threadsafe(self, message: dict) -> None:
        """Called from driver_location_ping_endpoint, which FastAPI runs in
        a worker thread (a plain `def` endpoint, not `async def`) - can't
        just `await self.broadcast(...)` from there, since that thread has
        no running event loop of its own. asyncio.run_coroutine_threadsafe
        is the documented way to schedule a coroutine onto a *different*
        thread's already-running loop without blocking the calling thread
        on it."""
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.broadcast(message), self._loop)
        except RuntimeError:
            # Loop already closed (shutting down) - never let a broadcast
            # failure take down the location ping it's piggybacking on.
            logger.debug("tracking broadcast skipped - event loop unavailable")


manager = TrackingConnectionManager()
