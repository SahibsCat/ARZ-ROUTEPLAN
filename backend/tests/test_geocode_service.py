import threading
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.geocoding.base import GeocodeResult
from app.geocode_service import (
    clean_address,
    geocode_address,
    geocode_address_detailed,
    geocode_orders,
    geocode_single_address,
    invalidate_cached_geocode,
)


class _FakeGeocoder:
    """Stand-in for any GeocodingProvider that records every address it was
    asked to resolve, so tests can assert on call counts (e.g. dedup
    caching)."""

    def __init__(self, results_by_address):
        self._results_by_address = results_by_address
        self.calls = []

    def geocode(self, address):
        self.calls.append(address)
        return self._results_by_address.get(address)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_clean_address_appends_city_when_missing():
    assert clean_address("12 Main Street") == "12 Main Street, Chennai, India"


def test_clean_address_leaves_city_untouched_when_present():
    assert clean_address("12 Main Street, Chennai") == "12 Main Street, Chennai"


def test_geocode_orders_uses_cache_for_duplicate_addresses(monkeypatch):
    # Two orders share the exact same address - it must only be geocoded
    # once, both to save billed API calls and to avoid pointless duplicate
    # requests.
    fake_result = GeocodeResult(lat=12.9, lng=80.2, formatted_address="Resolved, Chennai", status="OK", provider="nominatim")
    fake_geocoder = _FakeGeocoder({"12 Main Street, Chennai, India": fake_result})
    monkeypatch.setattr("app.geocode_service._build_geocoder", lambda: fake_geocoder)

    orders = [
        {"order_id": "1", "customer_name": "A", "address": "12 Main Street"},
        {"order_id": "2", "customer_name": "B", "address": "12 Main Street"},
    ]

    result, provider_error = geocode_orders(orders)

    assert len(fake_geocoder.calls) == 1
    for order in result:
        assert order["lat"] == 12.9
        assert order["lng"] == 80.2
        assert order["geocoded_address"] == "Resolved, Chennai"


def test_geocode_orders_marks_unresolved_address_as_failed(monkeypatch):
    fake_geocoder = _FakeGeocoder({})
    monkeypatch.setattr("app.geocode_service._build_geocoder", lambda: fake_geocoder)

    orders = [{"order_id": "1", "customer_name": "A", "address": "Nonexistent Place, Chennai"}]

    result, provider_error = geocode_orders(orders)

    assert result[0]["lat"] is None
    assert result[0]["lng"] is None
    assert "geocode_error" in result[0]


def test_geocode_orders_keeps_a_flagged_matchs_coordinates_as_a_suggestion(monkeypatch):
    # A flagged (not hard-failed) result still found SOMETHING real - most
    # often the correct street, just without a confirmed house number.
    # lat/lng must stay None (never silently trusted as the order's real
    # location) but suggested_lat/suggested_lng carry it through so Adjust
    # Location can offer it as a starting pin instead of nothing at all.
    flagged = GeocodeResult(
        lat=13.02, lng=80.21, formatted_address="Some St, Chennai",
        status="NEEDS_MANUAL_VERIFICATION", provider="google", confidence=0.3,
    )
    fake_geocoder = _FakeGeocoder({"12 Main Street, Chennai, India": flagged})
    monkeypatch.setattr("app.geocode_service._build_geocoder", lambda: fake_geocoder)

    orders = [{"order_id": "1", "customer_name": "A", "address": "12 Main Street"}]
    result, _ = geocode_orders(orders)

    assert result[0]["lat"] is None
    assert result[0]["lng"] is None
    assert result[0]["suggested_lat"] == 13.02
    assert result[0]["suggested_lng"] == 80.21
    assert result[0]["confidence"] == 0.3


def test_geocode_address_detailed_returns_suggestion_for_a_flagged_match(monkeypatch):
    flagged = GeocodeResult(
        lat=13.02, lng=80.21, formatted_address="Some St, Chennai",
        status="NEEDS_MANUAL_VERIFICATION", provider="google", confidence=0.3,
    )
    fake_geocoder = _FakeGeocoder({"12 Main Street, Chennai, India": flagged})
    monkeypatch.setattr("app.geocode_service._build_geocoder", lambda: fake_geocoder)

    result = geocode_address_detailed("12 Main Street")

    assert result["lat"] is None
    assert result["suggested_lat"] == 13.02
    assert result["suggested_lng"] == 80.21
    assert result["confidence"] == 0.3
    assert "Needs Manual Verification" in result["geocode_error"]

    # geocode_address() (used where a hard pass/fail is actually wanted -
    # e.g. crud.add_manual_address) keeps its existing plain-None contract;
    # only the _detailed variant changed.
    assert geocode_address("12 Main Street") is None


