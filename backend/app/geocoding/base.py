from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

# Shared status markers providers can set on GeocodeResult.status so the
# service layer can react consistently regardless of which provider is
# active. Providers aren't required to use these (Google just uses "OK"),
# but any provider that has a confidence/relevance concept should use
# STATUS_NEEDS_MANUAL_VERIFICATION for low-confidence matches.
STATUS_OK = "OK"
STATUS_NEEDS_MANUAL_VERIFICATION = "NEEDS_MANUAL_VERIFICATION"


@dataclass
class GeocodeResult:
    lat: float
    lng: float
    formatted_address: str
    status: str
    provider: str
    # 0-1 relevance/confidence score. Mapbox and Nominatim expose/compute one
    # directly; Google's Geocoding API has no such field, so GoogleGeocoder
    # derives one from geometry.location_type + result types + partial_match
    # instead (see google_geocoder._score_result) - never None in practice.
    confidence: Optional[float] = None


class GeocodingProvider(ABC):
    """Interface every geocoding backend implements, so swapping providers
    (Google, Mapbox, HERE, ...) never requires touching call sites - only a
    new class implementing this one method."""

    @abstractmethod
    def geocode(self, address: str) -> Optional[GeocodeResult]:
        ...


class GeocodingProviderError(Exception):
    """Raised when a request fails for a reason that has nothing to do with
    the specific address - billing not enabled, invalid/missing API key or
    token, the account/IP being blocked, etc. This is deliberately distinct
    from returning None (which means "this particular address wasn't
    found"): a provider-level failure means EVERY remaining address would
    fail identically, so callers should stop immediately and surface one
    clear, actionable message instead of hundreds of misleading per-order
    "could not be geocoded" errors."""

    def __init__(self, message: str, provider: str):
        super().__init__(message)
        self.message = message
        self.provider = provider
