import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

import httpx
from sqlalchemy.orm import Session

from app import crud
from app.geocoding import address_parser
from app.geocoding.chennai_localities import correct_locality_spelling
from app.geocoding.base import (
    STATUS_OK,
    GeocodeResult,
    GeocodingProvider,
    GeocodingProviderError,
)
from app.geocoding.provider_factory import build_geocoding_provider

# How many addresses geocode_orders will have in flight at once - see its
# own docstring for why this exists at all. Google/Mapbox have no
# meaningful rate limit at this volume and httpx.Client is documented
# thread-safe; Nominatim self-throttles to ~1 req/sec across ALL callers
# via its own module-level lock (nominatim_geocoder._throttle), so running
# it concurrently doesn't violate that policy - it just serializes there
# naturally, without this module needing to know which provider is active.
GEOCODE_CONCURRENCY = 8


def clean_address(address: str) -> str:
    """Normalize an address before sending it to any geocoding provider:
    repair the formatting damage real customer input arrives with (see
    address_parser.normalize - glued words, missing spaces after a
    house-number prefix, unambiguous abbreviations), then make sure the
    city/state is present so the query has enough context.

    Never mutates what the customer typed - Order.address keeps the
    original; this is only ever the text handed to a provider."""
    if not address:
        return ""

    address = address_parser.normalize(address)
    address = correct_locality_spelling(address)
    if not address:
        # Whitespace/punctuation-only input has nothing to geocode -
        # appending the city below would turn it into ", Chennai, India"
        # and send a query for the middle of the city.
        return ""

    lower_address = address.lower()
    if "chennai" not in lower_address and "tamil nadu" not in lower_address:
        address = f"{address}, Chennai, India"

    return address


def _build_geocoder() -> GeocodingProvider:
    return build_geocoding_provider()


def _cache_key(cleaned_address: str) -> str:
    return cleaned_address.strip().lower()


def invalidate_cached_geocode(address: str, db: Session) -> bool:
    """Clears one address's cached result (see crud.delete_cached_geocode
    for why this needs to exist at all) - takes the RAW address text, same
    as every other public function here, and normalizes it the same way
    before computing the cache key so callers never have to know the
    cache's internal key format."""
    cleaned = clean_address(address)
    if not cleaned:
        return False
    return crud.delete_cached_geocode(db, _cache_key(cleaned))


def _result_from_cache_row(row) -> GeocodeResult:
    return GeocodeResult(
        lat=row.lat,
        lng=row.lng,
        formatted_address=row.formatted_address or "",
        status=STATUS_OK,
        provider=f"{row.provider or 'cache'} (cached)",
        confidence=row.confidence,
    )


def _lookup_or_geocode(
    geocoder: GeocodingProvider, cleaned_address: str, db: Optional[Session],
) -> Optional[GeocodeResult]:
    """Cache-first geocoding: a normalized address that's already been
    resolved (in this run or a previous one) never spends another billed
    provider request. Only STATUS_OK results are cached - a low-confidence
    or failed lookup is retried next time rather than remembered as final."""
    key = _cache_key(cleaned_address)
    if db is not None:
        cached = crud.get_cached_geocode(db, key)
        if cached is not None:
            return _result_from_cache_row(cached)

    result = geocoder.geocode(cleaned_address)

    if db is not None and result is not None and result.status == STATUS_OK:
        crud.save_geocode_cache(
            db,
            address_key=key,
            address=cleaned_address,
            formatted_address=result.formatted_address,
            lat=result.lat,
            lng=result.lng,
            provider=result.provider,
            confidence=result.confidence,
        )

    return result


