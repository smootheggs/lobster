"""🦞 Exact answers by code: date math, a calculator, units and currency.

The model works out what you mean; code produces anything exact. (Home Assistant's voice
assistant works the same way.) A small model is unreliable at arithmetic and dates, so these
questions never reach it:

  "how many days until 25 July"      "what weekday is 13 August 2027"   "90 days from today"
  "15% of 240"   "(1200 - 150) * 1.08"   "10 km in miles"   "72°F in °C"   "100 USD in HKD"

Nothing here runs code from a message: arithmetic is read with Python's own syntax tree and
only numbers and + - * / ^ % and brackets are allowed, with limits on size.
"""
import ast
import math
import operator
import re
import time
from datetime import date, datetime, timedelta

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
          "october", "november", "december"]
MONTH_OF = {m: i + 1 for i, m in enumerate(MONTHS)} | {m[:3]: i + 1 for i, m in enumerate(MONTHS)} | {"sept": 9}


def _norm(text: str) -> str:
    t = text.lower().replace("’", "'").replace("−", "-").replace("–", "-").replace("×", "*").replace("÷", "/")
    t = re.sub(r"[?!]+", " ", t)
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)            # 1,200 -> 1200
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"[.,;:]+$", "", t).strip()


_LEAD = r"(?:(?:hey|so|ok|okay|and|please|quick question|lobster|sloth lobster),?\s+)*"


def _fmt_date(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1].title()} {d.year}"


def _num(x: float, digits: int = 4) -> str:
    """1134 -> "1,134"; 6.21371 -> "6.214"; 0.000123 -> "0.000123"."""
    if not math.isfinite(x):
        return str(x)
    if x == int(x) and abs(x) < 1e15:
        return f"{int(x):,}"
    if abs(x) >= 1:
        s = f"{x:,.{digits}f}".rstrip("0").rstrip(".")
    else:
        s = f"{x:.{digits}g}"
    return s


# --- Dates --------------------------------------------------------------------------------

HOLIDAYS = {"christmas": (12, 25), "christmas day": (12, 25), "christmas eve": (12, 24),
            "new year's eve": (12, 31), "new years eve": (12, 31), "new year": (1, 1),
            "new year's day": (1, 1), "new years day": (1, 1), "new year's": (1, 1),
            "halloween": (10, 31), "valentine's day": (2, 14), "valentines day": (2, 14),
            "independence day": (7, 4), "the fourth of july": (7, 4), "fourth of july": (7, 4)}


class Ambiguous(Exception):
    """A date that could be read two ways (03/04/2027)."""


def parse_date(text: str, today: date, direction: int = 1) -> date | None:
    """A date from words, or None. Without a year: the next one (direction 1) or the last one
    (direction -1). Raises Ambiguous for numeric dates that could be day/month or month/day."""
    t = re.sub(r"\b(the|on)\s+", "", text.strip().lower()).strip()
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", t).replace(",", " ")
    t = " ".join(t.split())
    t = re.sub(r"^(\d{1,2}) of ", r"\1 ", t)               # the 4th of july -> 4 july
    if t in ("today", "now"):
        return today
    if t == "tomorrow":
        return today + timedelta(days=1)
    if t == "yesterday":
        return today - timedelta(days=1)
    year = None
    m = re.fullmatch(r"(.+?)\s+(\d{4})", t)
    if m and m.group(1) in HOLIDAYS:
        t, year = m.group(1), int(m.group(2))
    if t in HOLIDAYS:
        month, day = HOLIDAYS[t]
        return _with_year(month, day, year, today, direction)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return _safe(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"(\d{1,2})\s+([a-z]+)(?:\s+(\d{4}))?", t)          # 25 july 2027
    if m and m.group(2) in MONTH_OF:
        return _with_year(MONTH_OF[m.group(2)], int(m.group(1)), int(m.group(3)) if m.group(3) else None, today, direction)
    m = re.fullmatch(r"([a-z]+)\s+(\d{1,2})(?:\s+(\d{4}))?", t)          # july 25 2027
    if m and m.group(1) in MONTH_OF:
        return _with_year(MONTH_OF[m.group(1)], int(m.group(2)), int(m.group(3)) if m.group(3) else None, today, direction)
    m = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})(?:[/.](\d{4}))?", t)       # 25/7/2027, 7/25/2027, 03/04/2027
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None
        if a > 12 and b <= 12:
            return _with_year(b, a, y, today, direction)
        if b > 12 and a <= 12:
            return _with_year(a, b, y, today, direction)
        if a == b:
            return _with_year(a, b, y, today, direction)
        raise Ambiguous(f"{a}/{b}")
    return None


