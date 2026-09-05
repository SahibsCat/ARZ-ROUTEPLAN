"""Free-form address normalization and component extraction.

Two jobs, both of which happen BEFORE any billed provider request:

1. `normalize()` - repair the formatting damage real customer input
   arrives with (glued-together words, missing spaces after a house-
   number prefix, abbreviations, punctuation noise) so the query we
   actually send has a fighting chance. This never changes the meaning
   of the address and never touches the customer's stored original -
   `Order.address` keeps exactly what they typed; this is only ever the
   text handed to a geocoding provider.

2. `parse()` - pull the address apart into the components an operator
   thinks in (house number, flat, building, street, landmark, area,
   city, PIN) so a flagged address can SHOW what the system understood
   instead of only saying it wasn't confident. When an address needs
   human verification, "we read house 12A, street Gandhi Road, area
   Velachery, no PIN" tells the operator what to fix in about a second;
   a bare confidence number tells them to go re-read the raw string
   themselves.

Deliberately conservative in both directions. Nothing here invents a
component that isn't in the customer's text - an absent building name
stays None rather than being guessed at from a nearby token, and an
unrecognized segment is reported as `extra` rather than being forced
into whichever field still happens to be empty. A wrong parse that
looks confident is worse than an honest gap, because the operator acts
on it.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Words that reliably stand alone in Indian addresses, so a token that
# ENDS in one and has a real word in front of it is a missing-space
# case: "annanagar" -> "anna nagar", "mainroad" -> "main road".
#
# Chosen narrowly on purpose. "puram", "pettai" and "bakkam" are NOT
# here even though they look like the same pattern - they're genuinely
# part of single place names ("Kotturpuram", "Nungambakkam"), so
# splitting them would corrupt a correct locality into two wrong words.
# The four below never occur glued inside a real single Chennai place
# name: every "X Nagar"/"X Road"/"X Street"/"X Salai" is two words.
_GLUED_SUFFIXES = ("nagar", "road", "street", "salai", "avenue", "layout")

# House/door/flat/plot prefixes that customers routinely type with no
# space before the number ("No12", "Flat5A", "DoorNo7"). Split ONLY
# after these known words - a blanket letter-then-digit split would
# destroy real house numbers that legitimately lead with a letter
# ("D1/5", "A1C" - see google_geocoder._HOUSE_NUMBER_TOKEN).
_NUMBER_PREFIX_WORDS = (
    "doorno", "door", "dno", "no", "plot", "flat", "houseno", "house",
    "block", "shop", "unit", "apt",
)

# Expanded only in the text sent to the provider, never in what's shown
# back to the customer. Kept to abbreviations with exactly one meaning
# in an address - "St" is deliberately absent: it's "Street" far more
# often than not, but it's also "Saint" in real Chennai road names
# ("St Mary's Road"), and expanding it wrong turns a findable road into
# an unfindable one. Google handles a bare "St" fine on its own.
_ABBREVIATIONS = {
    "rd": "Road",
    "strt": "Street",
    "steet": "Street",
    "sreet": "Street",
    "streey": "Street",
    "aven": "Avenue",
    "ngr": "Nagar",
    "opp": "Opposite",
    "nr": "Near",
    "bldg": "Building",
    "apts": "Apartments",
    "aptmt": "Apartments",
}

_PIN_PATTERN = re.compile(r"\b(\d{6})\b")

# Chennai's near-universal informal shorthand: "Chennai 41" or
# "chennai-78" meaning PIN 600041 / 600078 - the city name plus just the
# district digits, everyone locally understands it, Google's geocoder
# does not. Requires the number directly after "chennai"/"madras" (not
# glued into a street-suffix ordinal like "Chennai 2nd Street" - the
# lookahead rules out a following letter) and not already 6 digits
# itself (that's a real PIN already, handled by _PIN_PATTERN).
_SHORT_PINCODE_PATTERN = re.compile(
    r"\b(?:chennai|madras)\b\s*[-,]?\s*(\d{1,3})\b(?!\d)(?![a-zA-Z])", re.IGNORECASE
)


def _expand_short_pincode(address: str) -> str:
    """Appends the full 6-digit PIN when the short "Chennai NN" form is
    present and no real 6-digit PIN already exists anywhere in the
    address - never replaces or removes anything the customer wrote."""
    if _PIN_PATTERN.search(address):
        return address
    match = _SHORT_PINCODE_PATTERN.search(address)
    if not match:
        return address
    full_pin = f"600{int(match.group(1)):03d}"
    return f"{address}, {full_pin}"

_LANDMARK_PATTERN = re.compile(
    r"\b(?:near|nr|opp(?:osite)?|behind|beside|next\s+to|above|below|back\s+side)\b",
    re.IGNORECASE,
)

_BUILDING_KEYWORDS = (
    "apartment", "apartments", "apts", "flats", "tower", "towers",
    "residency", "enclave", "villa", "villas", "building", "complex",
    "mansion", "nivas", "illam", "heights", "castle", "court",
    "chambers", "plaza", "arcade",
)

_STREET_KEYWORDS = (
    "street", "st", "road", "rd", "salai", "avenue", "ave", "lane",
    "marg", "cross", "main", "bypass", "highway", "extn", "extension",
)

# Cities only. Ambattur/Avadi/Tambaram/Poonamallee are deliberately NOT
# here - they're localities within the Chennai area, and treating one as
# the city swallows the real locality ("Ambattur Chennai" read as the
# city, leaving the area blank).
_CITY_KEYWORDS = ("chennai", "madras")

_HOUSE_PREFIX_PATTERN = re.compile(
    r"^(?:door[.\s]*(?:no[.:]?)?|d\.?\s*no[.:]?|plot[.\s]*(?:no[.:]?)?"
    r"|house[.\s]*(?:no[.:]?)?|no[.:]?)\s*",
    re.IGNORECASE,
)
_HOUSE_TOKEN_PATTERN = re.compile(r"^([A-Za-z]?\d+[A-Za-z]?(?:[/-](?:\d+[A-Za-z]?|[A-Za-z]))?)\b")

# The keyword needs its own \b at the END too: without it "Apartments"
# matches "apartment" and then reads the trailing "s" as the flat
# number. The captured number must contain a digit for the same reason -
# a bare letter is never a flat number, it's the next word.
_FLAT_PATTERN = re.compile(
    r"\b(?:flat|apt|apartment|unit)\b[.\s]*(?:no[.:]?)?\s*([A-Za-z]?\d+[A-Za-z]?)\b",
    re.IGNORECASE,
)


def _split_glued_suffix(token: str) -> str:
    """"annanagar" -> "anna nagar". Only fires when what's left in front
    of the suffix is a real word (3+ chars), so "nagar" itself and short
    accidental matches are left alone."""
    lowered = token.lower()
    for suffix in _GLUED_SUFFIXES:
        if lowered.endswith(suffix) and len(lowered) > len(suffix) + 2:
            head = token[: -len(suffix)]
            tail = token[-len(suffix) :]
            # Already separated by punctuation the caller will handle.
            if head[-1].isalpha():
                return f"{head} {tail}"
    return token


def _split_camel_case(token: str) -> str:
    """"AnnaNagar" -> "Anna Nagar". Lower-to-upper boundaries only, so
    all-caps input ("RWD CORNICHE") and normal words are untouched."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", token)


