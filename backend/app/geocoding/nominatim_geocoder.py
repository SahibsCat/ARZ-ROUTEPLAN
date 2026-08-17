import re
import threading
import time
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

import httpx

from app.geocoding.base import (
    STATUS_NEEDS_MANUAL_VERIFICATION,
    STATUS_OK,
    GeocodeResult,
    GeocodingProvider,
    GeocodingProviderError,
)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 1.0
# Nominatim's public-instance usage policy caps requests at ~1/sec per IP.
MIN_REQUEST_INTERVAL_SECONDS = 1.1
RESULTS_PER_QUERY = 5
DEFAULT_MIN_CONFIDENCE = 0.5
# A fuzzy string-similarity ratio at or above this counts as "the same
# word" - catches typos, abbreviations (Rd/Road), and minor formatting
# differences without matching on unrelated short words.
FUZZY_MATCH_THRESHOLD = 0.82

HEADERS = {
    # Nominatim's usage policy requires a descriptive User-Agent identifying
    # the application - not a browser-spoofing one.
    "User-Agent": "Routeplan-DeliveryPlanner/1.0 (contact: rootplan@example.com)",
    "Accept-Language": "en-IN,en;q=0.9",
}

# Rough India bounding box - a "successful" geocode outside this is almost
# certainly wrong and must not be trusted.
INDIA_LAT_RANGE = (6.0, 37.6)
INDIA_LNG_RANGE = (68.0, 97.5)

_GENERIC_LOCALITY_WORDS = {
    "chennai", "tamil", "nadu", "india", "greater", "tn", "in",
    "street", "road", "nagar", "main", "cross",
}

# Used both to build a well-formed "locality + pincode" query variant, and
# as an absolute last-resort fallback (always flagged NEEDS_MANUAL_VERIFICATION
# - never silently accepted as a precise point).
CHENNAI_LOCALITY_MAP: Dict[str, Tuple[float, float]] = {
    "velachery": (12.9796, 80.2207),
    "guindy": (13.0067, 80.2020),
    "adyar": (13.0012, 80.2565),
    "t. nagar": (13.0418, 80.2341),
    "t nagar": (13.0418, 80.2341),
    "anna nagar": (13.0878, 80.2170),
    "mylapore": (13.0368, 80.2676),
    "tambaram": (12.9249, 80.1000),
    "chromepet": (12.9516, 80.1462),
    "saidapet": (13.0213, 80.2231),
    "porur": (13.0382, 80.1565),
    "thoraipakkam": (12.9382, 80.2372),
    "perungudi": (12.9654, 80.2461),
    "sholinganallur": (12.9010, 80.2279),
    "karapakkam": (12.9171, 80.2312),
    "nungambakkam": (13.0569, 80.2425),
    "egmore": (13.0732, 80.2609),
    "royapettah": (13.0522, 80.2642),
    "kodambakkam": (13.0515, 80.2209),
    "vadapalani": (13.0500, 80.2121),
    "ashok nagar": (13.0373, 80.2123),
    "k.k. nagar": (13.0380, 80.1983),
    "kk nagar": (13.0380, 80.1983),
    "madipakkam": (12.9647, 80.1961),
    "medavakkam": (12.9171, 80.1923),
    "pallikaranai": (12.9349, 80.2137),
    "selaiyur": (12.9110, 80.1416),
    "st thomas mount": (13.0033, 80.1986),
    "thiruvanmiyur": (12.9830, 80.2594),
    "kottivakkam": (12.9620, 80.2550),
    "palavakkam": (12.9510, 80.2540),
    "neelankarai": (12.9400, 80.2530),
    "ambattur": (13.1143, 80.1548),
    "avadi": (13.1167, 80.1013),
    "koyambedu": (13.0694, 80.1948),
    "alandur": (13.0040, 80.2010),
    "virugambakkam": (13.0538, 80.1927),
    "valasaravakkam": (13.0478, 80.1764),
    "ramapuram": (13.0306, 80.1784),
    "manapakkam": (13.0210, 80.1740),
    "nanganallur": (12.9806, 80.1887),
    "keelkattalai": (12.9566, 80.1834),
    "perambur": (13.1137, 80.2437),
    "kilpauk": (13.0805, 80.2415),
    "purasaivakkam": (13.0894, 80.2563),
    "chetpet": (13.0718, 80.2417),
    "choolaimedu": (13.0645, 80.2272),
    "west mambalam": (13.0383, 80.2227),
    "triplicane": (13.0587, 80.2757),
    "santhome": (13.0331, 80.2777),
    "mandaveli": (13.0280, 80.2612),
    "besant nagar": (13.0002, 80.2667),
}

# Process-lifetime cache, keyed by the exact address string handed to
# geocode() - avoids re-geocoding the same address across different orders,
# batches, and requests (distance_service.py's _ROUTE_CACHE follows the
# same pattern for OSRM lookups).
_GEOCODE_CACHE: Dict[str, Optional[GeocodeResult]] = {}
_cache_lock = threading.Lock()