def test_geocode_address_detailed_returns_none_for_a_true_zero_results(monkeypatch):
    fake_geocoder = _FakeGeocoder({})
    monkeypatch.setattr("app.geocode_service._build_geocoder", lambda: fake_geocoder)

    assert geocode_address_detailed("Nonexistent Place, Chennai") is None


def test_geocode_orders_skips_orders_that_already_have_coordinates(monkeypatch):
    fake_geocoder = _FakeGeocoder({})
    monkeypatch.setattr("app.geocode_service._build_geocoder", lambda: fake_geocoder)

    orders = [{"order_id": "1", "customer_name": "A", "address": "Some Address", "lat": 1.0, "lng": 2.0}]

    result, provider_error = geocode_orders(orders)

    assert fake_geocoder.calls == []
    assert result[0]["lat"] == 1.0
    assert result[0]["lng"] == 2.0


def test_geocode_address_returns_dict_on_success(monkeypatch):
    fake_result = GeocodeResult(lat=13.0, lng=80.1, formatted_address="Resolved Place, Chennai", status="OK", provider="nominatim")
    fake_geocoder = _FakeGeocoder({"12 Main Street, Chennai, India": fake_result})
    monkeypatch.setattr("app.geocode_service._build_geocoder", lambda: fake_geocoder)

    result = geocode_address("12 Main Street")

    assert result == {
        "address": "12 Main Street, Chennai, India",
        "lat": 13.0,
        "lng": 80.1,
        "display_name": "Resolved Place, Chennai",
        "confidence": None,
    }


def test_geocode_address_returns_none_on_empty_input():
    assert geocode_address("") is None


def test_geocode_single_address_delegates_to_geocode_address(monkeypatch):
    fake_result = GeocodeResult(lat=1.0, lng=2.0, formatted_address="X", status="OK", provider="nominatim")
    fake_geocoder = _FakeGeocoder({"Some Address, Chennai, India": fake_result})
    monkeypatch.setattr("app.geocode_service._build_geocoder", lambda: fake_geocoder)

    result = geocode_single_address("Some Address")

    assert result["lat"] == 1.0
    assert result["lng"] == 2.0


def test_geocode_orders_flags_low_confidence_result_as_needs_manual_verification(monkeypatch):
    # A provider (e.g. Mapbox) can return a real match that's too uncertain
    # to trust - that must land in Failed Orders for human review, not be
    # silently accepted as a precise delivery point.
    low_confidence_result = GeocodeResult(
        lat=12.9, lng=80.2, formatted_address="Roughly here, Chennai",
        status="NEEDS_MANUAL_VERIFICATION", provider="mapbox", confidence=0.3,
    )
    fake_geocoder = _FakeGeocoder({"Vague Place, Chennai, India": low_confidence_result})
    monkeypatch.setattr("app.geocode_service._build_geocoder", lambda: fake_geocoder)

    orders = [{"order_id": "1", "customer_name": "A", "address": "Vague Place"}]

    result, provider_error = geocode_orders(orders)

    assert result[0]["lat"] is None
    assert result[0]["lng"] is None
    assert "Needs Manual Verification" in result[0]["geocode_error"]
    assert "0.30" in result[0]["geocode_error"]


def test_geocode_orders_includes_confidence_on_success(monkeypatch):
    fake_result = GeocodeResult(lat=1.0, lng=2.0, formatted_address="X", status="OK", provider="mapbox", confidence=0.87)
    fake_geocoder = _FakeGeocoder({"Some Place, Chennai, India": fake_result})
    monkeypatch.setattr("app.geocode_service._build_geocoder", lambda: fake_geocoder)

    result, provider_error = geocode_orders([{"order_id": "1", "customer_name": "A", "address": "Some Place"}])

    assert result[0]["confidence"] == 0.87
    assert provider_error is None


def test_geocode_orders_stops_immediately_on_provider_error(monkeypatch):
    # A billing/auth/access failure isn't specific to any address - once
    # the provider signals this, every remaining order must be marked with
    # the same clear reason instead of retrying (and failing) individually.
    from app.geocoding.base import GeocodingProviderError

    class _BrokenGeocoder:
        def __init__(self):
            self.calls = []

        def geocode(self, address):
            self.calls.append(address)
            raise GeocodingProviderError("Google Geocoding is not working: billing not enabled", provider="google")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    broken_geocoder = _BrokenGeocoder()
    monkeypatch.setattr("app.geocode_service._build_geocoder", lambda: broken_geocoder)

    orders = [
        {"order_id": "1", "customer_name": "A", "address": "1 Main Street"},
        {"order_id": "2", "customer_name": "B", "address": "2 Main Street"},
        {"order_id": "3", "customer_name": "C", "address": "3 Main Street"},
    ]

    result, provider_error = geocode_orders(orders)

    assert provider_error is not None
    assert "billing not enabled" in provider_error
    assert len(result) == 3
    for order in result:
        assert order["lat"] is None
        assert order["geocode_error"] == provider_error
    # Uncached addresses are now fetched concurrently (see GEOCODE_CONCURRENCY
    # in geocode_service.py - the fix for a real upload's geocoding step
    # timing out server-side), so a broken provider is no longer guaranteed
    # to be hit exactly once before the batch stops - every address already
    # in flight when the first failure is seen gets attempted too, rather
    # than force-cancelled. The guarantee that actually matters, and is
    # asserted above, is that every order ends up correctly marked with the
    # same provider error regardless of how many of the 3 were attempted.
    assert 1 <= len(broken_geocoder.calls) <= 3