def _prefix_length_ignoring_dots(lowered: str, word: str) -> Optional[int]:
    """How many characters of `lowered` are consumed by `word`, allowing
    periods anywhere inside it ("d.no" consumes 4 for the word "dno").
    None when it isn't a prefix at all."""
    index = 0
    for letter in word:
        while index < len(lowered) and lowered[index] == ".":
            index += 1
        if index >= len(lowered) or lowered[index] != letter:
            return None
        index += 1
    return index


def _split_number_prefix(token: str) -> str:
    """"No12" -> "No 12", "No.12" -> "No. 12", "Flat5A" -> "Flat 5A".
    A period or colon between the prefix word and the number stays with
    the prefix - that's how it's written."""
    lowered = token.lower()
    for word in _NUMBER_PREFIX_WORDS:
        # Compared with periods removed so "D.No12" matches "dno" the
        # same way "DNo12" does - the period is a formatting choice, not
        # a different prefix.
        prefix_length = _prefix_length_ignoring_dots(lowered, word)
        if prefix_length is None:
            continue
        rest = token[prefix_length:]
        separator = ""
        while rest and rest[0] in ".:":
            separator += rest[0]
            rest = rest[1:]
        if rest and rest[0].isdigit():
            return f"{token[:prefix_length]}{separator} {rest}"
    return token