_last_request_time = 0.0
_rate_limit_lock = threading.Lock()


def clear_geocode_cache() -> None:
    with _cache_lock:
        _GEOCODE_CACHE.clear()


def _throttle() -> None:
    """Serialize + space out every outbound Nominatim request (across all
    addresses and all variants) to stay within the ~1 req/sec policy -
    not just between variants of the same address."""
    global _last_request_time
    with _rate_limit_lock:
        wait = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        _last_request_time = time.monotonic()


def _is_valid_coordinate(lat: object, lng: object) -> bool:
    try:
        lat_f, lng_f = float(lat), float(lng)
    except (TypeError, ValueError):
        return False
    if lat_f != lat_f or lng_f != lng_f:  # NaN
        return False
    return (
        INDIA_LAT_RANGE[0] <= lat_f <= INDIA_LAT_RANGE[1]
        and INDIA_LNG_RANGE[0] <= lng_f <= INDIA_LNG_RANGE[1]
    )


def _extract_pincode(text: str) -> Optional[str]:
    match = re.search(r"\b(6\d{5})\b", text)
    return match.group(1) if match else None


def _extract_locality(address: str) -> Optional[str]:
    lower = address.lower()
    for locality in CHENNAI_LOCALITY_MAP:
        if locality in lower:
            return locality
    return None


def _significant_tokens(text: str) -> List[str]:
    tokens = re.split(r"[,\s/#-]+", text.lower())
    significant = []
    for token in tokens:
        token = token.strip(".")
        if not token or token.isdigit() or len(token) < 3:
            continue
        if token in _GENERIC_LOCALITY_WORDS:
            continue
        significant.append(token)
    return significant


def _fuzzy_token_match(token: str, display_tokens: List[str]) -> bool:
    for candidate in display_tokens:
        if token == candidate or token in candidate or candidate in token:
            return True
        if SequenceMatcher(None, token, candidate).ratio() >= FUZZY_MATCH_THRESHOLD:
            return True
    return False


def build_address_variants(address: str) -> List[str]:
    """Progressively broader queries to try against Nominatim/OSM, whose
    house/flat-level India coverage is patchy. Door/flat/apartment/building
    detail is deliberately preserved through every variant - only genuine
    landmark references ("near X", "opposite Y") are stripped, since those
    describe a DIFFERENT place and confuse the geocoder rather than help
    it."""
    variants: List[str] = []
    if address:
        variants.append(address)

    stripped = re.sub(
        r"\b(near|opp(?:osite)?|behind|in front of|beside|next to)\s*\w*",
        "",
        address,
        flags=re.IGNORECASE,
    )
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if stripped and stripped not in variants:
        variants.append(stripped)

    pincode = _extract_pincode(address)
    locality = _extract_locality(address)

    if locality:
        query = f"{locality.title()}, Chennai, Tamil Nadu"
        if pincode:
            query += f" {pincode}"
        if query not in variants:
            variants.append(query)

    if pincode:
        query = f"{pincode}, Chennai, Tamil Nadu, India"
        if query not in variants:
            variants.append(query)

    tokens = [token.strip() for token in re.split(r"[,\n]", address) if token.strip()]
    if len(tokens) >= 2:
        tail = ", ".join(tokens[-2:])
        if tail not in variants:
            variants.append(tail)

    return variants


def score_candidate(address: str, display_name: str) -> Tuple[bool, float]:
    """(accepted, score in [0,1]). Hard-rejects a pincode conflict (the
    class of bug that put an order in the wrong city/pincode entirely);
    otherwise scores by token/fuzzy overlap with the original address so
    the BEST of several returned candidates can be chosen, not just the
    first one Nominatim happens to list."""
    if not display_name:
        return False, -1.0

    address_pincode = _extract_pincode(address)
    display_pincode = _extract_pincode(display_name)
    pincode_bonus = 0.0
    if address_pincode and display_pincode:
        if address_pincode != display_pincode:
            return False, -1.0
        pincode_bonus = 0.3

    significant = _significant_tokens(address)
    if not significant:
        return True, min(1.0, 0.5 + pincode_bonus)

    display_tokens = _significant_tokens(display_name)
    matched = sum(1 for token in significant if _fuzzy_token_match(token, display_tokens))

    if matched == 0 and pincode_bonus == 0.0:
        return False, -1.0

    overlap_ratio = matched / len(significant)
    return True, min(1.0, overlap_ratio + pincode_bonus)


