from app.geocoding.address_parser import ParsedAddress, normalize, parse


# --- normalization -------------------------------------------------------


def test_normalize_leaves_an_already_clean_address_alone():
    address = "12A, Gandhi Road, Velachery, Chennai 600042"
    assert normalize(address) == address


def test_normalize_collapses_whitespace_and_comma_noise():
    assert normalize("12A,,  Gandhi   Road ,Velachery,, Chennai") == (
        "12A, Gandhi Road, Velachery, Chennai"
    )


def test_normalize_splits_glued_camel_case_words():
    assert normalize("AnnaNagar") == "Anna Nagar"
    assert normalize("12, MainRoad, KodambakkamChennai") == "12, Main Road, Kodambakkam Chennai"


def test_normalize_splits_glued_lowercase_locality_suffixes():
    assert normalize("annanagar") == "anna nagar"
    assert normalize("mainroad") == "main road"


def test_normalize_does_not_split_place_names_that_merely_end_in_a_suffix_like_string():
    # "Kotturpuram"/"Nungambakkam" are single real place names - the
    # suffix list is deliberately narrow so these survive intact.
    assert normalize("Kotturpuram") == "Kotturpuram"
    assert normalize("Nungambakkam") == "Nungambakkam"
    assert normalize("Mylapore") == "Mylapore"


def test_normalize_splits_a_house_number_stuck_to_its_prefix_word():
    assert normalize("No12") == "No 12"
    assert normalize("No.12") == "No. 12"
    assert normalize("Flat5A") == "Flat 5A"
    assert normalize("DoorNo7") == "Door No 7"


def test_normalize_splits_glued_ordinals():
    assert normalize("2ndstreet") == "2nd street"
    assert normalize("3rdcross") == "3rd cross"


def test_normalize_never_breaks_apart_a_real_house_number():
    # The single most damaging thing this layer could do: "12A" split
    # into "12 A" stops matching Google's street_number component.
    assert normalize("12A") == "12A"
    assert normalize("12/2A") == "12/2A"
    assert normalize("D1/5") == "D1/5"
    assert normalize("A1C") == "A1C"
    assert normalize("600042") == "600042"


def test_normalize_expands_only_unambiguous_abbreviations():
    assert normalize("Gandhi Rd") == "Gandhi Road"
    assert normalize("Anna Ngr") == "Anna Nagar"
    # "St" is left alone on purpose - it's "Saint" as often as "Street"
    # in real road names, and guessing wrong makes the road unfindable.
    assert normalize("St Marys Rd") == "St Marys Road"


def test_normalize_splits_a_pincode_glued_to_the_locality_name():
    assert normalize("79A, 4th ST Ambika Nagar, Madambakkam600126") == (
        "79A, 4th ST Ambika Nagar, Madambakkam 600126"
    )


def test_normalize_expands_the_short_chennai_pincode_shorthand():
    # "Chennai 41" / "chennai-78" is Tamil Nadu's near-universal informal
    # shorthand for PIN 600041/600078 - locally understood, not
    # understood by the geocoder unless spelled out.
    assert "600078" in normalize("W.K.K.Nagar, Chennai - 78")
    assert "600041" in normalize("ECR Pallavakam Chennai 41 Behind Star Bucks")


def test_normalize_does_not_add_a_short_pincode_when_a_real_one_is_already_present():
    result = normalize("12, Gandhi Road, Chennai 600042")
    assert result.count("600042") == 1
    assert "600000" not in result


def test_normalize_does_not_mistake_a_street_ordinal_for_the_short_pincode_shorthand():
    # "Chennai 2nd Street" must not become "Chennai, 600002".
    result = normalize("12, Chennai 2nd Street, Adyar")
    assert "600002" not in result
    assert "600" not in result


def test_normalize_handles_empty_input():
    assert normalize("") == ""
    assert normalize("   ") == ""


# --- component extraction ------------------------------------------------