def _split_glued_pincode(token: str) -> str:
    """"Madambakkam600126" -> "Madambakkam 600126". A 6-digit run stuck
    to the end of a word is always a PIN code that lost its separator -
    left glued, neither the locality nor the PIN is findable."""
    return re.sub(r"^([A-Za-z]{3,})(\d{6})$", r"\1 \2", token)


def _split_ordinal(token: str) -> str:
    """"2ndstreet" -> "2nd street", "3rdcross" -> "3rd cross"."""
    return re.sub(r"^(\d+(?:st|nd|rd|th))([A-Za-z]{3,})$", r"\1 \2", token, flags=re.IGNORECASE)


def _expand_abbreviation(token: str) -> str:
    stripped = token.strip(".")
    replacement = _ABBREVIATIONS.get(stripped.lower())
    return replacement if replacement else token


def normalize(address: str) -> str:
    """The provider-facing form of a customer's address: same meaning,
    repaired formatting. Safe to call on already-clean input (it's a
    no-op there) and never returns empty for non-empty input."""
    if not address or not address.strip():
        return ""

    text = address.replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r",\s*(?:,\s*)+", ", ", text)
    text = " ".join(text.split())

    repaired: List[str] = []
    for token in text.split(" "):
        trailing = ""
        while token and token[-1] in ",.":
            trailing = token[-1] + trailing
            token = token[:-1]
        if not token:
            repaired.append(trailing)
            continue

        # Never restructure something that's already a number/PIN/house
        # number - those are correct as typed and splitting them is how
        # "12A" becomes "12 A" and stops matching Google's street_number.
        # A token that's a prefix WORD glued to a number ("No12",
        # "D.No12", "Flat5A") still needs splitting; anything else
        # containing a digit is a real number ("12A", "D1/5", "600042")
        # and must be left exactly as typed. The letter run may contain
        # periods - "D.No12" is the same prefix as "DNo12".
        if any(ch.isdigit() for ch in token) and not re.match(r"(?i)^[a-z][a-z.]*\d", token):
            token = _split_ordinal(token)
        elif _split_glued_pincode(token) != token:
            # A word with a PIN stuck to it looks like a prefix+number
            # token, but splitting it as one would give "Madambakkam
            # 600126" the wrong treatment below - handle it here.
            token = _split_glued_pincode(token)
        else:
            token = _split_number_prefix(token)
            token = _split_camel_case(token)
            if " " not in token:
                token = _split_glued_suffix(token)
            token = " ".join(_expand_abbreviation(part) for part in token.split(" "))

        repaired.append(token + trailing)

    text = " ".join(part for part in repaired if part)
    text = re.sub(r"\s+([,.])", r"\1", text)
    text = re.sub(r",\s*(?:,\s*)+", ", ", text)
    text = text.strip().strip(",").strip()
    return _expand_short_pincode(text)


@dataclass
class ParsedAddress:
    """What the system believes each part of a customer's address is.
    Every field is optional - a real address is allowed to be missing a
    building name, a landmark, or a PIN, and none of those absences make
    it unusable (see the module docstring)."""

    house_number: Optional[str] = None
    flat: Optional[str] = None
    building: Optional[str] = None
    street: Optional[str] = None
    landmark: Optional[str] = None
    area: Optional[str] = None
    city: Optional[str] = None
    pincode: Optional[str] = None
    extra: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Optional[str]]:
        return {
            "house_number": self.house_number,
            "flat": self.flat,
            "building": self.building,
            "street": self.street,
            "landmark": self.landmark,
            "area": self.area,
            "city": self.city,
            "pincode": self.pincode,
        }

    def missing(self) -> List[str]:
        """The components an operator would care that we don't have.
        Building/flat/landmark are excluded on purpose - they're
        genuinely optional and listing them as "missing" trains people
        to ignore the list."""
        labels = {
            "house_number": "house/door number",
            "street": "street name",
            "area": "area/locality",
            "city": "city",
            "pincode": "PIN code",
        }
        return [label for key, label in labels.items() if getattr(self, key) is None]

    def describe(self) -> str:
        """One line, operator-facing: what we read, then what we didn't.
        This is the thing that turns "confidence 0.45" into something
        someone can act on without re-reading the raw address."""
        labels = [
            ("House", self.house_number),
            ("Flat", self.flat),
            ("Building", self.building),
            ("Street", self.street),
            ("Landmark", self.landmark),
            ("Area", self.area),
            ("City", self.city),
            ("PIN", self.pincode),
        ]
        found = " | ".join(f"{label}: {value}" for label, value in labels if value)
        if not found:
            return "Could not read any address components"
        missing = self.missing()
        if missing:
            return f"{found} — missing: {', '.join(missing)}"
        return found


