import os

from app.geocoding.base import GeocodingProvider
from app.geocoding.google_geocoder import GoogleGeocoder
from app.geocoding.mapbox_geocoder import MapboxGeocoder
from app.geocoding.nominatim_geocoder import NominatimGeocoder

# Switch providers with one env var - no code changes needed elsewhere.
# Add a new `elif` here (plus its own *_geocoder.py implementing
# GeocodingProvider) to support HERE/LocationIQ/OpenCage/etc later.
DEFAULT_PROVIDER = "google"


def build_geocoding_provider() -> GeocodingProvider:
    provider_name = os.environ.get("GEOCODING_PROVIDER", DEFAULT_PROVIDER).strip().lower()

    if provider_name == "google":
        return GoogleGeocoder(api_key=os.environ.get("GOOGLE_MAPS_API_KEY", "").strip())

    if provider_name == "nominatim":
        return NominatimGeocoder()

    if provider_name == "mapbox":
        return MapboxGeocoder(access_token=os.environ.get("MAPBOX_ACCESS_TOKEN", "").strip())

    raise ValueError(
        f"Unsupported GEOCODING_PROVIDER '{provider_name}' - expected 'google', 'nominatim', or 'mapbox'"
    )
