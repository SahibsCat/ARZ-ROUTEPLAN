from app import geocode_service as gs
from app.geocoding.base import GeocodeResult, STATUS_OK, STATUS_NEEDS_MANUAL_VERIFICATION


class FakeGeocoder:
    def __init__(self, table):
        self.table = table
        self.calls = []

    def geocode(self, q):
        self.calls.append(q)
        if "Unknown" in q:
            return None
        for key, val in self.table.items():
            if key in q:
                return val
        return None


def _flagged(lat=13.0, lng=80.2):
    return GeocodeResult(lat, lng, "x", STATUS_NEEDS_MANUAL_VERIFICATION, "google", 0.3, "House number mismatch")


def test_flagged_match_becomes_approximate_and_routable():
    orders = [{"order_id": "1", "address": "12A Gandhi Road Velachery 600042", "lat": None, "lng": None,
               "suggested_lat": 12.98, "suggested_lng": 80.22, "confidence": 0.3, "geocode_error": "Needs Manual Verification"}]
    n = gs._rescue_unresolved(FakeGeocoder({}), orders)
    assert n == 1 and orders[0]["lat"] == 12.98
    assert orders[0]["location_source"] == "approximate"
    assert orders[0]["confidence"] <= gs.APPROXIMATE_CONFIDENCE_CAP


def test_not_found_falls_back_to_area_then_pin():
    orders = [{"order_id": "2", "address": "Flat 4, Unknown Apts, Velachery, 600042", "lat": None, "lng": None}]
    fake = FakeGeocoder({"Velachery": GeocodeResult(12.97, 80.22, "Velachery", STATUS_OK, "google", 0.6)})
    assert gs._rescue_unresolved(fake, orders) == 1
    assert orders[0]["lat"] == 12.97 and orders[0]["location_source"] == "approximate"


def test_transient_failure_retry_gives_exact_match():
    orders = [{"order_id": "3", "address": "5 Main Road Adyar", "lat": None, "lng": None, "geocode_error": "x"}]
    fake = FakeGeocoder({"Main Road": GeocodeResult(13.0, 80.25, "ok", STATUS_OK, "google", 0.9)})
    assert gs._rescue_unresolved(fake, orders) == 1
    assert "location_source" not in orders[0] and orders[0].get("geocode_error") is None


def test_empty_address_stays_failed():
    orders = [{"order_id": "4", "address": "", "lat": None, "lng": None}]
    assert gs._rescue_unresolved(FakeGeocoder({}), orders) == 0
