"""Canvas's friendly timezone labels, and how to turn one into an IANA id.

A datesheet records its zone twice: `iana=America/New_York` for programs and
`timezone=Eastern Time (US & Canada)` for people. `iana` is preferred -- it is
unambiguous and needs no table -- but when it is missing the friendly label is
**not** decoration to be thrown away. It names a zone.

That distinction is worth stating, because getting it wrong is what left this
module unwritten at first:

    Eastern Time (US & Canada)   a ZONE. Carries its own DST rules. Resolvable.
    EST / EDT                    a zone *observance*: half the year each.
    -05:00                       a fixed offset. Says nothing about DST.

Only the first can be resolved, and the first is exactly what `pull` writes --
`CourseSettings.timezone_label` strips Canvas's trailing offset pair, and the
generic form is chosen on purpose because a semester spans both observances.

## Where the table comes from

Canvas is Rails, so its dropdown is `ActiveSupport::TimeZone::MAPPING`
verbatim. This is that mapping. It is data, not logic: entries change only when
Rails or the IANA database renames something, which is why `KNOWN` is checked
against the running system's zone database in the test suite rather than
trusted. A label that resolves to a zone this machine does not have is dropped
at import rather than blowing up mid-push.
"""

from __future__ import annotations

import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: Rails' own label -> IANA mapping. Several labels share a zone (Rails offers
#: "Bern" and "Zurich" separately; both are Europe/Zurich), which is fine: the
#: lookup is one-way.
MAPPING = {
    "International Date Line West": "Etc/GMT+12",
    "Midway Island": "Pacific/Midway",
    "American Samoa": "Pacific/Pago_Pago",
    "Hawaii": "Pacific/Honolulu",
    "Alaska": "America/Juneau",
    "Pacific Time (US & Canada)": "America/Los_Angeles",
    "Tijuana": "America/Tijuana",
    "Mountain Time (US & Canada)": "America/Denver",
    "Arizona": "America/Phoenix",
    "Chihuahua": "America/Chihuahua",
    "Mazatlan": "America/Mazatlan",
    "Central Time (US & Canada)": "America/Chicago",
    "Saskatchewan": "America/Regina",
    "Guadalajara": "America/Mexico_City",
    "Mexico City": "America/Mexico_City",
    "Monterrey": "America/Monterrey",
    "Central America": "America/Guatemala",
    "Eastern Time (US & Canada)": "America/New_York",
    "Indiana (East)": "America/Indiana/Indianapolis",
    "Bogota": "America/Bogota",
    "Lima": "America/Lima",
    "Quito": "America/Lima",
    "Atlantic Time (Canada)": "America/Halifax",
    "Caracas": "America/Caracas",
    "La Paz": "America/La_Paz",
    "Santiago": "America/Santiago",
    "Newfoundland": "America/St_Johns",
    "Brasilia": "America/Sao_Paulo",
    "Buenos Aires": "America/Argentina/Buenos_Aires",
    "Montevideo": "America/Montevideo",
    "Georgetown": "America/Guyana",
    "Puerto Rico": "America/Puerto_Rico",
    "Greenland": "America/Godthab",
    "Mid-Atlantic": "Atlantic/South_Georgia",
    "Azores": "Atlantic/Azores",
    "Cape Verde Is.": "Atlantic/Cape_Verde",
    "Dublin": "Europe/Dublin",
    "Edinburgh": "Europe/London",
    "Lisbon": "Europe/Lisbon",
    "London": "Europe/London",
    "Casablanca": "Africa/Casablanca",
    "Monrovia": "Africa/Monrovia",
    "UTC": "Etc/UTC",
    "Belgrade": "Europe/Belgrade",
    "Bratislava": "Europe/Bratislava",
    "Budapest": "Europe/Budapest",
    "Ljubljana": "Europe/Ljubljana",
    "Prague": "Europe/Prague",
    "Sarajevo": "Europe/Sarajevo",
    "Skopje": "Europe/Skopje",
    "Warsaw": "Europe/Warsaw",
    "Zagreb": "Europe/Zagreb",
    "Brussels": "Europe/Brussels",
    "Copenhagen": "Europe/Copenhagen",
    "Madrid": "Europe/Madrid",
    "Paris": "Europe/Paris",
    "Amsterdam": "Europe/Amsterdam",
    "Berlin": "Europe/Berlin",
    "Bern": "Europe/Zurich",
    "Zurich": "Europe/Zurich",
    "Rome": "Europe/Rome",
    "Stockholm": "Europe/Stockholm",
    "Vienna": "Europe/Vienna",
    "West Central Africa": "Africa/Algiers",
    "Bucharest": "Europe/Bucharest",
    "Cairo": "Africa/Cairo",
    "Helsinki": "Europe/Helsinki",
    "Kyiv": "Europe/Kiev",
    "Kiev": "Europe/Kiev",
    "Riga": "Europe/Riga",
    "Sofia": "Europe/Sofia",
    "Tallinn": "Europe/Tallinn",
    "Vilnius": "Europe/Vilnius",
    "Athens": "Europe/Athens",
    "Istanbul": "Europe/Istanbul",
    "Minsk": "Europe/Minsk",
    "Jerusalem": "Asia/Jerusalem",
    "Harare": "Africa/Harare",
    "Pretoria": "Africa/Johannesburg",
    "Kaliningrad": "Europe/Kaliningrad",
    "Moscow": "Europe/Moscow",
    "St. Petersburg": "Europe/Moscow",
    "Volgograd": "Europe/Volgograd",
    "Samara": "Europe/Samara",
    "Kuwait": "Asia/Kuwait",
    "Riyadh": "Asia/Riyadh",
    "Nairobi": "Africa/Nairobi",
    "Baghdad": "Asia/Baghdad",
    "Tehran": "Asia/Tehran",
    "Abu Dhabi": "Asia/Muscat",
    "Muscat": "Asia/Muscat",
    "Baku": "Asia/Baku",
    "Tbilisi": "Asia/Tbilisi",
    "Yerevan": "Asia/Yerevan",
    "Kabul": "Asia/Kabul",
    "Ekaterinburg": "Asia/Yekaterinburg",
    "Islamabad": "Asia/Karachi",
    "Karachi": "Asia/Karachi",
    "Tashkent": "Asia/Tashkent",
    "Chennai": "Asia/Kolkata",
    "Kolkata": "Asia/Kolkata",
    "Mumbai": "Asia/Kolkata",
    "New Delhi": "Asia/Kolkata",
    "Kathmandu": "Asia/Kathmandu",
    "Astana": "Asia/Dhaka",
    "Dhaka": "Asia/Dhaka",
    "Sri Jayawardenepura": "Asia/Colombo",
    "Almaty": "Asia/Almaty",
    "Novosibirsk": "Asia/Novosibirsk",
    "Rangoon": "Asia/Rangoon",
    "Yangon": "Asia/Yangon",
    "Bangkok": "Asia/Bangkok",
    "Hanoi": "Asia/Bangkok",
    "Jakarta": "Asia/Jakarta",
    "Krasnoyarsk": "Asia/Krasnoyarsk",
    "Beijing": "Asia/Shanghai",
    "Chongqing": "Asia/Chongqing",
    "Hong Kong": "Asia/Hong_Kong",
    "Urumqi": "Asia/Urumqi",
    "Kuala Lumpur": "Asia/Kuala_Lumpur",
    "Singapore": "Asia/Singapore",
    "Taipei": "Asia/Taipei",
    "Perth": "Australia/Perth",
    "Irkutsk": "Asia/Irkutsk",
    "Ulaanbaatar": "Asia/Ulaanbaatar",
    "Seoul": "Asia/Seoul",
    "Osaka": "Asia/Tokyo",
    "Sapporo": "Asia/Tokyo",
    "Tokyo": "Asia/Tokyo",
    "Yakutsk": "Asia/Yakutsk",
    "Darwin": "Australia/Darwin",
    "Adelaide": "Australia/Adelaide",
    "Canberra": "Australia/Melbourne",
    "Melbourne": "Australia/Melbourne",
    "Sydney": "Australia/Sydney",
    "Brisbane": "Australia/Brisbane",
    "Hobart": "Australia/Hobart",
    "Vladivostok": "Asia/Vladivostok",
    "Guam": "Pacific/Guam",
    "Port Moresby": "Pacific/Port_Moresby",
    "Magadan": "Asia/Magadan",
    "Srednekolymsk": "Asia/Srednekolymsk",
    "Solomon Is.": "Pacific/Guadalcanal",
    "New Caledonia": "Pacific/Noumea",
    "Fiji": "Pacific/Fiji",
    "Kamchatka": "Asia/Kamchatka",
    "Marshall Is.": "Pacific/Majuro",
    "Auckland": "Pacific/Auckland",
    "Wellington": "Pacific/Auckland",
    "Nuku'alofa": "Pacific/Tongatapu",
    "Tokelau Is.": "Pacific/Fakaofo",
    "Chatham Is.": "Pacific/Chatham",
    "Samoa": "Pacific/Apia",
}


