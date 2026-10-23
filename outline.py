"""🐘 Outline-first research: plan the questions a good answer needs, then fill them.

Practices borrowed from research agents that work (see the deep-research design doc):
  - outline before writing (Stanford STORM), seeded from the overview article's own headings,
    so a small model only picks and merges sub-questions instead of inventing an outline
  - effort budgets set by code, never by the model (Anthropic's research system)
  - code, not the model, decides which sections are still thin (local-deep-researcher asks the
    model to reflect; small models do that badly)
  - every sentence the model writes is checked against the facts it cites (a citation pass)

The model only ever supplies words: sub-questions and search phrases, cleaned here. It never
picks a URL, a domain, a file or an article.
"""
import json
import re

import numpy as np

import ollama
import shiba
from config import CHAT_KEEP_ALIVE

# --- Budgets (code decides how much work a job gets) --------------------------------------
QUICK = {"sections": (2, 3), "searches": 1, "per_section": 2, "articles": 6, "points": 3, "sentences": 2,
         "candidates": 6, "rounds": 1}
DEEP = {"sections": (3, 5), "searches": 2, "per_section": 3, "articles": 12, "points": 4, "sentences": 3,
        "candidates": 10, "rounds": 3}         # 1 broad round + at most 2 gap rounds
MIN_FACTS = 3            # a section with fewer good facts is thin
DUPLICATE = 0.92         # two facts this close say the same thing: keep one

BOILERPLATE = {"see also", "references", "notes", "external links", "further reading", "bibliography",
               "sources", "citations", "gallery", "footnotes", "works cited", "explanatory notes",
               "notes and references", "literature", "primary sources", "secondary sources", "general sources"}
HEAD2 = re.compile(r"^\s*==\s*([^=].*?)\s*==\s*$")


def headings(text: str, limit: int = 15) -> list[str]:
    """The overview article's top-level section headings (== History ==), minus reference lists."""
    out = []
    for line in (text or "").split("\n"):
        m = HEAD2.match(line)
        if m:
            h = " ".join(m.group(1).split())[:60]
            if h.lower() not in BOILERPLATE and h not in out and re.search(r"[A-Za-z]", h):
                out.append(h)
    return out[:limit]


def clean_phrase(text: str) -> str:
    """Letters, digits, spaces, apostrophes and hyphens only; no links survive this."""
    text = re.sub(r"https?://\S+|www\.\S+", " ", text)
    text = re.sub(r"[^\w\s'\-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:80]


def clean_question(text: str) -> str:
    """A sub-question as plain words: no links, no markup, one line, at most 120 characters.
    Returns "" if what's left isn't a usable question (too short, or reads like an instruction)."""
    if not isinstance(text, str):
        return ""
    text = re.sub(r"^\s*(?:\d+[.)]|[-•*])\s*", "", text)          # "1." or "-" in front of a typed line
    text = re.sub(r"https?://\S+|www\.\S+", " ", text)
    text = re.sub(r"[^\w\s'’\-,:;()?]", " ", text)
    text = " ".join(text.split())[:120].strip(" ,;:")
    if len(text) < 6 or len(re.findall(r"[^\W\d_]{2,}", text)) < 2:
        return ""
    if shiba.fact_problem(f"{text} is one part of this research question."):
        return ""
    return text[:1].upper() + text[1:]


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


# --- 1. Plan --------------------------------------------------------------------------------

PLAN_SYSTEM = """You plan research for a personal knowledge base.
Split the owner's question into {lo} to {hi} sub-questions that together answer it, in the
order a reader would want them (background first, then the heart of the question, then what
followed). Each sub-question is short and answerable from an encyclopedia. The section
headings of the main encyclopedia article are given as hints: use them where they fit, but
the sub-questions must stay strictly on the owner's question. For each sub-question give
{n} short encyclopedia search phrase(s) of 2 to 6 words. Set needs_news to true only if the
question is about recent or current events. Return JSON only."""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "sections": {"type": "array", "items": {
            "type": "object",
            "properties": {"question": {"type": "string"},
                           "searches": {"type": "array", "items": {"type": "string"}}},
            "required": ["question", "searches"]}},
        "needs_news": {"type": "boolean"},
    },
    "required": ["sections", "needs_news"],
}


