import re
from typing import Dict, List, Optional, Tuple

import httpx
from sqlalchemy.orm import Session

from app import crud
from app.geocoding.base import (
    STATUS_OK,
    GeocodeResult,
    GeocodingProvider,
    GeocodingProviderError,
)
from app.geocoding.provider_factory import build_geocoding_provider


def clean_address(address: str) -> str:
    """Normalize an address before sending it to any geocoding provider:
    collapse line breaks/duplicate whitespace, fix comma spacing, trim, and
    make sure the city/state is present so the query has enough context."""
    if not address:
        return ""

    address = address.replace("\r", " ").replace("\n", " ")
    address = address.replace(",", ", ")
    address = " ".join(address.split())
    address = re.sub(r"\s+,\s+", ", ", address)
    address = re.sub(r",\s*,+", ", ", address)  # duplicate commas
    address = address.strip().strip(",").strip()

    lower_address = address.lower()
    if "chennai" not in lower_address and "tamil nadu" not in lower_address:
        address = f"{address}, Chennai, India"

    return address


def _build_geocoder() -> GeocodingProvider:
    return build_geocoding_provider()


def _cache_key(cleaned_address: str) -> str:
    return cleaned_address.strip().lower()


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


def _interpret_result(result: Optional[GeocodeResult]) -> Dict[str, object]:
    """Turn a provider-agnostic GeocodeResult into the fields geocode_orders/
    geocode_address attach to an order. A low-confidence match (any provider
    that sets a non-OK status - Nominatim's locality-only fallback or a weak
    variant match, Mapbox's low-relevance results) is treated the same way
    a hard failure is - lat/lng stay empty and it's surfaced through the
    existing Failed Orders / retry workflow, rather than silently accepting
    an imprecise guess."""
    if result is None:
        return {"lat": None, "lng": None, "geocode_error": "Address could not be geocoded"}

    if result.status != STATUS_OK:
        confidence_note = (
            f" (confidence: {result.confidence:.2f})" if result.confidence is not None else ""
        )
        return {
            "lat": None,
            "lng": None,
            "geocode_error": f"Needs Manual Verification - low confidence match{confidence_note}",
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
        # None for providers that don't expose a relevance concept (Google) -
        # the frontend shows that honestly rather than inventing a number.
        "confidence": result.confidence,
    }


def geocode_orders(
    orders: List[Dict[str, object]],
    db: Optional[Session] = None,
) -> Tuple[List[Dict[str, object]], Optional[str]]:
    """Returns (geocoded_orders, provider_error_message). provider_error_message
    is set when the geocoding PROVIDER itself is broken - billing not
    enabled, an invalid/missing key or token, the account/IP being blocked -
    as opposed to any particular address being unresolvable. In that case
    every remaining order is marked with the same clear message instead of
    hammering a known-broken provider hundreds more times."""
    geocoded_orders: List[Dict[str, object]] = []
    address_cache: Dict[str, object] = {}
    provider_error_message: Optional[str] = None

    print(f"\nStarting geocoding for {len(orders)} orders...\n")

    with _build_geocoder() as geocoder:
        for index, order in enumerate(orders, start=1):

            merged = dict(order)
            address = str(order.get("address", ""))
            cache_key = address.strip().lower()

            if merged.get("lat") is None or merged.get("lng") is None:

                if cache_key in address_cache:
                    result = address_cache[cache_key]
                    print(f"[{index}/{len(orders)}] {address} (cached)")
                else:
                    cleaned = clean_address(address)
                    print(f"[{index}/{len(orders)}] original='{address}' normalized='{cleaned}'")
                    try:
                        result = _lookup_or_geocode(geocoder, cleaned, db) if cleaned else None
                    except GeocodingProviderError as exc:
                        provider_error_message = str(exc)
                        print(f"   🛑 STOPPING - {provider_error_message}")
                        merged["lat"] = None
                        merged["lng"] = None
                        merged["geocode_error"] = provider_error_message
                        geocoded_orders.append(merged)
                        break
                    address_cache[cache_key] = result

                merged.update(_interpret_result(result))

                if merged.get("lat") is not None:
                    print(f"   ✅ Success ({merged.get('geocode_error') or 'OK'})")
                else:
                    print(f"   ❌ {merged.get('geocode_error')}")

            geocoded_orders.append(merged)

    if provider_error_message is not None:
        # Every order we never got to attempt gets the same clear reason,
        # rather than being left out or looking untouched.
        for order in orders[len(geocoded_orders):]:
            merged = dict(order)
            merged["lat"] = None
            merged["lng"] = None
            merged["geocode_error"] = provider_error_message
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
    return geocode_address(address, db=db)
