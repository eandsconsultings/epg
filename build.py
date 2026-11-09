#!/usr/bin/env python3
"""Builds one small XMLTV guide that matches the FreeTV US playlist.

Sources, in priority order:
  1. Pluto TV guide (matched by the Pluto code inside each stream link)
  2. IPTV-EPG.org US guide (matched by channel id)
  3. Samsung TV Plus / Plex / Roku guides (matched by channel name)
  4. Filler blocks for channels with no published schedule

Usage: python build.py <output-dir>
"""
import gzip
import io
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape, quoteattr

PLAYLIST = os.environ.get("PLAYLIST_URL") or (
    "https://freetv.fuckyouthisismywebsite.com/api/playlist.m3u?country=US"
    "&category=animation,auto,business,classic,comedy,cooking,culture,education,"
    "documentary,entertainment,family,general,interactive,kids,legislative,"
    "lifestyle,music,movies,news,outdoor,public,religious,relax,science,series,"
    "sports,shop,travel,weather"
)
MJH = os.environ.get("MJH_BASE", "https://i.mjh.nz/")
PLUTO = MJH + "PlutoTV/all.xml"
IPTV_EPG = os.environ.get("IPTV_EPG_URL", "https://iptv-epg.org/files/epg-us.xml.gz")
NAME_FEEDS = [
    "SamsungTVPlus/us.xml",
    "Plex/us.xml",
    "Roku/all.xml",
    "SamsungTVPlus/all.xml",
    "Plex/all.xml",
]
HOURS_BACK = 3
HOURS_AHEAD = 48
UA = {"User-Agent": "Mozilla/5.0 (tv-guide builder)"}

NOW = datetime.now(timezone.utc)
WIN_START = NOW - timedelta(hours=HOURS_BACK)
WIN_END = NOW + timedelta(hours=HOURS_AHEAD)


def log(*a):
    print(*a, flush=True)


def open_url(url, tries=3):
    last = None
    for i in range(tries):
        try:
            if url.startswith("file://"):
                raw = open(url[7:], "rb")
            else:
                raw = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120)
            head = raw.read(2)
            stream = io.BufferedReader(_Prefixed(head, raw), 1 << 20)
            if head == b"\x1f\x8b":
                return gzip.GzipFile(fileobj=stream)
            return stream
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5 * (i + 1))
    raise last


class _Prefixed(io.RawIOBase):
    """Puts the two sniffed bytes back in front of a stream."""

    def __init__(self, head, raw):
        self.head, self.raw = head, raw

    def readable(self):
        return True

    def readinto(self, b):
        if self.head:
            n = min(len(b), len(self.head))
            b[:n] = self.head[:n]
            self.head = self.head[n:]
            return n
        data = self.raw.read(len(b))
        b[: len(data)] = data
        return len(data)


