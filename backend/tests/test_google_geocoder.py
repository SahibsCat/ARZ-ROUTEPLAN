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


def test_geocode_once_validates_against_validate_against_not_the_queried_text():
    # LOAD-BEARING - the actual mechanism behind the Pallavakam/Pallavaram
    # fix. A wrong query-shaping correction must never be able to
    # validate ITSELF: what got SENT to Google (`address`) and what the
    # response is JUDGED against (`validate_against`) have to be able to
    # disagree. Here the queried text names a locality Google's response
    # doesn't contain at all - if validation used the queried text, this
    # would be flagged as a locality mismatch. It doesn't, because
    # validate_against names the locality the response DOES contain.
    components = [
        {"long_name": "12", "types": ["street_number"]},
        {"long_name": "Gandhi Road", "types": ["route"]},
        {"long_name": "Velachery", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "Chennai", "types": ["locality"]},
    ]
    client = _DummyClient([_ok_response("ROOFTOP", ["premise"], address_components=components)])
    geocoder = GoogleGeocoder(api_key="test-key", client=client, retry_backoff_seconds=0)

    result = geocoder._geocode_once(
        "12, Gandhi Road, Random Distant District, Chennai",
        validate_against="12, Gandhi Road, Velachery, Chennai",
    )

    assert result.status == "OK"


def test_geocode_once_without_validate_against_still_validates_the_queried_text():
    # The default (no override) behaves exactly as before this split -
    # every EXISTING call site keeps validating against what it queried
    # with.
    components = [
        {"long_name": "12", "types": ["street_number"]},
        {"long_name": "Gandhi Road", "types": ["route"]},
        {"long_name": "Velachery", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "Chennai", "types": ["locality"]},
    ]
    client = _DummyClient([_ok_response("ROOFTOP", ["premise"], address_components=components)])
    geocoder = GoogleGeocoder(api_key="test-key", client=client, retry_backoff_seconds=0)

    result = geocoder._geocode_once("12, Gandhi Road, Random Distant District, Chennai")

    assert result.status == "NEEDS_MANUAL_VERIFICATION"


def test_geocode_tries_a_spelling_corrected_query_when_the_plain_one_fails():
    # End-to-end through geocode()'s own orchestration: the plain query
    # finds nothing at all, the spelling-corrected retry succeeds.
    zero_results = {"status": "ZERO_RESULTS", "results": []}
    corrected_hit = _ok_response(
        "ROOFTOP", ["premise"],
        address_components=[
            {"long_name": "12", "types": ["street_number"]},
            {"long_name": "Gandhi Road", "types": ["route"]},
            {"long_name": "Velachery", "types": ["sublocality", "sublocality_level_1"]},
            {"long_name": "Chennai", "types": ["locality"]},
        ],
    )
    client = _DummyClient([zero_results, corrected_hit])
    geocoder = GoogleGeocoder(api_key="test-key", client=client, retry_backoff_seconds=0)

    result = geocoder.geocode("12, Gandhi Road, Velachary, Chennai")

    assert result is not None
    assert result.status == "OK"
    assert client.call_count == 2


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


def test_strip_leading_name_segment_drops_an_unindexed_building_name():
    from app.geocoding.google_geocoder import _strip_leading_name_segment

    assert _strip_leading_name_segment(
        "Narayana Swami Men's PG, Bhavani St, Bharathi Nagar, Velachery, Chennai, Tamil Nadu 600113"
    ) == "Bhavani St, Bharathi Nagar, Velachery, Chennai, Tamil Nadu 600113"


def test_strip_leading_name_segment_leaves_a_house_numbered_address_alone():
    # A leading house number means the structured parser already has a
    # real number to anchor on - a building name is not the problem here,
    # so there's nothing useful to strip.
    from app.geocoding.google_geocoder import _strip_leading_name_segment

    assert _strip_leading_name_segment("24 XYZ Street, Velachery, Chennai 600042") is None


def test_strip_leading_name_segment_does_not_eat_a_real_street_with_no_name_in_front():
    # No building/business name leads this address at all - the first
    # segment already IS the street. Stripping it would remove the one
    # thing the customer actually specified.
    from app.geocoding.google_geocoder import _strip_leading_name_segment

    assert _strip_leading_name_segment("Bhavani St, Bharathi Nagar, Velachery, Chennai, Tamil Nadu 600113") is None


def test_strip_leading_name_segment_needs_enough_left_over_to_be_worth_it():
    from app.geocoding.google_geocoder import _strip_leading_name_segment

    assert _strip_leading_name_segment("Sidharth Upscale Apartments, Porur") is None


def test_number_confirmed_in_text_matches_whole_tokens_only():
    from app.geocoding.google_geocoder import _number_confirmed_in_text

    assert _number_confirmed_in_text("17", "Door No.17, Narayana Swami Men's PG, Bhavani St") is True
    assert _number_confirmed_in_text("17", "17, Bhavani St, Chennai") is True
    # Must not match INSIDE a longer, unrelated number.
    assert _number_confirmed_in_text("17", "170, Bhavani St, Chennai 600113") is False
    assert _number_confirmed_in_text("17", "217, Bhavani St, Chennai") is False
    assert _number_confirmed_in_text("17", "Bhavani St, Chennai, Tamil Nadu 600113") is False


def test_extract_house_numbers_handles_colon_after_no():
    from app.geocoding.google_geocoder import _extract_house_numbers

    assert _extract_house_numbers("Door No: 15, XYZ Street") == ["15"]
    assert _extract_house_numbers("No: 12, XYZ Street") == ["12"]


