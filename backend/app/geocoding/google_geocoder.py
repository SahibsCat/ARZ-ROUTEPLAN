import re
import time
from typing import Iterable, Optional

import httpx

from app.geocoding.base import (
    STATUS_NEEDS_MANUAL_VERIFICATION,
    STATUS_OK,
    GeocodeResult,
    GeocodingProvider,
    GeocodingProviderError,
)

GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
# Places API "Find Place From Text" - a fuzzy, named-establishment search,
# not a structured-address parser. Used only as a fallback (see
# GoogleGeocoder._find_place) when the Geocoding API above can't place an
# address precisely - which is common for "Sidharth Upscale Apartments,
# Porur" style addresses that name a specific building/complex rather than
# a street + number, exactly the class of address the Geocoding API isn't
# built to recognize by name.
PLACES_FIND_PLACE_URL = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 1.0
# Below this derived confidence (see _score_result), a match is too loose to
# trust as a precise delivery point - same threshold/philosophy Mapbox and
# Nominatim already apply, so all three providers behave consistently.
DEFAULT_MIN_CONFIDENCE = 0.5
# A Places API match is trusted at this fixed confidence when used at all -
# not as certain as a real ROOFTOP geocode, but Places found and named an
# actual establishment/building, meaningfully more specific than the
# locality/postal-code-level fallback the Geocoding API landed on instead
# (the only time this fallback is even tried - see geocode()).
PLACES_FALLBACK_CONFIDENCE = 0.65
# Chennai-centered, ~50km radius - every address this app handles is in
# Chennai (see geocode_service.clean_address, which appends ", Chennai,
# India" whenever neither is already present) - biasing Places' fuzzy text
# search this way keeps it from matching a same-named place elsewhere in
# India entirely.
_CHENNAI_LOCATION_BIAS = "circle:50000@13.0827,80.2707"

# Phrases that describe a DIFFERENT nearby place, not the delivery address
# itself ("near X", "opposite Y") - stripping them and retrying is a free
# second attempt when the first one scores low, since these confuse a
# structured-address parser without adding any real address information.
# Same idea as nominatim_geocoder.build_address_variants's landmark strip.
_LANDMARK_PATTERN = re.compile(
    r"\b(near|opp(?:osite)?|behind|in front of|beside|next to)\b[^,]*",
    re.IGNORECASE,
)

# https://developers.google.com/maps/documentation/geocoding/requests-geocoding#StatusCodes
# OVER_QUERY_LIMIT/UNKNOWN_ERROR are transient - worth retrying. ZERO_RESULTS
# means this particular address genuinely doesn't resolve - a per-address
# failure. REQUEST_DENIED is an account/key/billing problem (see geocode()),
# not a per-address one - handled separately as a GeocodingProviderError.
_TERMINAL_FAILURE_STATUSES = {"ZERO_RESULTS", "INVALID_REQUEST"}

# https://developers.google.com/maps/documentation/geocoding/requests-geocoding#Results
# `types` on the top-level result - these mean Google actually matched a
# specific building/unit, not just an area.
_PRECISE_TYPES = {"street_address", "premise", "subpremise", "point_of_interest", "establishment"}
# Every one of these describes an AREA, never a single delivery point. A
# result made up only of types from this set - regardless of location_type -
# is Google saying "the best I could do was a city/neighbourhood/postal
# code", not a specific address, and must never be trusted as a rooftop pin.
_AREA_ONLY_TYPES = {
    "locality", "sublocality", "sublocality_level_1", "sublocality_level_2",
    "sublocality_level_3", "sublocality_level_4", "sublocality_level_5",
    "neighborhood", "administrative_area_level_1", "administrative_area_level_2",
    "administrative_area_level_3", "administrative_area_level_4", "administrative_area_level_5",
    "postal_code", "postal_town", "country", "political",
}