def _looks_like_street(segment: str) -> bool:
    tokens = [t.strip(".").lower() for t in segment.split()]
    return any(token in _STREET_KEYWORDS for token in tokens)


def _looks_like_building(segment: str) -> bool:
    tokens = [t.strip(".").lower() for t in segment.split()]
    return any(token in _BUILDING_KEYWORDS for token in tokens)


def _looks_like_city(segment: str) -> bool:
    tokens = [t.strip(".").lower() for t in segment.split()]
    return any(token in _CITY_KEYWORDS for token in tokens)


def building_signature(address: str) -> Optional[str]:
    """A stable key identifying "this specific building, in this area" -
    the thing that lets the system recognize a repeat apartment complex
    across two customers who worded their address completely
    differently. Used by crud.set_manual_location to remember a human-
    confirmed building forever, and by geocode_service to recognize it
    again on a later, different order (see VerifiedLocation's own
    docstring in models.py for the full mechanism).

    Deliberately requires BOTH a building name AND an area, each long
    enough to be reasonably specific - a bare building name alone
    ("Sri Apartments", "Ganga Enclave") repeats constantly across a city
    this size and would collide; requiring the area too narrows it to a
    combination genuinely unlikely to name two different real places.
    Returns None whenever that bar isn't met - most addresses (no
    building name given at all) never get a signature, which is
    correct: this is a bonus signal on top of normal geocoding, not a
    replacement for it, and a signature that could plausibly collide is
    worse than no signature."""
    parsed = parse(normalize(address))
    if not parsed.building or not parsed.area:
        return None

    normalized_building = re.sub(r"[^a-z0-9]+", "", parsed.building.lower())
    normalized_area = re.sub(r"[^a-z0-9]+", "", parsed.area.lower())
    if len(normalized_building) < 6 or len(normalized_area) < 3:
        return None

    return f"{normalized_building}|{normalized_area}"


def _split_at_street_word(segment: str) -> tuple:
    """("Kk Road 3rd Cross Street Ambattur") -> ("Kk Road", "3rd Cross
    Street Ambattur"). Splits just after the FIRST street word, and only
    when there are at least two more words behind it - otherwise the
    segment is already just a street and there's nothing to separate.
    Returns (segment, None) when no split applies."""
    tokens = segment.split()
    if len(tokens) < 4:
        return segment, None
    for index, token in enumerate(tokens):
        if token.strip(".").lower() not in _STREET_KEYWORDS:
            continue
        # A street word can't be the very first thing ("Road Ambattur"
        # isn't a street name), and needs real words behind it to be
        # worth splitting off.
        if index == 0 or index >= len(tokens) - 1:
            continue
        # "Main Road" / "Cross Street" are two-word street words - split
        # after the SECOND one, or the street comes out as bare "Main"
        # and "Road" leaks into the area.
        if tokens[index + 1].strip(".").lower() in _STREET_KEYWORDS:
            continue
        return " ".join(tokens[: index + 1]), " ".join(tokens[index + 1 :])
    return segment, None


def _split_city_from_segment(segment: str) -> tuple:
    """("Velachery Chennai") -> ("Velachery", "Chennai"). Returns
    (None, segment) when the segment is only the city."""
    tokens = segment.split()
    city_index = next(
        (i for i, t in enumerate(tokens) if t.strip(".").lower() in _CITY_KEYWORDS), None
    )
    if city_index is None or city_index == 0:
        return None, segment
    head = " ".join(tokens[:city_index]).strip(" ,")
    return (head or None), " ".join(tokens[city_index:])