def _usable(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


#: IANA renames, old name first. Rails' table still carries several of the old
#: ones, and a system's zone database has *either* spelling depending on its
#: vintage -- so each is tried both ways rather than assuming which era we are
#: on. Without this, "Greenland", "Rangoon" and "Kyiv" silently stop resolving
#: on a current tzdata, which is precisely the kind of quiet gap this module
#: exists to close.
_RENAMES = {
    "America/Godthab": "America/Nuuk",
    "Asia/Rangoon": "Asia/Yangon",
    "Asia/Calcutta": "Asia/Kolkata",
    "Asia/Saigon": "Asia/Ho_Chi_Minh",
    "Europe/Kiev": "Europe/Kyiv",
}


def _pick(name: str) -> str | None:
    """`name`, or whichever spelling of it this machine's tzdata has."""
    if _usable(name):
        return name
    alternative = _RENAMES.get(name) or next(
        (old for old, new in _RENAMES.items() if new == name), None
    )
    if alternative and _usable(alternative):
        return alternative
    return None


#: The subset this machine's zone database can actually resolve, keyed by a
#: normalised label. A stale entry should cost one unusable label, not an
#: exception in the middle of a push.
KNOWN = {
    label.casefold(): zone
    for label, zone in ((label, _pick(z)) for label, z in MAPPING.items())
    if zone
}

#: Canvas renders the label with its offset pair appended -- "Eastern Time
#: (US & Canada) (-05:00/-04:00)". `pull` strips that, but a hand-written or
#: hand-edited sheet may carry it, and the offsets are exactly the part that
#: makes a year-round label look season-specific. Dropped before matching.
_OFFSET_SUFFIX = re.compile(r"\s*\([+-]\d{2}:\d{2}(?:/[+-]\d{2}:\d{2})?\)\s*$")


def resolve_friendly(label: str | None) -> str | None:
    """An IANA id for one of Canvas's friendly labels, or None.

    None means "could not resolve", never "UTC" or any other default. The
    caller decides what to do with a label it cannot read; guessing a zone
    here would silently reinterpret every deadline in a file.
    """
    if not label:
        return None
    text = _OFFSET_SUFFIX.sub("", label.strip())
    if not text:
        return None

    found = KNOWN.get(text.casefold())
    if found:
        return found

    # A sheet may carry an IANA id in the friendly field -- someone hand-editing
    # the header is at least as likely to type America/New_York as Canvas's
    # wording. Accepting it costs nothing and is unambiguous.
    if _usable(text):
        return text
    return None