def test_geocode_orders_resolves_many_distinct_addresses_to_the_right_order(monkeypatch):
    # The actual regression risk in going concurrent: results must still
    # land back on the ORDER they belong to, not whichever order the
    # network calls happened to finish in.
    results_by_address = {
        f"{i} Main Street, Chennai, India": GeocodeResult(
            lat=float(i), lng=float(i) + 0.5, formatted_address=f"Resolved {i}", status="OK", provider="google",
        )
        for i in range(1, 11)
    }
    fake_geocoder = _FakeGeocoder(results_by_address)
    monkeypatch.setattr("app.geocode_service._build_geocoder", lambda: fake_geocoder)

    orders = [{"order_id": str(i), "customer_name": f"C{i}", "address": f"{i} Main Street"} for i in range(1, 11)]

    result, provider_error = geocode_orders(orders)

    assert provider_error is None
    assert len(fake_geocoder.calls) == 10  # every distinct address attempted exactly once
    for i, order in enumerate(result, start=1):
        assert order["order_id"] == str(i)
        assert order["lat"] == float(i)
        assert order["lng"] == float(i) + 0.5


def test_geocode_orders_actually_fetches_uncached_addresses_concurrently(monkeypatch):
    # Proves the concurrency is real, not just structurally present but
    # accidentally serialized somewhere - a slow fake geocoder (a stand-in
    # for a real network round-trip) records how many calls were ever
    # in-flight at once.
    lock = threading.Lock()
    state = {"current": 0, "max_seen": 0}

    class _SlowGeocoder:
        def geocode(self, address):
            with lock:
                state["current"] += 1
                state["max_seen"] = max(state["max_seen"], state["current"])
            time.sleep(0.05)
            with lock:
                state["current"] -= 1
            return GeocodeResult(lat=1.0, lng=2.0, formatted_address=address, status="OK", provider="google")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("app.geocode_service._build_geocoder", lambda: _SlowGeocoder())

    orders = [{"order_id": str(i), "customer_name": f"C{i}", "address": f"{i} Distinct Street"} for i in range(1, 6)]
    geocode_orders(orders)

    # More than one call was genuinely in flight at the same time - not a
    # strict lower bound on thread-scheduling timing, just proof this
    # isn't secretly still one-at-a-time.
    assert state["max_seen"] > 1


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def test_geocode_orders_reuses_db_cache_across_calls_without_hitting_provider(monkeypatch, db_session):
    # Simulates two separate uploads (two separate geocode_orders calls)
    # that both contain the same address - the second call must be served
    # entirely from the geocoding_cache table, spending zero provider calls.
    fake_result = GeocodeResult(lat=12.9, lng=80.2, formatted_address="Resolved, Chennai", status="OK", provider="google")
    fake_geocoder = _FakeGeocoder({"12 Main Street, Chennai, India": fake_result})
    monkeypatch.setattr("app.geocode_service._build_geocoder", lambda: fake_geocoder)

    first_orders = [{"order_id": "1", "customer_name": "A", "address": "12 Main Street"}]
    geocode_orders(first_orders, db=db_session)
    assert len(fake_geocoder.calls) == 1

    second_orders = [{"order_id": "2", "customer_name": "B", "address": "12 Main Street"}]
    result, _ = geocode_orders(second_orders, db=db_session)

    assert len(fake_geocoder.calls) == 1  # unchanged - served from cache
    assert result[0]["lat"] == 12.9
    assert result[0]["lng"] == 80.2