def test_extract_house_numbers_ignores_a_flat_number_inside_a_named_building():
    # Deliberately NOT extracted - see _HOUSE_NUMBER_PREFIX's own comment
    # for why a flat/unit number is the wrong kind of thing to validate
    # against Google's street_number data at all. The real street number
    # later in the address (if any) is still found normally.
    from app.geocoding.google_geocoder import _extract_house_numbers

    assert _extract_house_numbers("RWD Corniche, A Block, Flat No: 202, 2nd floor, Pantheon Road") == []
    assert _extract_house_numbers("Flat 2B, 12 XYZ Street, Chennai") == ["12"]


def test_extract_house_numbers_handles_a_letter_prefixed_number():
    # Block/door-letter leading the number - common in gated communities
    # and government housing ("D1/5" - block D, unit 1/5).
    from app.geocoding.google_geocoder import _extract_house_numbers

    assert _extract_house_numbers("D1/5 S3, NTECL Township, NCTPS Road, vallur, 600103") == ["D1/5"]
    assert _extract_house_numbers("A1C BBCL Apartments, Ambattur, 600058") == ["A1C"]
    # A single leading letter still requires a digit right after it - a
    # real word must never be mistaken for a house number.
    assert _extract_house_numbers("Sri Sai Apartments, Anna Nagar, Chennai") == []


def test_extract_house_numbers_handles_a_period_between_the_prefix_and_no():
    from app.geocoding.google_geocoder import _extract_house_numbers

    assert _extract_house_numbers("Plot.no 11, Sekar Villa, Injambakkam, 600115") == ["11"]


def test_strip_landmark_phrase_preserves_a_trailing_pin_code():
    # The landmark pattern consumes everything up to the next comma (or
    # end of string) after "near"/etc - when the landmark phrase is the
    # LAST thing in the address with nothing after it, that used to
    # swallow the PIN code along with it since there was no comma to stop
    # the match.
    from app.geocoding.google_geocoder import _strip_landmark_phrase

    assert _strip_landmark_phrase(
        "77 Arundale street Mylapore Near Mylapore post office 600004"
    ) == "77 Arundale street Mylapore, 600004"
    # A landmark phrase with real address text AFTER it is unaffected -
    # nothing was ever lost there in the first place (pre-existing stray
    # space before the comma is a separate, harmless cosmetic quirk of
    # the substitution, unrelated to this fix).
    assert _strip_landmark_phrase(
        "12 Example St near Big Mall, Chennai"
    ) == "12 Example St , Chennai"


def test_numeric_core_strips_letter_suffixes_and_sub_units():
    from app.geocoding.google_geocoder import _numeric_core

    assert _numeric_core("231B") == "231"
    assert _numeric_core("231C") == "231"
    assert _numeric_core("231B/1") == "231"
    assert _numeric_core("12A") == "12"
    assert _numeric_core("2/20") == "2"


def test_locality_tokens_keeps_city_state_country_words():
    # Unlike nominatim_geocoder._significant_tokens (which this used to
    # reuse), "chennai"/"tamil"/"nadu"/"tn"/"in" must survive - a
    # customer who only names the CITY needs credit for agreeing with a
    # Google locality match that's ALSO just the city.
    from app.geocoding.google_geocoder import _locality_tokens

    tokens = _locality_tokens("Taksh Traders, 48/58 Savari Muthu Sreet, Mannady Street, Chennai, Tamil Nadu, India")
    assert "chennai" in tokens
    assert "tamil" in tokens
    assert "nadu" in tokens
    # Generic STREET-type filler words are still stripped - "Kumaran
    # Nagar" and "Thirumalai Nagar" sharing "Nagar" must never look like
    # agreement on its own.
    assert "street" not in tokens
    assert "nagar" not in _locality_tokens("Kumaran Nagar")


def test_score_component_match_confirms_locality_via_the_city_name_alone():
    # Real case this fixes: customer's only locality-ish word is the CITY
    # itself ("Chennai") with no distinct sublocality named. Google's
    # match is also just the bare city. The OLD shared _significant_tokens
    # (borrowed from nominatim) stripped "chennai" as generic noise on
    # BOTH sides, leaving nothing to compare and forcing a locality-mismatch
    # flag even though they plainly agree. House/street match cleanly so
    # this isolates the locality signal.
    from app.geocoding.google_geocoder import _score_component_match

    customer = "48/58, Mannady Street, Chennai, 600001"
    google_components = [
        {"long_name": "48/58", "types": ["street_number"]},
        {"long_name": "Mannady Street", "types": ["route"]},
        {"long_name": "Chennai", "types": ["locality"]},
        {"long_name": "600001", "types": ["postal_code"]},
    ]

    assert _score_component_match(customer, google_components) is None


def test_locality_tokens_preserves_single_initial_locality_names():
    # LOAD-BEARING - a confirmed, live wrong-pin bug (found by adversarial
    # code review, not a support ticket): "T. Nagar" and "K K Nagar" are
    # among the highest-volume delivery areas in Chennai, and a bare
    # single-letter initial ("T"/"K") was too short to survive the length
    # floor while "Nagar" was stripped as generic - both sides' specific
    # identity vanished to nothing, which incorrectly satisfied the
    # "one side only named the city" fallback for city-name-only
    # agreement. See google_geocoder.py's own comment above
    # _INITIALS_BEFORE_NOISE_WORD for the full mechanism.
    from app.geocoding.google_geocoder import _locality_tokens

    assert _locality_tokens("T. Nagar") == ["tnagar"]
    assert _locality_tokens("T Nagar") == ["tnagar"]
    assert _locality_tokens("K K Nagar") == ["kknagar"]
    # A genuinely generic street-type phrase must stay empty - "Main" is
    # a real word, not a single initial, so this must never fire on it.
    assert _locality_tokens("Main Road") == []


