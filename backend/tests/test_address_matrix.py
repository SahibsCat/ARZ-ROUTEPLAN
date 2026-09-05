"""The address-shape matrix: one case per way a real customer mangles
an address, asserted end-to-end through normalization + parsing.

This is deliberately a separate file from test_address_parser.py. That
one tests the functions; this one is the checklist of INPUT SHAPES the
system has to survive, so a regression shows up as "we broke glued
lowercase localities", not as an anonymous assertion failure.

No network here - normalization and parsing are entirely local, which
is the point: everything below is decided before a single billed
request is made.
"""

import pytest

from app.geocode_service import clean_address
from app.geocoding.address_parser import normalize, parse


def resolve(address: str):
    """What the pipeline makes of an address: the query it would send,
    and the components it read."""
    return clean_address(address), parse(normalize(address))


# --- the house number must survive every format, always ------------------


@pytest.mark.parametrize(
    "address,expected",
    [
        ("12, Gandhi Road, Velachery, Chennai 600042", "12"),
        ("12A, Gandhi Road, Velachery, Chennai 600042", "12A"),
        ("12 A, Gandhi Road, Velachery, Chennai 600042", "12"),
        ("12-B, Gandhi Road, Velachery, Chennai 600042", "12-B"),
        ("12B, Gandhi Road, Velachery, Chennai 600042", "12B"),
        ("12/2, Gandhi Road, Velachery, Chennai 600042", "12/2"),
        ("12/2A, Gandhi Road, Velachery, Chennai 600042", "12/2A"),
        ("12-2, Gandhi Road, Velachery, Chennai 600042", "12-2"),
        ("No 12, Gandhi Road, Velachery, Chennai 600042", "12"),
        ("No. 12, Gandhi Road, Velachery, Chennai 600042", "12"),
        ("No12, Gandhi Road, Velachery, Chennai 600042", "12"),
        ("D.No 12, Gandhi Road, Velachery, Chennai 600042", "12"),
        ("D.No.12, Gandhi Road, Velachery, Chennai 600042", "12"),
        ("Door No 12, Gandhi Road, Velachery, Chennai 600042", "12"),
        ("Door No. 12, Gandhi Road, Velachery, Chennai 600042", "12"),
        ("Plot No 12, Gandhi Road, Velachery, Chennai 600042", "12"),
        ("4/12, Gandhi Road, Velachery, Chennai 600042", "4/12"),
        ("D1/5, Gandhi Road, Velachery, Chennai 600042", "D1/5"),
    ],
)
def test_house_number_is_read_from_every_format(address, expected):
    _query, parsed = resolve(address)
    assert parsed.house_number == expected


@pytest.mark.parametrize(
    "number",
    ["12A", "12/2A", "D1/5", "A1C", "12-B", "600042"],
)
def test_normalization_never_rewrites_a_house_number(number):
    # The one unforgivable bug in this layer: "12A" -> "12 A" stops
    # matching Google's street_number and turns a findable house into a
    # verification case.
    assert number in normalize(f"{number}, Gandhi Road, Velachery, Chennai")


# --- formatting damage ---------------------------------------------------


def test_missing_spaces_are_repaired():
    query, parsed = resolve("12A,LakshmiApts,GandhiRd,Velachery,Chennai 600042")

    assert "Lakshmi Apartments" in query
    assert "Gandhi Road" in query
    assert parsed.house_number == "12A"
    assert parsed.street == "Gandhi Road"


def test_glued_lowercase_locality_is_repaired():
    query, _parsed = resolve("No12 annanagar chennai")

    assert "anna nagar" in query.lower()
    assert "No 12" in query


def test_extra_spaces_and_duplicate_commas_are_repaired():
    query, parsed = resolve("12A,,   Gandhi   Road ,, Velachery,  Chennai")

    assert ",," not in query
    assert "  " not in query
    assert parsed.street == "Gandhi Road"


def test_wrong_capitalization_is_tolerated():
    _query, parsed = resolve("12a, gandhi road, velachery, chennai 600042")

    assert parsed.house_number == "12A"
    assert parsed.street == "gandhi road"
    assert parsed.pincode == "600042"


def test_glued_ordinal_street_is_repaired():
    query, _parsed = resolve("12, 2ndstreet, Velachery, Chennai")

    assert "2nd street" in query.lower()


# --- spelling ------------------------------------------------------------


