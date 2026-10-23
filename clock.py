"""🦞 The clock: time and date questions are answered by code, never by the model.

A small model has no clock and is bad at time-zone arithmetic, so "what time is it in Hong
Kong?" is matched here and worked out with Python's own time-zone database (daylight saving
included). Places come from the time-zone database itself (Hong Kong, Tokyo, Berlin, ...),
a short list of other cities and countries, and, for anything else while online, the same
map service the weather uses (Open-Meteo), which knows each place's time zone. Lookups are
remembered on the tablet, so each place is looked up at most once.
"""
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta

try:
    import zoneinfo
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones
except ImportError:                                          # very old Python: no time zones
    ZoneInfo = None


def _find_tzdata() -> None:
    """Some systems keep time-zone files where Python may not look; add the places that exist.
    (On Termux there are none: `pip install tzdata` gives Python its own copy, found automatically.)"""
    if ZoneInfo is None:
        return
    prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    extra = [p for p in (f"{prefix}/share/zoneinfo", "/usr/share/zoneinfo", "/usr/lib/zoneinfo")
             if os.path.isdir(p) and p not in zoneinfo.TZPATH]
    if extra:
        zoneinfo.reset_tzpath(to=tuple(zoneinfo.TZPATH) + tuple(extra))


_find_tzdata()

# Cities and countries that aren't time-zone names themselves.
ALIASES = {
    "hk": "Asia/Hong_Kong", "kowloon": "Asia/Hong_Kong",
    "beijing": "Asia/Shanghai", "shenzhen": "Asia/Shanghai", "guangzhou": "Asia/Shanghai",
    "china": "Asia/Shanghai", "mainland china": "Asia/Shanghai",
    "taiwan": "Asia/Taipei", "japan": "Asia/Tokyo", "osaka": "Asia/Tokyo", "kyoto": "Asia/Tokyo",
    "korea": "Asia/Seoul", "south korea": "Asia/Seoul", "india": "Asia/Kolkata", "mumbai": "Asia/Kolkata",
    "delhi": "Asia/Kolkata", "new delhi": "Asia/Kolkata", "bangalore": "Asia/Kolkata", "bengaluru": "Asia/Kolkata",
    "vietnam": "Asia/Ho_Chi_Minh", "hanoi": "Asia/Bangkok", "thailand": "Asia/Bangkok",
    "germany": "Europe/Berlin", "frankfurt": "Europe/Berlin", "munich": "Europe/Berlin", "hamburg": "Europe/Berlin",
    "cologne": "Europe/Berlin", "düsseldorf": "Europe/Berlin", "dusseldorf": "Europe/Berlin", "stuttgart": "Europe/Berlin",
    "hanau": "Europe/Berlin", "france": "Europe/Paris", "italy": "Europe/Rome", "milan": "Europe/Rome",
    "spain": "Europe/Madrid", "barcelona": "Europe/Madrid", "uk": "Europe/London", "england": "Europe/London",
    "britain": "Europe/London", "scotland": "Europe/London", "edinburgh": "Europe/London",
    "netherlands": "Europe/Amsterdam", "switzerland": "Europe/Zurich", "geneva": "Europe/Zurich",
    "austria": "Europe/Vienna", "poland": "Europe/Warsaw", "sweden": "Europe/Stockholm", "norway": "Europe/Oslo",
    "denmark": "Europe/Copenhagen", "belgium": "Europe/Brussels", "ireland": "Europe/Dublin",
    "russia": "Europe/Moscow", "turkey": "Europe/Istanbul", "israel": "Asia/Jerusalem",
    "uae": "Asia/Dubai", "abu dhabi": "Asia/Dubai", "australia": "Australia/Sydney", "melbourne": "Australia/Melbourne",
    "new zealand": "Pacific/Auckland", "wellington": "Pacific/Auckland",
    "san francisco": "America/Los_Angeles", "sf": "America/Los_Angeles", "palo alto": "America/Los_Angeles",
    "oakland": "America/Los_Angeles", "san jose": "America/Los_Angeles", "seattle": "America/Los_Angeles",
    "california": "America/Los_Angeles", "la": "America/Los_Angeles", "portland": "America/Los_Angeles",
    "las vegas": "America/Los_Angeles",
    "houston": "America/Chicago", "dallas": "America/Chicago", "austin": "America/Chicago", "texas": "America/Chicago",
    "san antonio": "America/Chicago", "new orleans": "America/Chicago",
    "nyc": "America/New_York", "new york city": "America/New_York", "boston": "America/New_York",
    "washington": "America/New_York", "washington dc": "America/New_York", "dc": "America/New_York",
    "miami": "America/New_York", "atlanta": "America/New_York", "philadelphia": "America/New_York",
    "denver": "America/Denver", "salt lake city": "America/Denver", "hawaii": "Pacific/Honolulu",
    "honolulu": "Pacific/Honolulu", "brazil": "America/Sao_Paulo", "rio de janeiro": "America/Sao_Paulo",
    "rio": "America/Sao_Paulo", "utc": "UTC", "gmt": "UTC",
}
HERE = {"", "here", "my time", "local", "my place", "home", "this tablet"}
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]

