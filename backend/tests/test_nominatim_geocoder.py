import pytest

from app.geocoding import nominatim_geocoder as nom
from app.geocoding.base import STATUS_NEEDS_MANUAL_VERIFICATION, STATUS_OK, GeocodingProviderError
from app.geocoding.nominatim_geocoder import NominatimGeocoder, score_candidate


@pytest.fixture(autouse=True)
def _isolate_module_state(monkeypatch):
    # Never actually sleep in tests, and never let cache/rate-limit state
    # leak between tests (both are module-level, mirroring
    # distance_service.py's _ROUTE_CACHE reset-fixture pattern).
    monkeypatch.setattr(nom.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(nom, "_last_request_time", 0.0)
    nom.clear_geocode_cache()
    yield
    nom.clear_geocode_cache()


class _DummyResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data


class _DummyClient:
    """Supports two modes: `responses_by_query` maps an exact query string
    to a canned response (for variant-broadening tests), or
    `sequential_responses` replays a fixed list in call order (for
    retry tests)."""

    def __init__(self, responses_by_query=None, sequential_responses=None):
        self._responses_by_query = responses_by_query or {}
        self._sequential_responses = sequential_responses
        self.call_count = 0
        self.queries = []

    def get(self, url, params=None, headers=None):
        query = params["q"]
        self.queries.append(query)
        self.call_count += 1
        if self._sequential_responses is not None:
            index = min(self.call_count - 1, len(self._sequential_responses) - 1)
            return self._sequential_responses[index]
        data = self._responses_by_query.get(query, [])
        return _DummyResponse(data)


def _result(lat, lon, display_name):
    return {"lat": str(lat), "lon": str(lon), "display_name": display_name}


def test_scores_pick_best_candidate_not_first():
    address = "Flat 5, Kamaraj Block, XYZ Apartment, Velachery, Chennai"
    accepted_low, score_low = score_candidate(address, "Gandhi Block, XYZ Apartment, Velachery, Chennai, India")
    accepted_high, score_high = score_candidate(address, "Kamaraj Block, XYZ Apartment, Velachery, Chennai, India")

    assert accepted_low and accepted_high
    assert score_high > score_low


def test_score_candidate_rejects_pincode_mismatch():
    accepted, score = score_candidate(
        "Indira Gandhi Nagar, Velachery, Chennai 600042",
        "Alandur, Chennai, Tamil Nadu 600016",
    )
    assert accepted is False


def test_score_candidate_handles_typos_via_fuzzy_matching():
    # "Velacherry" (typo) vs "Velachery" (correct) - close enough to match.
    accepted, score = score_candidate(
        "12 Main Street, Velacherry, Chennai",
        "12 Main Street, Velachery, Chennai, Tamil Nadu, India",
    )
    assert accepted is True
    assert score > 0.5


def test_geocode_returns_ok_for_strong_match_on_first_variant():
    address = "24 Kamaraj Street, T Nagar, Chennai, Tamil Nadu 600017"
    client = _DummyClient(responses_by_query={
        address: [_result(13.0418, 80.2341, "24 Kamaraj Street, T Nagar, Chennai, Tamil Nadu, 600017, India")],
    })
    geocoder = NominatimGeocoder(client=client, use_cache=False)

    result = geocoder.geocode(address)

    assert result is not None
    assert result.status == STATUS_OK
    assert result.lat == 13.0418
    assert result.lng == 80.2341
    # Strong match on the very first variant - no need to try broader ones.
    assert client.call_count == 1


def test_geocode_picks_best_of_multiple_candidates_from_same_query():
    address = "Flat 5, Kamaraj Block, XYZ Apartment, Velachery, Chennai"
    client = _DummyClient(responses_by_query={
        address: [
            _result(1.0, 1.0, "Gandhi Block, XYZ Apartment, Velachery, Chennai, India"),
            _result(12.99, 80.22, "Kamaraj Block, XYZ Apartment, Velachery, Chennai, India"),
        ],
    })
    geocoder = NominatimGeocoder(client=client, use_cache=False)

    result = geocoder.geocode(address)

    assert result is not None
    assert result.lat == 12.99
    assert result.lng == 80.22


def test_geocode_broadens_variants_when_full_address_fails():
    address = "24 Kamaraj Street, T Nagar, Chennai, Tamil Nadu 600017"
    broadened_query = "T Nagar, Chennai, Tamil Nadu 600017"
    client = _DummyClient(responses_by_query={
        broadened_query: [_result(13.0418, 80.2341, "T Nagar, Chennai, Tamil Nadu, 600017, India")],
    })
    geocoder = NominatimGeocoder(client=client, use_cache=False)

    result = geocoder.geocode(address)

    assert result is not None
    assert result.lat == 13.0418
    assert client.call_count >= 2  # full address (and maybe stripped) failed first


def test_geocode_falls_back_to_locality_when_nothing_resolves():
    address = "Some Totally Unmappable Place, Velachery, Chennai"
    client = _DummyClient()  # every query returns no results

    geocoder = NominatimGeocoder(client=client, use_cache=False)
    result = geocoder.geocode(address)

    assert result is not None
    assert result.status == STATUS_NEEDS_MANUAL_VERIFICATION
    assert result.provider == "nominatim"
    assert "velachery" in result.formatted_address.lower()


def test_geocode_returns_none_when_completely_unresolvable():
    address = "Nowhere In Particular, Nonexistent City"
    client = _DummyClient()

    geocoder = NominatimGeocoder(client=client, use_cache=False)
    result = geocoder.geocode(address)

    assert result is None


def test_geocode_rejects_out_of_bounds_coordinates():
    address = "Some Address, Chennai 600001"
    client = _DummyClient(responses_by_query={
        address: [_result(51.5, -0.12, "Somewhere in London, 600001")],  # nonsense for an India-only app
    })
    geocoder = NominatimGeocoder(client=client, use_cache=False)

    result = geocoder.geocode(address)

    assert result is None


def test_geocode_uses_cache_on_second_call():
    address = "12 Main Street, Chennai 600001"
    client = _DummyClient(responses_by_query={
        address: [_result(13.0, 80.1, "12 Main Street, Chennai, 600001, India")],
    })
    geocoder = NominatimGeocoder(client=client, use_cache=True)

    first = geocoder.geocode(address)
    second = geocoder.geocode(address)

    assert first == second
    assert client.call_count == 1


def test_geocode_retries_on_5xx_then_succeeds():
    # No comma/pincode/locality in the address, so build_address_variants
    # produces exactly one variant - keeps the retry-call-count assertion
    # unambiguous (a pincode or locality would add extra variants, each
    # with their own retry attempts, inflating the count).
    client = _DummyClient(sequential_responses=[
        _DummyResponse({}, status_code=503),
        _DummyResponse({}, status_code=503),
        _DummyResponse([_result(13.0, 80.1, "Resolved Somewhere, Chennai, India")], status_code=200),
    ])
    geocoder = NominatimGeocoder(client=client, max_retries=5, retry_backoff_seconds=0, use_cache=False)

    result = geocoder.geocode("Busy Unresolvable Location Somewhere")

    assert result is not None
    assert client.call_count == 3


def test_geocode_gives_up_after_max_retries_on_persistent_5xx():
    client = _DummyClient(sequential_responses=[_DummyResponse({}, status_code=503)])
    geocoder = NominatimGeocoder(client=client, max_retries=3, retry_backoff_seconds=0, use_cache=False)

    result = geocoder._fetch("Always down query")

    assert result is None
    assert client.call_count == 3


def test_geocode_does_not_retry_on_non_transient_4xx():
    client = _DummyClient(sequential_responses=[_DummyResponse({}, status_code=400)])
    geocoder = NominatimGeocoder(client=client, max_retries=3, retry_backoff_seconds=0, use_cache=False)

    result = geocoder._fetch("Malformed query")

    assert result is None
    assert client.call_count == 1


def test_geocode_returns_none_for_empty_address():
    geocoder = NominatimGeocoder(client=_DummyClient(), use_cache=False)
    assert geocoder.geocode("") is None


def test_geocode_raises_provider_error_on_access_blocked():
    # HTTP 403 from Nominatim means this IP/connection is blocked by their
    # usage policy - an account/IP problem, not specific to any address.
    # Every remaining variant and every other address would fail
    # identically, so this must raise so the caller can stop immediately.
    client = _DummyClient(sequential_responses=[_DummyResponse({}, status_code=403)])
    geocoder = NominatimGeocoder(client=client, max_retries=3, retry_backoff_seconds=0, use_cache=False)

    with pytest.raises(GeocodingProviderError):
        geocoder.geocode("Some Address, Chennai")

    assert client.call_count == 1
