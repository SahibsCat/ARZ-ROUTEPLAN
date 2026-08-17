import time
import urllib.parse
from typing import Optional

import httpx

from app.geocoding.base import (
    STATUS_NEEDS_MANUAL_VERIFICATION,
    STATUS_OK,
    GeocodeResult,
    GeocodingProvider,
    GeocodingProviderError,
)

MAPBOX_GEOCODE_URL_TEMPLATE = "https://api.mapbox.com/geocoding/v5/mapbox.places/{query}.json"

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 1.0
# Mapbox relevance is 0-1. Below this, the match is too loose to trust as a
# precise delivery point - flag it for a human to confirm instead of
# silently accepting a rough guess.
DEFAULT_MIN_RELEVANCE = 0.5

# 429 (rate limited) and 5xx are transient - worth retrying. Anything else
# (401 bad token, 422 bad query, ...) is a configuration/input problem that
# retrying won't fix.
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class MapboxGeocoder(GeocodingProvider):
    """Mapbox Forward Geocoding API client. Same shape as NominatimGeocoder:
    owns a single reused HTTP session, handles retries/timeouts itself, and
    returns the shared GeocodeResult type so callers never need to know
    which provider actually answered."""

    def __init__(
        self,
        access_token: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
        min_relevance: float = DEFAULT_MIN_RELEVANCE,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self._access_token = access_token
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._min_relevance = min_relevance
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "MapboxGeocoder":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def geocode(self, address: str) -> Optional[GeocodeResult]:
        if not address or not self._access_token:
            return None

        url = MAPBOX_GEOCODE_URL_TEMPLATE.format(query=urllib.parse.quote(address, safe=""))
        params = {
            "access_token": self._access_token,
            "country": "IN",
            "limit": 5,
        }

        for attempt in range(1, self._max_retries + 1):
            start = time.monotonic()
            try:
                response = self._client.get(url, params=params)
                elapsed = time.monotonic() - start
            except httpx.RequestError as exc:
                # Network-level failure (timeout, connection error, ...) -
                # always worth retrying.
                if attempt == self._max_retries:
                    print(f"Mapbox Geocoding request failed for '{address}': {exc}")
                    return None
                time.sleep(self._retry_backoff_seconds * attempt)
                continue

            if response.status_code in _RETRYABLE_STATUS_CODES:
                print(f"Mapbox Geocoding: transient HTTP {response.status_code} for '{address}' (attempt {attempt}, {elapsed:.2f}s)")
                if attempt == self._max_retries:
                    print(f"Mapbox Geocoding: giving up on '{address}' after {attempt} attempt(s)")
                    return None
                time.sleep(self._retry_backoff_seconds * attempt)
                continue

            if response.status_code in (401, 403):
                # Bad/missing/revoked token, or plan restriction - an
                # account problem, not specific to this address. Every
                # subsequent request would fail identically.
                raise GeocodingProviderError(
                    f"Mapbox Geocoding is not working: HTTP {response.status_code} - "
                    "check MAPBOX_ACCESS_TOKEN is valid and has geocoding scope.",
                    provider="mapbox",
                )

            if response.status_code >= 400:
                # Non-transient error specific to this query (malformed
                # address, ...) - retrying identical input won't help, fail
                # just this address.
                print(f"Mapbox Geocoding: HTTP {response.status_code} for '{address}' - not retrying")
                return None

            try:
                data = response.json()
            except ValueError as exc:
                if attempt == self._max_retries:
                    print(f"Mapbox Geocoding: invalid JSON response for '{address}': {exc}")
                    return None
                time.sleep(self._retry_backoff_seconds * attempt)
                continue

            features = data.get("features") or []
            if not features:
                print(f"Mapbox Geocoding: no results for '{address}' ({elapsed:.2f}s)")
                return None

            best = max(features, key=lambda feature: feature.get("relevance", 0.0))
            center = best.get("center")
            relevance = float(best.get("relevance", 0.0))

            if not center or len(center) != 2:
                print(f"Mapbox Geocoding: malformed result for '{address}' - missing coordinates")
                return None

            status = STATUS_OK if relevance >= self._min_relevance else STATUS_NEEDS_MANUAL_VERIFICATION

            print(
                f"Mapbox Geocoding: '{address}' -> "
                f"lat={center[1]}, lng={center[0]}, relevance={relevance:.2f}, "
                f"status={status}, response_time={elapsed:.2f}s"
            )

            return GeocodeResult(
                lat=float(center[1]),
                lng=float(center[0]),
                formatted_address=best.get("place_name", ""),
                status=status,
                provider="mapbox",
                confidence=relevance,
            )

        return None