def fallback_plan(question: str) -> dict:
    """The model is away or unhelpful: one section, the question itself."""
    return {"sections": [{"question": clean_question(question) or question[:120],
                          "searches": [clean_phrase(question)]}], "needs_news": False}


def plan(question: str, heads: list[str], budget: dict, model: str) -> dict:
    """{"sections": [{"question", "searches"}], "needs_news"}. Everything the model returns is
    cleaned and capped here; if nothing usable comes back, the question itself is the plan."""
    lo, hi = budget["sections"]
    system = PLAN_SYSTEM.format(lo=lo, hi=hi, n=budget["searches"])
    hints = ("\nSection headings of the main article: " + "; ".join(heads)) if heads else ""
    try:
        reply = ollama.chat(model, [{"role": "system", "content": system},
                                    {"role": "user", "content": f"Question: {question}{hints}"}],
                            schema=PLAN_SCHEMA, keep_alive=CHAT_KEEP_ALIVE, purpose="plan")
        data = json.loads(reply)
    except Exception:
        return fallback_plan(question)
    sections, seen_q, seen_s = [], set(), set()
    for s in data.get("sections") or []:
        if not isinstance(s, dict):
            continue
        q = clean_question(s.get("question"))
        if not q or _norm(q) in seen_q:
            continue
        searches = []
        for p in s.get("searches") or []:
            p = clean_phrase(p) if isinstance(p, str) else ""
            if len(p) >= 3 and _norm(p) not in seen_s:
                seen_s.add(_norm(p))
                searches.append(p)
        if not searches:                       # no usable phrase: search the sub-question's own words
            p = clean_phrase(q)
            if _norm(p) in seen_s:
                continue
            seen_s.add(_norm(p))
            searches = [p]
        seen_q.add(_norm(q))
        sections.append({"question": q, "searches": searches[:budget["searches"]]})
        if len(sections) >= hi:
            break
    if not sections:
        return fallback_plan(question)
    return {"sections": sections, "needs_news": data.get("needs_news") is True}


def owner_plan(lines: list[str], limit: int = 6) -> list[dict]:
    """Sub-questions typed by the owner (one per line). Searches come from their own words, by code."""
    out, seen = [], set()
    for line in lines:
        q = clean_question(line)
        if q and _norm(q) not in seen:
            seen.add(_norm(q))
            out.append({"question": q, "searches": [clean_phrase(q)]})
    return out[:limit]


# --- 2. Gaps: new search words for thin sections only ------------------------------------------

GAP_SYSTEM = """You help a researcher fill one thin section of a brief.
Given the section's question and the searches already tried, give 2 NEW short encyclopedia
search phrases (2 to 6 words) likely to find articles that answer it. Different words from
the ones tried. Return JSON only."""

GAP_SCHEMA = {"type": "object", "properties": {"searches": {"type": "array", "items": {"type": "string"}}},
              "required": ["searches"]}


def gap_searches(section_question: str, tried: list[str], model: str, n: int = 2) -> list[str]:
    """Fresh phrases for a thin section. Never repeats a tried phrase and never pads with filler
    ("tell me more about ..."): no new phrase means the section stays thin and says so."""
    done = {_norm(t) for t in tried}
    out = []
    try:
        reply = ollama.chat(model, [{"role": "system", "content": GAP_SYSTEM},
                                    {"role": "user", "content": f"Section question: {section_question}\n"
                                                                f"Already tried: {'; '.join(tried) or 'nothing'}"}],
                            schema=GAP_SCHEMA, keep_alive=CHAT_KEEP_ALIVE, purpose="plan")
        phrases = json.loads(reply).get("searches") or []
    except Exception:
        phrases = []
    for p in list(phrases) + [section_question]:     # the sub-question's own words, if not tried yet
        p = clean_phrase(p) if isinstance(p, str) else ""
        if len(p) >= 3 and _norm(p) not in done and re.search(r"[^\W\d_]{3,}", p):
            done.add(_norm(p))
            out.append(p)
        if len(out) >= n:
            break
    return out


# --- 3. Assign facts to sections, and measure coverage (code only) -----------------------------

