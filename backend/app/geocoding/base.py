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
    # Plain-English reason(s) a match was flagged - "PIN mismatch: you
    # entered 600007, Google found 600021", "House number mismatch: you
    # entered 231B/1, the street only has 231C" - joined into one string
    # when more than one check fired. None for a clean STATUS_OK match, or
    # for a provider/path with no component-level validation at all (see
    # google_geocoder._score_component_match, the only thing that
    # currently sets this). Surfaced to the admin (geocode_service.
    # _interpret_result folds it into geocode_error) so Failed Orders
    # shows WHAT specifically looked wrong, not just a bare confidence
    # number - the admin can then tell in one glance whether it's their
    # own typo or a geocoding gap.
    mismatch_reason: Optional[str] = None


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
