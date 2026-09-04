import re
import time
from typing import Dict, Iterable, List, Optional

import httpx

from app.geocoding.base import (
    STATUS_NEEDS_MANUAL_VERIFICATION,
    STATUS_OK,
    GeocodeResult,
    GeocodingProvider,
    GeocodingProviderError,
)
# Reused rather than reimplemented - nominatim_geocoder already does exactly
# this class of comparison (PIN extraction, token-level fuzzy matching) to
# validate ITS OWN candidates against the customer's original address text;
# no reason for Google's geocoder to score component matches differently.
from app.geocoding.nominatim_geocoder import _extract_pincode, _fuzzy_token_match, _significant_tokens

GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
# Places API "Find Place From Text" - a fuzzy, named-establishment search,
# not a structured-address parser. Used only as a fallback (see
# GoogleGeocoder._find_place) when the Geocoding API above can't place an
# address precisely - which is common for "Sidharth Upscale Apartments,
# Porur" style addresses that name a specific building/complex rather than
# a street + number, exactly the class of address the Geocoding API isn't
# built to recognize by name.
PLACES_FIND_PLACE_URL = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
# Find Place From Text's supported `fields` never includes address_components
# (only formatted_address as free text) - so a place_id from it is resolved
# through Place Details, which does return them, purely so _find_place can
# run the SAME _score_component_match validation _geocode_once already gets.
# Without this second call, a Places match could only ever be trusted by
# name/formatted-address text - exactly how a same-named establishment in
# the wrong neighbourhood ("Sarathy Nagar" matched for a "Bharathi Nagar"
# address, both in Velachery) used to slip through at a flat, unvalidated
# 0.65 confidence with no PIN/locality/house-number cross-check at all.
PLACE_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 1.0
# Below this derived confidence (see _score_result), a match is too loose to
# trust as a precise delivery point - same threshold/philosophy Mapbox and
# Nominatim already apply, so all three providers behave consistently.
DEFAULT_MIN_CONFIDENCE = 0.5
# A Places API match STARTS at this confidence when used at all - not as
# certain as a real ROOFTOP geocode, but Places found and named an actual
# establishment/building, meaningfully more specific than the
# locality/postal-code-level fallback the Geocoding API landed on instead
# (the only time this fallback is even tried - see geocode()). Same as the
# Geocoding API path, this is only the STARTING point - _score_component_match
# against the Place Details result (see _place_details) can still cap it
# below DEFAULT_MIN_CONFIDENCE when Places matched the wrong PIN/locality/
# house number for what the customer actually typed.
PLACES_FALLBACK_CONFIDENCE = 0.65
# Chennai-centered, ~50km radius - every address this app handles is in
# Chennai (see geocode_service.clean_address, which appends ", Chennai,
# India" whenever neither is already present) - biasing Places' fuzzy text
# search this way keeps it from matching a same-named place elsewhere in
# India entirely.
_CHENNAI_LOCATION_BIAS = "circle:50000@13.0827,80.2707"

# Phrases that describe a DIFFERENT nearby place, not the delivery address
# itself ("near X", "opposite Y") - stripping them and retrying is a free
# second attempt when the first one scores low, since these confuse a
# structured-address parser without adding any real address information.
# Same idea as nominatim_geocoder.build_address_variants's landmark strip.
_LANDMARK_PATTERN = re.compile(
    r"\b(near|opp(?:osite)?|behind|in front of|beside|next to)\b[^,]*",
    re.IGNORECASE,
)

# https://developers.google.com/maps/documentation/geocoding/requests-geocoding#StatusCodes
# OVER_QUERY_LIMIT/UNKNOWN_ERROR are transient - worth retrying. ZERO_RESULTS
# means this particular address genuinely doesn't resolve - a per-address
# failure. REQUEST_DENIED is an account/key/billing problem (see geocode()),
# not a per-address one - handled separately as a GeocodingProviderError.
_TERMINAL_FAILURE_STATUSES = {"ZERO_RESULTS", "INVALID_REQUEST"}

# https://developers.google.com/maps/documentation/geocoding/requests-geocoding#Results
# `types` on the top-level result - these mean Google actually matched a
# specific building/unit, not just an area.
_PRECISE_TYPES = {"street_address", "premise", "subpremise", "point_of_interest", "establishment"}
# Every one of these describes an AREA, never a single delivery point. A
# result made up only of types from this set - regardless of location_type -
# is Google saying "the best I could do was a city/neighbourhood/postal
# code", not a specific address, and must never be trusted as a rooftop pin.
_AREA_ONLY_TYPES = {
    "locality", "sublocality", "sublocality_level_1", "sublocality_level_2",
    "sublocality_level_3", "sublocality_level_4", "sublocality_level_5",
    "neighborhood", "administrative_area_level_1", "administrative_area_level_2",
    "administrative_area_level_3", "administrative_area_level_4", "administrative_area_level_5",
    "postal_code", "postal_town", "country", "political",
}


