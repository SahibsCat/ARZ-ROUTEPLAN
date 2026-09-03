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

def _ok_response(location_type, types, partial_match=False, lat=13.0, lng=80.2, address_components=None):
    return {
        "status": "OK",
        "results": [{
            "formatted_address": "Some Address, Chennai, Tamil Nadu, India",
            "geometry": {"location": {"lat": lat, "lng": lng}, "location_type": location_type},
            "types": types,
            "partial_match": partial_match,
            "address_components": address_components or [],
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
        _ok_response(  # the stripped variant - a real ROOFTOP match confirms the "12" too
            "ROOFTOP", ["street_address"], lat=13.05, lng=80.25,
            address_components=[{"long_name": "12", "types": ["street_number"]}],
        ),
    ]
    client = _DummyClient(responses)
    geocoder = GoogleGeocoder(api_key="test-key", client=client, retry_backoff_seconds=0)

    result = geocoder.geocode("12 Example St near Big Mall, Chennai")

    assert result.status == "OK"
    assert result.lat == 13.05
    assert client.call_count == 2  # stopped after the stripped retry - no Places call needed


def _find_place_response(place_id="place123"):
    return {"status": "OK", "candidates": [{"place_id": place_id}]}


def _place_details_response(lat, lng, formatted_address, address_components=None):
    return {
        "status": "OK",
        "result": {
            "geometry": {"location": {"lat": lat, "lng": lng}},
            "formatted_address": formatted_address,
            "address_components": address_components or [],
        },
    }


def test_places_fallback_rescues_a_named_apartment_the_geocoding_api_cant_place():
    # THE OTHER BUG CASE: a named apartment complex ("Sidharth Upscale
    # Apartments") isn't a street + number, so the structured Geocoding API
    # can only geometric-center the surrounding route. Places' fuzzy text
    # search is built for exactly this - matching a named establishment.
    # Find Place From Text can only return a place_id (it has no
    # address_components field) - the actual location and component
    # validation come from the Place Details follow-up call.
    responses = [
        _ok_response("GEOMETRIC_CENTER", ["route"]),  # Geocoding API: only the street, not the building
        _find_place_response(),
        _place_details_response(
            13.02, 80.21, "Sidharth Upscale Apartments, Porur, Chennai, Tamil Nadu, India",
        ),
    ]
    client = _DummyClient(responses)
    geocoder = GoogleGeocoder(api_key="test-key", client=client, retry_backoff_seconds=0)

    result = geocoder.geocode("Sidharth Upscale Apartments, Porur")

    assert result.status == "OK"
    assert result.lat == 13.02
    assert result.provider == "google-places"
    assert result.confidence == PLACES_FALLBACK_CONFIDENCE
    assert client.call_count == 3  # no landmark phrase to strip: raw geocode, find place, place details


def test_places_fallback_never_accepts_a_match_in_the_wrong_locality():
    # THE REPORTED BUG: "...Bharathi Nagar, Velachery...600113" resolved via
    # Places to a real but WRONG place - "Sarathy Nagar" a few streets over,
    # a different PIN entirely (600042) - previously accepted outright at a
    # flat 0.65 with no cross-check against what the customer actually
    # typed. The Place Details follow-up's address_components now run
    # through the same _score_component_match every Geocoding API result
    # already gets, so this must be capped below the accept threshold
    # instead of silently trusted.
    responses = [
        _ok_response("GEOMETRIC_CENTER", ["route"]),  # only the street matched, not the named PG
        _find_place_response(),
        _place_details_response(
            12.9749, 80.2224,
            "X6FC+XX2, Sarathy Nagar, Velachery, Chennai, Tamil Nadu 600042, India",
            address_components=[
                {"long_name": "Sarathy Nagar", "types": ["sublocality", "sublocality_level_1"]},
                {"long_name": "600042", "types": ["postal_code"]},
            ],
        ),
    ]
    client = _DummyClient(responses)
    geocoder = GoogleGeocoder(api_key="test-key", client=client, retry_backoff_seconds=0)

    result = geocoder.geocode(
        "Narayana Swami Men's PG, Bhavani St, Bharathi Nagar, Velachery, Chennai, Tamil Nadu 600113"
    )

    assert result.status == "NEEDS_MANUAL_VERIFICATION"
    assert result.confidence < 0.5
    assert client.call_count == 3


def test_places_fallback_still_ok_when_place_details_confirms_the_right_locality():
    # The other half of the same fix: a Places match that DOES agree with
    # the customer's stated PIN/locality must not be penalized just for
    # having gone through Place Details - it still lands at the normal
    # PLACES_FALLBACK_CONFIDENCE, same as before this change.
    responses = [
        _ok_response("GEOMETRIC_CENTER", ["route"]),
        _find_place_response(),
        _place_details_response(
            12.9906, 80.2181,
            "Narayana Swami Men's PG, Bhavani St, Bharathi Nagar, Velachery, Chennai, Tamil Nadu 600113, India",
            address_components=[
                {"long_name": "Bharathi Nagar", "types": ["sublocality", "sublocality_level_1"]},
                {"long_name": "600113", "types": ["postal_code"]},
            ],
        ),
    ]
    client = _DummyClient(responses)
    geocoder = GoogleGeocoder(api_key="test-key", client=client, retry_backoff_seconds=0)

    result = geocoder.geocode(
        "Narayana Swami Men's PG, Bhavani St, Bharathi Nagar, Velachery, Chennai, Tamil Nadu 600113"
    )

    assert result.status == "OK"
    assert result.confidence == PLACES_FALLBACK_CONFIDENCE
    assert client.call_count == 3


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


# --- Address-component validation (_score_component_match / spec examples) -
# a SECOND confidence signal alongside location_type/types: catches Google
# confidently geocoding the WRONG place (a ROOFTOP-precise match for a
# same-named building in a different locality), which the precision-only
# score above has no way to see on its own.

def test_score_component_match_catches_wrong_locality_despite_rooftop_precision():
    from app.geocoding.google_geocoder import _score_component_match

    customer = "ABC Apartments, XYZ Street, Velachery, Chennai, Tamil Nadu 600042"
    google_components = [
        {"long_name": "ABC Apartments", "types": ["premise"]},
        {"long_name": "XYZ Street", "types": ["route"]},
        {"long_name": "Thiruvanmiyur", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "Chennai", "types": ["locality"]},
        {"long_name": "600041", "types": ["postal_code"]},
    ]

    assert _score_component_match(customer, google_components) is not None
    assert _score_component_match(customer, google_components) < 0.5


def test_score_component_match_finds_no_issue_with_a_correct_match():
    from app.geocoding.google_geocoder import _score_component_match

    customer = "ABC Apartments, XYZ Street, Velachery, Chennai, Tamil Nadu 600042"
    google_components = [
        {"long_name": "ABC Apartments", "types": ["premise"]},
        {"long_name": "XYZ Street", "types": ["route"]},
        {"long_name": "Velachery", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "Chennai", "types": ["locality"]},
        {"long_name": "600042", "types": ["postal_code"]},
    ]

    assert _score_component_match(customer, google_components) is None


def test_score_component_match_flags_pin_code_mismatch():
    from app.geocoding.google_geocoder import _score_component_match

    customer = "Some Address, Velachery, Chennai 600042"
    google_components = [
        {"long_name": "Velachery", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "600032", "types": ["postal_code"]},
    ]

    assert _score_component_match(customer, google_components) is not None


def test_score_component_match_tolerates_pure_formatting_differences():
    """Spec example: same real address, differently formatted/ordered -
    must NOT be flagged just because the words don't line up 1:1."""
    from app.geocoding.google_geocoder import _score_component_match

    customer = "Flat 2B ABC Apts XYZ St Velachery Chennai 600042"
    google_components = [
        {"long_name": "2B", "types": ["subpremise"]},
        {"long_name": "ABC Apartments", "types": ["premise"]},
        {"long_name": "XYZ Street", "types": ["route"]},
        {"long_name": "Velachery", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "Chennai", "types": ["locality"]},
        {"long_name": "600042", "types": ["postal_code"]},
    ]

    assert _score_component_match(customer, google_components) is None


def test_score_component_match_has_no_opinion_on_a_bare_locality_query():
    """A customer address with nothing specific to compare (just a bare
    locality name, no significant tokens, no PIN) must defer entirely to
    the precision-based score, not get penalized for "no overlap" when
    there was nothing to overlap with in the first place."""
    from app.geocoding.google_geocoder import _score_component_match

    assert _score_component_match("Velachery, Chennai", [
        {"long_name": "Velachery", "types": ["sublocality", "sublocality_level_1"]},
    ]) is None


def test_geocode_downgrades_a_rooftop_match_in_the_wrong_locality():
    """End-to-end: even a location_type=ROOFTOP/street_address result (the
    highest precision score _score_result alone can give) must still be
    flagged when Google's own returned locality doesn't match anything in
    the customer's address - confirms the two signals actually combine in
    GoogleGeocoder.geocode(), not just in the standalone scoring function."""
    address_components = [
        {"long_name": "ABC Apartments", "types": ["premise"]},
        {"long_name": "Thiruvanmiyur", "types": ["sublocality", "sublocality_level_1"]},
    ]
    client = _DummyClient([
        _ok_response("ROOFTOP", ["street_address"], address_components=address_components),
    ])
    geocoder = GoogleGeocoder(api_key="test-key", client=client, retry_backoff_seconds=0)

    result = geocoder.geocode("ABC Apartments, Velachery, Chennai")

    assert result.status == "NEEDS_MANUAL_VERIFICATION"
    assert result.confidence < 0.95  # would have been 0.95 from precision alone


# --- House/door-number validation (_extract_house_number /
# _score_component_match's third check) - a building name is never
# required; a customer address that's just house-number + street + locality
# must be handled correctly on its own, and "Google found the street but not
# necessarily this exact house number" must never auto-accept.

def test_extract_house_number_handles_every_format_the_spec_lists():
    from app.geocoding.google_geocoder import _extract_house_number

    assert _extract_house_number("24 XYZ Street, Velachery, Chennai 600042") == "24"
    assert _extract_house_number("12A XYZ Road, Velachery, Chennai") == "12A"
    assert _extract_house_number("12-B XYZ Road, Velachery, Chennai") == "12-B"
    assert _extract_house_number("12/2 XYZ Street, Velachery") == "12/2"
    assert _extract_house_number("12/2A XYZ Street, Velachery") == "12/2A"
    assert _extract_house_number("Plot 24, XYZ Road, Chennai") == "24"
    assert _extract_house_number("Door No 15, ABC Main Road, Chennai 600042") == "15"
    assert _extract_house_number("D.No 12, XYZ Street, Chennai") == "12"
    assert _extract_house_number("No. 12, XYZ Street, Chennai") == "12"
    # A flat/unit number can precede the house number - spec's own example.
    assert _extract_house_number("Flat 2B, 12 XYZ Street, Chennai") == "12"
    # A building name can precede it too - Case 1 must keep working.
    assert _extract_house_number("ABC Apartments, 12 XYZ Street, Velachery, Chennai 600042") == "12"
    # No house number stated at all - must not invent one.
    assert _extract_house_number("Velachery, Chennai") is None
    # A 6-digit run is a PIN code, never mistaken for a house number.
    assert _extract_house_number("600042, Velachery, Chennai") is None


def test_score_component_match_accepts_an_exact_house_level_result():
    """Spec Test 1/2/16: house number + street, no building name required -
    Google confirms the exact house number -> no penalty."""
    from app.geocoding.google_geocoder import _score_component_match

    customer = "24 XYZ Street, Velachery, Chennai 600042"
    google_components = [
        {"long_name": "24", "types": ["street_number"]},
        {"long_name": "XYZ Street", "types": ["route"]},
        {"long_name": "Velachery", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "Chennai", "types": ["locality"]},
        {"long_name": "600042", "types": ["postal_code"]},
    ]

    assert _score_component_match(customer, google_components) is None


def test_score_component_match_flags_street_found_but_house_number_unconfirmed():
    """Spec Test 3/15/20 - THE central case this turn is about: Google only
    matched the street, not the specific house number. Must not be treated
    as exact, and the resulting cap must be low enough to actually require
    verification (below the 0.5 accept threshold) on its own."""
    from app.geocoding.google_geocoder import STREET_NUMBER_UNCONFIRMED_CONFIDENCE_CAP, _score_component_match

    customer = "24 XYZ Street, Velachery, Chennai 600042"
    google_components = [
        {"long_name": "XYZ Street", "types": ["route"]},  # no street_number component at all
        {"long_name": "Velachery", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "Chennai", "types": ["locality"]},
        {"long_name": "600042", "types": ["postal_code"]},
    ]

    cap = _score_component_match(customer, google_components)
    assert cap == STREET_NUMBER_UNCONFIRMED_CONFIDENCE_CAP
    assert cap < 0.5


def test_score_component_match_flags_a_different_house_number_on_the_same_street():
    """Spec section 8: customer says 24, Google's result is for 22 - a
    stronger, more specific mismatch than merely "unconfirmed"."""
    from app.geocoding.google_geocoder import STREET_NUMBER_MISMATCH_CONFIDENCE_CAP, _score_component_match

    customer = "24 XYZ Street, Velachery, Chennai 600042"
    google_components = [
        {"long_name": "22", "types": ["street_number"]},
        {"long_name": "XYZ Street", "types": ["route"]},
        {"long_name": "Velachery", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "Chennai", "types": ["locality"]},
        {"long_name": "600042", "types": ["postal_code"]},
    ]

    assert _score_component_match(customer, google_components) == STREET_NUMBER_MISMATCH_CONFIDENCE_CAP


def test_score_component_match_never_requires_a_building_name():
    """Spec's most-repeated non-negotiable rule: an address with no
    building name (just house number + street + locality + city + PIN) is
    completely valid and must be handled the same as one that has a
    building name - confirmed here by using the exact same customer address
    style as the "building name available" tests above, minus the name."""
    from app.geocoding.google_geocoder import _score_component_match

    customer = "12 XYZ Street, Velachery, Chennai 600042"  # no building name anywhere
    google_components = [
        {"long_name": "12", "types": ["street_number"]},
        {"long_name": "XYZ Street", "types": ["route"]},
        {"long_name": "Velachery", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "Chennai", "types": ["locality"]},
        {"long_name": "600042", "types": ["postal_code"]},
    ]

    assert _score_component_match(customer, google_components) is None


def test_score_component_match_does_not_invent_a_house_number_penalty_when_none_was_stated():
    """A bare locality query (or any address the extractor can't find a
    confident house number in) must never get a manufactured penalty - the
    house-number check only ever engages when the customer's own address
    actually contained one."""
    from app.geocoding.google_geocoder import _score_component_match

    assert _score_component_match("Velachery, Chennai", [
        {"long_name": "Velachery", "types": ["sublocality", "sublocality_level_1"]},
    ]) is None


def test_geocode_flags_a_street_level_only_match_even_at_high_precision_location_type():
    """End-to-end: RANGE_INTERPOLATED (a location_type _score_result alone
    scores at 0.8, comfortably over the accept threshold) must still be
    flagged once the house-number check runs and finds no street_number
    component at all - the exact scenario this turn's spec is about."""
    address_components = [
        {"long_name": "XYZ Street", "types": ["route"]},
        {"long_name": "Velachery", "types": ["sublocality", "sublocality_level_1"]},
    ]
    client = _DummyClient([
        _ok_response("RANGE_INTERPOLATED", ["street_address"], address_components=address_components),
    ])
    geocoder = GoogleGeocoder(api_key="test-key", client=client, retry_backoff_seconds=0)

    result = geocoder.geocode("24 XYZ Street, Velachery, Chennai 600042")

    assert result.status == "NEEDS_MANUAL_VERIFICATION"
    assert result.confidence < 0.5