def _verification_message(result: GeocodeResult, address: Optional[str] = None) -> str:
    """The admin-facing reason a flagged order needs a look, shared by
    _interpret_result and geocode_address_detailed so Failed Orders and
    the retry flow never disagree on wording. Leads with WHAT specifically
    looked wrong (result.mismatch_reason - "PIN code mismatch: you
    entered 600007, Google found 600021", say - see
    google_geocoder._score_component_match) when the provider set one,
    falling back to the bare confidence number for a provider/path with
    no such detail (Mapbox, Nominatim - only Google's Geocoding API
    currently sets mismatch_reason)."""
    confidence_note = (
        f" (confidence: {result.confidence:.2f})" if result.confidence is not None else ""
    )
    # What the system actually READ out of the address, appended so the
    # operator can see which component is the problem without going back
    # and re-parsing the raw string in their head. "House: 12A | Street:
    # Gandhi Road | Area: Velachery - missing: PIN code" turns a bare
    # confidence number into something immediately actionable.
    breakdown = ""
    if address:
        # Normalized first: "12A,LakshmiApts,GandhiRd" only yields
        # sensible components once the glued words are separated.
        parsed = address_parser.parse(address_parser.normalize(address))
        described = parsed.describe()
        if described:
            breakdown = f" — We read: {described}"

    if result.mismatch_reason:
        return f"Needs Manual Verification - {result.mismatch_reason}{confidence_note}{breakdown}"
    return f"Needs Manual Verification - low confidence match{confidence_note}{breakdown}"


def _interpret_result(
    result: Optional[GeocodeResult], address: Optional[str] = None
) -> Dict[str, object]:
    """Turn a provider-agnostic GeocodeResult into the fields geocode_orders/
    geocode_address attach to an order. A low-confidence match (any provider
    that sets a non-OK status - Nominatim's locality-only fallback or a weak
    variant match, Mapbox's low-relevance results) is treated the same way
    a hard failure is - lat/lng stay empty and it's surfaced through the
    existing Failed Orders / retry workflow, rather than silently accepting
    an imprecise guess as the order's real location.

    A flagged (not hard-failed) result still carries real coordinates the
    provider found SOMETHING at - most often the correct street/area, just
    without a confirmed house number ("found the street, not the house").
    Those are kept separately as suggested_lat/suggested_lng/confidence -
    never used as the order's actual location, but given to Adjust Location
    as a starting pin so the admin corrects a probably-close guess instead
    of placing a pin from scratch on a street they have to go find
    themselves (previously: the map opened centered on the depot, wherever
    in the city the real address happened to be)."""
    if result is None:
        return {"lat": None, "lng": None, "geocode_error": "Address could not be geocoded"}

    if result.status != STATUS_OK:
        return {
            "lat": None,
            "lng": None,
            "geocode_error": _verification_message(result, address),
            "suggested_lat": result.lat,
            "suggested_lng": result.lng,
            "confidence": result.confidence,
        }

    return {
        "lat": result.lat,
        "lng": result.lng,
        "geocoded_address": result.formatted_address,
        "confidence": result.confidence,
    }


def geocode_address(
    address: str,
    client: Optional[httpx.Client] = None,
    db: Optional[Session] = None,
) -> Optional[Dict[str, object]]:
    """`client` is accepted for backward compatibility with existing call
    sites (e.g. the /api/debug/geocode endpoint) but unused - each provider
    manages its own HTTP session."""
    cleaned = clean_address(address)
    if not cleaned:
        return None

    print(f"Geocoding: original='{address}' normalized='{cleaned}'")

    with _build_geocoder() as geocoder:
        result = _lookup_or_geocode(geocoder, cleaned, db)

    if result is None or result.status != STATUS_OK:
        if result is not None:
            print(f"Geocoding: '{cleaned}' flagged ({result.status}, confidence={result.confidence})")
        return None

    return {
        "address": cleaned,
        "lat": result.lat,
        "lng": result.lng,
        "display_name": result.formatted_address,
        # Google's is a derived heuristic (location_type/types/partial_match
        # - see google_geocoder._score_result), Mapbox/Nominatim's are the
        # provider's own relevance score - shown as-is either way.
        "confidence": result.confidence,
    }


