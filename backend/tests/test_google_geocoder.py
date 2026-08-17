import pytest

from app.geocoding.base import GeocodingProviderError
from app.geocoding.google_geocoder import GoogleGeocoder


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
            "geometry": {"location": {"lat": 12.99, "lng": 80.22}},
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
    assert client.call_count == 1


def test_geocode_returns_none_on_zero_results_without_retry():
    client = _DummyClient([{"status": "ZERO_RESULTS"}])
    geocoder = GoogleGeocoder(api_key="test-key", client=client, max_retries=3, retry_backoff_seconds=0)

    result = geocoder.geocode("Nonexistent Address")

    assert result is None
    assert client.call_count == 1


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


def test_geocode_returns_none_on_invalid_request_without_retry():
    client = _DummyClient([{"status": "INVALID_REQUEST"}])
    geocoder = GoogleGeocoder(api_key="test-key", client=client, max_retries=3, retry_backoff_seconds=0)

    result = geocoder.geocode("Some malformed query")

    assert result is None
    assert client.call_count == 1


def test_geocode_retries_on_over_query_limit_then_succeeds():
    responses = [
        {"status": "OVER_QUERY_LIMIT"},
        {"status": "OVER_QUERY_LIMIT"},
        {
            "status": "OK",
            "results": [{
                "formatted_address": "Resolved Address, Chennai",
                "geometry": {"location": {"lat": 13.0, "lng": 80.2}},
            }],
        },
    ]
    client = _DummyClient(responses)
    geocoder = GoogleGeocoder(api_key="test-key", client=client, max_retries=5, retry_backoff_seconds=0)

    result = geocoder.geocode("Some Busy Address")

    assert result is not None
    assert result.lat == 13.0
    assert client.call_count == 3


def test_geocode_gives_up_after_max_retries_on_persistent_over_query_limit():
    client = _DummyClient([{"status": "OVER_QUERY_LIMIT"}])
    geocoder = GoogleGeocoder(api_key="test-key", client=client, max_retries=3, retry_backoff_seconds=0)

    result = geocoder.geocode("Some Address")

    assert result is None
    assert client.call_count == 3


def test_geocode_returns_none_without_api_key():
    client = _DummyClient([{"status": "OK", "results": []}])
    geocoder = GoogleGeocoder(api_key="", client=client)

    result = geocoder.geocode("Some Address")

    assert result is None
    assert client.call_count == 0