def _safe(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _with_year(month: int, day: int, year: int | None, today: date, direction: int) -> date | None:
    if year is not None:
        return _safe(year, month, day)
    d = _safe(today.year, month, day)
    if d is None:                       # 29 February in a non-leap year: the next or last one that exists
        for k in range(1, 9):
            d = _safe(today.year + direction * k, month, day)
            if d:
                return d
        return None
    if direction > 0 and d < today:
        d = _safe(today.year + 1, month, day) or d
    if direction < 0 and d > today:
        d = _safe(today.year - 1, month, day) or d
    return d


_DATE = r"(?P<date>[a-z0-9' ,./-]{3,40})"
UNTIL = re.compile(rf"^{_LEAD}(?:how many days|how long|days|how many weeks|weeks)\s+(?:are there\s+|is it\s+|left\s+)?"
                   rf"(?:until|till|til|to|before)\s+{_DATE}(?:\s+from (?:today|now))?$")
SINCE = re.compile(rf"^{_LEAD}(?:how many days|how long|days|how many weeks|weeks)\s+(?:has it been\s+|is it\s+|have passed\s+)?"
                   rf"(?:since|from)\s+{_DATE}(?:\s+(?:until|till|to) (?:today|now))?$")
WEEKDAY = re.compile(rf"^{_LEAD}what\s+(?:day|weekday|day of the week)\s+(?:is|was|will be|falls on|does)\s+{_DATE}"
                     rf"(?:\s+(?:fall on|on|be))?$")
OFFSET = re.compile(rf"^{_LEAD}(?:what(?:'s| is| will be)?\s+(?:the\s+)?date\s+)?(?:(?:in|is)\s+)?(?P<n>\d{{1,5}})\s+"
                    rf"(?P<unit>days?|weeks?|months?|years?)\s+(?P<dir>from|after|before|ago)(?:\s+{_DATE})?$")
OFFSET_IN = re.compile(rf"^{_LEAD}what(?:'s| is| will be)?\s+(?:the\s+)?date\s+in\s+(?P<n>\d{{1,5}})\s+(?P<unit>days?|weeks?|months?|years?)$")
BETWEEN = re.compile(rf"^{_LEAD}(?:how many days|days)\s+(?:are there\s+)?between\s+(?P<a>[a-z0-9' ,./-]{{3,40}}?)\s+and\s+(?P<b>[a-z0-9' ,./-]{{3,40}})$")


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        got = _safe(year, month, day)
        if got:
            return got
    return d


def _span(days: int) -> str:
    weeks, rest = divmod(abs(days), 7)
    return f" ({weeks} week{'s' if weeks != 1 else ''}{f' and {rest} day' + ('s' if rest != 1 else '') if rest else ''})" if weeks else ""


def _no_such_date(text: str) -> str | None:
    words = set(re.findall(r"[a-z]+", text.lower()))
    if words & set(MONTH_OF) and re.search(r"\d", text):
        return f"🦞 There's no such date as “{text.strip()}”."
    return None


def date_answer(text: str, today: date | None = None) -> str | None:
    n = _norm(text)
    today = today or date.today()
    try:
        m = UNTIL.match(n)
        if m:
            d = parse_date(m.group("date"), today, 1)
            if d is None:
                return _no_such_date(m.group("date"))
            days = (d - today).days
            if days < 0:
                return f"🦞 {_fmt_date(d)} was {-days} day{'s' if days != -1 else ''} ago."
            return f"🦞 {days} day{'s' if days != 1 else ''} until {_fmt_date(d)}{_span(days)}."
        m = SINCE.match(n)
        if m:
            d = parse_date(m.group("date"), today, -1)
            if d is None:
                return _no_such_date(m.group("date"))
            days = (today - d).days
            if days < 0:
                return f"🦞 {_fmt_date(d)} is still {-days} day{'s' if days != -1 else ''} away."
            return f"🦞 {days} day{'s' if days != 1 else ''} since {_fmt_date(d)}{_span(days)}."
        m = WEEKDAY.match(n)
        if m:
            d = parse_date(m.group("date"), today, 1)
            if d is None:
                return _no_such_date(m.group("date"))
            verb = "was" if d < today else "is"
            return f"🦞 {d.day} {MONTHS[d.month - 1].title()} {d.year} {verb} a {WEEKDAYS[d.weekday()]}."
        m = BETWEEN.match(n)
        if m:
            a, b = parse_date(m.group("a"), today, 1), parse_date(m.group("b"), today, 1)
            if a is None or b is None:
                return None
            days = abs((b - a).days)
            return f"🦞 {days} day{'s' if days != 1 else ''} between {_fmt_date(min(a, b))} and {_fmt_date(max(a, b))}{_span(days)}."
        m = OFFSET.match(n) or OFFSET_IN.match(n)
        if m:
            amount = int(m.group("n"))
            back = m.groupdict().get("dir") in ("before", "ago")
            start = today
            if m.groupdict().get("date"):
                got = parse_date(m.group("date"), today, 1)
                if got is None:
                    return None
                start = got
            unit = m.group("unit").rstrip("s")
            if unit in ("day", "week"):
                d = start + timedelta(days=(amount * (7 if unit == "week" else 1)) * (-1 if back else 1))
            else:
                d = _add_months(start, amount * (12 if unit == "year" else 1) * (-1 if back else 1))
            if not date.min < d < date.max:
                return None
            when = "today" if start == today else _fmt_date(start)
            return (f"🦞 {amount} {m.group('unit')} {'before' if back else 'after'} {when}: {_fmt_date(d)}."
                    if m.groupdict().get("dir") != "ago" else f"🦞 {amount} {m.group('unit')} ago: {_fmt_date(d)}.")
    except Ambiguous as e:
        a, b = (int(x) for x in str(e).split("/"))
        return (f"🦞 Did you mean {b} {MONTHS[a - 1].title()} or {a} {MONTHS[b - 1].title()}? "
                f"Write it with the month's name and I'll count.")
    except (OverflowError, ValueError):
        return None
    return None


# --- Calculator -----------------------------------------------------------------------------

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.USub: operator.neg, ast.UAdd: operator.pos}
MAX_EXPR = 120