def _score_result(location_type: Optional[str], types: Iterable[str], partial_match: bool) -> float:
    """Derives a 0-1 confidence score from the three signals Google's
    Geocoding API actually gives for how precise a match is - it has no
    single relevance number the way Mapbox does, so this combines them the
    same way a human reviewing the raw JSON would:

    This is the actual fix for "the address text is right but the map pin
    isn't": every one of these signals used to be ignored entirely - any
    result Google returned, however rough, was accepted as a fully-precise
    match. In practice, an address Google can't pin exactly (a flat/door
    number or building name it doesn't have) doesn't error out - it falls
    back to the nearest area it CAN match (the street, the neighbourhood,
    the postal code, sometimes just the city) and still returns a clean,
    reasonable-looking `formatted_address` for THAT area. The text looks
    fine because it is fine, for the area Google actually matched - the
    marker is just planted at that area's center, not the specific address
    that was asked for.

      - location_type: ROOFTOP (an exact building match) down to APPROXIMATE
        (a rough guess) - see the module-level comment above _PRECISE_TYPES.
      - types: whether the match resolved to a specific place/building or
        only to an administrative area/postal code/locality.
      - partial_match: Google's own admission that it couldn't match every
        component of the query (most often a flat/door number or building
        name) - the point returned may be for the street/area, not the
        specific place asked for.
    """
    types_set = set(types or [])

    if location_type == "ROOFTOP":
        score = 0.95
    elif location_type == "RANGE_INTERPOLATED":
        # Interpolated between two known points along a street - not exact,
        # but still anchored to the right road and roughly the right spot.
        # Normal (and reliable) for a full street address when rooftop-level
        # data isn't available, which is common in India.
        score = 0.8
    elif location_type == "GEOMETRIC_CENTER":
        # Center of a line/polygon - fine if that polygon IS the specific
        # building/POI asked for, not fine if it's the center of an entire
        # street/area with no real number match. Only `types` tells them
        # apart.
        score = 0.65 if types_set & _PRECISE_TYPES else 0.3
    elif location_type == "APPROXIMATE":
        # Google's own "rough guess" marker - normally means it fell all the
        # way back to a locality/postal-code/administrative-area centroid.
        # This is the case that most often looks like "text is right, pin
        # is nowhere near it."
        score = 0.55 if types_set & _PRECISE_TYPES else 0.2
    else:
        score = 0.3

    if types_set and not (types_set & _PRECISE_TYPES) and not (types_set - _AREA_ONLY_TYPES):
        # Belt-and-suspenders: whatever location_type said, if every type on
        # this result is an area-level type, it is not a delivery point.
        score = min(score, 0.25)

    if partial_match:
        score = max(0.05, score - 0.15)

    return round(score, 2)


# --- Address-component validation (_score_component_match) - a SECOND,
# independent confidence signal alongside _score_result. That one reads
# Google's OWN claimed precision (location_type/types/partial_match) - it
# has no way to notice Google confidently geocoding the WRONG place (a
# ROOFTOP-precise match for a same-named street/building in a different
# locality, say). This one compares Google's returned address_components
# against what the customer actually typed. Deliberately a DOWNGRADE-ONLY
# signal - it never boosts confidence past what _score_result already
# found; a clean component match is just the absence of a red flag, not
# extra proof of precision. -----------------------------------------------

# An exact 6-digit PIN mismatch is the closest thing to unambiguous proof
# Google matched a different area than the customer stated - still not an
# automatic hard reject (data sources can genuinely disagree at a PIN
# boundary), but real cause to require verification.
PIN_MISMATCH_CONFIDENCE_CAP = 0.3
# The ONE deliberate exception to "a cap only ever downgrades, never
# overrides what the customer typed" (explicitly confirmed as an
# accepted trade-off): when a PIN mismatch is the ONLY thing wrong -
# street name, house number, and locality all confirm cleanly on their
# own - and Google's own match precision is at least this good, it's
# trusted as a likely customer-side PIN typo instead of capping. Set at
# RANGE_INTERPOLATED-or-better (see _score_result) WITHOUT the
# partial_match penalty already applied - excludes every GEOMETRIC_CENTER
# guess (even a "precise-type" one, capped at 0.65) and excludes a
# partial-matched RANGE_INTERPOLATED (drops to 0.65) - only a genuinely
# confident, complete structural match qualifies, real case: "New
# Thandavarayan Street" resolves to the exact same ROOFTOP point (0.95)
# whether or not the customer's stated PIN is even included in the query
# at all - strong evidence the STREET is right and the PIN was mistyped,
# not that the match itself is uncertain.
PIN_MISMATCH_TRUST_OVERRIDE_MIN_PRECISION = 0.8
# Google names a specific locality/sublocality and NONE of its words show
# up anywhere in the customer's own address text - the "ABC Apartments,
# XYZ Street, Velachery" vs "ABC Apartments, XYZ Street, Thiruvanmiyur"
# scenario: everything else about the match can look fine, only the area
# itself is wrong.
LOCALITY_MISMATCH_CONFIDENCE_CAP = 0.4
# Every sublocality depth Google actually returns (level_1 through
# level_5 - see the module-level _AREA_ONLY_TYPES set above, which
# already covers all five for the SEPARATE precision check) - a real
# Chennai address routinely nests 3-4 deep ("Yeswanth Nagar" inside
# "Madambakkam" inside "Tambaram", say), and the customer's own named
# area can legitimately be the deeper one, not the top-level locality.
# Only checking level_1/level_2 silently skipped every match that could
# only be confirmed at level_3 or beyond.
_LOCALITY_COMPONENT_TYPES = {
    "locality", "sublocality",
    "sublocality_level_1", "sublocality_level_2", "sublocality_level_3",
    "sublocality_level_4", "sublocality_level_5",
}