def _score_result(location_type: Optional[str], types: Iterable[str], partial_match: bool) -> float:
    """Derives a 0-1 confidence score from the three signals Google's
    Geocoding API actually gives for how precise a match is - it has no
    single relevance number the way Mapbox does, so this combines them the
    same way a human reviewing the raw JSON would:

    This is the actual fix for "the address text is right but the map pin
    isn't": every one of these signals used to be ignored entirely - any
    result Google returned, however rough, was accepted as a fully-precise
    match. In practice, an address Google can't pin exactly (a flat/door
    number or building name it doesn't have) doesn't error out - it falls
    back to the nearest area it CAN match (the street, the neighbourhood,
    the postal code, sometimes just the city) and still returns a clean,
    reasonable-looking `formatted_address` for THAT area. The text looks
    fine because it is fine, for the area Google actually matched - the
    marker is just planted at that area's center, not the specific address
    that was asked for.

      - location_type: ROOFTOP (an exact building match) down to APPROXIMATE
        (a rough guess) - see the module-level comment above _PRECISE_TYPES.
      - types: whether the match resolved to a specific place/building or
        only to an administrative area/postal code/locality.
      - partial_match: Google's own admission that it couldn't match every
        component of the query (most often a flat/door number or building
        name) - the point returned may be for the street/area, not the
        specific place asked for.
    """
    types_set = set(types or [])

    if location_type == "ROOFTOP":
        score = 0.95
    elif location_type == "RANGE_INTERPOLATED":
        # Interpolated between two known points along a street - not exact,
        # but still anchored to the right road and roughly the right spot.
        # Normal (and reliable) for a full street address when rooftop-level
        # data isn't available, which is common in India.
        score = 0.8
    elif location_type == "GEOMETRIC_CENTER":
        # Center of a line/polygon - fine if that polygon IS the specific
        # building/POI asked for, not fine if it's the center of an entire
        # street/area with no real number match. Only `types` tells them
        # apart.
        score = 0.65 if types_set & _PRECISE_TYPES else 0.3
    elif location_type == "APPROXIMATE":
        # Google's own "rough guess" marker - normally means it fell all the
        # way back to a locality/postal-code/administrative-area centroid.
        # This is the case that most often looks like "text is right, pin
        # is nowhere near it."
        score = 0.55 if types_set & _PRECISE_TYPES else 0.2
    else:
        score = 0.3

    if types_set and not (types_set & _PRECISE_TYPES) and not (types_set - _AREA_ONLY_TYPES):
        # Belt-and-suspenders: whatever location_type said, if every type on
        # this result is an area-level type, it is not a delivery point.
        score = min(score, 0.25)

    if partial_match:
        score = max(0.05, score - 0.15)

    return round(score, 2)


def _strip_landmark_phrase(address: str) -> str:
    stripped = _LANDMARK_PATTERN.sub("", address)
    stripped = re.sub(r"\s+", " ", stripped).strip().strip(",").strip()
    return stripped


def _rank(result: Optional[GeocodeResult]) -> float:
    """Comparable score for 'is this candidate better than that one' - a
    missing result never beats even the lowest real confidence."""
    if result is None:
        return -1.0
    return result.confidence if result.confidence is not None else 0.0


