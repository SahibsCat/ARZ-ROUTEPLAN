import pytest

from app.geocoding.base import STATUS_NEEDS_MANUAL_VERIFICATION, STATUS_OK, GeocodingProviderError
from app.geocoding.mapbox_geocoder import MapboxGeocoder


class _DummyResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

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
        return self._responses[index]


def _feature(lng, lat, place_name, relevance):
    return {
        "center": [lng, lat],
        "place_name": place_name,
        "relevance": relevance,
    }


def test_geocode_returns_result_for_high_relevance_match():
    data = {"features": [_feature(80.22, 12.99, "XYZ Apartment, Velachery, Chennai, India", 0.95)]}
    client = _DummyClient([_DummyResponse(data)])
    geocoder = MapboxGeocoder(access_token="test-token", client=client, retry_backoff_seconds=0)

    result = geocoder.geocode("XYZ Apartment, Velachery, Chennai")

    assert result is not None
    assert result.lat == 12.99
    assert result.lng == 80.22
    assert result.status == STATUS_OK
    assert result.confidence == 0.95
    assert result.provider == "mapbox"
    assert client.call_count == 1


def test_geocode_flags_low_relevance_as_needs_manual_verification():
    data = {"features": [_feature(80.0, 13.0, "Somewhere vaguely nearby", 0.3)]}
    client = _DummyClient([_DummyResponse(data)])
    geocoder = MapboxGeocoder(access_token="test-token", client=client, retry_backoff_seconds=0, min_relevance=0.5)

    result = geocoder.geocode("Some vague address")

    assert result is not None
    assert result.status == STATUS_NEEDS_MANUAL_VERIFICATION
    assert result.confidence == 0.3


def test_geocode_picks_highest_relevance_among_multiple_features():
    data = {
        "features": [
            _feature(80.0, 13.0, "Low relevance match", 0.4),
            _feature(80.22, 12.99, "High relevance match", 0.9),
        ]
    }
    client = _DummyClient([_DummyResponse(data)])
    geocoder = MapboxGeocoder(access_token="test-token", client=client, retry_backoff_seconds=0)

    result = geocoder.geocode("Some address")

    assert result.formatted_address == "High relevance match"
    assert result.confidence == 0.9


def test_geocode_returns_none_on_zero_results():
    client = _DummyClient([_DummyResponse({"features": []})])
    geocoder = MapboxGeocoder(access_token="test-token", client=client, retry_backoff_seconds=0)

    result = geocoder.geocode("Nonexistent place")

    assert result is None
    assert client.call_count == 1


def test_geocode_retries_on_429_then_succeeds():
    data_ok = {"features": [_feature(80.22, 12.99, "Resolved", 0.9)]}
    client = _DummyClient([
        _DummyResponse({}, status_code=429),
        _DummyResponse({}, status_code=429),
        _DummyResponse(data_ok, status_code=200),
    ])
    geocoder = MapboxGeocoder(access_token="test-token", client=client, max_retries=5, retry_backoff_seconds=0)

    result = geocoder.geocode("Busy address")

    assert result is not None
    assert result.confidence == 0.9
    assert client.call_count == 3


def test_geocode_gives_up_after_max_retries_on_persistent_429():
    client = _DummyClient([_DummyResponse({}, status_code=429)])
    geocoder = MapboxGeocoder(access_token="test-token", client=client, max_retries=3, retry_backoff_seconds=0)

    result = geocoder.geocode("Always busy address")

    assert result is None
    assert client.call_count == 3


def test_geocode_raises_provider_error_on_bad_token():
    # 401/403 mean the token itself is bad/revoked - an account problem,
    # not specific to this address. Every remaining address would fail
    # identically, so this must raise (not just return None) so the caller
    # can stop immediately instead of retrying per address.
    client = _DummyClient([_DummyResponse({}, status_code=401)])
    geocoder = MapboxGeocoder(access_token="bad-token", client=client, max_retries=3, retry_backoff_seconds=0)

    with pytest.raises(GeocodingProviderError):
        geocoder.geocode("Some address")

    assert client.call_count == 1


def test_geocode_does_not_retry_on_non_transient_per_address_error():
    # 422 (malformed query) is specific to this one address - retrying
    # identical input won't help, but it's not an account-level failure,
    # so it should just return None, not raise.
    client = _DummyClient([_DummyResponse({}, status_code=422)])
    geocoder = MapboxGeocoder(access_token="test-token", client=client, max_retries=3, retry_backoff_seconds=0)

    result = geocoder.geocode("Some malformed address")

    assert result is None
    assert client.call_count == 1


def test_geocode_returns_none_without_access_token():
    client = _DummyClient([_DummyResponse({"features": []})])
    geocoder = MapboxGeocoder(access_token="", client=client)

    result = geocoder.geocode("Some address")

    assert result is None
    assert client.call_count == 0