WORD = re.compile(r"[^\W\d_]{3,}|\d+")
_STOP = set("the and for with from that this what when where which who whom why how was were are did does "
            "into over about after before during its their his her".split())


def _words(text: str) -> set[str]:
    return {w for w in WORD.findall(text.lower()) if w not in _STOP}


def _overlap_matrix(questions: list[str], texts: list[str]) -> np.ndarray:
    qs = [_words(q) for q in questions]
    return np.asarray([[len(q & _words(t)) / (len(q) or 1) for q in qs] for t in texts], dtype=np.float32)


def assign(questions: list[str], facts: list[dict], vecs: dict, qvecs: np.ndarray | None,
           per_section: int) -> list[list[tuple[int, float]]]:
    """For each section, the facts that answer it best: [(fact index, score)], best first.
    Each fact goes to the one section it fits best; near-duplicates are dropped."""
    n = len(questions)
    if not facts or not n:
        return [[] for _ in range(n)]
    use_vecs = qvecs is not None and all(f["id"] in vecs for f in facts) and \
        all(vecs[f["id"]].shape[0] == qvecs.shape[1] for f in facts)
    if use_vecs:
        m = np.stack([vecs[f["id"]] for f in facts])
        scores = m @ qvecs.T
    else:
        m = None
        scores = _overlap_matrix(questions, [f["text"] for f in facts])
    best_of = scores.argmax(axis=1)
    out: list[list[tuple[int, float]]] = []
    for s in range(n):
        mine = sorted((i for i in range(len(facts)) if best_of[i] == s), key=lambda i: -scores[i, s])
        kept: list[int] = []
        for i in mine:
            if m is not None:
                if any(float(m[i] @ m[j]) >= DUPLICATE for j in kept):
                    continue
            else:
                wi = _words(facts[i]["text"])
                if any(len(wi & _words(facts[j]["text"])) / (len(wi | _words(facts[j]["text"])) or 1) >= 0.8
                       for j in kept):
                    continue
            kept.append(i)
            if len(kept) >= per_section:
                break
        out.append([(i, float(scores[i, s])) for i in kept])
    return out


def coverage(assigned: list[list[tuple[int, float]]], facts: list[dict], need_sources: int = 1) -> list[dict]:
    """Per section: {"good": n, "sources": n, "thin": bool}. Scale-free on purpose: "good" is
    measured against the best-answered section, so it works with any embedding model."""
    best_overall = max((sc for a in assigned for _, sc in a), default=0.0)
    out = []
    for a in assigned:
        best_here = max((sc for _, sc in a), default=0.0)
        bar = max(0.75 * best_here, 0.6 * best_overall)
        good = [i for i, sc in a if sc >= bar and sc > 0]
        sources = {facts[i]["source"] for i in good}
        thin = (len(good) < MIN_FACTS or len(sources) < need_sources
                or best_here < 0.7 * best_overall or best_here <= 0)
        out.append({"good": len(good), "sources": len(sources), "thin": thin})
    return out


# --- 4. Check what the model wrote (a citation pass, plain code) --------------------------------

OPENERS = {"however", "meanwhile", "later", "earlier", "eventually", "today", "also", "moreover", "thus",
           "despite", "although", "because", "since", "following", "by", "over", "between", "among", "after",
           "before", "during", "while", "when", "in", "on", "at", "the", "a", "an", "it", "its", "this", "these",
           "they", "their", "as", "from", "with", "under", "through", "throughout", "around", "until", "within"}


def verify(sentence: str, cited: list[str], allowed: str = "") -> str | None:
    """Why this written sentence isn't backed by the facts it cites, or None if it is.
    Every number must appear in a cited fact; every name in a cited fact (or the question)."""
    s = " ".join((sentence or "").split())
    if not 20 <= len(s) <= 300:
        return "wrong length"
    if shiba.URL_IN_TEXT.search(s):
        return "has a link"
    if not cited:
        return "cites nothing"
    problem = shiba.fact_problem(s)
    if problem:
        return problem
    first, _, rest = s.partition(" ")
    if first.lower().strip(",") in OPENERS:             # "However," / "In 1669" aren't names
        s = first.lower() + " " + rest
    joined = " ".join(cited)
    return shiba.grounding_problem(s, joined, joined + " " + (allowed or ""))