class GoogleGeocoder(GeocodingProvider):
    """Google Geocoding API client. Owns a single HTTP session reused across
    every call (pass an explicit `client` to share one across geocoders, or
    let it create/own its own via the context manager).

    `places_api_key` is optional and separate from `api_key` on purpose -
    Google Cloud API-restricted keys are commonly scoped to exactly one API
    each (least-privilege), so a project may have one key allowlisted for
    Geocoding API only and a different key allowlisted for Places API only.
    Defaults to `api_key` (the single-key setup, one key enabled for both
    APIs) when not given separately."""

    def __init__(
        self,
        api_key: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        client: Optional[httpx.Client] = None,
        places_api_key: Optional[str] = None,
    ) -> None:
        self._api_key = api_key
        self._places_api_key = places_api_key or api_key
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._min_confidence = min_confidence
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
        """Up to three attempts, each only tried when the one before it
        didn't already land a confident (STATUS_OK) match - cheapest first:
          1. The address as given, straight to the Geocoding API.
          2. The same address with landmark phrases ("near X", "opposite Y")
             stripped - free (same API), just a cleaner query. Skipped if
             there was no landmark phrase to strip.
          3. Places API's fuzzy text search - for addresses that name a
             specific apartment/building the structured Geocoding API isn't
             built to recognize (see _find_place). Real added cost per call,
             which is exactly why this is a last resort, not the first
             attempt.
        Whichever attempt scores highest wins; if none reach STATUS_OK, the
        best (still-flagged) one found is returned rather than nothing -
        never worse than a single plain attempt would have been."""
        if not address or not self._api_key:
            return None

        best = self._geocode_once(address)
        if best is not None and best.status == STATUS_OK:
            return best

        stripped = _strip_landmark_phrase(address)
        if stripped and stripped.lower() != address.lower():
            variant = self._geocode_once(stripped)
            if _rank(variant) > _rank(best):
                print(f"Google Geocoding: landmark-stripped retry '{address}' -> '{stripped}' improved the match")
                best = variant
            if best is not None and best.status == STATUS_OK:
                return best

        fallback = self._find_place(address)
        if _rank(fallback) > _rank(best):
            best = fallback

        return best

    def _find_place(self, address: str) -> Optional[GeocodeResult]:
        """Falls back to Places API's text search when the structured
        Geocoding API could only place an address at area level - Places is
        built to match a NAMED establishment/apartment complex/building,
        which the Geocoding API's structured address parser often can't do
        (it expects a street + number, not "Sidharth Upscale Apartments").

        Deliberately non-fatal on every failure path, unlike _geocode_once's
        REQUEST_DENIED handling: a denial here means "this fallback isn't
        enabled on this key/project", not "the provider is broken" - the
        Geocoding API calls above already prove the key/billing works, so
        this only ever returns None instead of raising, leaving the
        Geocoding API's own (possibly flagged) result as the answer."""
        if not self._places_api_key:
            return None

        params = {
            "input": address,
            "inputtype": "textquery",
            "fields": "geometry,formatted_address,name",
            "locationbias": _CHENNAI_LOCATION_BIAS,
            "key": self._places_api_key,
        }

        try:
            response = self._client.get(PLACES_FIND_PLACE_URL, params=params)
            response.raise_for_status()
            data = response.json()
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
            print(f"Places API fallback request failed for '{address}': {exc}")
            return None

        status = data.get("status", "UNKNOWN_ERROR")
        if status != "OK":
            if status != "ZERO_RESULTS":
                print(f"Places API fallback: {status} for '{address}' - skipping (Places API may not be enabled)")
            return None

        candidates = data.get("candidates") or []
        if not candidates:
            return None
        location = candidates[0].get("geometry", {}).get("location", {})
        if "lat" not in location or "lng" not in location:
            return None

        formatted = candidates[0].get("formatted_address") or candidates[0].get("name") or address
        print(f"Places API fallback: '{address}' -> matched '{formatted}' (confidence={PLACES_FALLBACK_CONFIDENCE:.2f})")
        return GeocodeResult(
            lat=float(location["lat"]),
            lng=float(location["lng"]),
            formatted_address=formatted,
            status=STATUS_OK if PLACES_FALLBACK_CONFIDENCE >= self._min_confidence else STATUS_NEEDS_MANUAL_VERIFICATION,
            provider="google-places",
            confidence=PLACES_FALLBACK_CONFIDENCE,
        )

    def _geocode_once(self, address: str) -> Optional[GeocodeResult]:
        """One Geocoding API call for one exact address string - no retries
        across query variants, that's geocode()'s job. Still retries on
        transient failures (network errors, OVER_QUERY_LIMIT) for this one
        call, same as before this method was split out of geocode()."""
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
                geometry = result.get("geometry", {})
                location = geometry.get("location", {})
                if "lat" not in location or "lng" not in location:
                    return None

                location_type = geometry.get("location_type")
                result_types = result.get("types") or []
                partial_match = bool(result.get("partial_match", False))
                confidence = _score_result(location_type, result_types, partial_match)
                result_status = STATUS_OK if confidence >= self._min_confidence else STATUS_NEEDS_MANUAL_VERIFICATION

                if result_status != STATUS_OK:
                    print(
                        f"Google Geocoding: '{address}' -> low-precision match "
                        f"(location_type={location_type}, types={result_types}, "
                        f"partial_match={partial_match}, confidence={confidence:.2f}) - "
                        "flagged for manual verification"
                    )

                return GeocodeResult(
                    lat=float(location["lat"]),
                    lng=float(location["lng"]),
                    formatted_address=result.get("formatted_address", ""),
                    status=result_status,
                    provider="google",
                    confidence=confidence,
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