# The customer gave a house/door number and Google's result has NO
# street_number component at all - "the street was found, the house was
# not" (a building name is never required for this check to apply; see
# _extract_house_number). Deliberately just under DEFAULT_MIN_CONFIDENCE
# (0.5) - a customer-stated house number that Google couldn't confirm must
# never auto-accept on its own, no matter how confident location_type
# looked (RANGE_INTERPOLATED, in particular, can still be a plausible-
# looking street-level guess with no real number match behind it).
STREET_NUMBER_UNCONFIRMED_CONFIDENCE_CAP = 0.45
# Google DID return a street_number, and it's a different number than the
# customer gave - stronger, more specific evidence of a wrong match than
# the "unconfirmed" case above (this isn't "Google doesn't know", it's
# "Google matched a different house on the same street").
STREET_NUMBER_MISMATCH_CONFIDENCE_CAP = 0.3

# The customer named a specific street and NONE of Google's address
# components mention it anywhere - real case this catches: a GEOMETRIC_
# CENTER match against a named ESTABLISHMENT/POI (types include
# "establishment"/"point_of_interest", already in _PRECISE_TYPES, so
# _score_result alone scores it 0.5-0.65) where Google's fuzzy business-
# name search landed on a real but different place - "No:16, GodhavariSt,
# Bharathi Nagar" for a customer address naming "Bhavani St, Bharathi
# Nagar". PIN and locality can both genuinely match (same postal code,
# same neighbourhood) while the actual street is simply wrong - neither
# of those checks would ever catch that on their own. Same tier as a
# locality mismatch: everything else about the match can look fine, only
# the street itself is wrong.
STREET_NAME_MISMATCH_CONFIDENCE_CAP = 0.4
# Common Indian street-type suffixes - the token immediately before one
# of these in the customer's own text is treated as the street's
# identifying name ("Bhavani" in "Bhavani St"). Deliberately excludes
# "Nagar" - that overwhelmingly names an AREA in Indian addresses
# ("Bharathi Nagar"), not a street, and is already covered by the
# locality check above; treating it as a street suffix here would extract
# the wrong word entirely and double up on (or contradict) that check.
_STREET_SUFFIX_PATTERN = re.compile(
    r"\b([A-Za-z]+)\s+(?:st|street|road|rd|salai|avenue|ave|lane|marg)\.?\b",
    re.IGNORECASE,
)

# Common Indian house/door/plot-number prefixes, stripped before matching
# the number itself - "Door No 15", "Door 15", and "15" all mean the same
# thing. "No" is optional after every prefix word here (matching "plot"'s
# original pattern) - "Door 2 Plot 107, ..." (no "No" at all) needs "door"
# alone to strip just as cleanly as "Door No 2" does. "No" itself accepts
# a colon as well as a period ("No: 202", not just "No. 202" - both are
# common) before the number.
#
# Deliberately does NOT include "Flat"/"Unit" - a flat/unit number is an
# INTERNAL designator within a named building, not a street-level fact
# Google's Geocoding API can ever confirm (apartment interiors aren't
# individually geocoded) - extracting "202" out of "Flat No: 202" and then
# checking it against street_number the same way a real door number is
# checked doesn't add confidence, it manufactures a guaranteed miss: a
# correct, precise BUILDING match ("Rwd Corniche, Pantheon Rd, Egmore")
# has no reason to ever carry a "202" anywhere in its components, so the
# check would cap it as "unconfirmed" even though nothing about the match
# was actually wrong. Confirmed by testing this addition against a real
# address - it broke a previously-correct 0.8-confidence match.
_HOUSE_NUMBER_PREFIX = re.compile(
    r"^(?:door\s*(?:no[.:]?)?|d\.?\s*no[.:]?|plot\s*(?:no[.:]?)?|house\s*(?:no[.:]?)?|no[.:]?)\s*",
    re.IGNORECASE,
)
# A house number token - plain (24), letter-suffixed (12A), or with a
# second part after a slash/dash that's itself a number+letter or a bare
# letter (12/2, 12/2A, 12-B). Anchored to the START of whatever segment is
# being checked - Indian addresses reliably lead each relevant segment
# with the number, never bury it mid-sentence.
_HOUSE_NUMBER_TOKEN = re.compile(r"^(\d+[A-Za-z]?(?:[/-](?:\d+[A-Za-z]?|[A-Za-z]))?)\b")


