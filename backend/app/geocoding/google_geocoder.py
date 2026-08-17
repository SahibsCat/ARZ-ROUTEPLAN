import time
from typing import Optional

import httpx

from app.geocoding.base import GeocodeResult, GeocodingProvider, GeocodingProviderError

GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 1.0

# https://developers.google.com/maps/documentation/geocoding/requests-geocoding#StatusCodes
# OVER_QUERY_LIMIT/UNKNOWN_ERROR are transient - worth retrying. ZERO_RESULTS
# means this particular address genuinely doesn't resolve - a per-address
# failure. REQUEST_DENIED is an account/key/billing problem (see geocode()),
# not a per-address one - handled separately as a GeocodingProviderError.
_TERMINAL_FAILURE_STATUSES = {"ZERO_RESULTS", "INVALID_REQUEST"}


class GoogleGeocoder(GeocodingProvider):
    """Google Geocoding API client. Owns a single HTTP session reused across
    every call (pass an explicit `client` to share one across geocoders, or
    let it create/own its own via the context manager)."""

    def __init__(
        self,
        api_key: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self._api_key = api_key
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "GoogleGeocoder":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def geocode(self, address: str) -> Optional[GeocodeResult]:
        if not address or not self._api_key:
            return None

        params = {
            "address": address,
            "key": self._api_key,
            # Every address this app handles is in India - a hard country
            # restriction avoids ambiguous same-name-different-country
            # matches (stronger than a soft region bias).
            "components": "country:IN",
        }

        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._client.get(GOOGLE_GEOCODE_URL, params=params)
                response.raise_for_status()
                data = response.json()
            except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
                if attempt == self._max_retries:
                    print(f"Google Geocoding request failed for '{address}': {exc}")
                    return None
                time.sleep(self._retry_backoff_seconds * attempt)
                continue

            status = data.get("status", "UNKNOWN_ERROR")

            if status == "OK":
                results = data.get("results") or []
                if not results:
                    return None
                result = results[0]
                location = result.get("geometry", {}).get("location", {})
                if "lat" not in location or "lng" not in location:
                    return None
                return GeocodeResult(
                    lat=float(location["lat"]),
                    lng=float(location["lng"]),
                    formatted_address=result.get("formatted_address", ""),
                    status=status,
                    provider="google",
                )

            if status == "REQUEST_DENIED":
                # Account/key/billing problem - NOT specific to this address.
                # Every subsequent request would fail identically, so the
                # caller needs to know to stop immediately rather than treat
                # this as "this one address couldn't be found."
                reason = data.get("error_message", "Request denied by Google - check API key and billing status.")
                raise GeocodingProviderError(
                    f"Google Geocoding is not working: {reason}", provider="google"
                )

            if status in _TERMINAL_FAILURE_STATUSES:
                reason = data.get("error_message", "")
                print(f"Google Geocoding: {status} for '{address}'{f' - {reason}' if reason else ''}")
                return None

            # OVER_QUERY_LIMIT / UNKNOWN_ERROR / anything undocumented.
            if attempt == self._max_retries:
                print(f"Google Geocoding: giving up on '{address}' after {attempt} attempt(s) (last status: {status})")
                return None
            time.sleep(self._retry_backoff_seconds * attempt)

        return None