_names: dict[str, str] = {}


def available() -> bool:
    """True when this Python has a time-zone database (Termux: pip install tzdata if not)."""
    if ZoneInfo is None:
        return False
    try:
        ZoneInfo("Asia/Hong_Kong")
        return True
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False


def _zone_names() -> dict[str, str]:
    """{"hong kong": "Asia/Hong_Kong", ...} from the time-zone database's own city names."""
    if not _names and ZoneInfo is not None:
        try:
            keys = available_timezones()
        except Exception:
            keys = set()
        for key in keys:
            if "/" in key and not key.startswith(("Etc/", "SystemV/", "US/", "posix/", "right/")):
                _names.setdefault(key.rsplit("/", 1)[1].replace("_", " ").lower(), key)
    return _names


def _norm(text: str) -> str:
    text = re.sub(r"[?!.,;:]+", " ", text.lower().replace("’", "'"))
    return " ".join(text.split())


# "what time is it in hong kong", "what's the time in Tokyo right now", "time in berlin",
# "current time in sf", "what time is it", "what's the date today", "what day is it"
_LEAD = r"(?:(?:hey|so|ok|okay|and|quick question|please|lobster|sloth lobster)\s+)*"
_TAIL = r"(?:\s+(?:right now|now|currently|today|at the moment|please))*"
TIME_Q = re.compile(
    rf"^{_LEAD}(?:(?:what(?:'s| is)?|whats|tell me)\s+(?:the\s+)?(?:current\s+|local\s+)?time(?:\s+is\s+it)?"
    rf"|what\s+time\s+is\s+it|(?:the\s+)?(?:current|local)\s+time|time(?:\s+now)?)"
    rf"(?:\s+(?:is\s+it\s+)?(?:in|at|for)\s+(?P<place>.+?))?{_TAIL}$")
DATE_Q = re.compile(
    rf"^{_LEAD}(?:what(?:'s| is)?|whats)\s+(?:(?:the|today's|todays)\s+)?(?:date|day)(?:\s+is\s+it)?"
    rf"(?:\s+(?:is\s+it\s+)?(?:in|at|for)\s+(?P<place>.+?))?{_TAIL}$")
PLACE_OK = re.compile(r"^[^\W\d_][\w .'()-]{0,59}$")


def match(text: str) -> tuple[str, str] | None:
    """("time" | "date", place) for a time or date question, else None. Place "" means here."""
    n = _norm(text)
    for kind, rx in (("time", TIME_Q), ("date", DATE_Q)):
        m = rx.match(n)
        if m:
            place = (m.group("place") or "").strip()
            place = re.sub(r"^(?:the\s+)", "", place)
            if place and (not PLACE_OK.match(place) or re.search(r"\b(when|if|while|after|before)\b", place)):
                return None                          # "...when it's 9 here": not a plain question
            return kind, place
    return None


def zone_for(place: str, lookup=None) -> tuple[str, str] | None:
    """(time-zone key, display name) for a place, or None. lookup(place) -> key | None is the
    online fallback (the Spider's map service); it's only asked for places not known here."""
    p = _norm(place)
    if p in HERE:
        return "", "here"
    names = _zone_names()
    key = None
    words = p.split()
    for n in range(len(words), 0, -1):          # "frankfurt am main" -> "frankfurt"
        for cand in {" ".join(words[:n]), _fold(" ".join(words[:n]))}:
            key = key or ALIASES.get(cand) or names.get(cand) or names.get(cand.removesuffix(" city"))
        if key:
            break
    if key is None and lookup is not None:
        key = lookup(place.strip())
    if key is None:
        return None
    try:
        ZoneInfo(key)
    except Exception:
        return None
    pretty = SHORT.get(p) or " ".join(w if w in ("of", "de", "am", "an", "la") else w[:1].upper() + w[1:] for w in p.split())
    return key, pretty


