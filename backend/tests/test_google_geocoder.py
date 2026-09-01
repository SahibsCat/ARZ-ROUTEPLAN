import pytest

from app.geocoding.base import GeocodingProviderError
from app.geocoding.google_geocoder import PLACES_FALLBACK_CONFIDENCE, GoogleGeocoder


class _DummyResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _DummyClient:
    def __init__(self, responses):
        self._responses = responses
        self.call_count = 0

    def get(self, url, params=None):
        index = min(self.call_count, len(self._responses) - 1)
        self.call_count += 1
        return _DummyResponse(self._responses[index])


def test_geocode_returns_result_on_ok_status():
    data = {
        "status": "OK",
        "results": [{
            "formatted_address": "XYZ Apartment, Velachery, Chennai, Tamil Nadu 600042, India",
            "geometry": {
                "location": {"lat": 12.99, "lng": 80.22},
                "location_type": "ROOFTOP",
            },
            "types": ["premise"],
        }],
    }
    client = _DummyClient([data])
    geocoder = GoogleGeocoder(api_key="test-key", client=client, retry_backoff_seconds=0)

    result = geocoder.geocode("XYZ Apartment, Velachery, Chennai")

    assert result is not None
    assert result.lat == 12.99
    assert result.lng == 80.22
    assert result.status == "OK"
    assert result.provider == "google"
    assert result.confidence == 0.95
    assert client.call_count == 1


# --- location_type/types/partial_match precision scoring -------------------
# The actual fix for "address text is right, map pin is wrong": Google
# doesn't error out when it can't match a specific building - it falls back
# to the nearest area it CAN match (street/neighbourhood/postal code/city)
# and still returns a clean formatted_address for that broader area. These
# lock in that a rough match is now flagged instead of silently trusted.

def _ok_response(location_type, types, partial_match=False, lat=13.0, lng=80.2):
    return {
        "status": "OK",
        "results": [{
            "formatted_address": "Some Address, Chennai, Tamil Nadu, India",
            "geometry": {"location": {"lat": lat, "lng": lng}, "location_type": location_type},
            "types": types,
            "partial_match": partial_match,
        }],
    }


def test_rooftop_street_address_is_high_confidence_ok():
    client = _DummyClient([_ok_response("ROOFTOP", ["street_address"])])
    geocoder = GoogleGeocoder(api_key="test-key", client=client, retry_backoff_seconds=0)

    result = geocoder.geocode("A real, complete street address")

    assert result.status == "OK"
    assert result.confidence >= 0.9


def test_approximate_locality_only_match_is_flagged_not_trusted():
    # THE BUG CASE: Google fell all the way back to a city/neighbourhood
    # centroid - before this fix, this was accepted as a fully-precise pin.
    client = _DummyClient([_ok_response("APPROXIMATE", ["locality", "political"])])
    geocoder = GoogleGeocoder(api_key="test-key", client=client, retry_backoff_seconds=0)

    result = geocoder.geocode("An address Google could only place at city level")

    assert result is not None  # still returns coordinates - just not trusted as-is
    assert result.status == "NEEDS_MANUAL_VERIFICATION"
    assert result.confidence < 0.5


def test_geometric_center_of_a_route_with_no_number_match_is_flagged():
    client = _DummyClient([_ok_response("GEOMETRIC_CENTER", ["route"])])
    geocoder = GoogleGeocoder(api_key="test-key", client=client, retry_backoff_seconds=0)

    result = geocoder.geocode("A street with no specific number matched")

    assert result.status == "NEEDS_MANUAL_VERIFICATION"


def test_geometric_center_of_a_specific_premise_is_ok():
    client = _DummyClient([_ok_response("GEOMETRIC_CENTER", ["premise"])])
    geocoder = GoogleGeocoder(api_key="test-key", client=client, retry_backoff_seconds=0)

    result = geocoder.geocode("A specific building matched by its footprint")

    assert result.status == "OK"


def test_partial_match_lowers_confidence_below_threshold_for_a_weak_match():
    # Rooftop + partial_match should still hold up fine...
    strong = _DummyClient([_ok_response("ROOFTOP", ["street_address"], partial_match=True)])
    strong_result = GoogleGeocoder(api_key="k", client=strong, retry_backoff_seconds=0).geocode("addr")
    assert strong_result.status == "OK"

    # ...but a borderline match combined with partial_match should tip over
    # into flagged, since Google is admitting it couldn't match everything.
    weak = _DummyClient([_ok_response("APPROXIMATE", ["premise"], partial_match=True)])
    weak_result = GoogleGeocoder(api_key="k", client=weak, retry_backoff_seconds=0).geocode("addr")
    assert weak_result.status == "NEEDS_MANUAL_VERIFICATION"


def test_landmark_stripped_retry_rescues_a_match_the_raw_address_missed():
    # "near Big Mall" describes a DIFFERENT nearby place, not the delivery
    # address - Google can only geometric-center a whole route with that
    # noise in the query, but resolves cleanly to a real ROOFTOP once it's
    # stripped. This is the free (same-API) automatic retry, no Places call
    # needed - it should stop as soon as the stripped variant scores OK.
    responses = [
        _ok_response("GEOMETRIC_CENTER", ["route"]),  # the raw address, landmark noise confuses it
        _ok_response("ROOFTOP", ["street_address"], lat=13.05, lng=80.25),  # the stripped variant
    ]
    client = _DummyClient(responses)
    geocoder = GoogleGeocoder(api_key="test-key", client=client, retry_backoff_seconds=0)

    result = geocoder.geocode("12 Example St near Big Mall, Chennai")

    assert result.status == "OK"
    assert result.lat == 13.05
    assert client.call_count == 2  # stopped after the stripped retry - no Places call needed


