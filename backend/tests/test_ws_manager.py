import asyncio
import threading

from fastapi.testclient import TestClient

from app.main import app
from app.ws_manager import TrackingConnectionManager


class _FakeWebSocket:
    """Just enough of the WebSocket surface for TrackingConnectionManager -
    accept()/send_json() - to unit-test broadcast delivery without pulling
    in a real ASGI connection. Records every message it "sent" so the test
    can assert on them directly."""

    def __init__(self):
        self.accepted = False
        self.sent = []

    async def accept(self):
        self.accepted = True

    async def send_json(self, message):
        self.sent.append(message)


def test_tracking_connection_manager_broadcasts_across_threads():
    # This is the actual risky mechanism: driver_location_ping_endpoint in
    # main.py is a plain sync `def`, which FastAPI runs in a worker thread -
    # broadcast_threadsafe is what lets that thread safely hand a message
    # back to the event loop thread that owns the WebSocket connections
    # (via asyncio.run_coroutine_threadsafe). Reproduces that exact shape
    # here: a real event loop running on one thread, broadcast_threadsafe
    # called from a different one - without a real FastAPI app, DB, or
    # driver auth in the way, so this isolates the one thing worth being
    # nervous about.
    manager = TrackingConnectionManager()
    ws1, ws2 = _FakeWebSocket(), _FakeWebSocket()
    loop_ready = threading.Event()
    broadcast_done = threading.Event()
    loop_holder = {}

    def run_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop_holder["loop"] = loop
        manager.bind_loop(loop)

        async def setup_and_wait():
            await manager.connect(ws1)
            await manager.connect(ws2)
            loop_ready.set()
            # Keep the loop alive until the broadcast (scheduled from the
            # test's main thread, below) has actually landed.
            while not broadcast_done.is_set():
                await asyncio.sleep(0.01)

        loop.run_until_complete(setup_and_wait())
        loop.close()

    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()
    assert loop_ready.wait(timeout=5), "event loop never became ready"

    # This call happens on the *test's* thread - a different thread than
    # the one running the loop above, exactly mirroring how the sync ping
    # endpoint calls it from FastAPI's threadpool.
    manager.broadcast_threadsafe({"type": "location", "route_id": 42, "lat": 13.0, "lng": 80.2})

    # Give the scheduled coroutine a moment to actually run on the other
    # thread's loop before checking - run_coroutine_threadsafe only
    # *schedules* it, it doesn't block the calling thread until done.
    for _ in range(200):
        if ws1.sent and ws2.sent:
            break
        threading.Event().wait(0.01)
    broadcast_done.set()
    thread.join(timeout=5)

    assert ws1.accepted and ws2.accepted
    assert ws1.sent == [{"type": "location", "route_id": 42, "lat": 13.0, "lng": 80.2}]
    assert ws2.sent == ws1.sent


def test_tracking_websocket_endpoint_delivers_broadcast_message():
    # End-to-end through the real app: proves /ws/tracking is wired to the
    # same shared tracking_manager singleton main.py's ping endpoint
    # broadcasts through, and that lifespan's bind_loop actually took
    # effect. `with TestClient(app) as client` (not the bare constructor
    # test_main.py's HTTP-only tests use) is what makes Starlette actually
    # run the lifespan startup here - without it, bind_loop never runs and
    # broadcast_threadsafe silently no-ops.
    with TestClient(app) as client:
        with client.websocket_connect("/ws/tracking") as websocket:
            from app.ws_manager import manager as shared_manager

            shared_manager.broadcast_threadsafe({"type": "location", "route_id": 7, "lat": 12.9, "lng": 80.1})
            message = websocket.receive_json()

    assert message == {"type": "location", "route_id": 7, "lat": 12.9, "lng": 80.1}