def geocode_address_detailed(
    address: str,
    db: Optional[Session] = None,
) -> Optional[Dict[str, object]]:
    """Same lookup as geocode_address(), but never collapses a flagged
    (NEEDS_MANUAL_VERIFICATION) match down to a bare None - callers that
    need to offer the admin a starting pin for Adjust Location (the retry-
    a-Failed-Order flow) use this instead of geocode_address() so a
    genuinely-unresolvable address (None here too - clean_address emptied
    it out, or the provider found literally nothing) still reads
    differently from "found the street, just not confirmed house-number-
    precise" (suggested_lat/suggested_lng/confidence set, lat/lng still
    None - never treated as the order's real location, see
    _interpret_result's docstring for why)."""
    cleaned = clean_address(address)
    if not cleaned:
        return None

    print(f"Geocoding: original='{address}' normalized='{cleaned}'")

    with _build_geocoder() as geocoder:
        result = _lookup_or_geocode(geocoder, cleaned, db)

    if result is None:
        return None

    if result.status != STATUS_OK:
        print(f"Geocoding: '{cleaned}' flagged ({result.status}, confidence={result.confidence})")
        return {
            "address": cleaned,
            "lat": None,
            "lng": None,
            "geocode_error": _verification_message(result, address),
            "suggested_lat": result.lat,
            "suggested_lng": result.lng,
            "confidence": result.confidence,
        }

    return {
        "address": cleaned,
        "lat": result.lat,
        "lng": result.lng,
        "display_name": result.formatted_address,
        "confidence": result.confidence,
    }


def _fetch_concurrently(
    geocoder: GeocodingProvider,
    to_fetch: Dict[str, str],
    resolved: Dict[str, Optional[GeocodeResult]],
) -> Optional[str]:
    """Runs every still-uncached address at once (bounded by
    GEOCODE_CONCURRENCY) instead of one at a time - this is the actual fix
    for a real Excel upload timing out server-side. Geocoding used to be
    fully sequential: for a real dispatch sheet with dozens of addresses
    that haven't been seen before (a fresh route area, a new customer),
    every one of them was a real network round-trip, strictly one after
    another - easily tens of seconds even before Render's own cold-start
    delay (independently confirmed at ~30s), which is exactly what a
    request that then has nothing to show for over a minute looks like
    from the frontend: "backend not responding", with the orders that DID
    get created (the upload itself succeeded) sitting unrouted in
    Unassigned because /api/routes/generate never got a chance to run.

    Fills `resolved` in place (cache_key -> GeocodeResult|None) as futures
    complete. Returns the provider error message if the provider itself
    turned out to be broken (billing/key/access) - every future already
    in flight at that point is left to finish rather than force-cancelled
    (a `with ThreadPoolExecutor` always waits out what's already running),
    but nothing here treats a provider error as anything other than fatal
    for the whole batch, same as before.
    """
    provider_error_message: Optional[str] = None
    with ThreadPoolExecutor(max_workers=min(GEOCODE_CONCURRENCY, len(to_fetch))) as executor:
        futures = {executor.submit(geocoder.geocode, cleaned): cache_key for cache_key, cleaned in to_fetch.items()}
        for future in as_completed(futures):
            cache_key = futures[future]
            try:
                resolved[cache_key] = future.result()
            except GeocodingProviderError as exc:
                if provider_error_message is None:
                    provider_error_message = str(exc)
                    print(f"   🛑 Provider error - {provider_error_message}")
    return provider_error_message