def test_score_component_match_flags_a_wrong_locality_hidden_behind_a_single_initial_name():
    # End-to-end reproduction of the exact confirmed bug: customer names
    # T. Nagar, Google's actual match is a different real locality
    # (Velachery) with a different PIN - both must be caught, not
    # silently accepted via the city-name-only fallback.
    from app.geocoding.google_geocoder import _score_component_match

    customer = "12, Main Road, T. Nagar, Chennai 600017"
    google_components = [
        {"long_name": "12", "types": ["street_number"]},
        {"long_name": "Main Road", "types": ["route"]},
        {"long_name": "Velachery", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "Chennai", "types": ["locality"]},
        {"long_name": "600042", "types": ["postal_code"]},
    ]

    cap, reason = _score_component_match(customer, google_components)
    assert cap is not None
    assert "Velachery" in reason


def test_score_component_match_still_confirms_a_genuine_single_initial_locality_match():
    # The flip side - a customer naming T. Nagar whose match genuinely IS
    # T. Nagar must not be flagged just because the fix touches this path.
    from app.geocoding.google_geocoder import _score_component_match

    customer = "12, Main Road, T. Nagar, Chennai 600017"
    google_components = [
        {"long_name": "12", "types": ["street_number"]},
        {"long_name": "Main Road", "types": ["route"]},
        {"long_name": "T. Nagar", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "Chennai", "types": ["locality"]},
        {"long_name": "600017", "types": ["postal_code"]},
    ]

    assert _score_component_match(customer, google_components) is None


def test_score_component_match_does_not_let_the_city_name_alone_confirm_the_locality():
    # LOAD-BEARING. Practically every Chennai address says "Chennai" and
    # so does practically every Google response. If that counts as
    # locality agreement, locality validation is silently switched off
    # for the whole city. Observed live before this was fixed: "Adyar
    # 600020" accepted as Thiruvanmiyur 600041, and "Kilpauk 600010"
    # accepted as Purasaiwakkam 600012, both at 0.8 confidence - two
    # confidently wrong pins.
    from app.geocoding.google_geocoder import LOCALITY_MISMATCH_CONFIDENCE_CAP, _score_component_match

    customer = "12, Main Road, Adyar, Chennai 600020"
    google_components = [
        {"long_name": "12", "types": ["street_number"]},
        {"long_name": "Main Road", "types": ["route"]},
        {"long_name": "Kamaraj Nagar", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "Thiruvanmiyur", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "Chennai", "types": ["locality"]},
        {"long_name": "600020", "types": ["postal_code"]},
    ]

    cap, reason = _score_component_match(customer, google_components)
    assert cap == LOCALITY_MISMATCH_CONFIDENCE_CAP
    assert "Thiruvanmiyur" in reason


def test_score_component_match_still_confirms_a_locality_google_states_more_precisely():
    # The flip side, and just as important: Google routinely answers with
    # a MORE specific sublocality than the customer named ("Karunabigai
    # Colony, Velachery" for a customer who wrote "Velachery"). The
    # customer's word appearing anywhere in Google's locality set is
    # genuine agreement and must not be flagged.
    from app.geocoding.google_geocoder import _score_component_match

    customer = "12, Gandhi Road, Velachery, Chennai 600042"
    google_components = [
        {"long_name": "12", "types": ["street_number"]},
        {"long_name": "Gandhi Road", "types": ["route"]},
        {"long_name": "Karunabigai Colony", "types": ["sublocality", "sublocality_level_2"]},
        {"long_name": "Velachery", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "Chennai", "types": ["locality"]},
        {"long_name": "600042", "types": ["postal_code"]},
    ]

    assert _score_component_match(customer, google_components) is None


def test_transliteration_match_recognizes_a_doubled_vowel_variant():
    # "Noombal" (customer) vs "Numbal" (Google) - the same place, common
    # Tamil-to-English transliteration variance a plain fuzzy ratio
    # rejects (0.77, just under FUZZY_MATCH_THRESHOLD's 0.82).
    from app.geocoding.google_geocoder import _transliteration_match

    assert _transliteration_match("noombal", ["numbal"]) is True
    assert _transliteration_match("koovur", ["kovur"]) is True
    # A genuinely different word must still be rejected - this isn't a
    # blanket threshold loosening.
    assert _transliteration_match("adyar", ["velachery"]) is False


def test_score_component_match_trusts_house_number_over_an_abbreviated_street_name():
    # Real case: customer wrote "KK Road" (a common local abbreviation),
    # Google's structured match is "Kalli Kuppam Rd" - no fuzzy text
    # match will ever recognize "kk" against "kalli"/"kuppam", but the
    # house number landing on the exact same structured street_number is
    # strong, independent, numeric confirmation it's the same street.
    from app.geocoding.google_geocoder import _score_component_match

    customer = "No 4 Kk Road 3rd CROSS Street Ambattur 600053 Chennai TN"
    google_components = [
        {"long_name": "4", "types": ["street_number"]},
        {"long_name": "Kalli Kuppam Rd", "types": ["route"]},
        {"long_name": "Ambattur", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "600053", "types": ["postal_code"]},
    ]

    assert _score_component_match(customer, google_components) is None


def test_score_component_match_does_not_trust_an_abbreviated_street_name_when_anything_else_is_off():
    # The override only applies when the street name is the ONLY thing
    # flagged - here the house number ALSO doesn't match, so it stays strict.
    from app.geocoding.google_geocoder import STREET_NUMBER_MISMATCH_CONFIDENCE_CAP, _score_component_match

    customer = "No 4 Kk Road 3rd CROSS Street Ambattur 600053 Chennai TN"
    google_components = [
        {"long_name": "47", "types": ["street_number"]},
        {"long_name": "Kalli Kuppam Rd", "types": ["route"]},
        {"long_name": "Ambattur", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "600053", "types": ["postal_code"]},
    ]

    cap, _reason = _score_component_match(customer, google_components)
    assert cap == STREET_NUMBER_MISMATCH_CONFIDENCE_CAP