def _extract_house_numbers(address: str) -> List[str]:
    """Every house/door/plot-number-shaped token found in the first few
    COMMA-SEPARATED segments, not just the first one found - real case
    that motivated this: "Plot No 172, 17/10, Vinobaji Street, ..." states
    TWO numbers (a plot/survey number AND the actual postal door number,
    both genuine), and Google's data only ever confirms whichever one is
    actually indexed - there's no reliable way to know in advance which,
    so matching ANY of them is enough; requiring a SPECIFIC one (the
    first-found only) rejected a precise ROOFTOP match over a number the
    customer had, in fact, also stated, just second.

    A flat/unit number can precede the real one ("Flat 2B, 12 XYZ Street"
    - the spec's own listed format) or a building name can ("ABC
    Apartments, 12 XYZ Street"), which is why every one of the first 3
    segments is checked, not just the very first. A bare 6-digit run is
    explicitly rejected - that's a PIN code, not a house number (see
    _extract_pincode, the actual PIN check, reused from nominatim_geocoder
    same as this function's sibling checks). Returns [] when the address
    has none at all - never required to be present."""
    segments = [s.strip() for s in re.split(r"[,\n]", address) if s.strip()]
    numbers: List[str] = []
    for segment in segments[:3]:
        candidate = _HOUSE_NUMBER_PREFIX.sub("", segment).strip()
        match = _HOUSE_NUMBER_TOKEN.match(candidate)
        if match and not re.fullmatch(r"\d{6}", match.group(1)):
            number = match.group(1).upper()
            if number not in numbers:
                numbers.append(number)
    return numbers


def _extract_house_number(address: str) -> Optional[str]:
    """The single leading house/door/plot number, if any - used only where
    a specific ONE number matters (does this address lead with a real
    number at all, for _strip_leading_name_segment/
    _reorder_house_number_to_street's own structural checks). Component
    validation against Google's result uses _extract_house_numbers
    (plural) instead, since more than one candidate can be genuine there."""
    numbers = _extract_house_numbers(address)
    return numbers[0] if numbers else None


def _normalize_house_number(number: str) -> str:
    """"17/10" and "17-10" are the same flat/plot notation, written with
    either separator interchangeably in real addresses (and in Google's
    own data) - canonicalized before comparing so that difference alone
    never causes a false mismatch."""
    return number.replace("-", "/")


def _house_number_matches(google_number: str, customer_numbers: Iterable[str]) -> bool:
    """True when Google's confirmed street_number genuinely corresponds to
    one of the customer's stated numbers - either an exact match (after
    separator normalization - see _normalize_house_number), or Google's
    number is just the BASE building/plot number while the customer's
    also names a specific sub-unit Google's data doesn't carry down to
    ("2" confirmed vs customer's "2/20" - the base "2" matching is still
    strong confirmation of the same building, not a mismatch just because
    Google lacks flat-level granularity). Never the other direction - a
    customer who only gave a bare base number is never "confirmed" by a
    MORE specific Google number, since that would be inventing precision
    the customer never actually claimed."""
    normalized_google = _normalize_house_number(google_number)
    for customer_number in customer_numbers:
        normalized_customer = _normalize_house_number(customer_number)
        if normalized_google == normalized_customer:
            return True
        if normalized_google == normalized_customer.split("/")[0]:
            return True
    return False


def _number_confirmed_in_text(number: str, text: str) -> bool:
    """Whole-token match (word boundaries both sides) so a customer's "17"
    confirms against free text containing "Door No.17" or "17, Bhavani
    St" but never spuriously matches inside an unrelated longer number
    like "170" or "217" - or, just as importantly, inside the PIN code
    that's almost always present somewhere in that same joined text."""
    return bool(re.search(r"\b" + re.escape(number) + r"\b", text, re.IGNORECASE))


def _component_text_tokens(text: str) -> List[str]:
    """Tokenizes Google's address_components text for the street-name
    check specifically - NOT nominatim_geocoder._significant_tokens,
    deliberately. That one is built for LOCALITY matching, where "Main",
    "Road", "Street", "Nagar" are noise words that show up in a huge
    fraction of Chennai locality names and would swamp any real signal
    (see its own _GENERIC_LOCALITY_WORDS) - but a street-name check needs
    exactly those words back: "Main" in "Main Road" is very often the
    actual distinguishing street name, not filler, and filtering it out
    here caused this check to wrongly flag "0 Main Street" as a street
    mismatch against a component list that plainly contained "...Main
    Road..." - the word was being stripped from BOTH sides before they
    were ever compared."""
    tokens = re.split(r"[,\s/#-]+", text.lower())
    return [t for t in (tok.strip(".") for tok in tokens) if t and not t.isdigit()]


def _extract_street_keyword(address: str) -> Optional[str]:
    """The customer's stated street NAME, not its type-suffix - "bhavani"
    out of "Bhavani St". Optional by nature (an address with no street-
    type suffix at all - a locality/landmark-only address - simply has
    none, and this correctly returns None rather than guessing at one).
    First match wins - Indian addresses reliably name the actual street
    once, early in the string; a later "St"/"Road" mention (inside a
    locality or landmark phrase) is rare enough not to special-case."""
    match = _STREET_SUFFIX_PATTERN.search(address)
    return match.group(1).lower() if match else None