def test_places_fallback_rescues_a_named_apartment_the_geocoding_api_cant_place():
    # THE OTHER BUG CASE: a named apartment complex ("Sidharth Upscale
    # Apartments") isn't a street + number, so the structured Geocoding API
    # can only geometric-center the surrounding route. Places' fuzzy text
    # search is built for exactly this - matching a named establishment.
    responses = [
        _ok_response("GEOMETRIC_CENTER", ["route"]),  # Geocoding API: only the street, not the building
        {
            "status": "OK",
            "candidates": [{
                "geometry": {"location": {"lat": 13.02, "lng": 80.21}},
                "formatted_address": "Sidharth Upscale Apartments, Porur, Chennai, Tamil Nadu, India",
                "name": "Sidharth Upscale Apartments",
            }],
        },
    ]
    client = _DummyClient(responses)
    geocoder = GoogleGeocoder(api_key="test-key", client=client, retry_backoff_seconds=0)

    result = geocoder.geocode("Sidharth Upscale Apartments, Porur")

    assert result.status == "OK"
    assert result.lat == 13.02
    assert result.provider == "google-places"
    assert result.confidence == PLACES_FALLBACK_CONFIDENCE
    assert client.call_count == 2  # no landmark phrase to strip, straight to the Places fallback


def test_geocode_returns_none_on_zero_results_after_places_fallback_also_empty():
    # ZERO_RESULTS doesn't retry the Geocoding API itself (that's still
    # pointless - retrying identical input gets identical ZERO_RESULTS) but
    # DOES still try the Places API fallback once, since Places' fuzzy
    # search can occasionally find a named place the structured Geocoding
    # API found nothing for at all. The dummy client's one canned response
    # has no "candidates" key, so that fallback call comes back empty too.
    client = _DummyClient([{"status": "ZERO_RESULTS"}])
    geocoder = GoogleGeocoder(api_key="test-key", client=client, max_retries=3, retry_backoff_seconds=0)

    result = geocoder.geocode("Nonexistent Address")

    assert result is None
    assert client.call_count == 2


def test_geocode_raises_provider_error_on_request_denied():
    # REQUEST_DENIED (bad key, billing not enabled, ...) is an account
    # problem, not specific to this address - every remaining address
    # would fail identically, so this must raise so the caller can stop
    # immediately instead of retrying per address.
    client = _DummyClient([{"status": "REQUEST_DENIED", "error_message": "You must enable Billing"}])
    geocoder = GoogleGeocoder(api_key="bad-key", client=client, max_retries=3, retry_backoff_seconds=0)

    with pytest.raises(GeocodingProviderError) as exc_info:
        geocoder.geocode("Some Address")

    assert "You must enable Billing" in str(exc_info.value)
    assert client.call_count == 1


def test_geocode_returns_none_on_invalid_request_after_places_fallback_also_empty():
    # Same reasoning as the ZERO_RESULTS case above - no point retrying the
    # Geocoding API on identical malformed input, but the Places fallback is
    # still tried once, and comes back empty (no "candidates" in the dummy
    # response) rather than crashing.
    client = _DummyClient([{"status": "INVALID_REQUEST"}])
    geocoder = GoogleGeocoder(api_key="test-key", client=client, max_retries=3, retry_backoff_seconds=0)

    result = geocoder.geocode("Some malformed query")

    assert result is None
    assert client.call_count == 2


def test_geocode_retries_on_over_query_limit_then_succeeds():
    responses = [
        {"status": "OVER_QUERY_LIMIT"},
        {"status": "OVER_QUERY_LIMIT"},
        {
            "status": "OK",
            "results": [{
                "formatted_address": "Resolved Address, Chennai",
                # ROOFTOP/street_address so this scores a confident OK and
                # geocode() returns right after this 3rd attempt - without
                # it, a real Google response would never omit these, but a
                # bare-minimum fixture like this would score low and trigger
                # this test's OWN unrelated concern (the Places fallback)
                # instead of testing what it's meant to: the OVER_QUERY_LIMIT
                # retry loop.
                "geometry": {"location": {"lat": 13.0, "lng": 80.2}, "location_type": "ROOFTOP"},
                "types": ["street_address"],
            }],
        },
    ]
    client = _DummyClient(responses)
    geocoder = GoogleGeocoder(api_key="test-key", client=client, max_retries=5, retry_backoff_seconds=0)

    result = geocoder.geocode("Some Busy Address")

    assert result is not None
    assert result.lat == 13.0
    assert result.status == "OK"
    assert client.call_count == 3


def test_geocode_gives_up_after_max_retries_on_persistent_over_query_limit():
    client = _DummyClient([{"status": "OVER_QUERY_LIMIT"}])
    geocoder = GoogleGeocoder(api_key="test-key", client=client, max_retries=3, retry_backoff_seconds=0)

    result = geocoder.geocode("Some Address")

    assert result is None
    # 3 retries against the Geocoding API, then one more for the Places
    # fallback (also empty - the dummy response has no "candidates").
    assert client.call_count == 4


def test_geocode_returns_none_without_api_key():
    client = _DummyClient([{"status": "OK", "results": []}])
    geocoder = GoogleGeocoder(api_key="", client=client)

    result = geocoder.geocode("Some Address")

    assert result is None
    assert client.call_count == 0