def test_score_component_match_confirms_a_letter_suffixed_number_via_its_numeric_base_in_free_text():
    # Real case: customer's "43B" against an establishment match's free-
    # text name reading "43/160, 2nd Cross St, ..." - the exact token
    # "43B" never appears, but the numeric base "43" does, and is strong
    # confirmation of the same building (see _numeric_core).
    from app.geocoding.google_geocoder import _score_component_match

    customer = "43B, 2nd Cross Street, Kumaran Nagar 600092"
    google_components = [
        {"long_name": "43/160, 2nd Cross St", "types": []},
        {"long_name": "Kumaran Nagar", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "600092", "types": ["postal_code"]},
    ]

    assert _score_component_match(customer, google_components) is None


def test_house_number_matches_treats_a_bare_base_number_as_confirmation():
    # Google's data often doesn't carry sub-unit granularity ("2" rather
    # than "2/20") - the base number still matching is strong confirmation
    # of the same building, not a mismatch just because Google lacks
    # flat-level detail the customer happened to include.
    from app.geocoding.google_geocoder import _house_number_matches

    assert _house_number_matches("2", ["2/20"]) is True
    assert _house_number_matches("17-10", ["17/10"]) is True  # separator-only difference
    assert _house_number_matches("2", ["172", "2/20"]) is True  # matches whichever candidate applies
    # Checked symmetrically now - a customer citing just the base part
    # ("77") is also confirmed by Google's more specific number ("77/28"),
    # since a base/sub-unit number is frequently the single official
    # premise number for one property, not several - see the function's
    # own docstring for the real case this covers.
    assert _house_number_matches("77/28", ["77"]) is True
    # Same leading numeric base, different letter-suffixed unit - the
    # same plot/building, not a different address (see _numeric_core).
    assert _house_number_matches("231C", ["231B/1"]) is True
    # A genuinely different base number is still a real mismatch.
    assert _house_number_matches("99/5", ["2"]) is False
    assert _house_number_matches("4/2", ["2/20"]) is False


def test_reorder_house_number_to_street_moves_the_number_next_to_its_street():
    # Conventional Indian order - house number, THEN a name, THEN the
    # street - that Google's parser doesn't recognize as-is.
    from app.geocoding.google_geocoder import _reorder_house_number_to_street

    assert _reorder_house_number_to_street(
        "17, Narayana Swami Men's PG, Bhavani St, Bharathi Nagar, Velachery, Chennai, Tamil Nadu 600113"
    ) == "17 Bhavani St, Bharathi Nagar, Velachery, Chennai, Tamil Nadu 600113"
    # "Door No 17" prefix form must resolve to the same underlying number.
    assert _reorder_house_number_to_street(
        "Door No 17, Narayana Swami Men's PG, Bhavani St, Bharathi Nagar, Chennai 600113"
    ) == "17 Bhavani St, Bharathi Nagar, Chennai 600113"


def test_reorder_house_number_to_street_does_nothing_when_already_adjacent():
    from app.geocoding.google_geocoder import _reorder_house_number_to_street

    assert _reorder_house_number_to_street("17 Bhavani St, Bharathi Nagar, Chennai 600113") is None


def test_reorder_house_number_to_street_needs_a_leading_house_number():
    # No number leads the address at all - this is _strip_leading_name_
    # segment's case instead, not this function's.
    from app.geocoding.google_geocoder import _reorder_house_number_to_street

    assert _reorder_house_number_to_street(
        "Narayana Swami Men's PG, Bhavani St, Bharathi Nagar, Chennai 600113"
    ) is None


def test_reorder_house_number_to_street_needs_a_street_segment_somewhere():
    from app.geocoding.google_geocoder import _reorder_house_number_to_street

    assert _reorder_house_number_to_street("17, Narayana Swami Men's PG, Bharathi Nagar, Chennai") is None


def test_name_stripped_places_retry_rescues_the_pg_case_end_to_end():
    # THE FULL FIX: the customer's exact wording is kept and still
    # succeeds - "can't enter the PG name" is no longer true. A wrong
    # Places match on the ORIGINAL (named) text is retried against the
    # name-stripped text, which resolves correctly.
    wrong_place_details = _place_details_response(
        12.9749, 80.2224,
        "X6FC+XX2, Sarathy Nagar, Velachery, Chennai, Tamil Nadu 600042, India",
        address_components=[
            {"long_name": "Sarathy Nagar", "types": ["sublocality", "sublocality_level_1"]},
            {"long_name": "600042", "types": ["postal_code"]},
        ],
    )
    right_place_details = _place_details_response(
        12.980685, 80.2336006,
        "Bhavani St, Bharathi Nagar, Tharamani, Chennai, Tamil Nadu, India",
        address_components=[
            {"long_name": "Bhavani St", "types": ["route"]},
            {"long_name": "Bharathi Nagar", "types": ["sublocality", "sublocality_level_1"]},
            {"long_name": "600113", "types": ["postal_code"]},
        ],
    )
    responses = [
        _ok_response("GEOMETRIC_CENTER", ["route"]),  # raw geocode of the full (named) address
        _ok_response("GEOMETRIC_CENTER", ["route"]),  # name-stripped retry - still only street-level
        _find_place_response(),  # Find Place on the original (named) address
        wrong_place_details,  # ...the wrong establishment, rejected
        _find_place_response(),  # Find Place retried on the name-stripped address
        right_place_details,  # ...the correct street this time - accepted
    ]
    client = _DummyClient(responses)
    geocoder = GoogleGeocoder(api_key="test-key", client=client, retry_backoff_seconds=0)

    result = geocoder.geocode(
        "Narayana Swami Men's PG, Bhavani St, Bharathi Nagar, Velachery, Chennai, Tamil Nadu 600113"
    )

    assert result.status == "OK"
    assert result.lat == 12.980685
    assert result.confidence == PLACES_FALLBACK_CONFIDENCE
    assert client.call_count == 6


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
    wrong_place_details = _place_details_response(
        12.9749, 80.2224,
        "X6FC+XX2, Sarathy Nagar, Velachery, Chennai, Tamil Nadu 600042, India",
        address_components=[
            {"long_name": "Sarathy Nagar", "types": ["sublocality", "sublocality_level_1"]},
            {"long_name": "600042", "types": ["postal_code"]},
        ],
    )
    responses = [
        _ok_response("GEOMETRIC_CENTER", ["route"]),  # raw geocode: only the street matched, not the named PG
        _ok_response("GEOMETRIC_CENTER", ["route"]),  # name-stripped retry: still only street-level
        _find_place_response(),  # Find Place on the original (named) address
        wrong_place_details,  # ...resolves to the wrong place - rejected
        _find_place_response(),  # Find Place retried on the name-stripped address
        wrong_place_details,  # ...same wrong place either way - still rejected
    ]
    client = _DummyClient(responses)
    geocoder = GoogleGeocoder(api_key="test-key", client=client, retry_backoff_seconds=0)

    result = geocoder.geocode(
        "Narayana Swami Men's PG, Bhavani St, Bharathi Nagar, Velachery, Chennai, Tamil Nadu 600113"
    )

    assert result.status == "NEEDS_MANUAL_VERIFICATION"
    assert result.confidence < 0.5
    assert client.call_count == 6


