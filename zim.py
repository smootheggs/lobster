"""🕷️ The Spider's offline library: reads Wikipedia straight from a Kiwix .zim file.

No server, no port, no network: the Spider opens a file on the tablet and reads articles
from it, so Shiba needs no exception and nothing new listens anywhere.

How it finds articles: the search words from the research plan are matched against the
file's article titles (and the alternative titles Wikipedia redirects from). Code does the
matching; the model never chooses a file or an article.

Safety for a file that could be damaged (or crafted): every offset is checked against the
file size, every block's unpacked size is capped, redirects are followed at most 5 times,
and anything odd is skipped instead of crashing research.

Format: openZIM version 5/6 (all integers little-endian). Blocks ("clusters") are stored
plain, xz-compressed (Python's own lzma) or zstd-compressed (Python 3.14's compression.zstd,
or the small `zstandard` package; pip install zstandard).
"""
import json
import lzma
import re
import struct
import time
import unicodedata
from collections import OrderedDict
from hashlib import sha256
from pathlib import Path

MAGIC = 0x044D495A
HEADER = struct.Struct("<IHH16sIIQQQQIIQ")          # 80 bytes
REDIRECT = 0xFFFF
MAX_CLUSTER = 64 * 1024 * 1024                      # unpacked bytes per block
MAX_ARTICLE = 4 * 1024 * 1024
MAX_ENTRIES = 20_000_000
ARTICLE_NAMESPACES = ("C", "A")                     # "C" in current files, "A" in older ones
FILE_NAME = re.compile(r"^wikipedia_([a-z]{2,3})_[a-z0-9_]+_\d{4}-\d{2}\.zim$")


# Words that don't say what an article is about ("who was the ..."); never matched on their own.
STOPWORDS = frozenset("""a an the of and or in on at to for from by with about as is was were are be been
who what when where why how which did does do tell me us please history list""".split())


class ZimError(Exception):
    pass


def _zstd(data: bytes, cap: int) -> bytes:
    try:
        from compression import zstd                     # Python 3.14+
        return zstd.ZstdDecompressor().decompress(data, max_length=cap + 1)
    except ImportError:
        pass
    try:
        import io
        import zstandard
    except ImportError:
        raise ZimError("this file needs zstd: pip install zstandard") from None
    with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)) as r:
        return r.read(cap + 1)


def _xz(data: bytes, cap: int) -> bytes:
    return lzma.LZMADecompressor().decompress(data, max_length=cap + 1)


DECOMPRESS = {4: _xz, 5: _zstd}


def norm(text: str) -> str:
    """Lowercase, no accents, no punctuation: 'Lübeck' and 'lubeck' match."""
    t = unicodedata.normalize("NFKD", text.casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w]+", " ", t.replace("_", " ")).split())


