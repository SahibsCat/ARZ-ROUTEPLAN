from app.geocoding.chennai_localities import correct_locality_spelling, correct_locality_word


def test_corrects_the_common_real_misspellings():
    assert correct_locality_word("Velachary") == "velachery"
    assert correct_locality_word("Velacherri") == "velachery"
    assert correct_locality_word("Adyarr") == "adyar"
    assert correct_locality_word("Thiruvanmiyoor") == "thiruvanmiyur"


def test_leaves_borderline_transliteration_variants_to_the_downstream_matcher():
    # "Noombal"/"Numbal" is an o-vs-u transliteration variant scoring
    # 0.83 - below this module's deliberately strict 0.86 bar. It is NOT
    # corrected here, and doesn't need to be: google_geocoder's
    # _transliteration_match (threshold 0.82) already recognizes the two
    # as the same place when scoring Google's response. Loosening this
    # bar to catch it would start swapping genuinely different localities
    # for each other, which is the one failure mode worth avoiding most.
    assert correct_locality_word("Noombal") is None


def test_leaves_correctly_spelled_localities_completely_alone():
    for name in ["Velachery", "Adyar", "Kilpauk", "Mylapore", "Numbal", "Perungudi"]:
        assert correct_locality_word(name) is None, name


def test_never_swaps_one_real_locality_for_a_different_real_one():
    # These are genuinely different places that merely resemble each
    # other. Correcting between them would silently move an order across
    # the city - much worse than not correcting at all.
    assert correct_locality_word("Madipakkam") is None
    assert correct_locality_word("Madambakkam") is None
    assert correct_locality_word("Adambakkam") is None
    assert correct_locality_word("Kottivakkam") is None
    assert correct_locality_word("Kovilambakkam") is None


def test_refuses_to_guess_on_a_genuine_tie_between_two_real_localities():
    # LOAD-BEARING. Real production bug: "Pallavakam" (customer's typo)
    # scores an EXACT 0.9 against BOTH "Palavakkam" (an ECR-area
    # locality, the intended target) and "Pallavaram" (a real, different
    # locality near the airport - nowhere close). candidates is iterated
    # from a set, so which one "won" was arbitrary across runs - live,
    # it picked "Pallavaram" and would have moved the order across the
    # city. An ambiguous tie must refuse to correct, not coin-flip.
    assert correct_locality_word("Pallavakam") is None


def test_never_corrects_across_a_different_first_letter():
    # A wrong first letter is a different place, not a typo.
    assert correct_locality_word("Adyar") is None
    assert correct_locality_word("Belachery") is None


def test_ignores_words_that_are_not_locality_candidates():
    assert correct_locality_word("Road") is None
    assert correct_locality_word("Nagar") is None
    assert correct_locality_word("12A") is None
    assert correct_locality_word("St") is None
    assert correct_locality_word("") is None


def test_correct_spelling_across_a_full_address_preserves_the_rest():
    corrected = correct_locality_spelling("12A, Gandhi Road, Velachary, Chennai 600042")

    assert "Velachery" in corrected
    assert corrected.startswith("12A, Gandhi Road,")
    assert corrected.endswith("Chennai 600042")


def test_correct_spelling_preserves_casing_style():
    assert "VELACHERY" in correct_locality_spelling("12, VELACHARY")
    assert "Velachery" in correct_locality_spelling("12, Velachary")
    assert "velachery" in correct_locality_spelling("12, velachary")


def test_a_correctly_spelled_address_passes_through_byte_identical():
    address = "45, Bhavani Street, Kilpauk, Chennai 600010"

    assert correct_locality_spelling(address) == address