def _score_component_match(
    original_address: str,
    address_components: List[Dict[str, object]],
    precision_confidence: Optional[float] = None,
) -> Optional[float]:
    """Returns a confidence CAP (never a floor/boost) when a real mismatch
    is found, or None when nothing meaningful was found to flag - callers
    combine this with _score_result via min(), so None means "no opinion,
    defer entirely to the precision-based score" rather than "confirmed
    fine". Checks PIN, locality, street name, and house/door number
    independently and returns the MOST restrictive (lowest) cap among
    whichever ones actually found something to flag - a result can fail
    more than one check at once.

    `precision_confidence` (Google's OWN location_type-based score - see
    _score_result - passed in by _geocode_once, never by the Places path,
    which has no such signal) enables ONE deliberate exception: a PIN
    mismatch that stands entirely alone - street, house number, and
    locality all confirm cleanly, and Google's own match is genuinely
    precise (ROOFTOP/RANGE_INTERPOLATED, not just an area-level guess) -
    is trusted as a likely customer-side PIN typo rather than a wrong
    address, and does not cap confidence on its own. This is a real,
    explicit trade-off (confirmed with the user): every OTHER cap in this
    file only ever downgrades, never overrides what the customer typed -
    PIN mismatch is the one deliberate exception, and only when every
    other independent signal already confirms the match on its own."""
    non_pin_caps: List[float] = []

    customer_pin = _extract_pincode(original_address)
    google_pin = next(
        (c.get("long_name") for c in address_components if "postal_code" in (c.get("types") or [])),
        None,
    )
    pin_mismatch = bool(customer_pin and google_pin and customer_pin != str(google_pin))

    google_localities = [
        str(c.get("long_name") or "") for c in address_components
        if set(c.get("types") or []) & _LOCALITY_COMPONENT_TYPES
    ]
    customer_tokens = _significant_tokens(original_address)
    if google_localities and customer_tokens:
        locality_tokens: List[str] = []
        for locality in google_localities:
            locality_tokens.extend(_significant_tokens(locality))
        if locality_tokens and not any(_fuzzy_token_match(token, customer_tokens) for token in locality_tokens):
            non_pin_caps.append(LOCALITY_MISMATCH_CONFIDENCE_CAP)

    # House/door number - the spec's central "street found, house not
    # confirmed" case. Building name is never required (see
    # _extract_house_numbers' own docstring) - only whether the customer's
    # address happened to include a number, independent of anything else.
    # Matching ANY stated number is enough (see _extract_house_numbers -
    # some addresses genuinely state more than one, e.g. a plot/survey
    # number alongside the actual door number, and there's no reliable way
    # to know which one Google's data indexes against in advance).
    customer_house_numbers = _extract_house_numbers(original_address)
    if customer_house_numbers:
        google_house_number = next(
            (str(c.get("long_name") or "").upper() for c in address_components if "street_number" in (c.get("types") or [])),
            None,
        )
        if google_house_number is None:
            # No STRUCTURED street_number component - but an establishment/
            # POI match's most specific component is often free text with
            # no "street_number" type at all ("Door No.17, Narayana Swami
            # Men's PG, Bhavani St...", types: [] - the exact same shape
            # that already needed a text-based fallback for the street
            # name below). Confirmed via that free text is still confirmed
            # - only genuinely absent from every component counts as
            # unconfirmed.
            all_component_text = " ".join(str(c.get("long_name") or "") for c in address_components)
            if not any(_number_confirmed_in_text(n, all_component_text) for n in customer_house_numbers):
                non_pin_caps.append(STREET_NUMBER_UNCONFIRMED_CONFIDENCE_CAP)
        elif not _house_number_matches(google_house_number, customer_house_numbers):
            non_pin_caps.append(STREET_NUMBER_MISMATCH_CONFIDENCE_CAP)

    # Street name - independent of house number, and independent of PIN/
    # locality (both of THOSE can genuinely match while the street itself
    # is still wrong - see this constant's own comment above for the real
    # case that motivated it). Checked against every component's long_name
    # joined together, not just route-typed ones - an establishment/POI
    # match's most specific component often carries free text with no
    # "route" type at all ("No:16, GodhavariSt", types: []), so requiring
    # a structured route component here would just silently skip the
    # exact case this needs to catch.
    street_keyword = _extract_street_keyword(original_address)
    if street_keyword:
        all_component_text = " ".join(str(c.get("long_name") or "") for c in address_components)
        component_tokens = _component_text_tokens(all_component_text)
        if component_tokens and not _fuzzy_token_match(street_keyword, component_tokens):
            non_pin_caps.append(STREET_NAME_MISMATCH_CONFIDENCE_CAP)

    if pin_mismatch:
        trust_precision_over_pin = (
            not non_pin_caps
            and precision_confidence is not None
            and precision_confidence >= PIN_MISMATCH_TRUST_OVERRIDE_MIN_PRECISION
        )
        if not trust_precision_over_pin:
            non_pin_caps.append(PIN_MISMATCH_CONFIDENCE_CAP)

    return min(non_pin_caps) if non_pin_caps else None


def _strip_landmark_phrase(address: str) -> str:
    stripped = _LANDMARK_PATTERN.sub("", address)
    stripped = re.sub(r"\s+", " ", stripped).strip().strip(",").strip()
    return stripped


