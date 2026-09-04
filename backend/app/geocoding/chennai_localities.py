"""A gazetteer of real Chennai localities, and spelling correction
against it.

This is the piece that makes "Velachary" findable. A geocoder given a
misspelled locality either returns nothing or - worse - returns
something confidently wrong somewhere else in the city, and no amount
of clever scoring after the fact recovers from a query that named a
place which doesn't exist.

The correction is deliberately conservative in three ways, because a
WRONG correction is far more damaging than no correction (it moves the
order to a real, findable, different part of Chennai):

1. It only ever corrects TO a name on this list. It never invents a
   spelling, and it never "corrects" a word it doesn't recognize into
   the nearest thing it happens to know.
2. It requires a high similarity ratio AND a matching first letter -
   "Adyar"/"Anna Nagar" style near-collisions between genuinely
   different localities are common in Chennai and must not be swapped
   for each other.
3. A word already ON the list is never touched, so correctly-spelled
   input passes through byte-identical.

Everything here feeds the query only. The customer's stored address is
never rewritten (see geocode_service.clean_address).
"""

import re
from difflib import SequenceMatcher
from typing import Iterable, List, Optional, Set

# Chennai localities/neighbourhoods, as they're spelled in Google's own
# data (which is what the query is ultimately matched against). Kept to
# single- and two-word area names - street names are NOT here, they're
# far too numerous and too easily confused with each other to correct
# safely by similarity.
CHENNAI_LOCALITIES: List[str] = [
    "Adambakkam", "Adyar", "Alandur", "Alapakkam", "Alwarpet", "Alwarthirunagar",
    "Ambattur", "Aminjikarai", "Anakaputhur", "Anna Nagar", "Annanur", "Arumbakkam",
    "Ashok Nagar", "Avadi", "Ayanavaram", "Basin Bridge", "Besant Nagar", "Bharathi Nagar",
    "Broadway", "Chepauk", "Chetpet", "Chintadripet", "Chitlapakkam", "Choolai",
    "Choolaimedu", "Chrompet", "Egmore", "Ekkaduthangal", "Ennore", "Foreshore Estate",
    "Fort St George", "George Town", "Gerugambakkam", "Gopalapuram", "Guduvancheri",
    "Guindy", "Injambakkam", "Iyyappanthangal", "Jafferkhanpet", "Kanathur",
    "Kancheepuram", "Kandanchavadi", "Karapakkam", "Kasturba Nagar", "Kattupakkam",
    "Kazhipattur", "Keelkattalai", "Kelambakkam", "Kilpauk", "Kodambakkam",
    "Kolathur", "Kondithope", "Korattur", "Korukkupet", "Kotturpuram", "Kottivakkam",
    "Kovilambakkam", "Kovur", "Koyambedu", "Kundrathur", "Madambakkam", "Madhavaram",
    "Madipakkam", "Maduravoyal", "Mambalam", "Manali", "Manapakkam", "Mandaveli",
    "Mangadu", "Mannady", "Maraimalai Nagar", "Medavakkam", "Meenambakkam",
    "Mogappair", "Moolakadai", "Moulivakkam", "Mount Road", "Mudichur", "Mugalivakkam",
    "Mylapore", "Nandanam", "Nanganallur", "Nanmangalam", "Navalur", "Neelankarai",
    "Nerkundram", "Nesapakkam", "Nolambur", "Numbal", "Nungambakkam", "Okkiyam",
    "Padi", "Padur", "Palavakkam", "Pallavaram", "Pallikaranai", "Pammal",
    "Parrys Corner", "Pattabiram", "Pattaravakkam", "Peerkankaranai", "Perambur",
    "Peravallur", "Perumbakkam", "Perungalathur", "Perungudi", "Pozhichalur",
    "Poonamallee", "Porur", "Pudupet", "Pulianthope", "Purasawalkam", "Puzhal",
    "Puzhuthivakkam", "Raja Annamalai Puram", "Ramapuram", "Red Hills", "Royapettah",
    "Royapuram", "Saidapet", "Saligramam", "Santhome", "Selaiyur", "Semmancheri",
    "Sembakkam", "Sholavaram", "Sholinganallur", "Siruseri", "Sithalapakkam",
    "Sowcarpet", "St Thomas Mount", "Surapet", "Taramani", "Teynampet",
    "Thirumangalam", "Thirumullaivoyal", "Thiruneermalai", "Thiruninravur",
    "Thiruvanmiyur", "Thiruverkadu", "Thiruvottiyur", "Thoraipakkam", "Tondiarpet",
    "Triplicane", "Urapakkam", "Vadapalani", "Valasaravakkam", "Vanagaram",
    "Vandalur", "Velachery", "Vellore", "Vepery", "Vettuvankeni", "Vijayanagaram",
    "Villivakkam", "Virugambakkam", "Vyasarpadi", "Washermanpet", "West Mambalam",
]