class Refuse(Exception):
    pass


def _eval(node, depth: int = 0) -> float:
    if depth > 30:
        raise Refuse("too deep")
    if isinstance(node, ast.Expression):
        return _eval(node.body, depth + 1)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand, depth + 1))
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        a, b = _eval(node.left, depth + 1), _eval(node.right, depth + 1)
        if isinstance(node.op, ast.Pow):
            if abs(b) > 100 or abs(a) > 1e6:
                raise Refuse("too big")
        if isinstance(node.op, (ast.Div, ast.Mod)) and b == 0:
            raise ZeroDivisionError
        out = _OPS[type(node.op)](a, b)
        if isinstance(out, complex) or (isinstance(out, (int, float)) and abs(out) > 1e30):
            raise Refuse("too big")
        return out
    raise Refuse("not arithmetic")


def calculate(expr: str) -> float:
    """Arithmetic only: numbers, + - * / ^ % and brackets. Raises Refuse or ZeroDivisionError."""
    if len(expr) > MAX_EXPR:
        raise Refuse("too long")
    e = expr.replace("^", "**")
    if not re.fullmatch(r"[0-9.+\-*/%() ]+", e):
        raise Refuse("not arithmetic")
    try:
        tree = ast.parse(e, mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        raise Refuse("not arithmetic")
    if sum(1 for _ in ast.walk(tree)) > 80:
        raise Refuse("too long")
    return _eval(tree)


CALC = re.compile(rf"^{_LEAD}(?:(?:what(?:'s| is)|whats|calculate|compute|work out|how much is)\s+)?(?P<expr>[0-9.+\-*/%^() x]+?)(?:\s*=)?$")
PERCENT = re.compile(rf"^{_LEAD}(?:(?:what(?:'s| is)|whats|calculate|how much is)\s+)?(?P<p>[0-9.]+)\s*(?:%|percent|per cent)\s+of\s+(?P<n>[0-9.]+)$")
WORDS_OPS = [(r"\bplus\b", "+"), (r"\bminus\b", "-"), (r"\btimes\b", "*"), (r"\bmultiplied by\b", "*"),
             (r"\bdivided by\b", "/"), (r"\bover\b", "/"), (r"\bto the power of\b", "^"), (r"\bsquared\b", "^2"),
             (r"\bcubed\b", "^3")]
NOT_MATH = {"9/11", "24/7", "50/50", "1/2", "3/4"}     # read as names or phrases, not sums (1/2, 3/4: fractions, fine either way)


def calc_answer(text: str) -> str | None:
    n = _norm(text)
    for pat, op in WORDS_OPS:
        n = re.sub(pat, op, n)
    m = PERCENT.match(n)
    try:
        if m:
            p, x = float(m.group("p")), float(m.group("n"))
            return f"🦞 {_num(p)}% of {_num(x)} = {_num(p / 100 * x)}"
        m = CALC.match(n)
        if not m:
            return None
        expr = re.sub(r"(?<=[\d)])\s*x\s*(?=[\d(])", "*", m.group("expr")).strip()
        if "x" in expr or not re.search(r"\d", expr) or not re.search(r"[0-9)]\s*[+\-*/%^]\s*[-(0-9.]", expr):
            return None                                   # needs at least one operation between numbers
        if expr.replace(" ", "") in NOT_MATH - {"1/2", "3/4"} or re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", expr.replace(" ", "")):
            return None                                   # 9/11, 24/7, 7/4/1776: not sums
        value = calculate(expr)
    except ZeroDivisionError:
        return "🦞 That divides by zero, so there's no answer."
    except Refuse as e:
        return "🦞 That number would be far too big to work out here." if str(e) == "too big" else None
    except (OverflowError, ValueError):
        return "🦞 That number would be far too big to work out here."
    shown = expr.replace("**", "^").replace("*", " × ").replace("/", " ÷ ")
    shown = re.sub(r"\s+", " ", re.sub(r"\s*([+\-%^])\s*", r" \1 ", shown)).replace("( ", "(").replace(" )", ")").strip()
    shown = re.sub(r"(^|[(×÷+^] )- (\d)", r"\1-\2", shown)
    return f"🦞 {shown} = {_num(value, 6)}"


# --- Units --------------------------------------------------------------------------------
# (dimension, factor to the base unit). Base units: m, kg, l, m/s, m², byte, second.

UNITS = {}


def _add(dim: str, factor: float, *names: str) -> None:
    for name in names:
        UNITS[name] = (dim, factor)


_add("length", 0.001, "mm", "millimeter", "millimeters", "millimetre", "millimetres")
_add("length", 0.01, "cm", "centimeter", "centimeters", "centimetre", "centimetres")
_add("length", 1.0, "m", "meter", "meters", "metre", "metres")
_add("length", 1000.0, "km", "kilometer", "kilometers", "kilometre", "kilometres")
_add("length", 0.0254, "in", "inch", "inches", '"')
_add("length", 0.3048, "ft", "foot", "feet", "'")
_add("length", 0.9144, "yd", "yard", "yards")
_add("length", 1609.344, "mi", "mile", "miles")
_add("length", 1852.0, "nautical mile", "nautical miles", "nmi")
_add("weight", 0.000001, "mg", "milligram", "milligrams")
_add("weight", 0.001, "g", "gram", "grams", "gramme", "grammes")
_add("weight", 1.0, "kg", "kilo", "kilos", "kilogram", "kilograms", "kilogramme")
_add("weight", 0.45359237, "lb", "lbs", "pound", "pounds")
_add("weight", 0.028349523125, "oz", "ounce", "ounces")
_add("weight", 6.35029318, "stone", "stones", "st")
_add("weight", 1000.0, "tonne", "tonnes", "metric ton", "metric tons", "t")
_add("volume", 0.001, "ml", "milliliter", "milliliters", "millilitre", "millilitres")
_add("volume", 0.01, "cl", "centiliter", "centiliters", "centilitre", "centilitres")
_add("volume", 1.0, "l", "liter", "liters", "litre", "litres")
_add("volume", 0.0295735295625, "fl oz", "fluid ounce", "fluid ounces", "floz")
_add("volume", 0.2365882365, "cup", "cups")
_add("volume", 0.01478676478125, "tbsp", "tablespoon", "tablespoons")
_add("volume", 0.00492892159375, "tsp", "teaspoon", "teaspoons")
_add("volume", 0.473176473, "pint", "pints", "pt")
_add("volume", 0.946352946, "quart", "quarts", "qt")
_add("volume", 3.785411784, "gallon", "gallons", "gal")
_add("speed", 1 / 3.6, "km/h", "kmh", "kph", "kilometers per hour", "kilometres per hour")
_add("speed", 0.44704, "mph", "miles per hour")
_add("speed", 1.0, "m/s", "meters per second", "metres per second")
_add("speed", 0.514444, "knot", "knots", "kn")
_add("area", 1.0, "m2", "m²", "sq m", "square meter", "square meters", "square metre", "square metres")
_add("area", 1e6, "km2", "km²", "sq km", "square kilometer", "square kilometers", "square kilometre", "square kilometres")
_add("area", 0.09290304, "ft2", "ft²", "sq ft", "square foot", "square feet")
_add("area", 2589988.110336, "mi2", "mi²", "sq mi", "square mile", "square miles")
_add("area", 4046.8564224, "acre", "acres")
_add("area", 10000.0, "ha", "hectare", "hectares")
_add("data", 1.0, "byte", "bytes", "b")
_add("data", 1e3, "kb", "kilobyte", "kilobytes")
_add("data", 1e6, "mb", "megabyte", "megabytes")
_add("data", 1e9, "gb", "gigabyte", "gigabytes")
_add("data", 1e12, "tb", "terabyte", "terabytes")
_add("data", 1024.0, "kib")
_add("data", 1024.0 ** 2, "mib")
_add("data", 1024.0 ** 3, "gib")
_add("time", 1.0, "s", "sec", "secs", "second", "seconds")
_add("time", 60.0, "min", "mins", "minute", "minutes")
_add("time", 3600.0, "h", "hr", "hrs", "hour", "hours")
_add("time", 86400.0, "day", "days")
_add("time", 604800.0, "week", "weeks")
TEMPS = {"c": "°C", "°c": "°C", "celsius": "°C", "degrees c": "°C", "degrees celsius": "°C", "centigrade": "°C",
         "f": "°F", "°f": "°F", "fahrenheit": "°F", "degrees f": "°F", "degrees fahrenheit": "°F",
         "k": "K", "kelvin": "K"}
AMBIGUOUS = {"ton": "a US ton (907 kg) or a metric tonne (1,000 kg)", "tons": "US tons (907 kg) or metric tonnes (1,000 kg)",
             "gallon (uk)": None}
SHOWN = {"km": "km", "mi": "miles", "mile": "miles", "miles": "miles", "m": "m", "ft": "ft", "in": "inches",
         "kg": "kg", "lb": "lb", "lbs": "lb", "g": "g", "oz": "oz", "l": "litres", "ml": "ml"}

_UNIT = r"(?P<{name}>(?:°\s?)?[a-z²/ ']{{1,22}}?|\"|')"
CONVERT = re.compile(
    rf"^{_LEAD}(?:(?:convert|what(?:'s| is)|whats|how much is|how many [a-z/² ]+ (?:is|are|in))\s+)?"
    rf"(?P<n>-?[0-9]+(?:\.[0-9]+)?)\s*{_UNIT.format(name='a')}\s+(?:in|to|into|as)\s+{_UNIT.format(name='b')}$")
HOW_MANY = re.compile(rf"^{_LEAD}how many\s+{_UNIT.format(name='b')}\s+(?:is|are|in|make)\s+(?P<n>-?[0-9]+(?:\.[0-9]+)?)\s*"
                      rf"{_UNIT.format(name='a')}$")


def _unit(name: str, other_dim: str | None = None):
    n = " ".join(name.strip().replace("° ", "°").split())
    n = re.sub(r"^degrees? ", "degrees ", n) if n.startswith("degree") else n
    if n in TEMPS:
        return ("temperature", TEMPS[n])
    if n in ("oz", "ounce", "ounces") and other_dim == "volume":
        return UNITS["fl oz"]
    if n in UNITS:
        return UNITS[n]
    if n.endswith("s") and n[:-1] in UNITS:
        return UNITS[n[:-1]]
    return None


def _temp(x: float, a: str, b: str) -> float:
    c = x if a == "°C" else (x - 32) * 5 / 9 if a == "°F" else x - 273.15
    return c if b == "°C" else c * 9 / 5 + 32 if b == "°F" else c + 273.15


def unit_answer(text: str) -> str | None:
    n = _norm(text)
    m = CONVERT.match(n) or HOW_MANY.match(n)
    if not m:
        return None
    a_name, b_name = m.group("a").strip(), m.group("b").strip()
    for name in (a_name, b_name):
        if name in AMBIGUOUS and AMBIGUOUS[name]:
            return f"🦞 By “{name}”, do you mean {AMBIGUOUS[name]}? Write “tonnes” or “US tons” and I'll convert."
    b = _unit(b_name)
    a = _unit(a_name, b[0] if b else None)
    if b is None or a is None:
        return None                                       # not units we know (maybe currency): let others try
    if b_name in ("oz", "ounce", "ounces") and a[0] == "volume":
        b = UNITS["fl oz"]
    x = float(m.group("n"))
    if a[0] != b[0]:
        return f"🦞 {a_name} measures {a[0]} and {b_name} measures {b[0]}, so one can't be turned into the other."
    if a[0] == "temperature":
        out = _temp(x, a[1], b[1])
        return f"🦞 {_num(x)}{a[1]} = {_num(round(out, 1))}{b[1]}"
    out = x * a[1] / b[1]
    note = " (US measures)" if a[0] == "volume" and {a_name, b_name} & {"cup", "cups", "pint", "pints", "quart", "quarts",
                                                                      "gallon", "gallons", "gal", "pt", "qt", "fl oz",
                                                                      "fluid ounce", "fluid ounces", "oz", "ounces", "ounce",
                                                                      "tbsp", "tsp"} else ""
    if a[0] == "data":
        a_name, b_name = a_name.upper().replace("IB", "iB"), b_name.upper().replace("IB", "iB")
    shown = _num(out, 2) if abs(out) >= 1 else _num(out, 3)
    return f"🦞 {_num(x)} {a_name} = {shown} {b_name}{note}"


# --- Currency -----------------------------------------------------------------------------

CURRENCIES = {
    "usd": "USD", "us dollar": "USD", "us dollars": "USD", "dollar": "USD", "dollars": "USD", "bucks": "USD", "$": "USD",
    "eur": "EUR", "euro": "EUR", "euros": "EUR", "€": "EUR",
    "hkd": "HKD", "hk dollar": "HKD", "hk dollars": "HKD", "hong kong dollar": "HKD", "hong kong dollars": "HKD", "hk$": "HKD",
    "cny": "CNY", "rmb": "CNY", "yuan": "CNY", "renminbi": "CNY", "chinese yuan": "CNY",
    "jpy": "JPY", "yen": "JPY", "japanese yen": "JPY", "¥": "JPY",
    "gbp": "GBP", "pound sterling": "GBP", "pounds sterling": "GBP", "british pound": "GBP", "british pounds": "GBP",
    "quid": "GBP", "£": "GBP", "pound": "GBP", "pounds": "GBP",
    "chf": "CHF", "swiss franc": "CHF", "swiss francs": "CHF", "franc": "CHF", "francs": "CHF",
    "cad": "CAD", "canadian dollar": "CAD", "canadian dollars": "CAD",
    "aud": "AUD", "australian dollar": "AUD", "australian dollars": "AUD",
    "nzd": "NZD", "sgd": "SGD", "singapore dollar": "SGD", "singapore dollars": "SGD",
    "krw": "KRW", "won": "KRW", "korean won": "KRW", "inr": "INR", "rupee": "INR", "rupees": "INR",
    "sek": "SEK", "swedish krona": "SEK", "nok": "NOK", "norwegian krone": "NOK", "dkk": "DKK", "danish krone": "DKK",
    "pln": "PLN", "zloty": "PLN", "czk": "CZK", "koruna": "CZK", "huf": "HUF", "forint": "HUF",
    "try": "TRY", "lira": "TRY", "turkish lira": "TRY", "mxn": "MXN", "mexican peso": "MXN", "mexican pesos": "MXN",
    "brl": "BRL", "real": "BRL", "reais": "BRL", "zar": "ZAR", "rand": "ZAR", "thb": "THB", "baht": "THB",
    "idr": "IDR", "rupiah": "IDR", "myr": "MYR", "ringgit": "MYR", "php": "PHP", "philippine peso": "PHP",
    "ils": "ILS", "shekel": "ILS", "shekels": "ILS", "isk": "ISK", "ron": "RON", "bgn": "BGN",
    "twd": "TWD", "nt$": "TWD", "taiwan dollar": "TWD", "taiwan dollars": "TWD", "new taiwan dollars": "TWD",
    "vnd": "VND", "dong": "VND", "aed": "AED", "dirham": "AED", "dirhams": "AED", "sar": "SAR", "riyal": "SAR",
    "rub": "RUB", "ruble": "RUB", "rubles": "RUB", "rouble": "RUB", "roubles": "RUB", "mop": "MOP", "pataca": "MOP",
}
ASSUMED = {"dollar": "US dollars", "dollars": "US dollars", "bucks": "US dollars", "$": "US dollars",
           "pound": "British pounds", "pounds": "British pounds", "franc": "Swiss francs", "francs": "Swiss francs"}
_CUR = r"(?P<{name}>[a-z$€£¥ ]{{1,22}}?)"
MONEY = re.compile(
    rf"^{_LEAD}(?:(?:convert|what(?:'s| is)|whats|how much is|how much are)\s+)?"
    rf"(?:(?P<sym>[$€£¥])\s?)?(?P<n>[0-9]+(?:\.[0-9]+)?)\s*(?:{_CUR.format(name='a')})?\s+(?:in|to|into|as)\s+{_CUR.format(name='b')}$")


def currency_answer(text: str, rates_source=None) -> str | None:
    n = _norm(text)
    m = MONEY.match(n)
    if not m:
        return None
    a_name = (m.group("a") or m.group("sym") or "").strip()
    b_name = m.group("b").strip()
    a, b = CURRENCIES.get(a_name), CURRENCIES.get(b_name)
    if not a or not b:
        return None
    if a_name in ("pound", "pounds") and _unit(b_name) is not None:
        return None                                       # "pounds in kg": weight, not money
    if rates_source is None:
        return None
    data = rates_source()
    if not data:
        return "🦞 I don't have exchange rates yet. Ask again when the tablet is online."
    rates = data["rates"]
    if a not in rates or b not in rates:
        missing = a if a not in rates else b
        return f"🦞 The European Central Bank doesn't publish a rate for {missing}, so I can't convert it."
    x = float(m.group("n"))
    out = x / rates[a] * rates[b]
    try:
        when = datetime.strptime(data["date"], "%Y-%m-%d").date()
        day = f"{when.day} {MONTHS[when.month - 1].title()} {when.year}"
    except (KeyError, ValueError):
        day = "the last known date"
    assumed = [ASSUMED[x] for x in (a_name, b_name) if x in ASSUMED]
    note = f" Taking {' and '.join(dict.fromkeys(assumed))} as meant." if assumed else ""
    return f"🦞 {x:,.2f} {a} = {out:,.2f} {b} (European Central Bank rate of {day}).{note}"


# --- All of them ------------------------------------------------------------------------

def answer(text: str, rates_source=None, today: date | None = None) -> tuple[str, str] | None:
    """(kind, reply) for a question code can answer exactly, else None."""
    if len(text) > 200:
        return None
    for kind, fn in (("date", lambda t: date_answer(t, today)), ("currency", lambda t: currency_answer(t, rates_source)),
                     ("units", unit_answer), ("calc", calc_answer)):
        reply = fn(text)
        if reply:
            return kind, reply
    return None
