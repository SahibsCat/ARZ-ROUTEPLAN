import pytest

from app.geocoding.google_geocoder import GoogleGeocoder
from app.geocoding.mapbox_geocoder import MapboxGeocoder
from app.geocoding.nominatim_geocoder import NominatimGeocoder
from app.geocoding.provider_factory import build_geocoding_provider


def test_defaults_to_google_when_unset(monkeypatch):
    monkeypatch.delenv("GEOCODING_PROVIDER", raising=False)
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "key-123")

    provider = build_geocoding_provider()

    assert isinstance(provider, GoogleGeocoder)


def test_selects_nominatim_when_configured(monkeypatch):
    monkeypatch.setenv("GEOCODING_PROVIDER", "nominatim")

    provider = build_geocoding_provider()

    assert isinstance(provider, NominatimGeocoder)


def test_selects_mapbox_when_configured(monkeypatch):
    monkeypatch.setenv("GEOCODING_PROVIDER", "mapbox")
    monkeypatch.setenv("MAPBOX_ACCESS_TOKEN", "token-123")

    provider = build_geocoding_provider()

    assert isinstance(provider, MapboxGeocoder)


def test_selection_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("GEOCODING_PROVIDER", "MapBox")
    monkeypatch.setenv("MAPBOX_ACCESS_TOKEN", "token-123")

    provider = build_geocoding_provider()

    assert isinstance(provider, MapboxGeocoder)


def test_unsupported_provider_raises(monkeypatch):
    monkeypatch.setenv("GEOCODING_PROVIDER", "here")

    with pytest.raises(ValueError):
        build_geocoding_provider()
