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
    "Ashok Nagar", "Athipattu", "Avadi", "Ayapakkam", "Ayanavaram", "Basin Bridge",
    "Besant Nagar", "Bharathi Nagar", "Broadway", "Camp Road", "Chepauk", "Chetpet",
    "Chintadripet", "Chitlapakkam", "Choolai", "Choolaimedu", "Chromepet", "Chrompet",
    "Egmore", "Ekkaduthangal", "Ennore", "Foreshore Estate", "Fort St George",
    "George Town", "Gerugambakkam", "Gopalapuram", "Guduvancheri", "Guduvanchery",
    "Guindy", "Hasthinapuram", "Injambakkam", "Iyyappanthangal", "Jafferkhanpet",
    "K K Nagar", "KK Nagar", "Kanathur", "Kancheepuram", "Kandanchavadi",
    "Karapakkam", "Kasturba Nagar", "Kattupakkam", "Kazhipattur", "Keelkattalai",
    "Kelambakkam", "Kilpauk", "Kodambakkam", "Kolathur", "Kondithope", "Korattur",
    "Korukkupet", "Kotturpuram", "Kottivakkam", "Kovalam", "Kovilambakkam", "Kovur",
    "Koyambedu", "Kundrathur", "Madambakkam", "Madhavaram", "Madipakkam",
    "Maduravoyal", "Mahindra World City", "Mambalam", "Manali", "Manapakkam",
    "Mandaveli", "Mangadu", "Mannady", "Maraimalai Nagar", "Medavakkam",
    "Meenambakkam", "Minjur", "Mogappair", "Mogappair East", "Mogappair West",
    "Moolakadai", "Moovarasanpet", "Moulivakkam", "Mount Road", "Mudichur",
    "Mugalivakkam", "Mylapore", "Nandanam", "Nanganallur", "Nanmangalam",
    "Navalur", "Neelankarai", "Nerkundram", "Nesapakkam", "Nolambur", "Numbal",
    "Nungambakkam", "Okkiyam", "Padappai", "Padi", "Padur", "Palavakkam",
    "Pallavaram", "Pallikaranai", "Pammal", "Parrys Corner", "Pattabiram",
    "Pattaravakkam", "Peerkankaranai", "Perambur", "Peravallur", "Perumbakkam",
    "Perungalathur", "Perungudi", "Polichalur", "Potheri", "Pozhichalur",
    "Poonamallee", "Porur", "Pudupet", "Pulianthope", "Purasawalkam", "Puzhal",
    "Puzhuthivakkam", "Raja Annamalai Puram", "Ramapuram", "Red Hills", "Royapettah",
    "Royapuram", "Saidapet", "Saligramam", "Santhome", "Selaiyur", "Semmancheri",
    "Sembakkam", "Sholavaram", "Sholinganallur", "Singaperumal Koil", "Siruseri",
    "Sithalapakkam", "Sowcarpet", "Sriperumbudur", "St Thomas Mount", "Sunguvarchatram",
    "Surapet", "T Nagar", "T. Nagar", "Taramani", "Teynampet", "Thalambur",
    "Thirumangalam", "Thirumazhisai", "Thirumullaivoyal", "Thiruneermalai",
    "Thiruninravur", "Thiruvanmiyur", "Thiruverkadu", "Thiruvottiyur", "Thoraipakkam",
    "Tondiarpet", "Triplicane", "Urapakkam", "Uttandi", "Vadapalani",
    "Valasaravakkam", "Vanagaram", "Vandalur", "Velachery", "Vellore", "Vengaivasal",
    "Vepery", "Vettuvankeni", "Vijayanagaram", "Villivakkam", "Virugambakkam",
    "Vyasarpadi", "Washermanpet", "West Mambalam",
]

# Similarity floor for accepting a correction. 0.86 is high on purpose:
# "Velachary"->"Velachery" scores 0.94, "Adyarr"->"Adyar" 0.91, while
# genuinely different localities that merely rhyme (Madipakkam vs
# Madambakkam, 0.79) stay below it and are left alone.
LOCALITY_CORRECTION_THRESHOLD = 0.86

# How much better the best candidate must score than the runner-up to be
# trusted at all. Real case that forced this: "Pallavakam" scored an
# EXACT tie (0.9) against "palavakkam" (correct) and "pallavaram" (a
# real, different, distant locality) - below this margin, the input is
# ambiguous between two genuinely different places and is left
# uncorrected rather than guessed at.
AMBIGUOUS_CORRECTION_MARGIN = 0.03

