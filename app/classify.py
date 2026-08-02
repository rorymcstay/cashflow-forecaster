"""Keyword-based category classifier for bank/card transaction descriptions.

Seeded from a year of Monzo/HSBC/Amex statement data (see scripts/classify_statements.py
for the one-off pass that built this out). Reuse `classify()` against any new
statement export to get a first-pass category before manual review.

Categories are checked top-to-bottom in CATEGORY_KEYWORDS order — put more
specific/exact rules (bills, subscriptions, internal transfers) ahead of broad
catch-alls (Eating out, Transport) so a known bill never gets swept into a
generic bucket by an unlucky substring match.
"""

import re

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "Housing": [
        "TLP RE CLIENT ACC", "HALIFAX MORTGAGE", "MORTGAGE", "WANDSWORTH BOROUGH COUNCIL",
        "COUNCIL TAX", "THAMES WATER",
    ],
    "Utilities": [
        "E.ON", "EON NEXT", "COMMUNITY FIBRE", "TV LICENSING", "VOXI", "VODAFONE",
    ],
    "Insurance": [
        "URBAN JUNGLE", "ELEPHANT", "AA MEMBERSHIP", "CYCLING UK",
    ],
    "Subscriptions": [
        "NETFLIX", "SPOTIFY", "AMAZON PRIME", "AMAZON WEB SERVICES", "AWS",
        "CLAUDE.AI", "ANTHROPIC", "APPLE.COM", "DIGITALOCEAN", "PROTON AG",
        "LINKEDIN", "UBER ONE MEMBERSHIP",
    ],
    "Income": [
        "PAYROLL", "DWP", "JSA", "SALARY", "NORTHEAST RE",
    ],
    "Transfers": [
        "TRADING 212", "SPLITWISE", "REVOLUT", "COIN JAR POT", "MCSTAY RORY",
        "RORY MCSTAY", "JOINT ACCOUNT", "INTERNET TRANSFER", "IG INDEX", "IG LIMITED",
        "AMERICAN EXPRESS", "RHIANNON NORTHEAST", "RUARI CLARKE", "CLARK REJ",
    ],
    "Fees": [
        "NON-STERLING", "TRANSACTION FEE", "ATM", "OVERDRAFT", "NOTEMACHINE",
    ],
    "Public Transport": [
        # "TFL" alone would also catch "TFL BOPS ... CHANNEL" (a Eurotunnel toll
        # code, nothing to do with Transport for London) — keep this specific.
        "TFL TRAVEL CHARGE", "TFL.GOV", "LIME*", "LIME PASS", "LIME RIDE",
        "TRAINLINE", "IRISH RAIL", "DUBLIN PORT TUNNEL", "STENA LINE",
        "LIGNES D AZUR", "SANTANDER CYCLE",
    ],
    "Transport": [
        "DVLA", "SHELL", "ESSO", "MOTOR FUEL GROUP", "MFG", "TOTALENERGIES",
        "TOTAL ENERGIES", "TOTALERG", "CERTAS", "APRR", "SANEF", "VINCI", "ESCOTA",
        "M6 TOLL", "RINGGO", "RINGSEND TOLL", "JUSTPARK", "CAR PARK", "PARKING",
        "TOLL", "UBER TRIP", "CAR WASH", "AREA NFC", "A.R.E.A.",
        "LECLERC", "STATION AVIA", "IPMATIC", "APPLEGREEN", "BP", "AUTOS", "GARAGE",
        "CIRCLE K",
    ],
    "Eating out": [
        "DELIVEROO", "UBER EATS", "STARBUCKS", "COSTA", "GAILS", "PIZZA", "SUSHI",
        "PUB", "INN", "ARMS", "TAVERN", "BREWERY", "BISTRO", "RISTORANTE",
        "TRATTORIA", "OSTERIA", "CAFE", "CAFFE", "COFFEE", "RESTAURANT", "GRILL",
        "KITCHEN", "BAR", "BREWDOG", "CHIPOTLE", "KFC", "NANDO", "WASABI",
        "TORTILLA", "FRANCO MANCA", "DOJO*", "HOP POLE",
    ],
    "Groceries": [
        "SAINSBURY", "TESCO", "WAITROSE", "CO-OP", "COOP", "OCADO", "LIDL", "ALDI",
        "SUPER FOOD STORES", "LOCAL EXPRESS", "SPAR", "CARREFOUR", "INTERMARCHE",
        "VILLAGE STORES", "FARM", "GROCER", "SUPERMARKET", "SUPERETTE",
    ],
    "Shopping": [
        "AMAZON", "AMZNMKTPLACE", "EBAY", "PAYPAL", "M&S", "B&Q", "DECATHLON",
        "INTERSPORT", "SPORT", "PERFECT HOMEWARE", "PATAGONIA",
    ],
    "Entertainment": [
        "TICKETCO", "BIKEPARK", "FUNIVIE", "BOWLS CLUB", "FOOTBALL CLUB", "HARLEQUINS",
        "CYCLES", "BIKE", "YACHT CLUB",
    ],
    "Holidays": [
        "EASYJET", "RYANAIR", "BOOKING.COM", "AIR INDIA", "DISCOVERCARS", "HOTEL",
        "HOMESTAY", "AIRPORT", "HOSTEL", "RETREAT", "GUEST HOUSE", "WYNNSTAY",
        "TOUR GUIDATO",
    ],
    "Personal": [
        "BARBER", "SALON", "WELLNESS", "RIDERS HUB", "PEIXEIRO", "BICYCLE",
    ],
}

# Single-word keywords are matched with a word boundary so e.g. "BAR" doesn't
# hit "BARBERS", and "INN" doesn't hit "SPINNING". Multi-word phrases are
# matched as a plain substring since they're specific enough not to collide.
_WORD_RE_CACHE: dict[str, re.Pattern] = {}


def _keyword_matches(keyword: str, text: str) -> bool:
    keyword = keyword.strip()
    if " " not in keyword:
        pattern = _WORD_RE_CACHE.get(keyword)
        if pattern is None:
            pattern = re.compile(rf"\b{re.escape(keyword)}\b")
            _WORD_RE_CACHE[keyword] = pattern
        return pattern.search(text) is not None
    return keyword in text


def classify(description: str) -> str | None:
    """Return the first matching category for a transaction description,
    or None if nothing in the keyword bank matches."""
    text = description.upper()
    for category, keywords in CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            if _keyword_matches(keyword, text):
                return category
    return None
