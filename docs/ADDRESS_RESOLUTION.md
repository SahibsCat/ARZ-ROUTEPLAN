# Address Resolution — how it works and how to maintain it

This is the permanent reference for how a customer's address becomes a
map pin. Read this before changing anything in `backend/app/geocoding/`
or `backend/app/geocode_service.py`.

**The one rule everything else serves:** never silently place a pin on
a house we aren't confident about. An address that needs a human look
is a small cost. A wrong pin that looks confident sends a driver to the
wrong door and nobody finds out until the delivery fails.

---

## 1. The pipeline

```
Customer's address (stored untouched on Order.address)
   │
   ├─ normalize()            address_parser.py      repair formatting
   ├─ correct_locality_...   chennai_localities.py  fix known misspellings
   │                                                → this is the QUERY
   ├─ cache lookup           crud.get_cached_geocode
   │
   ├─ Google Geocoding API   google_geocoder.py     up to 6 query variants
   │     · raw query
   │     · landmark phrase stripped
   │     · leading name segment stripped / house number reordered
   │     · Places Find Place → Place Details (on original, then cleaned)
   │
   ├─ _score_result()        precision of the match (rooftop/range/centre)
   ├─ _score_component_match() does it agree with what the customer wrote?
   │     · PIN code
   │     · locality
   │     · street name
   │     · house/door number
   │
   └─ confidence ≥ threshold ? → pin used as the order's location
                              ↘ flagged → Failed Addresses queue, WITH
                                a suggested pin + a reason + a breakdown
```

Two things that are easy to confuse and must stay separate:

- **Precision** — how exact Google's answer is (a rooftop, a street, an
  area centre). Comes from `_score_result`.
- **Agreement** — whether Google's answer is about the *same address*
  the customer wrote. Comes from `_score_component_match`.

A rooftop-precise pin for the wrong street is worse than an approximate
pin for the right one. Both signals are required; the lower one wins.

---

## 2. What each file does

| File | Responsibility |
|---|---|
| `app/geocoding/address_parser.py` | Normalization (formatting repair) + component extraction (house/flat/building/street/landmark/area/city/PIN) |
| `app/geocoding/chennai_localities.py` | Gazetteer of real Chennai localities + conservative spelling correction |
| `app/geocoding/google_geocoder.py` | Query variants, Google calls, precision scoring, component agreement scoring |
| `app/geocode_service.py` | Orchestration, caching, concurrency, the operator-facing message |
| `app/crud.py` | Persistence, manual pin overrides, failed-address tracking |

---

## 3. Maintenance: the three things you'll actually need to do

### 3a. A locality keeps failing because it isn't in the gazetteer

Add it to `CHENNAI_LOCALITIES` in
`app/geocoding/chennai_localities.py`. Spell it **the way Google spells
it** — that's what the query is ultimately matched against. One entry
per line, alphabetical. That's the whole change; no other file needs
touching.

Do **not** add street names to this list. Streets are too numerous and
too similar to each other to correct safely by resemblance — correcting
"Bhavani St" to "Bhagani St" would move the order to a real, findable,
wrong street.

### 3b. A customer's house-number format isn't recognized

`_HOUSE_NUMBER_TOKEN` and `_HOUSE_NUMBER_PREFIX` in
`google_geocoder.py`, plus `_NUMBER_PREFIX_WORDS` in
`address_parser.py`. Add a case to
`tests/test_address_matrix.py::test_house_number_is_read_from_every_format`
first — if it passes already, the format is handled and the problem is
elsewhere.

**Never** add "Flat"/"Unit" to `_HOUSE_NUMBER_PREFIX`. A flat number is
inside a building; Google's geocoder cannot confirm it and never will,
so validating against it turns correct matches into failures. This was
tried and reverted — the comment in the code says so, and it stays.

### 3c. Diagnosing one specific address

Open this in a browser, with the address that's misbehaving:

```
https://rootplan-backend.onrender.com/api/debug/geocode?address=12A, Gandhi Road, Velachary, Chennai
```

It returns:

- `normalized_query` — what the system actually sends to Google after
  formatting repair and spelling correction
- `components` / `components_summary` — what it read out of the text
- `missing_components` — what it couldn't find
- `result` — what came back, with confidence

That distinguishes the two causes that look identical from the admin
screen: **we misread the address** (fix the parser or the gazetteer) vs
**Google has no data for that house** (a genuine verification case, no
code change will help).

### 3d. Too many addresses need verification

Work from the real data, never from invented examples:

1. Take the actual flagged addresses out of the Failed Addresses queue.
2. Run each one through the pipeline and read the reason it gives.
3. Fix the *class* of problem, not the individual address.
4. Re-run the whole batch before and after, and compare the counts.

A fix that resolves one address and can't be explained as a general
rule is a fix that will place a wrong pin on a different address later.

---

## 4. The deliberate exceptions

Three places accept a match despite an imperfect signal. Each is
narrow, each is gated on everything *else* agreeing, and each is
commented in the code with its reasoning. They exist because the strict
version was rejecting genuinely correct matches.

1. **PIN-code trust** (`PIN_MISMATCH_TRUST_OVERRIDE_MIN_PRECISION`) —
   when the PIN is the *only* disagreement and precision is high, the
   customer's PIN is treated as a typo. Customers get their own PIN
   wrong constantly; Google's is derived from the actual location.

2. **Street-name trust** — when the customer's house number lands on a
   real structured `street_number` component that matches exactly, an
   abbreviated or alternately-spelled street name is trusted. Google
   cannot structurally confirm the same house number on a different
   street.

3. **Same base number, different letter unit** (`_numeric_core`) —
   "231B/1" and "231C" are units of one subdivided plot, physically
   adjacent. A different *base* number ("2" vs "4") stays a mismatch.

4. **Nearby house number when street + locality both confirm**
   (`house_number_is_specific_mismatch`, explicit product decision) —
   "you entered 78, Google found 79" on a street and area we've
   independently confirmed describes two doors a few metres apart, not
   a wrong address. Restricted to a *specific* mismatch (Google
   positively found a different, real number) — never extended to
   "unconfirmed" (Google has no house-level data at all), which stays
   flagged, since that proves only the street exists and could be a
   range-interpolated guess anywhere along it.

5. **Flat/block designator never blocks alone**
   (`house_number_is_unit_designator`) — "A103", "B311", "S1", "F1" name
   a unit *inside* a building, not a street door number. Google's data
   never indexes apartment interiors, so validating one against
   `street_number` manufactures a guaranteed miss regardless of whether
   the address is correct.

A matching PIN is deliberately **never** treated as locality
confirmation on its own — a PIN spans several square kilometres and
commonly covers multiple named Chennai sublocalities. This was tried
and reverted after it reintroduced the exact wrong-pin bug these
exceptions exist to avoid (see `google_geocoder.py`'s own comment at
the locality check, and
`test_score_component_match_does_not_let_the_city_name_alone_confirm_the_locality`).

If you're tempted to add a fourth, the bar is: it must be impossible
for the exception to fire when the address is genuinely different.

---

## 5. What is NOT part of this system

Address resolution must never read from, or be influenced by, the
routing side. Specifically:

- Route splitting, optimization, grouping
- Driver assignment and selection
- Delivery slots, timing, capacity
- Order allocation

A pin is decided **only** from the customer's own text and the
provider's response. Never from where other orders are, never from what
would make a route look tidier. If an address is uncertain, it gets
flagged — it does not get nudged toward the nearest known point.

---

## 6. Tests

| File | Covers |
|---|---|
| `tests/test_address_matrix.py` | Every input *shape* — the checklist of ways real addresses arrive broken |
| `tests/test_address_parser.py` | Normalization and extraction functions |
| `tests/test_chennai_localities.py` | Spelling correction, and the corrections it must refuse to make |
| `tests/test_google_geocoder.py` | Precision scoring, component agreement, the three exceptions above |

Run before any deploy:

```bash
cd backend && ./venv/Scripts/python.exe -m pytest -q --ignore=tests/test_excel_service.py
```

The negative tests matter as much as the positive ones. Tests named
"never", "does not", and "refuses" are load-bearing: they're what stops
a future accuracy improvement from quietly becoming a wrong-pin
generator.

---

## 7. Honest limits

- **"Zero failed addresses" is not the target, and shouldn't be.** An
  address naming a house number that doesn't exist on the street it
  names cannot be resolved by any amount of logic — the information
  needed isn't in the input. Forcing those through means guessing.
- Newly constructed buildings are frequently absent from Google's data.
  The street will resolve; the house won't. That's a verification case,
  correctly.
- The gazetteer covers Chennai. Expanding to another city means adding
  its localities and re-checking the city keywords in
  `address_parser._CITY_KEYWORDS`.
- Spelling correction only fires above a 0.86 similarity with a
  matching first letter. Raising the catch rate by lowering that bar
  will start swapping genuinely different localities for each other.