class Zim:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.f = open(self.path, "rb")
        self.size = self.path.stat().st_size
        raw = self.f.read(HEADER.size)
        if len(raw) < HEADER.size:
            raise ZimError("too short to be a ZIM file")
        (magic, self.major, self.minor, uuid, self.entries, self.clusters, self.url_ptr, self.title_ptr,
         self.cluster_ptr, self.mime_pos, self.main_page, _layout, self.checksum_pos) = HEADER.unpack(raw)
        if magic != MAGIC or self.major not in (5, 6):
            raise ZimError("not a ZIM file (or an unknown version)")
        if not 0 < self.entries <= MAX_ENTRIES or self.clusters <= 0:
            raise ZimError("implausible entry or block count")
        for pos, length in ((self.url_ptr, 8 * self.entries), (self.cluster_ptr, 8 * self.clusters),
                            (self.mime_pos, 1)):
            if pos + length > self.size:
                raise ZimError("damaged: a table points past the end of the file")
        self.uuid = uuid.hex()
        self.mimes = self._mimes()
        self._ptrs = struct.unpack(f"<{self.entries}Q", self._read(self.url_ptr, 8 * self.entries))
        self._cptrs = struct.unpack(f"<{self.clusters}Q", self._read(self.cluster_ptr, 8 * self.clusters))
        self._cache: OrderedDict[int, tuple[bytes, int]] = OrderedDict()   # the last few blocks
        self._index: dict | None = None
        self._words: dict | None = None
        m = FILE_NAME.match(self.path.name)
        self.lang = m.group(1) if m else ""
        self.copy = self.path.stem

    # --- low level -------------------------------------------------------------------
    def _read(self, pos: int, n: int) -> bytes:
        if pos < 0 or n < 0 or pos + n > self.size:
            raise ZimError("damaged: read past the end of the file")
        self.f.seek(pos)
        return self.f.read(n)

    def _mimes(self) -> list[str]:
        raw = self._read(self.mime_pos, min(65536, self.size - self.mime_pos))
        out = []
        for part in raw.split(b"\0"):
            if not part:
                break
            out.append(part.decode("utf-8", "replace"))
        return out

    def entry(self, i: int) -> dict:
        if not 0 <= i < self.entries:
            raise ZimError("entry number out of range")
        pos = self._ptrs[i]
        raw = self._read(pos, min(512, self.size - pos))          # names are short: read a little first
        if raw.count(b"\0", 12) < 2 and self.size - pos > 512:
            raw = self._read(pos, min(4096, self.size - pos))
        if len(raw) < 12:
            raise ZimError("damaged entry")
        mime, _plen, ns, _rev = struct.unpack_from("<HBcI", raw, 0)
        if mime == REDIRECT:
            (target,) = struct.unpack_from("<I", raw, 8)
            rest, e = raw[12:], {"redirect": target}
        else:
            if len(raw) < 16:
                raise ZimError("damaged entry")
            cluster, blob = struct.unpack_from("<II", raw, 8)
            rest, e = raw[16:], {"cluster": cluster, "blob": blob,
                                 "mime": self.mimes[mime] if mime < len(self.mimes) else ""}
        parts = rest.split(b"\0", 2)
        if len(parts) < 3:
            raise ZimError("damaged entry name")
        e["ns"] = ns.decode("latin-1")
        e["path"] = parts[0].decode("utf-8", "replace")
        e["title"] = parts[1].decode("utf-8", "replace") or e["path"]
        return e

    def _cluster(self, c: int) -> tuple[bytes, int]:
        """A block's unpacked bytes and its offset width (4, or 8 for very large blocks)."""
        if c in self._cache:
            self._cache.move_to_end(c)
            return self._cache[c]
        if not 0 <= c < self.clusters:
            raise ZimError("block number out of range")
        start = self._cptrs[c]
        end = self._cptrs[c + 1] if c + 1 < self.clusters else self.checksum_pos or self.size
        if not start < end <= self.size or end - start > MAX_CLUSTER:
            raise ZimError("damaged block position")
        raw = self._read(start, end - start)
        kind, extended = raw[0] & 0x0F, bool(raw[0] & 0x10)
        if kind in (0, 1):
            data = raw[1:]
        elif kind in DECOMPRESS:
            try:
                data = DECOMPRESS[kind](raw[1:], MAX_CLUSTER)
            except ZimError:
                raise
            except Exception as e:
                raise ZimError(f"block {c} won't unpack ({type(e).__name__})") from None
        else:
            raise ZimError(f"unsupported compression {kind}")
        if len(data) > MAX_CLUSTER:
            raise ZimError("block unpacks too large: refused")
        self._cache[c] = (data, 8 if extended else 4)
        while len(self._cache) > 4:
            self._cache.popitem(last=False)
        return self._cache[c]

    def blob(self, cluster: int, blob: int) -> bytes:
        data, w = self._cluster(cluster)
        fmt = "<Q" if w == 8 else "<I"
        if len(data) < w:
            raise ZimError("damaged block")
        (first,) = struct.unpack_from(fmt, data, 0)
        count = first // w - 1
        if first % w or not 0 <= blob < count or first > len(data):
            raise ZimError("damaged block table")
        a, b = struct.unpack_from(fmt, data, w * blob)[0], struct.unpack_from(fmt, data, w * (blob + 1))[0]
        if not first <= a <= b <= len(data):
            raise ZimError("damaged item position")
        return data[a:b]

    # --- articles -------------------------------------------------------------------
    def resolve(self, i: int) -> dict:
        """The article an entry stands for, following redirects (at most 5)."""
        for _ in range(6):
            e = self.entry(i)
            if "redirect" not in e:
                e["index"] = i
                return e
            i = e["redirect"]
        raise ZimError("too many redirects")

    def article(self, i: int) -> dict | None:
        e = self.resolve(i)
        if e["ns"] not in ARTICLE_NAMESPACES or not e["mime"].startswith("text/html"):
            return None
        html = self.blob(e["cluster"], e["blob"])
        if len(html) > MAX_ARTICLE:
            html = html[:MAX_ARTICLE]
        return {"title": e["title"], "path": e["path"], "html": html.decode("utf-8", "replace")}

    # --- finding articles by title -------------------------------------------------------
    def index(self, cache_dir: Path | None = None) -> dict:
        """{normalized title: entry number} for every article and redirect. Built once per file
        (a few seconds), then kept in a small cache next to Sloth Lobster's data."""
        if self._index is not None:
            return self._index
        cached = cache_dir / f"zim-{self.uuid}.json" if cache_dir else None
        if cached and cached.exists():
            try:
                self._index = json.loads(cached.read_text(encoding="utf-8"))
                return self._index
            except (OSError, ValueError):
                pass
        titles: dict[str, int] = {}
        for i in range(self.entries):
            try:
                e = self.entry(i)
            except ZimError:
                continue
            if e["ns"] not in ARTICLE_NAMESPACES:
                continue
            if "redirect" not in e and not e["mime"].startswith("text/html"):
                continue
            key = norm(e["title"])
            if key and (key not in titles or "redirect" not in e):
                titles[key] = i
        self._index = titles
        if cached:
            try:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(json.dumps(titles, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        return titles

    def search(self, phrase: str, k: int = 3, cache_dir: Path | None = None) -> list[int]:
        """Entry numbers whose titles best match the phrase, best first:
        1. the exact title  2. titles containing every word of the phrase (most general first)
        3. titles the phrase mentions whole, e.g. "Augustus" in "who was Augustus" (most specific first)
        4. otherwise the titles sharing the most words."""
        titles = self.index(cache_dir)
        if self._words is None:
            self._words = {t: frozenset(t.split()) - STOPWORDS for t in titles}
        q = norm(phrase)
        words = frozenset(q.split()) - STOPWORDS
        if not words:
            return []
        out: list[int] = []
        for exact in (q, " ".join(w for w in q.split() if w not in STOPWORDS)):
            if exact in titles:
                out.append(titles[exact])
        out += [titles[t] for t in sorted((t for t, tw in self._words.items() if tw and words <= tw),
                                          key=lambda t: (len(t), t))[: k * 3]]
        out += [titles[t] for t in sorted((t for t, tw in self._words.items() if tw and tw <= words),
                                          key=lambda t: (-len(self._words[t]), len(t)))[: k * 3]]
        if not out:
            scored = sorted(((len(words & tw), t) for t, tw in self._words.items() if words & tw),
                            key=lambda s: (-s[0], len(s[1])))
            out += [titles[t] for n, t in scored[:k] if n >= max(1, len(words) - 1)]
        seen, result = set(), []
        for i in out:
            try:
                target = self.resolve(i)["index"]
            except ZimError:
                continue
            if target not in seen:
                seen.add(target)
                result.append(target)
            if len(result) >= k:
                break
        return result


# --- The library: every Wikipedia file in the Kiwix folder ------------------------------

_open: dict[Path, Zim] = {}


def library(folder: Path) -> list[Zim]:
    """Open Wikipedia files named like Kiwix's downloads (wikipedia_<lang>_..._YYYY-MM.zim)."""
    if not folder or not Path(folder).is_dir():
        return []
    out = []
    for p in sorted(Path(folder).glob("*.zim")):
        if not FILE_NAME.match(p.name):
            continue
        if p not in _open:
            try:
                _open[p] = Zim(p)
            except (OSError, ZimError):
                continue
        out.append(_open[p])
    return out


def articles(folder: Path, phrases: list[str], limit: int, per_phrase: int = 3, should_stop=None,
             cache_dir: Path | None = None) -> dict:
    """Same shape as spider.wikipedia(): {"articles": [...], "failed": [...], "network_errors": 0}.
    Each article carries its HTML; the URL is the real Wikipedia address of the same page."""
    from urllib.parse import quote
    books, found, failed, seen = library(folder), [], [], set()
    if not books:
        return {"articles": [], "failed": ["no offline Wikipedia file"], "network_errors": 0}
    for phrase in phrases:
        for z in books:
            if len(found) >= limit or (should_stop and should_stop()):
                break
            try:
                hits = z.search(phrase, per_phrase, cache_dir)
            except ZimError as e:
                failed.append(f"{z.copy}: {e}")
                continue
            for i in hits:
                if len(found) >= limit or (z.path, i) in seen:
                    continue
                seen.add((z.path, i))
                try:
                    a = z.article(i)
                except ZimError as e:
                    failed.append(f"{z.copy}: {e}")
                    continue
                if not a:
                    continue
                found.append({"title": a["title"], "html": a["html"], "copy": z.copy,
                              "url": f"https://{z.lang or 'en'}.wikipedia.org/wiki/" + quote(a["path"].replace(" ", "_")),
                              "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                              "sha256": sha256(a["html"].encode()).hexdigest()})
    return {"articles": found, "failed": failed, "network_errors": 0}


if __name__ == "__main__":
    # A quick check on the tablet:  python zim.py "Hanseatic League"
    import sys
    import config
    t0 = time.monotonic()
    books = library(config.KIWIX_DIR)
    if not books:
        sys.exit(f"No Wikipedia .zim file in {config.KIWIX_DIR}")
    for z in books:
        print(f"📚 {z.path.name}: {z.entries:,} entries, {len(z.index(config.CACHE_DIR)):,} titles "
              f"(index ready in {time.monotonic() - t0:.1f} s)")
    phrase = " ".join(sys.argv[1:]) or "Hanseatic League"
    got = articles(config.KIWIX_DIR, [phrase], 3, cache_dir=config.CACHE_DIR)
    for a in got["articles"]:
        text = re.sub(r"<[^>]+>", " ", a["html"])
        print(f"\n🕷️ {a['title']}  ({a['url']})\n   {' '.join(text.split())[:300]}…")
    if not got["articles"]:
        print(f"Nothing found for {phrase!r}. {got['failed']}")