def test_places_fallback_still_ok_when_place_details_confirms_the_right_locality():
    # The other half of the same fix: a Places match that DOES agree with
    # the customer's stated PIN/locality/street must not be penalized just
    # for having gone through Place Details - it still lands at the normal
    # PLACES_FALLBACK_CONFIDENCE, same as before this change.
    responses = [
        _ok_response("GEOMETRIC_CENTER", ["route"]),  # raw geocode: street only
        _ok_response("GEOMETRIC_CENTER", ["route"]),  # name-stripped retry: still street only
        _find_place_response(),  # Find Place on the original (named) address
        _place_details_response(
            12.9906, 80.2181,
            "Narayana Swami Men's PG, Bhavani St, Bharathi Nagar, Velachery, Chennai, Tamil Nadu 600113, India",
            address_components=[
                {"long_name": "Bhavani Street", "types": ["route"]},
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
    # Succeeds via Places on the ORIGINAL (named) address, before the
    # name-stripped Places retry is ever needed - confirms that retry is
    # only reached when the first Places attempt didn't already work.
    assert client.call_count == 4


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
    assert _score_component_match(customer, google_components)[0] < 0.5


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
    # "Flat" is deliberately NOT a recognized prefix (see
    # _HOUSE_NUMBER_PREFIX's own comment), so "Flat 2B" is correctly
    # skipped in favor of the real street number that follows.
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

    cap, reason = _score_component_match(customer, google_components)
    assert cap == STREET_NUMBER_UNCONFIRMED_CONFIDENCE_CAP
    assert cap < 0.5
    assert "24" in reason


def test_score_component_match_confirms_a_house_number_embedded_in_free_text():
    """An establishment/POI match's most specific component is often free
    text with no "street_number" type at all - "Door No.17, Narayana
    Swami Men's PG, Bhavani St...", types: [] (the exact same shape that
    already needed a text-based fallback for the street name). The
    number is genuinely confirmed there even though no component is
    TYPED street_number - must not be treated as unconfirmed."""
    from app.geocoding.google_geocoder import _score_component_match

    customer = "Door No 17, Narayana Swami Men's PG, Bhavani St, Bharathi Nagar, Velachery, Chennai, Tamil Nadu 600113"
    google_components = [
        {"long_name": "Door No.17, Narayana Swami Men's PG", "types": []},
        {"long_name": "Bhavani St", "types": ["route"]},
        {"long_name": "Bharathi Nagar", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "600113", "types": ["postal_code"]},
    ]

    assert _score_component_match(customer, google_components) is None


def test_score_component_match_still_flags_an_unconfirmed_house_number_even_with_street_and_area_confirmed():
    # The trust override is for a specific MISMATCH only. When Google has
    # NO house-level data at all (no street_number component, and the
    # number doesn't appear anywhere in the response's free text), that
    # proves only that the street exists - on a range-interpolated match
    # the actual pin could be anywhere along it. This must keep flagging.
    from app.geocoding.google_geocoder import STREET_NUMBER_UNCONFIRMED_CONFIDENCE_CAP, _score_component_match

    customer = "24 XYZ Street, Velachery, Chennai 600042"
    google_components = [
        {"long_name": "XYZ Street", "types": ["route"]},
        {"long_name": "Velachery", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "Chennai", "types": ["locality"]},
        {"long_name": "600042", "types": ["postal_code"]},
    ]

    cap, _reason = _score_component_match(customer, google_components)
    assert cap == STREET_NUMBER_UNCONFIRMED_CONFIDENCE_CAP


def test_score_component_match_does_not_block_on_a_flat_or_block_number_alone():
    # "A103" is a unit designator inside a named building, not a street
    # door number - Google's data indexes street numbers, never
    # apartment interiors, so this can never be structurally confirmed
    # and must not block on its own when nothing else disagrees.
    from app.geocoding.google_geocoder import _score_component_match

    customer = "A103, Urbantree Fantastic, Survey No 106, Vanagaram, Chennai 600077"
    google_components = [
        {"long_name": "106", "types": ["premise"]},
        {"long_name": "Urbantree Fantastic", "types": ["point_of_interest", "establishment"]},
        {"long_name": "Vanagaram", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "Chennai", "types": ["locality"]},
        {"long_name": "600077", "types": ["postal_code"]},
    ]

    assert _score_component_match(customer, google_components) is None


def test_extract_street_keyword_looks_one_word_further_back_past_a_positional_word():
    # Real production case (batch 112, Dr G Amalraj): "3rd Canal Cross
    # Road" - "Cross" sits directly before "Road" and is purely
    # positional (see _UNNAMED_STREET_WORDS), but "Canal" one word
    # further back IS the real, distinguishing street name. The old
    # extraction only ever looked at the single word immediately before
    # the suffix, so it saw "Cross", correctly rejected it as positional,
    # and gave up entirely - "Canal" was never even considered.
    from app.geocoding.google_geocoder import _extract_street_keyword

    assert _extract_street_keyword("9C, 3rd Canal Cross Road, Gandhi Nagar, Adyar") == "canal"
    # Confirms this doesn't regress the plain single-word case.
    assert _extract_street_keyword("12, Gandhi Road, Velachery") == "gandhi"
    # Two positional words in a row (no real name at all) still
    # correctly yields nothing to check.
    assert _extract_street_keyword("12, New Cross Road, Velachery") is None


def test_extract_street_keyword_ignores_a_purely_positional_street_name():
    # "1st Cross Street" and "16th cross Street" identify a street by its
    # number within a grid, not by a proper name - there is nothing to
    # fuzzy-match against, and treating "cross"/"main" as the name being
    # tested fails constantly since Google routinely writes the same
    # street differently or omits the ordinal.
    from app.geocoding.google_geocoder import _extract_street_keyword

    assert _extract_street_keyword("D15, 16th cross Street, Hindu colony") is None
    assert _extract_street_keyword("White Petals, 1st Cross Street, MAC Nagar") is None
    # A real named street after a positional one is still found.
    assert _extract_street_keyword("12, 1st Cross Street, Bhavani Road, Adyar") == "bhavani"


def test_score_component_match_recognizes_a_local_road_alias():
    # "Mount Road" and "Anna Salai" are two names locals use for the same
    # road - they share no letters, so no fuzzy matcher bridges them
    # without an explicit alias table.
    from app.geocoding.google_geocoder import _score_component_match

    customer = "12, Mount Road, Chennai 600002"
    google_components = [
        {"long_name": "12", "types": ["street_number"]},
        {"long_name": "Anna Salai", "types": ["route"]},
        {"long_name": "Chennai", "types": ["locality"]},
        {"long_name": "600002", "types": ["postal_code"]},
    ]

    assert _score_component_match(customer, google_components) is None


def test_locality_token_matches_a_run_together_acronym():
    # Real case: customer wrote "w.k.k.nagar" (a run-together local
    # abbreviation for "West K.K. Nagar"), Google answered "KK Nagar
    # West" - no fuzzy ratio recognizes "wkknagar" against "kk", but the
    # acronym appearing inside the customer's own token is real evidence.
    from app.geocoding.google_geocoder import _locality_token_matches

    assert _locality_token_matches("kk", ["wkknagar"]) is True
    # A genuinely unrelated locality - no substring relationship, no
    # fuzzy similarity - must not match.
    assert _locality_token_matches("chennai", ["thiruvanmiyur"]) is False
    # The acronym-containment path itself is restricted to short tokens
    # (2-4 chars); a long token falls through to plain fuzzy matching,
    # which correctly rejects two unrelated locality names of similar
    # length rather than finding a coincidental one-way substring.
    assert _locality_token_matches("velachery", ["thiruvanmiyur"]) is False


def test_score_component_match_trusts_a_nearby_house_number_when_street_and_area_both_confirm():
    # Explicit product decision (confirmed with the user): "you entered
    # 24, Google found 22" on a street and locality we've independently
    # confirmed describes two doors a few metres apart on the correct
    # road - a categorically smaller, lower-risk error than a wrong
    # street or wrong locality, and not worth sending an otherwise-good
    # address to a human for. The mismatch stays visible in the accepted
    # match (Google's own house number is what gets used), it just stops
    # being a BLOCKER. This case previously flagged; that was superseded.
    from app.geocoding.google_geocoder import _score_component_match

    customer = "24 XYZ Street, Velachery, Chennai 600042"
    google_components = [
        {"long_name": "22", "types": ["street_number"]},
        {"long_name": "XYZ Street", "types": ["route"]},
        {"long_name": "Velachery", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "Chennai", "types": ["locality"]},
        {"long_name": "600042", "types": ["postal_code"]},
    ]

    assert _score_component_match(customer, google_components) is None


def test_score_component_match_still_flags_a_house_number_mismatch_when_the_street_is_not_confirmed():
    # The trust above is conditional, not a general amnesty for house-
    # number mismatches - it requires the STREET to be independently
    # confirmed too. Here the street name itself doesn't match anything
    # in Google's response, so the same numeric mismatch stays blocking:
    # a wrong number on an unverified street is exactly the "confidently
    # wrong pin" case this file exists to prevent.
    from app.geocoding.google_geocoder import _score_component_match

    customer = "24 XYZ Street, Velachery, Chennai 600042"
    google_components = [
        {"long_name": "22", "types": ["street_number"]},
        {"long_name": "ABC Road", "types": ["route"]},
        {"long_name": "Velachery", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "Chennai", "types": ["locality"]},
        {"long_name": "600042", "types": ["postal_code"]},
    ]

    # Two things are wrong here (street AND number) - the returned cap is
    # whichever is most restrictive, but the point of this test is that
    # it is flagged at all (cap is not None), not which specific cap wins.
    cap, reason = _score_component_match(customer, google_components)
    assert cap is not None
    assert "24" in reason and "22" in reason


def test_extract_house_numbers_finds_a_second_number_joined_by_a_connector():
    # Real production case (batch 112, Kedarvignesh P): "No.78 And 79A,
    # 4th ST Ambika Nagar, Madambakkam 600126" states BOTH numbers side by
    # side - a plot re-numbered, or shared between two families, is a
    # real Chennai addressing pattern. Missing "79A" entirely meant
    # Google's confirmed "79" (base-matching the customer's own "79A")
    # was never even considered a candidate - it looked like a plain,
    # blocking mismatch against "78" instead of the safe, expected
    # nearby-number case it actually is.
    from app.geocoding.google_geocoder import _extract_house_numbers, _house_number_matches

    numbers = _extract_house_numbers("No.78 And 79A, 4th ST Ambika Nagar,Madambakkam 600126")
    assert numbers == ["78", "79A"]
    assert _house_number_matches("79", numbers) is True


def test_extract_house_numbers_handles_the_ampersand_and_slash_connectors_too():
    from app.geocoding.google_geocoder import _extract_house_numbers

    assert _extract_house_numbers("12 & 14, Gandhi Road") == ["12", "14"]
    assert _extract_house_numbers("12 / 14A, Gandhi Road") == ["12", "14A"]


def test_score_component_match_resolves_a_dual_stated_house_number_via_the_second_one():
    # End-to-end: the customer's SECOND stated number is the one Google
    # actually confirms, on a street/area that both check out - the
    # street+locality trust decision applies here exactly as it would
    # for a single-number address.
    from app.geocoding.google_geocoder import _score_component_match

    customer = "No.78 And 79A, 4th ST Ambika Nagar, Madambakkam 600126"
    google_components = [
        {"long_name": "79", "types": ["street_number"]},
        {"long_name": "4th St", "types": ["route"]},
        {"long_name": "Ambika Nagar", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "Madambakkam", "types": ["sublocality", "sublocality_level_2"]},
        {"long_name": "600126", "types": ["postal_code"]},
    ]

    assert _score_component_match(customer, google_components) is None


def test_extract_house_numbers_finds_every_candidate_not_just_the_first():
    # Real case: a plot/survey number AND the actual postal door number,
    # both genuinely stated, in that order.
    from app.geocoding.google_geocoder import _extract_house_numbers

    assert _extract_house_numbers(
        "Plot No 172, 17/10, Vinobaji Street, Kamarajar Nagar, Choolaimedu 600094"
    ) == ["172", "17/10"]


def test_extract_house_numbers_handles_a_bare_door_prefix_with_no_no():
    from app.geocoding.google_geocoder import _extract_house_numbers

    assert _extract_house_numbers("Door 2 Plot 107 Yeshwanth nagar link street Madambakkam") == ["2"]


def test_score_component_match_confirms_against_any_stated_house_number():
    """The exact bug this fixes: Google's confirmed street_number ("17-10")
    matches the customer's SECOND stated number ("17/10"), not the first
    ("172", a separate plot/survey reference) - must not be capped just
    because the first-found number doesn't match. Also confirms "-" and
    "/" are treated as the same separator."""
    from app.geocoding.google_geocoder import _score_component_match

    customer = "Plot No 172, 17/10, Vinobaji Street, Kamarajar Nagar, Choolaimedu 600094"
    google_components = [
        {"long_name": "17-10", "types": ["street_number"]},
        {"long_name": "Vinobaji Street", "types": ["route"]},
        {"long_name": "Choolaimedu", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "600094", "types": ["postal_code"]},
    ]

    assert _score_component_match(customer, google_components) is None


def test_score_component_match_confirms_a_locality_nested_three_levels_deep():
    # "Yeswanth Nagar" only appears as a sublocality_level_2/3+ component
    # (nested inside "Madambakkam") - a real Chennai address shape, and
    # the customer's stated area can legitimately be the deeper one.
    from app.geocoding.google_geocoder import _score_component_match

    customer = "Door 2, Yeswanth Nagar, Madambakkam, Chennai 600126"
    google_components = [
        {"long_name": "2", "types": ["subpremise"]},
        {"long_name": "Padamavathy Nagar", "types": ["sublocality", "sublocality_level_3"]},
        {"long_name": "Yeswanth Nagar", "types": ["sublocality", "sublocality_level_2"]},
        {"long_name": "Madambakkam", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "600126", "types": ["postal_code"]},
    ]

    assert _score_component_match(customer, google_components) is None


def test_score_component_match_trusts_precision_over_a_lone_pin_typo():
    # Real case: the customer's PIN doesn't exist for this street at all,
    # but street/house-number/locality all confirm cleanly AND the match
    # is genuinely precise (ROOFTOP) - trusted as a customer-side typo
    # rather than a wrong address, when explicitly given precision_confidence.
    from app.geocoding.google_geocoder import PIN_MISMATCH_TRUST_OVERRIDE_MIN_PRECISION, _score_component_match

    customer = "2/20 New Thandavarayan Street, Purasaiwakkam, Chennai 600007"
    google_components = [
        {"long_name": "2", "types": ["street_number"]},
        {"long_name": "New Thandavarayan Street", "types": ["route"]},
        {"long_name": "Purasaiwakkam", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "600021", "types": ["postal_code"]},  # different from customer's 600007
    ]

    assert _score_component_match(
        customer, google_components, precision_confidence=0.95
    ) is None
    # Sanity: the constant this depends on is what's documented.
    assert PIN_MISMATCH_TRUST_OVERRIDE_MIN_PRECISION == 0.8


def test_score_component_match_never_trusts_a_pin_typo_without_precision_passed():
    # The Places fallback path never passes precision_confidence (no
    # location_type signal exists there) - a PIN mismatch must stay fully
    # strict when the caller doesn't explicitly vouch for match precision.
    from app.geocoding.google_geocoder import PIN_MISMATCH_CONFIDENCE_CAP, _score_component_match

    customer = "2/20 New Thandavarayan Street, Purasaiwakkam, Chennai 600007"
    google_components = [
        {"long_name": "2", "types": ["street_number"]},
        {"long_name": "New Thandavarayan Street", "types": ["route"]},
        {"long_name": "Purasaiwakkam", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "600021", "types": ["postal_code"]},
    ]

    cap, reason = _score_component_match(customer, google_components)
    assert cap == PIN_MISMATCH_CONFIDENCE_CAP
    assert "600007" in reason and "600021" in reason


def test_score_component_match_does_not_trust_a_pin_typo_when_anything_else_is_off():
    # Even with high precision_confidence, a PIN mismatch stays strict if
    # ANYTHING else about the match also looks wrong (here: the locality
    # doesn't match either) - the override only applies when the PIN is
    # the ONLY thing standing between the match and full confidence.
    from app.geocoding.google_geocoder import PIN_MISMATCH_CONFIDENCE_CAP, _score_component_match

    customer = "2/20 New Thandavarayan Street, Purasaiwakkam, Chennai 600007"
    google_components = [
        {"long_name": "2", "types": ["street_number"]},
        {"long_name": "New Thandavarayan Street", "types": ["route"]},
        {"long_name": "Some Unrelated Area", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "600021", "types": ["postal_code"]},
    ]

    cap, _reason = _score_component_match(
        customer, google_components, precision_confidence=0.95
    )
    assert cap == PIN_MISMATCH_CONFIDENCE_CAP


def test_score_component_match_does_not_trust_a_pin_typo_at_low_precision():
    # Same clean street/house-number/locality match, but precision itself
    # is only a GEOMETRIC_CENTER-level guess (below the override
    # threshold) - not confident enough to override what the customer
    # actually typed.
    from app.geocoding.google_geocoder import PIN_MISMATCH_CONFIDENCE_CAP, _score_component_match

    customer = "2/20 New Thandavarayan Street, Purasaiwakkam, Chennai 600007"
    google_components = [
        {"long_name": "2", "types": ["street_number"]},
        {"long_name": "New Thandavarayan Street", "types": ["route"]},
        {"long_name": "Purasaiwakkam", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "600021", "types": ["postal_code"]},
    ]

    cap, _reason = _score_component_match(
        customer, google_components, precision_confidence=0.65
    )
    assert cap == PIN_MISMATCH_CONFIDENCE_CAP


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


def test_extract_street_keyword_handles_common_suffixes():
    from app.geocoding.google_geocoder import _extract_street_keyword

    assert _extract_street_keyword("Bhavani St, Bharathi Nagar, Chennai") == "bhavani"
    assert _extract_street_keyword("12 Example Street, Adyar, Chennai") == "example"
    assert _extract_street_keyword("OMR Road, Perungudi, Chennai") == "omr"
    assert _extract_street_keyword("Anna Salai, Chennai") == "anna"
    # No street-type suffix anywhere - a locality/landmark-only address -
    # must return None rather than guessing at one.
    assert _extract_street_keyword("Bharathi Nagar, Velachery, Chennai") is None
    assert _extract_street_keyword("ABC Apartments, Velachery, Chennai") is None


def test_score_component_match_flags_a_different_street_even_when_pin_and_locality_match():
    """THE reported bug: Google's Geocoding API matched a real ESTABLISHMENT
    (types include "establishment"/"point_of_interest", already in
    _PRECISE_TYPES - GEOMETRIC_CENTER + partial_match alone still scores
    0.5, comfortably over the accept threshold) whose PIN and locality both
    genuinely agree with the customer's own address, but the street itself
    is simply different - "Bhavani St" asked for, "GodhavariSt" matched.
    Neither the PIN nor the locality check has any way to catch this on
    its own."""
    from app.geocoding.google_geocoder import STREET_NAME_MISMATCH_CONFIDENCE_CAP, _score_component_match

    customer = "Narayana Swami Men's PG, Bhavani St, Bharathi Nagar, Velachery, Chennai, Tamil Nadu 600113"
    google_components = [
        # An establishment match's most specific component is often free
        # text with no "route" type at all - exactly this shape.
        {"long_name": "No:16, GodhavariSt", "types": []},
        {"long_name": "Bharathi Nagar", "types": ["sublocality", "sublocality_level_2"]},
        {"long_name": "Tharamani", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "600113", "types": ["postal_code"]},
    ]

    cap, reason = _score_component_match(customer, google_components)
    assert cap == STREET_NAME_MISMATCH_CONFIDENCE_CAP
    assert "bhavani" in reason.lower()


def test_score_component_match_accepts_a_confirmed_street_match():
    from app.geocoding.google_geocoder import _score_component_match

    customer = "Narayana Swami Men's PG, Bhavani St, Bharathi Nagar, Velachery, Chennai, Tamil Nadu 600113"
    google_components = [
        {"long_name": "Bhavani Street", "types": ["route"]},
        {"long_name": "Bharathi Nagar", "types": ["sublocality", "sublocality_level_1"]},
        {"long_name": "600113", "types": ["postal_code"]},
    ]

    assert _score_component_match(customer, google_components) is None


def test_score_component_match_does_not_invent_a_street_penalty_when_none_was_stated():
    """A locality/landmark-only address (no street-type suffix anywhere in
    the customer's text) must never get a manufactured street-mismatch
    penalty - same principle as the house-number check's own equivalent
    test."""
    from app.geocoding.google_geocoder import _score_component_match

    assert _score_component_match("ABC Apartments, Velachery, Chennai", [
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