# Canonical pincodes for Chennai localities to auto-repair customer pincode typos
LOCALITY_PINCODES = {
    "velachery": "600042", "adyar": "600020", "thiruvanmiyur": "600041",
    "mylapore": "600004", "anna nagar": "600040", "t nagar": "600017",
    "porur": "600116", "guindy": "600032", "vadapalani": "600026",
    "kodambakkam": "600024", "ashok nagar": "600083", "kk nagar": "600078",
    "west mambalam": "600033", "saidapet": "600015", "teynampet": "600018",
    "alwarpet": "600018", "nungambakkam": "600034", "egmore": "600008",
    "chetpet": "600031", "kilpauk": "600010", "perambur": "600011",
    "vyasarpadi": "600039", "royapettah": "600014", "triplicane": "600005",
    "mandaveli": "600028", "kotturpuram": "600085", "besant nagar": "600090",
    "perungudi": "600096", "thoraipakkam": "600097", "sholinganallur": "600119",
    "karapakkam": "600097", "navalur": "603103", "padur": "603103",
    "siruseri": "603103", "kelambakkam": "603103", "medavakkam": "600100",
    "perumbakkam": "600100", "sithalapakkam": "600126", "madipakkam": "600091",
    "keelkattalai": "600117", "nanganallur": "600061", "adambakkam": "600088",
    "pallavaram": "600043", "chromepet": "600044", "chrompet": "600044",
    "tambaram": "600045", "selaiyur": "600073", "guduvancheri": "603202",
    "guduvanchery": "603202", "urapakkam": "603210", "vandalur": "600048",
    "virugambakkam": "600092", "valasaravakkam": "600087", "ramapuram": "600089",
    "manapakkam": "600125", "gerugambakkam": "600128", "iyyappanthangal": "600056",
    "kattupakkam": "600056", "poonamallee": "600056", "kundrathur": "600069",
    "mangadu": "600122", "avadi": "600054", "ambattur": "600053",
    "mogappair": "600037", "nolambur": "600095", "arumbakkam": "600106",
    "aminjikarai": "600029", "koyambedu": "600107", "padi": "600050",
    "korattur": "600080", "kolathur": "600099", "madhavaram": "600060",
}


def get_canonical_pincode(locality: str) -> Optional[str]:
    """Returns the canonical 6-digit PIN code for a known Chennai locality."""
    if not locality:
        return None
    return LOCALITY_PINCODES.get(locality.strip().lower())


def repair_pincode_by_locality(address: str) -> Optional[str]:
    """Auto-repairs customer pincode typos ONLY when a 6-digit pincode is typed
    that differs from the canonical pincode of a recognized locality in the address."""
    lowered = address.lower()
    pin_match = re.search(r"\b(\d{6})\b", address)
    if not pin_match:
        return None

    typed_pin = pin_match.group(1)
    for locality, canonical_pin in LOCALITY_PINCODES.items():
        if locality in lowered and typed_pin != canonical_pin:
            return address.replace(typed_pin, canonical_pin)

    return None

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
    # Real bug this closes: "Pallavakam" (customer typo) scored EXACTLY
    # 0.9 against BOTH "palavakkam" (an ECR-area locality, the intended
    # target) and "pallavaram" (a real, different locality near the
    # airport, nowhere close). candidates is a set - iteration order is
    # not guaranteed - so which one won was arbitrary from one run to the
    # next, and a wrong pick here silently moves an order to a genuinely
    # different part of the city. runner_up_score tracks the second-best
    # distinct candidate's score so a near-tie can be detected below.
    runner_up_score = 0.0

    for candidate in candidates:
        if candidate in _STOPWORDS or len(candidate) < 5:
            continue
        # A different first letter is a different place, not a typo -
        # this is what keeps "Adyar" from ever becoming "Anna".
        if candidate[0] != lowered[0]:
            continue
        score = max(_similarity(lowered, candidate), _similarity(canonical_input, _canonical(candidate)))
        if score > best_score:
            runner_up_score = best_score
            best_score = score
            best = candidate
        elif score > runner_up_score:
            runner_up_score = score

    if best is None or best_score < LOCALITY_CORRECTION_THRESHOLD:
        return None
    # A close second candidate means the input is genuinely ambiguous
    # between two real, different places - refusing to correct here is
    # far safer than a coin-flip that can silently relocate an order.
    if best_score - runner_up_score < AMBIGUOUS_CORRECTION_MARGIN:
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
