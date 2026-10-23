"""Test helper: writes small but real-format ZIM files (openZIM v6), so the reader is tested
against the documented layout. Used only by test_lobster.py; never by Sloth Lobster itself."""
import hashlib
import lzma
import struct
from pathlib import Path

MIMES = ["text/html", "image/png"]


def _cluster(blobs: list[bytes], kind: int, extended: bool) -> bytes:
    w = 8 if extended else 4
    fmt = "<Q" if extended else "<I"
    offsets, pos = [], w * (len(blobs) + 1)
    for b in blobs:
        offsets.append(pos)
        pos += len(b)
    offsets.append(pos)
    payload = b"".join(struct.pack(fmt, o) for o in offsets) + b"".join(blobs)
    if kind == 4:
        payload = lzma.compress(payload, format=lzma.FORMAT_XZ)
    elif kind == 5:
        payload = ZSTD_FAKE_PREFIX + lzma.compress(payload, format=lzma.FORMAT_XZ)
    return bytes([kind | (0x10 if extended else 0)]) + payload


ZSTD_FAKE_PREFIX = b"FAKEZSTD"      # the tests swap in a stand-in decoder for zstd blocks


def make_zim(path: Path, articles: dict[str, str], redirects: dict[str, str] | None = None,
             kind: int = 1, extended: bool = False, image: bool = True, raw_cluster: bytes | None = None) -> Path:
    """articles: {title: html}; redirects: {alternative title: target title}."""
    redirects = redirects or {}
    entries = []                                    # (ns, path, title, kind, payload)
    for title, html in articles.items():
        entries.append(("C", title.replace(" ", "_"), title, "html", html.encode()))
    for alt, target in redirects.items():
        entries.append(("C", alt.replace(" ", "_"), alt, "redirect", target.replace(" ", "_")))
    if image:
        entries.append(("C", "logo.png", "logo.png", "image", b"\x89PNG fake"))
    entries.append(("M", "Title", "Title", "meta", b"Test Wikipedia"))
    entries.sort(key=lambda e: e[0] + e[1])
    index_of = {(e[0], e[1]): i for i, e in enumerate(entries)}
    html_blobs, other_blobs, where = [], [], {}
    for e in entries:
        if e[3] == "html":
            where[(e[0], e[1])] = (0, len(html_blobs))
            html_blobs.append(e[4])
        elif e[3] in ("image", "meta"):
            where[(e[0], e[1])] = (1, len(other_blobs))
            other_blobs.append(e[4])
    clusters = [raw_cluster if raw_cluster is not None else _cluster(html_blobs, kind, extended),
                _cluster(other_blobs, 1, False)]
    dirents = []
    for e in entries:
        tail = e[1].encode() + b"\0" + (e[2].encode() if e[2] != e[1] else b"") + b"\0"
        if e[3] == "redirect":
            dirents.append(struct.pack("<HBcII", 0xFFFF, 0, e[0].encode(), 0, index_of[("C", e[4])]) + tail)
        else:
            c, b = where[(e[0], e[1])]
            mime = 0 if e[3] == "html" else 1
            dirents.append(struct.pack("<HBcIII", mime, 0, e[0].encode(), 0, c, b) + tail)
    mime_list = b"".join(m.encode() + b"\0" for m in MIMES) + b"\0"
    n = len(entries)
    mime_pos = 80
    url_ptr = mime_pos + len(mime_list)
    title_ptr = url_ptr + 8 * n
    dirent_pos = title_ptr + 4 * n
    ptrs, pos = [], dirent_pos
    for d in dirents:
        ptrs.append(pos)
        pos += len(d)
    cluster_ptr = pos
    cpos, cptrs = cluster_ptr + 8 * len(clusters), []
    for c in clusters:
        cptrs.append(cpos)
        cpos += len(c)
    checksum_pos = cpos
    by_title = sorted(range(n), key=lambda i: entries[i][0] + entries[i][2])
    header = struct.pack("<IHH16sIIQQQQIIQ", 0x044D495A, 6, 1, b"0123456789abcdef", n, len(clusters),
                         url_ptr, title_ptr, cluster_ptr, mime_pos, 0xFFFFFFFF, 0xFFFFFFFF, checksum_pos)
    body = (header + mime_list + b"".join(struct.pack("<Q", p) for p in ptrs)
            + b"".join(struct.pack("<I", i) for i in by_title) + b"".join(dirents)
            + b"".join(struct.pack("<Q", p) for p in cptrs) + b"".join(clusters))
    assert len(body) == checksum_pos
    path.write_bytes(body + hashlib.md5(body).digest())
    return path