def _reorder_house_number_to_street(address: str) -> Optional[str]:
    """The other half of "a name Google can't find confuses the match":
    Indian addresses conventionally write house-number, THEN a building/
    business name, THEN the street ("17, Narayana Swami Men's PG, Bhavani
    St, ..."), but Google's parsers only recognize a house number when
    it's adjacent to the street it belongs to - "17 Bhavani St" parses
    cleanly, "17, [anything else], Bhavani St" doesn't, and neither
    _strip_leading_name_segment (this address already leads with a real
    house number, so it correctly declines to touch it) nor the plain
    house-number check (nothing to compare against - Google's own match
    for the un-reordered text has no confirmed number at all) can fix
    that on their own.

    Moves the number next to the first street-suffix segment found and
    drops whatever sat between them - "17, Narayana Swami Men's PG,
    Bhavani St, Bharathi Nagar, ..." becomes "17 Bhavani St, Bharathi
    Nagar, ...", the exact form Google actually parses a house number
    from. Returns None when there's nothing to fix: no house number
    leads the address, no later segment looks like a street, or the
    street segment is already the very next one (nothing between them to
    skip in the first place)."""
    segments = [s.strip() for s in re.split(r"[,\n]", address) if s.strip()]
    if len(segments) < 3:
        return None
    house_number = _extract_house_number(segments[0])
    if not house_number:
        return None
    for i in range(1, len(segments)):
        if _STREET_SUFFIX_PATTERN.search(segments[i]):
            if i == 1:
                return None  # already adjacent - nothing to fix
            return ", ".join([f"{house_number} {segments[i]}"] + segments[i + 1:])
    return None


def _strip_leading_name_segment(address: str) -> Optional[str]:
    """Drops a leading building/business-name segment ("Narayana Swami
    Men's PG, Bhavani St, Bharathi Nagar, ...") so the remaining street +
    locality + city + PIN can be geocoded on its own - a name Google
    doesn't have independently indexed doesn't just fail to help, it
    actively drags the Geocoding API's own fuzzy establishment-matching
    (and _find_place's) toward a wrong nearby business instead (the
    GodhavariSt/Sarathy Nagar cases - see STREET_NAME_MISMATCH_CONFIDENCE_
    CAP and PLACES_FALLBACK_CONFIDENCE's own comments). Stripping it and
    asking again with just the structured part is what actually turns
    that into a clean, confident street-level match.

    Returns None (nothing to strip) whenever:
      - the address already leads with a house number
        (_extract_house_number found one - the exact case a building name
        is NOT the problem, since the structured parser has a real number
        to anchor on already), or
      - the first segment already looks like a street reference itself
        (_STREET_SUFFIX_PATTERN matches it - "Bhavani St, Bharathi Nagar,
        ..." with no name in front at all would otherwise have its actual
        street stripped as if it were a name), or
      - fewer than 3 segments total, so there's not enough left after
        stripping to form a real address."""
    if _extract_house_number(address):
        return None
    segments = [s.strip() for s in re.split(r"[,\n]", address) if s.strip()]
    if len(segments) < 3:
        return None
    if _STREET_SUFFIX_PATTERN.search(segments[0]):
        return None
    return ", ".join(segments[1:])


def _rank(result: Optional[GeocodeResult]) -> float:
    """Comparable score for 'is this candidate better than that one' - a
    missing result never beats even the lowest real confidence."""
    if result is None:
        return -1.0
    return result.confidence if result.confidence is not None else 0.0