# Similarity floor for accepting a correction. 0.86 is high on purpose:
# "Velachary"->"Velachery" scores 0.94, "Adyarr"->"Adyar" 0.91, while
# genuinely different localities that merely rhyme (Madipakkam vs
# Madambakkam, 0.79) stay below it and are left alone.
LOCALITY_CORRECTION_THRESHOLD = 0.86

_VOWEL_RUN = re.compile(r"([aeiou])\1+")

_KNOWN_LOWER: Set[str] = {name.lower() for name in CHENNAI_LOCALITIES}
_KNOWN_WORDS: Set[str] = {word for name in CHENNAI_LOCALITIES for word in name.lower().split()}

# Words that appear inside locality names but carry no identity of their
# own - never corrected, and never used as evidence of a match.
_STOPWORDS = {"nagar", "puram", "street", "road", "main", "cross", "east", "west",
              "north", "south", "new", "old", "st", "mount", "town", "hills"}


def _canonical(token: str) -> str:
    """Fold the two spelling variances that dominate Tamil-to-English
    transliteration: doubled vowels ("Noombal"/"Numbal") and a trailing
    "y"/"i" ("Velachery"/"Velacheri")."""
    folded = _VOWEL_RUN.sub(r"\1", token.lower())
    return re.sub(r"[yi]$", "", folded)


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def correct_locality_word(word: str, known_words: Optional[Iterable[str]] = None) -> Optional[str]:
    """The correctly-spelled locality word `word` was probably meant to
    be, or None to leave it exactly as typed.

    None is returned far more often than a correction - that's the
    point. A word that's already correct, too short to judge, a
    stopword, or not close enough to anything real all come back None.
    """
    candidates = set(known_words) if known_words is not None else _KNOWN_WORDS
    lowered = word.lower().strip(".,")

    if len(lowered) < 5 or not lowered.isalpha():
        return None
    if lowered in _STOPWORDS or lowered in candidates:
        return None

    canonical_input = _canonical(lowered)
    best: Optional[str] = None
    best_score = 0.0

    for candidate in candidates:
        if candidate in _STOPWORDS or len(candidate) < 5:
            continue
        # A different first letter is a different place, not a typo -
        # this is what keeps "Adyar" from ever becoming "Anna".
        if candidate[0] != lowered[0]:
            continue
        score = max(_similarity(lowered, candidate), _similarity(canonical_input, _canonical(candidate)))
        if score > best_score:
            best_score = score
            best = candidate

    if best is None or best_score < LOCALITY_CORRECTION_THRESHOLD:
        return None
    if best == lowered:
        return None
    return best


def correct_locality_spelling(address: str) -> str:
    """Apply `correct_locality_word` across an address, preserving the
    original casing style of each word it replaces. Correctly-spelled
    input comes back unchanged."""
    if not address:
        return address

    def replace(match: re.Match) -> str:
        word = match.group(0)
        corrected = correct_locality_word(word)
        if corrected is None:
            return word
        if word.isupper():
            return corrected.upper()
        if word[0].isupper():
            return corrected.capitalize()
        return corrected

    return re.sub(r"[A-Za-z]+", replace, address)