@pytest.mark.parametrize(
    "misspelled",
    ["Velachary", "Velacherri", "Adyarr", "Thiruvanmiyoor"],
)
def test_clean_address_leaves_locality_spelling_alone(misspelled):
    # clean_address() deliberately does NOT correct spelling - see its
    # own comment for why (a wrong correction must never become the text
    # every downstream check validates against). The correction is
    # applied by GoogleGeocoder.geocode() itself, as one additional QUERY
    # attempt that still validates against the customer's true original
    # text - see test_google_geocoder.py's own coverage of that split,
    # and test_chennai_localities.py for the correction function itself.
    query, _parsed = resolve(f"12, Gandhi Road, {misspelled}, Chennai")

    assert misspelled in query


def test_a_correctly_spelled_locality_is_never_altered():
    query, _parsed = resolve("12, Gandhi Road, Madipakkam, Chennai 600091")

    assert "Madipakkam" in query


# --- optional components -------------------------------------------------


def test_building_name_is_optional():
    _query, parsed = resolve("45, Bhavani Street, Kilpauk, Chennai 600010")

    assert parsed.building is None
    assert parsed.house_number == "45"
    assert parsed.street == "Bhavani Street"
    # Nothing about a missing building name makes this address unusable.
    assert "building" not in " ".join(parsed.missing())


def test_landmark_is_optional_and_never_becomes_the_street():
    _query, parsed = resolve("22, Gandhi Road, Near ABC School, Adyar, Chennai")

    assert parsed.landmark == "Near ABC School"
    assert parsed.street == "Gandhi Road"


def test_landmark_only_address_still_reads_its_other_components():
    _query, parsed = resolve("12, Opposite Bhavani Street, Anna Nagar, Chennai 600040")

    assert parsed.house_number == "12"
    assert parsed.landmark == "Opposite Bhavani Street"
    assert parsed.pincode == "600040"


def test_pin_code_is_optional():
    _query, parsed = resolve("12, Gandhi Road, Velachery, Chennai")

    assert parsed.pincode is None
    assert "PIN code" in parsed.missing()
    assert parsed.house_number == "12"


def test_flat_number_does_not_displace_the_door_number():
    _query, parsed = resolve("Flat No 4B, 12, Gandhi Road, Velachery, Chennai 600042")

    assert parsed.flat == "4B"
    assert parsed.house_number == "12"


# --- unusual ordering and sparse input -----------------------------------


def test_comma_less_address_is_still_split_into_components():
    _query, parsed = resolve("No 4 Kk Road 3rd Cross Street Ambattur Chennai 600053")

    assert parsed.house_number == "4"
    assert parsed.street == "Kk Road"
    assert parsed.city == "Chennai"
    assert parsed.pincode == "600053"
    # Ambattur is a locality, not the city - it must not be swallowed.
    assert parsed.city == "Chennai"


def test_main_road_is_kept_whole_as_the_street():
    _query, parsed = resolve("D.No12 Main Road Adyar Chennai 600020")

    assert parsed.house_number == "12"
    assert parsed.street.lower() == "main road"


def test_house_street_locality_only_is_a_complete_enough_address():
    _query, parsed = resolve("12A, Gandhi Road, Velachery")

    assert parsed.house_number == "12A"
    assert parsed.street == "Gandhi Road"
    assert parsed.area == "Velachery"


def test_city_is_added_to_the_query_when_the_customer_omits_it():
    query, _parsed = resolve("12A, Gandhi Road, Velachery")

    assert "Chennai" in query


def test_empty_and_whitespace_addresses_do_not_crash():
    assert clean_address("") == ""
    assert clean_address("    ") == ""
    assert parse("").describe() == "Could not read any address components"


# --- what the operator is shown ------------------------------------------


def test_the_breakdown_names_every_component_it_read():
    _query, parsed = resolve("12A, Lakshmi Apartments, Gandhi Road, Velachery, Chennai 600042")
    described = parsed.describe()

    for expected in ["House: 12A", "Street: Gandhi Road", "Area: Velachery", "PIN: 600042"]:
        assert expected in described


def test_the_breakdown_names_what_is_missing_so_it_can_be_fixed():
    described = parse(normalize("Gandhi Road, Velachery")).describe()

    assert "missing" in described
    assert "house/door number" in described
    assert "PIN code" in described