class GoogleGeocoder(GeocodingProvider):
    """Google Geocoding API client. Owns a single HTTP session reused across
    every call (pass an explicit `client` to share one across geocoders, or
    let it create/own its own via the context manager).

    `places_api_key` is optional and separate from `api_key` on purpose -
    Google Cloud API-restricted keys are commonly scoped to exactly one API
    each (least-privilege), so a project may have one key allowlisted for
    Geocoding API only and a different key allowlisted for Places API only.
    Defaults to `api_key` (the single-key setup, one key enabled for both
    APIs) when not given separately."""

    def __init__(
        self,
        api_key: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        client: Optional[httpx.Client] = None,
        places_api_key: Optional[str] = None,
    ) -> None:
        self._api_key = api_key
        self._places_api_key = places_api_key or api_key
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._min_confidence = min_confidence
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "GoogleGeocoder":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def geocode(self, address: str) -> Optional[GeocodeResult]:
        """Up to six attempts, each only tried when the one before it
        didn't already land a confident (STATUS_OK) match - cheapest first:
          1. The address as given, straight to the Geocoding API.
          2. The same address with landmark phrases ("near X", "opposite Y")
             stripped - free (same API), just a cleaner query. Skipped if
             there was no landmark phrase to strip.
          3. The same address with a leading building/business name
             stripped (see _strip_leading_name_segment) - also free (same
             API): a name Google has no independent listing for doesn't
             just fail to help the structured parser, it actively confuses
             it toward a wrong nearby match, so asking again with just the
             street/locality/city/PIN often succeeds cleanly where the
             full text didn't. Skipped when there's no real name segment
             to strip (see that function's own docstring for exactly when).
          4. The same address with the house number moved next to its
             street (see _reorder_house_number_to_street) - the OTHER
             shape a name in the way breaks: "17, [Name], Bhavani St,
             ..." (house number, then name, then street - the
             conventional Indian order) instead of "[Name], 17 Bhavani
             St, ...". Google only recognizes the number when it's
             adjacent to the street, so this fixes exactly the case step 3
             correctly declines to touch (a real house number already
             leads the address). Mutually exclusive with step 3 by
             construction - only ever one of them has something to do.
          5. Places API's fuzzy text search, against the ORIGINAL full
             address (this is the one attempt actually built to recognize
             a named establishment/apartment complex BY NAME - the
             structured Geocoding API calls above never are).
          6. Places API again, against whichever cleaned-up variant from
             steps 3-4 actually applied (only if step 5 didn't already
             succeed) - a named building Places has no listing for
             confuses ITS fuzzy matching the exact same way it confuses
             the structured API, and clearing it out helps here for the
             same reason it helped there.
        Real added cost per call for attempts 5-6, which is exactly why
        they're the last resort, not the first attempt. Whichever attempt
        scores highest wins; if none reach STATUS_OK, the best (still-
        flagged) one found is returned rather than nothing - never worse
        than a single plain attempt would have been."""
        if not address or not self._api_key:
            return None

        best = self._geocode_once(address)
        if best is not None and best.status == STATUS_OK:
            return best

        stripped_landmark = _strip_landmark_phrase(address)
        if stripped_landmark and stripped_landmark.lower() != address.lower():
            variant = self._geocode_once(stripped_landmark)
            if _rank(variant) > _rank(best):
                print(f"Google Geocoding: landmark-stripped retry '{address}' -> '{stripped_landmark}' improved the match")
                best = variant
            if best is not None and best.status == STATUS_OK:
                return best

        # Mutually exclusive by construction (see each function's own
        # docstring) - cleaned_text is whichever one actually applied, used
        # again below for the Places retry.
        cleaned_text = _strip_leading_name_segment(address) or _reorder_house_number_to_street(address)
        if cleaned_text and cleaned_text.lower() != address.lower():
            variant = self._geocode_once(cleaned_text)
            if _rank(variant) > _rank(best):
                print(f"Google Geocoding: cleaned-text retry '{address}' -> '{cleaned_text}' improved the match")
                best = variant
            if best is not None and best.status == STATUS_OK:
                return best

        fallback = self._find_place(address)
        if _rank(fallback) > _rank(best):
            best = fallback
        if best is not None and best.status == STATUS_OK:
            return best

        if cleaned_text and cleaned_text.lower() != address.lower():
            fallback_cleaned = self._find_place(cleaned_text)
            if _rank(fallback_cleaned) > _rank(best):
                print(f"Places API fallback: cleaned-text retry '{address}' -> '{cleaned_text}' improved the match")
                best = fallback_cleaned

        return best

    def _find_place(self, address: str) -> Optional[GeocodeResult]:
        """Falls back to Places API's text search when the structured
        Geocoding API could only place an address at area level - Places is
        built to match a NAMED establishment/apartment complex/building,
        which the Geocoding API's structured address parser often can't do
        (it expects a street + number, not "Sidharth Upscale Apartments").

        Only resolves a place_id here - Find Place From Text has no
        address_components field to validate against, so the actual
        location/confidence comes from a Place Details follow-up (see
        _place_details) that CAN see them.

        Deliberately non-fatal on every failure path, unlike _geocode_once's
        REQUEST_DENIED handling: a denial here means "this fallback isn't
        enabled on this key/project", not "the provider is broken" - the
        Geocoding API calls above already prove the key/billing works, so
        this only ever returns None instead of raising, leaving the
        Geocoding API's own (possibly flagged) result as the answer."""
        if not self._places_api_key:
            return None

        params = {
            "input": address,
            "inputtype": "textquery",
            "fields": "place_id",
            "locationbias": _CHENNAI_LOCATION_BIAS,
            "key": self._places_api_key,
        }

        try:
            response = self._client.get(PLACES_FIND_PLACE_URL, params=params)
            response.raise_for_status()
            data = response.json()
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
            print(f"Places API fallback request failed for '{address}': {exc}")
            return None

        status = data.get("status", "UNKNOWN_ERROR")
        if status != "OK":
            if status != "ZERO_RESULTS":
                print(f"Places API fallback: {status} for '{address}' - skipping (Places API may not be enabled)")
            return None

        candidates = data.get("candidates") or []
        if not candidates:
            return None
        place_id = candidates[0].get("place_id")
        if not place_id:
            return None

        return self._place_details(address, place_id)

    def _place_details(self, original_address: str, place_id: str) -> Optional[GeocodeResult]:
        """Resolves a Places place_id to a location AND validates it against
        the customer's own address text, exactly like _geocode_once does for
        the Geocoding API - reusing _score_component_match so a Places match
        gets no free pass just for coming from a different Google API. This
        is what catches Places confidently matching a same-named place in
        the wrong neighbourhood (e.g. an unrelated "Sarathy Nagar" result
        for a "Bharathi Nagar" address, both inside Velachery) - previously
        accepted outright at a flat 0.65 with no cross-check at all."""
        params = {
            "place_id": place_id,
            "fields": "geometry,formatted_address,name,address_component",
            "key": self._places_api_key,
        }

        try:
            response = self._client.get(PLACE_DETAILS_URL, params=params)
            response.raise_for_status()
            data = response.json()
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
            print(f"Places Details request failed for '{original_address}': {exc}")
            return None

        status = data.get("status", "UNKNOWN_ERROR")
        if status != "OK":
            if status != "ZERO_RESULTS":
                print(f"Places Details: {status} for '{original_address}' - skipping")
            return None

        result = data.get("result") or {}
        location = result.get("geometry", {}).get("location", {})
        if "lat" not in location or "lng" not in location:
            return None

        address_components = result.get("address_components") or []
        component_cap = _score_component_match(original_address, address_components)
        confidence = PLACES_FALLBACK_CONFIDENCE if component_cap is None else min(PLACES_FALLBACK_CONFIDENCE, component_cap)
        result_status = STATUS_OK if confidence >= self._min_confidence else STATUS_NEEDS_MANUAL_VERIFICATION
        formatted = result.get("formatted_address") or result.get("name") or original_address

        if result_status != STATUS_OK:
            print(
                f"Places API fallback: '{original_address}' -> matched '{formatted}' but address component "
                f"mismatch capped it at {component_cap:.2f} (confidence={confidence:.2f}) - flagged for manual verification"
            )
        else:
            print(f"Places API fallback: '{original_address}' -> matched '{formatted}' (confidence={confidence:.2f})")

        return GeocodeResult(
            lat=float(location["lat"]),
            lng=float(location["lng"]),
            formatted_address=formatted,
            status=result_status,
            provider="google-places",
            confidence=confidence,
        )

    def _geocode_once(self, address: str) -> Optional[GeocodeResult]:
        """One Geocoding API call for one exact address string - no retries
        across query variants, that's geocode()'s job. Still retries on
        transient failures (network errors, OVER_QUERY_LIMIT) for this one
        call, same as before this method was split out of geocode()."""
        params = {
            "address": address,
            "key": self._api_key,
            # Every address this app handles is in India - a hard country
            # restriction avoids ambiguous same-name-different-country
            # matches (stronger than a soft region bias).
            "components": "country:IN",
        }

        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._client.get(GOOGLE_GEOCODE_URL, params=params)
                response.raise_for_status()
                data = response.json()
            except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
                if attempt == self._max_retries:
                    print(f"Google Geocoding request failed for '{address}': {exc}")
                    return None
                time.sleep(self._retry_backoff_seconds * attempt)
                continue

            status = data.get("status", "UNKNOWN_ERROR")

            if status == "OK":
                results = data.get("results") or []
                if not results:
                    return None
                result = results[0]
                geometry = result.get("geometry", {})
                location = geometry.get("location", {})
                if "lat" not in location or "lng" not in location:
                    return None

                location_type = geometry.get("location_type")
                result_types = result.get("types") or []
                partial_match = bool(result.get("partial_match", False))
                address_components = result.get("address_components") or []
                precision_confidence = _score_result(location_type, result_types, partial_match)
                component_cap = _score_component_match(address, address_components, precision_confidence)
                confidence = precision_confidence if component_cap is None else min(precision_confidence, component_cap)
                result_status = STATUS_OK if confidence >= self._min_confidence else STATUS_NEEDS_MANUAL_VERIFICATION

                if result_status != STATUS_OK:
                    reason = (
                        f"address component mismatch (capped at {component_cap:.2f})"
                        if component_cap is not None and component_cap < precision_confidence
                        else f"location_type={location_type}, types={result_types}, partial_match={partial_match}"
                    )
                    print(
                        f"Google Geocoding: '{address}' -> low-precision match "
                        f"({reason}, confidence={confidence:.2f}) - flagged for manual verification"
                    )

                return GeocodeResult(
                    lat=float(location["lat"]),
                    lng=float(location["lng"]),
                    formatted_address=result.get("formatted_address", ""),
                    status=result_status,
                    provider="google",
                    confidence=confidence,
                )

            if status == "REQUEST_DENIED":
                # Account/key/billing problem - NOT specific to this address.
                # Every subsequent request would fail identically, so the
                # caller needs to know to stop immediately rather than treat
                # this as "this one address couldn't be found."
                reason = data.get("error_message", "Request denied by Google - check API key and billing status.")
                raise GeocodingProviderError(
                    f"Google Geocoding is not working: {reason}", provider="google"
                )

            if status in _TERMINAL_FAILURE_STATUSES:
                reason = data.get("error_message", "")
                print(f"Google Geocoding: {status} for '{address}'{f' - {reason}' if reason else ''}")
                return None

            # OVER_QUERY_LIMIT / UNKNOWN_ERROR / anything undocumented.
            if attempt == self._max_retries:
                print(f"Google Geocoding: giving up on '{address}' after {attempt} attempt(s) (last status: {status})")
                return None
            time.sleep(self._retry_backoff_seconds * attempt)

        return None