def norm(s):
    s = s.lower().replace("&amp;", "and").replace("&", "and")
    s = re.sub(r"\(.*?\)|\[.*?\]", "", s)
    s = re.sub(r"\b(hd|fhd|sd|uhd|4k)\b", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


def parse_time(v):
    m = re.match(r"(\d{14})\s*([+-]\d{4})?", v or "")
    if not m:
        return None
    dt = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    off = m.group(2) or "+0000"
    delta = timedelta(hours=int(off[1:3]), minutes=int(off[3:5]))
    if off[0] == "-":
        delta = -delta
    return (dt - delta).replace(tzinfo=timezone.utc)


def fmt_time(dt):
    return dt.strftime("%Y%m%d%H%M%S +0000")


def load_playlist():
    text = open_url(PLAYLIST).read().decode("utf-8", "replace")
    chans, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            def g(k):
                m = re.search(k + r'="([^"]*)"', line)
                return m.group(1) if m else ""
            cur = {
                "id": g("tvg-id"),
                "name": g("tvg-name") or line.rsplit(",", 1)[-1],
                "group": g("group-title"),
                "logo": g("tvg-logo"),
            }
        elif line and not line.startswith("#") and cur:
            cur["url"] = line
            if cur["id"]:
                chans.append(cur)
            cur = None
    return chans


def scan(url, want):
    """Streams an XMLTV file. want(channel_id, display_name) returns the
    playlist id to file a channel under, or None to skip it.
    Returns {playlist_id: [programme xml, ...]}."""
    out, mapping, names = {}, {}, {}
    src = open_url(url)
    ctx = ET.iterparse(src, events=("start", "end"))
    _, root = next(ctx)
    for ev, el in ctx:
        if ev != "end":
            continue
        if el.tag == "channel":
            names[el.get("id")] = el.findtext("display-name") or ""
            root.clear()
        elif el.tag == "programme":
            cid = el.get("channel")
            if cid not in mapping:
                mapping[cid] = want(cid, names.get(cid, ""))
            target = mapping[cid]
            if target:
                start, stop = parse_time(el.get("start")), parse_time(el.get("stop"))
                if start and stop and stop > WIN_START and start < WIN_END:
                    el.set("channel", target)
                    el.set("start", fmt_time(start))
                    el.set("stop", fmt_time(stop))
                    el.tail = None
                    out.setdefault(target, []).append(
                        (start, ET.tostring(el, encoding="unicode"))
                    )
            root.clear()
    return out


def filler(ch):
    blocks = []
    t = WIN_START.replace(minute=0, second=0, microsecond=0)
    t -= timedelta(hours=t.hour % 6)
    desc = "%s channel. No published schedule for this channel." % (ch["group"] or "Live")
    while t < WIN_END:
        e = t + timedelta(hours=6)
        blocks.append(
            (t, "<programme start=%s stop=%s channel=%s><title>%s</title><desc>%s</desc></programme>"
             % (quoteattr(fmt_time(t)), quoteattr(fmt_time(e)), quoteattr(ch["id"]),
                escape(ch["name"]), escape(desc)))
        )
        t = e
    return blocks


def main(outdir):
    os.makedirs(outdir, exist_ok=True)
    chans = load_playlist()
    ids = {c["id"] for c in chans}
    log("playlist channels:", len(chans))
    if len(chans) < 100:
        raise SystemExit("playlist looks broken, refusing to publish")

    guide, source, report = {}, {}, []

    def take(found, label):
        n = 0
        for cid, progs in found.items():
            if cid not in guide and progs:
                guide[cid] = progs
                source[cid] = label
                n += 1
        report.append("%-28s %d" % (label, n))
        log(label, "->", n)

    # 1. Pluto, by the code in the stream link
    pluto = {}
    for c in chans:
        m = re.search(r"plu-([0-9a-f]{8,})", c.get("url", ""), re.I)
        if m:
            pluto[m.group(1).lower()] = c["id"]
    try:
        take(scan(PLUTO, lambda cid, nm: pluto.get((cid or "").lower())), "pluto")
    except Exception as e:  # noqa: BLE001
        log("pluto failed:", e)

    # 2. IPTV-EPG.org, by channel id
    try:
        take(scan(IPTV_EPG, lambda cid, nm: cid if cid in ids and cid not in guide else None), "iptv-epg")
    except Exception as e:  # noqa: BLE001
        log("iptv-epg failed:", e)

    # 3. Other free-streaming guides, by channel name
    for feed in NAME_FEEDS:
        by_name = {}
        for c in chans:
            if c["id"] not in guide:
                k = norm(c["name"])
                if len(k) >= 3:
                    by_name.setdefault(k, c["id"])
        claimed = set()

        def want(cid, nm, by_name=by_name, claimed=claimed):
            t = by_name.get(norm(nm))
            if t and t not in claimed:
                claimed.add(t)
                return t
            return None

        try:
            take(scan(MJH + feed, want), feed)
        except Exception as e:  # noqa: BLE001
            log(feed, "failed:", e)

    real = len(guide)
    if real < 50:
        raise SystemExit("almost no listings found, refusing to publish")

    # 4. Filler
    for c in chans:
        if c["id"] not in guide:
            guide[c["id"]] = filler(c)
            source[c["id"]] = "filler"

    path = os.path.join(outdir, "guide.xml.gz")
    total = 0
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="tv-guide">\n')
        for c in chans:
            f.write("<channel id=%s><display-name>%s</display-name>" % (quoteattr(c["id"]), escape(c["name"])))
            if c["logo"]:
                f.write("<icon src=%s/>" % quoteattr(c["logo"]))
            f.write("</channel>\n")
        for c in chans:
            for _, xml in sorted(guide[c["id"]], key=lambda p: p[0]):
                f.write(xml)
                f.write("\n")
                total += 1
        f.write("</tv>\n")

    with open(os.path.join(outdir, "report.txt"), "w") as f:
        f.write("built %s UTC\n" % NOW.strftime("%Y-%m-%d %H:%M"))
        f.write("channels %d, real listings %d, filler %d, programmes %d\n\n"
                % (len(chans), real, len(chans) - real, total))
        f.write("\n".join(report) + "\n\n")
        for c in chans:
            f.write("%s\t%s\t%s\n" % (source[c["id"]], c["id"], c["name"]))
    log("real listings: %d of %d, programmes: %d, size: %.1f MB"
        % (real, len(chans), total, os.path.getsize(path) / 1e6))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "out")