def parse(address: str) -> ParsedAddress:
    """Best-effort component extraction from free-form input. Works on
    whatever it's given - raw customer text or a normalized form - and
    reports only what it can actually identify."""
    parsed = ParsedAddress()
    if not address or not address.strip():
        return parsed

    working = address

    pin_match = _PIN_PATTERN.search(working)
    if pin_match:
        parsed.pincode = pin_match.group(1)
        working = working.replace(pin_match.group(1), " ")

    flat_match = _FLAT_PATTERN.search(working)
    if flat_match:
        parsed.flat = flat_match.group(1).upper()
        working = working[: flat_match.start()] + " " + working[flat_match.end() :]

    pending = [s.strip(" .").strip() for s in re.split(r"[,\n]", working)]
    pending = [s for s in pending if s]

    # Segments that didn't match anything specific (no street/building/
    # city keyword) - decided AFTER the full pass, not immediately, so a
    # complex name with no recognizable keyword ("Urbantree Fantastic",
    # unlike "... Apartments"/"... Towers") can still be told apart from
    # the actual area named after it. See the decision below the loop.
    unclassified: List[str] = []

    while pending:
        segment = pending.pop(0)

        # Plenty of real addresses arrive with no commas at all ("No 4 Kk
        # Road 3rd Cross Street Ambattur Chennai"). Left whole, the entire
        # tail gets labelled as one enormous "street" and the area is lost.
        # Cut it just after the first street word so the street is the
        # street and everything behind it goes back in the queue to be
        # classified on its own.
        if parsed.street is None:
            head, tail = _split_at_street_word(segment)
            if tail:
                segment = head
                pending.insert(0, tail)
        # A landmark phrase is checked first - "Near Bhavani Street"
        # names a landmark, not this customer's own street, and letting
        # the street check see it first would pin the wrong road.
        if parsed.landmark is None and _LANDMARK_PATTERN.search(segment):
            parsed.landmark = segment
            continue

        if parsed.house_number is None:
            candidate = _HOUSE_PREFIX_PATTERN.sub("", segment).strip()
            house_match = _HOUSE_TOKEN_PATTERN.match(candidate)
            if house_match and not re.fullmatch(r"\d{6}", house_match.group(1)):
                parsed.house_number = house_match.group(1).upper()
                remainder = candidate[house_match.end() :].strip(" ,.")
                if remainder:
                    segment = remainder
                else:
                    continue

        if parsed.building is None and _looks_like_building(segment):
            parsed.building = segment
            continue

        if parsed.street is None and _looks_like_street(segment):
            parsed.street = segment
            continue

        if parsed.city is None and _looks_like_city(segment):
            # "Velachery Chennai" in one segment (no comma) is common -
            # keep the city as the city and give the words in front of
            # it back to the area, rather than labelling the whole thing
            # as the city and losing the locality entirely.
            head, city = _split_city_from_segment(segment)
            parsed.city = city
            if head and parsed.area is None:
                parsed.area = head
            continue

        if re.search(r"[A-Za-z]{3,}", segment):
            unclassified.append(segment)
            continue

        if re.search(r"[A-Za-z0-9]", segment):
            parsed.extra.append(segment)

    if len(unclassified) >= 2 and parsed.building is None:
        # Two or more segments matched no known keyword at all, and
        # nothing has been read as the building yet - the conventional
        # Indian address order (complex/building name, THEN its area) is
        # far more informative here than "first one wins": real case,
        # "Urbantree Fantastic, Vanagaram" naming a complex with no
        # recognizable building keyword. First becomes the building,
        # last becomes the area (whichever named the finished delivery
        # zone is more likely to be nearest the city/PIN); anything
        # genuinely in between is too ambiguous to guess at and is kept
        # as extra rather than forced into either.
        parsed.building = unclassified[0]
        rest = unclassified[1:]
        if parsed.area is None and rest:
            parsed.area = rest[-1]
            rest = rest[:-1]
        parsed.extra.extend(rest)
    elif unclassified:
        if parsed.area is None:
            parsed.area = unclassified[0]
        parsed.extra.extend(unclassified[1:])

    return parsed