SHORT = {"hk": "Hong Kong", "sf": "San Francisco", "la": "Los Angeles", "nyc": "New York City",
         "dc": "Washington, D.C.", "uk": "the UK", "uae": "the UAE", "utc": "UTC", "gmt": "GMT"}


def _fold(text: str) -> str:
    """zürich -> zurich, são paulo -> sao paulo (the time-zone names are plain letters)."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _clock(dt: datetime) -> str:
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def _day(dt: datetime) -> str:
    return f"{WEEKDAYS[dt.weekday()]} {dt.day} {MONTHS[dt.month - 1]}"


def _offset(dt: datetime) -> str:
    off = dt.utcoffset() or timedelta(0)
    mins = int(off.total_seconds() // 60)
    sign = "+" if mins >= 0 else "-"
    h, m = divmod(abs(mins), 60)
    return f"UTC{sign}{h}" + (f":{m:02d}" if m else "")


def _abbr(dt: datetime) -> str:
    name = dt.tzname() or ""
    return name if re.fullmatch(r"[A-Z]{2,5}", name) else ""


def _difference(there: datetime, here: datetime) -> str:
    diff = int(((there.utcoffset() or timedelta(0)) - (here.utcoffset() or timedelta(0))).total_seconds() // 60)
    if diff == 0:
        return "Same time as you."
    h, m = divmod(abs(diff), 60)
    span = (f"{h} hour{'s' if h != 1 else ''}" if h else "") + (f" {m} minutes" if m else "")
    return f"That's {span.strip()} {'ahead of' if diff > 0 else 'behind'} you."


def answer(kind: str, place: str, now: float | None = None, lookup=None) -> str:
    """The reply to a time or date question, worked out by code."""
    if not available():
        return ("🦞 I can't tell the time anywhere else yet: this tablet has no time-zone data. "
                "In Termux: pip install tzdata")
    stamp = time.time() if now is None else now
    here = datetime.fromtimestamp(stamp).astimezone()
    zone = zone_for(place, lookup)
    if zone is None:
        return (f"🦞 I don't know the time zone of “{place}”. Try the nearest big city"
                + (", or ask again when the tablet is online." if lookup is None else "."))
    key, pretty = zone
    there = here if not key else datetime.fromtimestamp(stamp, ZoneInfo(key))
    label = ", ".join(x for x in (_abbr(there), _offset(there)) if x)
    if kind == "date":
        where = "" if not key else f" in {pretty}"
        return f"🦞 Today{where} is {_day(there)} {there.year}."
    if not key:
        return f"🦞 It's {_clock(there)}, {_day(there)} ({label})."
    return f"🦞 In {pretty} it's {_clock(there)}, {_day(there)} ({label}). {_difference(there, here)}"


def today_line(now: float | None = None) -> str:
    """For the model's instructions: the date only, so they change once a day (keeps replies fast)."""
    d = datetime.fromtimestamp(time.time() if now is None else now).astimezone()
    return f"Today is {_day(d)} {d.year}."


def now_note(now: float | None = None) -> str:
    """Added to your latest message for the model: the tablet's time and zone right now."""
    d = datetime.fromtimestamp(time.time() if now is None else now).astimezone()
    label = ", ".join(x for x in (_abbr(d), _offset(d)) if x)
    return f"(The tablet's clock: {_clock(d)}, {_day(d)} {d.year}, {label}.)"


CLOCK_IN_REPLY = re.compile(r"\b\d{1,2}[:.]\d{2}\b|\b\d{1,2}\s*(?:am|pm|a\.m\.|p\.m\.)", re.I)


def model_guessed_time(question: str, reply: str) -> bool:
    """Backstop: the question was about the time and the model's reply states a time anyway."""
    q = _norm(question)
    about_now = re.search(r"\btime\b", q) and not re.search(r"\b(did|was|were|will|does|do|open|opens|close|closes|start|starts|leave|leaves)\b", q)
    return bool(about_now) and bool(CLOCK_IN_REPLY.search(reply))