def geocode_orders(
    orders: List[Dict[str, object]],
    db: Optional[Session] = None,
) -> Tuple[List[Dict[str, object]], Optional[str]]:
    """Returns (geocoded_orders, provider_error_message). provider_error_message
    is set when the geocoding PROVIDER itself is broken - billing not
    enabled, an invalid/missing key or token, the account/IP being blocked -
    as opposed to any particular address being unresolvable. In that case
    every remaining order is marked with the same clear message instead of
    hammering a known-broken provider hundreds more times.

    Three passes, deliberately kept separate: (1) sequential, DB cache
    lookups only - fast, and this is the only phase allowed to touch `db`
    from more than one place at a time; (2) concurrent, network calls only,
    for whatever's left after cache hits - see _fetch_concurrently; (3)
    sequential again, writing freshly-resolved addresses back to the DB
    cache. SQLAlchemy Sessions aren't safe to use from multiple threads,
    which is exactly why phase 2 never touches `db` at all."""
    print(f"\nStarting geocoding for {len(orders)} orders...\n")

    # Batch-level in-memory dedup, keyed by the raw address text (same
    # semantics the old `address_cache` dict had) - a within-batch
    # duplicate address is only ever looked up once, whether that ends up
    # being a cache hit or a fresh provider call.
    prepared: List[Dict[str, object]] = []  # one entry per order, in input order
    resolved: Dict[str, Optional[GeocodeResult]] = {}
    to_fetch: Dict[str, str] = {}  # raw cache_key -> cleaned address, still needing a provider call

    with _build_geocoder() as geocoder:
        for order in orders:
            merged = dict(order)
            address = str(order.get("address", ""))
            cache_key = address.strip().lower()
            needs_lookup = merged.get("lat") is None or merged.get("lng") is None
            prepared.append({"merged": merged, "cache_key": cache_key, "needs_lookup": needs_lookup})

            if not needs_lookup or cache_key in resolved or cache_key in to_fetch:
                continue

            cleaned = clean_address(address)
            print(f"original='{address}' normalized='{cleaned}'")
            if not cleaned:
                resolved[cache_key] = None
                continue

            cached_row = crud.get_cached_geocode(db, _cache_key(cleaned)) if db is not None else None
            if cached_row is not None:
                resolved[cache_key] = _result_from_cache_row(cached_row)
                print(f"   {address} (cached)")
            else:
                to_fetch[cache_key] = cleaned

        provider_error_message: Optional[str] = None
        if to_fetch:
            print(f"Fetching {len(to_fetch)} uncached address(es), up to {GEOCODE_CONCURRENCY} at a time...")
            provider_error_message = _fetch_concurrently(geocoder, to_fetch, resolved)

    # Persist freshly-resolved (non-cached) results - sequential, main
    # thread only, after every network call above has already finished.
    if db is not None:
        for cache_key, cleaned in to_fetch.items():
            result = resolved.get(cache_key)
            if result is not None and result.status == STATUS_OK:
                crud.save_geocode_cache(
                    db,
                    address_key=_cache_key(cleaned),
                    address=cleaned,
                    formatted_address=result.formatted_address,
                    lat=result.lat,
                    lng=result.lng,
                    provider=result.provider,
                    confidence=result.confidence,
                )

    geocoded_orders: List[Dict[str, object]] = []
    for entry in prepared:
        merged = entry["merged"]
        if entry["needs_lookup"]:
            cache_key = entry["cache_key"]
            if provider_error_message is not None and cache_key not in resolved:
                # Never got a chance to attempt this one (the provider
                # broke before its turn, or it was still queued when a
                # sibling future's error was seen) - same clear reason as
                # every other order in this batch, not left looking
                # untouched.
                merged["lat"] = None
                merged["lng"] = None
                merged["geocode_error"] = provider_error_message
            else:
                # Parsed from the CUSTOMER'S original text, not the
                # normalized query - the breakdown is there to help an
                # operator reconcile what we read against what they
                # actually typed.
                merged.update(
                    _interpret_result(resolved.get(cache_key), str(merged.get("address", "")))
                )
            print(f"   ✅ Success" if merged.get("lat") is not None else f"   ❌ {merged.get('geocode_error')}")
        geocoded_orders.append(merged)

    success = len([order for order in geocoded_orders if order.get("lat") is not None])
    failed = len(geocoded_orders) - success

    print("\n======================================")
    print(f"Total Orders : {len(geocoded_orders)}")
    print(f"Success      : {success}")
    print(f"Failed       : {failed}")
    if provider_error_message:
        print(f"Provider error: {provider_error_message}")
    print("======================================\n")

    return geocoded_orders, provider_error_message


def geocode_single_address(address: str, db: Optional[Session] = None) -> Optional[Dict[str, object]]:
    """Used by the Failed Orders "Retry" flow - geocode_address_detailed(),
    not geocode_address(), so a flagged (not hard-failed) retry still
    surfaces suggested_lat/suggested_lng for Adjust Location instead of
    collapsing to a bare failure. See geocode_address_detailed's docstring."""
    return geocode_address_detailed(address, db=db)