class NominatimGeocoder(GeocodingProvider):
    """Free OpenStreetMap/Nominatim geocoder. Since OSM's India coverage is
    inconsistent at house/flat level, this tries progressively broader
    query variants, scores every candidate a variant returns (rather than
    trusting the first result), rejects cross-pincode mismatches, and
    flags low-confidence matches for manual review instead of silently
    accepting a rough guess."""

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        client: Optional[httpx.Client] = None,
        use_cache: bool = True,
    ) -> None:
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._min_confidence = min_confidence
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self._use_cache = use_cache

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "NominatimGeocoder":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _fetch(self, query: str) -> Optional[list]:
        params = {
            "q": query,
            "format": "jsonv2",
            "limit": RESULTS_PER_QUERY,
            "addressdetails": 0,
            "countrycodes": "in",
            "email": "rootplan@example.com",
        }

        for attempt in range(1, self._max_retries + 1):
            _throttle()
            try:
                response = self._client.get(NOMINATIM_URL, params=params, headers=HEADERS)
            except httpx.RequestError as exc:
                if attempt == self._max_retries:
                    print(f"Nominatim: request failed for '{query}': {exc}")
                    return None
                time.sleep(self._retry_backoff_seconds * attempt)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                print(f"Nominatim: transient HTTP {response.status_code} for '{query}' (attempt {attempt})")
                if attempt == self._max_retries:
                    print(f"Nominatim: giving up on '{query}' after {attempt} attempt(s)")
                    return None
                time.sleep(self._retry_backoff_seconds * attempt)
                continue

            if response.status_code == 403:
                # Nominatim's usage-policy block (bad/missing User-Agent, or
                # this IP flagged for exceeding fair use) - an account/IP
                # problem, not specific to this query. Every remaining
                # variant and every other address would fail identically.
                raise GeocodingProviderError(
                    "Nominatim access is blocked for this connection (HTTP 403 - usage "
                    "policy). See https://operations.osmfoundation.org/policies/nominatim/ "
                    "- try again later or switch GEOCODING_PROVIDER.",
                    provider="nominatim",
                )

            if response.status_code >= 400:
                print(f"Nominatim: HTTP {response.status_code} for '{query}' - not retrying")
                return None

            try:
                return response.json()
            except ValueError as exc:
                if attempt == self._max_retries:
                    print(f"Nominatim: invalid JSON for '{query}': {exc}")
                    return None
                time.sleep(self._retry_backoff_seconds * attempt)
                continue

        return None

    def geocode(self, address: str) -> Optional[GeocodeResult]:
        if not address:
            return None

        if self._use_cache:
            with _cache_lock:
                if address in _GEOCODE_CACHE:
                    return _GEOCODE_CACHE[address]

        result = self._geocode_uncached(address)

        if self._use_cache:
            with _cache_lock:
                _GEOCODE_CACHE[address] = result

        return result

    def _geocode_uncached(self, address: str) -> Optional[GeocodeResult]:
        variants = build_address_variants(address)
        best: Optional[Dict[str, object]] = None
        best_score = -1.0

        for variant_index, query in enumerate(variants):
            start = time.monotonic()
            results = self._fetch(query)
            elapsed = time.monotonic() - start

            if not results:
                print(f"Nominatim: no results for '{query}' ({elapsed:.2f}s)")
                continue

            for candidate in results:
                display_name = candidate.get("display_name", "")
                accepted, score = score_candidate(address, display_name)
                if not accepted:
                    continue

                lat, lng = candidate.get("lat"), candidate.get("lon")
                if not _is_valid_coordinate(lat, lng):
                    continue

                if score > best_score:
                    best_score = score
                    best = {"lat": float(lat), "lng": float(lng), "display_name": display_name}

            # A strong match from the full (or landmark-stripped) address
            # is as good as it gets - stop spending requests broadening
            # further.
            if best is not None and variant_index <= 1 and best_score >= 0.8:
                break

        if best is None:
            return self._locality_fallback(address)

        status = STATUS_OK if best_score >= self._min_confidence else STATUS_NEEDS_MANUAL_VERIFICATION
        print(
            f"Nominatim: '{address}' -> lat={best['lat']}, lng={best['lng']}, "
            f"confidence={best_score:.2f}, status={status}"
        )

        return GeocodeResult(
            lat=best["lat"],
            lng=best["lng"],
            formatted_address=str(best["display_name"]),
            status=status,
            provider="nominatim",
            confidence=round(best_score, 2),
        )

    def _locality_fallback(self, address: str) -> Optional[GeocodeResult]:
        locality = _extract_locality(address)
        if not locality:
            print(f"Nominatim: '{address}' completely unresolved")
            return None

        lat, lng = CHENNAI_LOCALITY_MAP[locality]
        print(f"Nominatim: '{address}' -> locality-only fallback ({locality}), flagged for review")
        return GeocodeResult(
            lat=lat,
            lng=lng,
            formatted_address=f"{locality.title()}, Chennai, Tamil Nadu, India",
            status=STATUS_NEEDS_MANUAL_VERIFICATION,
            provider="nominatim",
            confidence=0.2,
        )