def test_invalidate_cached_geocode_forces_a_real_relookup(monkeypatch, db_session):
    # The exact scenario this exists for: an address was successfully
    # geocoded once (cached), a validation rule has since gotten stricter,
    # and a retry/re-add on the SAME address text must not just replay the
    # old cached answer - it has to actually ask the provider again.
    stale_result = GeocodeResult(lat=12.9, lng=80.2, formatted_address="Old Wrong Match, Chennai", status="OK", provider="google")
    fresh_result = GeocodeResult(lat=13.5, lng=80.9, formatted_address="Correct Match, Chennai", status="OK", provider="google")
    fake_geocoder = _FakeGeocoder({"12 Main Street, Chennai, India": stale_result})
    monkeypatch.setattr("app.geocode_service._build_geocoder", lambda: fake_geocoder)

    first = geocode_address("12 Main Street", db=db_session)
    assert first["lat"] == 12.9
    assert len(fake_geocoder.calls) == 1

    # Without invalidating, a second call is served straight from cache -
    # confirms the premise before proving the fix.
    second = geocode_address("12 Main Street", db=db_session)
    assert second["lat"] == 12.9
    assert len(fake_geocoder.calls) == 1

    assert invalidate_cached_geocode("12 Main Street", db_session) is True

    # Now that the stale cache entry is gone, swap in what the provider
    # would return today and confirm the next lookup actually reaches it.
    fake_geocoder._results_by_address["12 Main Street, Chennai, India"] = fresh_result
    third = geocode_address("12 Main Street", db=db_session)
    assert third["lat"] == 13.5
    assert len(fake_geocoder.calls) == 2


def test_geocode_address_falls_back_to_a_previously_verified_building(monkeypatch, db_session):
    # The learning mechanism's read side: a DIFFERENT order (a different
    # flat in the same complex, most likely) names a building an admin
    # already hand-verified on some earlier order - Google's own attempt
    # is flagged (a real, common case for a large complex), and the
    # learned coordinate is used instead of landing in Failed Addresses
    # again for a building this system has already been taught.
    from app import crud
    from app.geocoding.address_parser import building_signature

    address = "A103, Urbantree Fantastic, Survey No 106, Vanagaram, Chennai 600077"
    signature = building_signature(address)
    assert signature is not None
    crud.upsert_verified_location(
        db_session, signature, lat=13.06, lng=80.14,
        formatted_address="Urbantree Fantastic, Vanagaram, Chennai",
        sample_address="B204, " + address,
    )

    flagged = GeocodeResult(
        lat=13.061, lng=80.141, formatted_address="Vanagaram, Chennai",
        status="NEEDS_MANUAL_VERIFICATION", provider="google", confidence=0.4,
    )
    fake_geocoder = _FakeGeocoder({})
    monkeypatch.setattr("app.geocode_service._build_geocoder", lambda: fake_geocoder)
    monkeypatch.setattr(fake_geocoder, "geocode", lambda addr: flagged)

    result = geocode_address_detailed(address, db=db_session)

    assert result["lat"] == 13.06
    assert result["lng"] == 80.14


def test_geocode_address_prefers_a_fresh_confident_google_match_over_a_learned_one(monkeypatch, db_session):
    # The learned building is a FALLBACK, never a replacement for a
    # genuinely confident fresh match - Google confirming the exact unit
    # this time is strictly better information than a past order's
    # complex-level coordinate.
    from app import crud
    from app.geocoding.address_parser import building_signature

    address = "A103, Urbantree Fantastic, Survey No 106, Vanagaram, Chennai 600077"
    crud.upsert_verified_location(
        db_session, building_signature(address), lat=13.06, lng=80.14,
        formatted_address="Urbantree Fantastic, Vanagaram, Chennai", sample_address=address,
    )

    confident = GeocodeResult(
        lat=13.0602, lng=80.1401, formatted_address="A103, Urbantree Fantastic, Chennai",
        status="OK", provider="google", confidence=0.95,
    )
    fake_geocoder = _FakeGeocoder({})
    monkeypatch.setattr("app.geocode_service._build_geocoder", lambda: fake_geocoder)
    monkeypatch.setattr(fake_geocoder, "geocode", lambda addr: confident)

    result = geocode_address(address, db=db_session)

    assert result["lat"] == 13.0602
    assert result["display_name"] == "A103, Urbantree Fantastic, Chennai"


def test_geocode_address_does_not_fall_back_when_nothing_has_been_learned(monkeypatch, db_session):
    # No VerifiedLocation exists for this building - must behave exactly
    # as it always has (flagged stays flagged), not silently invent a
    # fallback.
    flagged = GeocodeResult(
        lat=13.0, lng=80.2, formatted_address="Somewhere, Chennai",
        status="NEEDS_MANUAL_VERIFICATION", provider="google", confidence=0.4,
    )
    fake_geocoder = _FakeGeocoder({})
    monkeypatch.setattr("app.geocode_service._build_geocoder", lambda: fake_geocoder)
    monkeypatch.setattr(fake_geocoder, "geocode", lambda addr: flagged)

    result = geocode_address_detailed(
        "A103, Never Seen Apartments, Somewhere Nagar, Chennai 600001", db=db_session
    )

    assert result["lat"] is None
    assert result["confidence"] == 0.4