def test_parse_extracts_every_component_of_a_complete_address():
    parsed = parse("12A, Lakshmi Apartments, Gandhi Road, near ABC School, Velachery, Chennai 600042")

    assert parsed.house_number == "12A"
    assert parsed.building == "Lakshmi Apartments"
    assert parsed.street == "Gandhi Road"
    assert parsed.landmark == "near ABC School"
    assert parsed.area == "Velachery"
    assert parsed.city == "Chennai"
    assert parsed.pincode == "600042"
    assert parsed.missing() == []


def test_parse_handles_an_address_with_no_building_and_no_landmark():
    # Both are optional - their absence must not empty out the fields
    # that DO matter for delivery.
    parsed = parse("45, Bhavani Street, Kilpauk, Chennai 600010")

    assert parsed.house_number == "45"
    assert parsed.street == "Bhavani Street"
    assert parsed.area == "Kilpauk"
    assert parsed.city == "Chennai"
    assert parsed.pincode == "600010"
    assert parsed.building is None
    assert parsed.landmark is None
    assert parsed.missing() == []


def test_parse_reports_what_is_genuinely_missing():
    parsed = parse("Gandhi Road, Velachery")

    assert parsed.street == "Gandhi Road"
    assert parsed.area == "Velachery"
    assert "house/door number" in parsed.missing()
    assert "PIN code" in parsed.missing()
    assert "city" in parsed.missing()


def test_parse_reads_house_numbers_in_every_format_customers_use():
    for text, expected in [
        ("12, X Street", "12"),
        ("12A, X Street", "12A"),
        ("12-B, X Street", "12-B"),
        ("12/2, X Street", "12/2"),
        ("No 12, X Street", "12"),
        ("No. 12, X Street", "12"),
        ("D.No 12, X Street", "12"),
        ("Door No. 12, X Street", "12"),
        ("Plot No 12, X Street", "12"),
        ("D1/5, X Street", "D1/5"),
    ]:
        assert parse(text).house_number == expected, text


def test_parse_separates_a_flat_number_from_the_door_number():
    parsed = parse("Flat No 4B, 12 Gandhi Road, Velachery, Chennai 600042")

    assert parsed.flat == "4B"
    assert parsed.house_number == "12"
    assert parsed.street == "Gandhi Road"


def test_parse_does_not_mistake_a_pin_code_for_a_house_number():
    parsed = parse("600042, Velachery, Chennai")

    assert parsed.pincode == "600042"
    assert parsed.house_number is None


def test_parse_treats_a_landmark_street_as_a_landmark_not_the_customers_street():
    # "Near Bhavani Street" names a landmark road, not this customer's
    # own street - pinning it as the street would place the order on the
    # wrong road entirely.
    parsed = parse("22, Opposite Bhavani Street, Anna Nagar, Chennai")

    assert parsed.landmark == "Opposite Bhavani Street"
    assert parsed.street != "Opposite Bhavani Street"


def test_parse_splits_a_city_named_in_the_same_segment_as_the_area():
    parsed = parse("14, Kamaraj Street, Velachery Chennai 600042")

    assert parsed.area == "Velachery"
    assert parsed.city == "Chennai"


def test_parse_keeps_the_house_number_when_it_shares_a_segment_with_the_street():
    parsed = parse("14 Kamaraj Street, Velachery, Chennai")

    assert parsed.house_number == "14"
    assert parsed.street == "Kamaraj Street"


def test_parse_returns_an_empty_result_for_empty_input():
    parsed = parse("")

    assert parsed == ParsedAddress()
    assert parsed.describe() == "Could not read any address components"


def test_describe_lists_what_was_read_and_what_is_missing():
    described = parse("12A, Gandhi Road, Velachery").describe()

    assert "House: 12A" in described
    assert "Street: Gandhi Road" in described
    assert "Area: Velachery" in described
    assert "missing" in described
    assert "PIN code" in described


def test_parse_never_invents_a_component_it_cannot_see():
    parsed = parse("Chennai")

    assert parsed.house_number is None
    assert parsed.street is None
    assert parsed.building is None
    assert parsed.city == "Chennai"
