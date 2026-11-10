"""Extra guide sources: Cablecast public access stations and the sources
registered in the iptv-org guide index (i.mjh.nz files and listing sites)."""
import json
import os
import re
import signal
import subprocess
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape, quoteattr

UA = {"User-Agent": "Mozilla/5.0 (tv-guide builder)"}
GRAB_SITES = ["tvpassport.com", "tvguide.com", "mi.tv", "gatotv.com", "watchyour.tv",
              "distro.tv", "maxstream.tv", "shahid.mbc.net", "sat.tv", "tvprofil.com", "xumo.tv"]
GRAB_MINUTES = 18
CC_URLS = {}    # channel id -> current stream address, when it moved
CC_STATE = {}   # channel id -> [station, station channel number]
STOP = {"tv", "channel", "network", "the", "hd", "sd", "fhd", "us", "usa", "live", "by", "on",
        "and", "television"}


def loose(s):
    """Channel name with filler words removed, for looser matching."""
    s = s.lower().replace("&amp;", "and").replace("&", " and ")
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"['.!]", "", s)
    return "".join(w for w in re.split(r"[^a-z0-9]+", s) if w and w not in STOP)


def get_json(url, timeout=15):
    if url.startswith("file://"):
        with open(url[7:]) as f:
            return json.load(f)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def load_state(repo):
    """What the last build learned about each public access channel."""
    if not repo:
        return {}
    try:
        return get_json("https://raw.githubusercontent.com/%s/dist/state.json" % repo, 20)
    except Exception:  # noqa: BLE001
        return {}


def save_state(path):
    with open(path, "w") as f:
        json.dump(CC_STATE, f, sort_keys=True)


def alive(url):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200 and b"#EXT" in r.read(2000)
    except Exception:  # noqa: BLE001
        return False


def repair(alts, log):
    """alts: {channel id: (current link, self-updating link)}. Swaps a link
    only when the current one is dead and the other one answers."""
    def check(item):
        cid, (cur, alt) = item
        if alive(cur) or not alive(alt):
            return None
        return cid, alt
    with ThreadPoolExecutor(16) as pool:
        fixed = dict(r for r in pool.map(check, alts.items()) if r)
    log("link check: %d channels have a self-updating link, %d swapped in" % (len(alts), len(fixed)))
    return fixed


def prog(cid, start, stop, title, fmt_time):
    return (start, "<programme start=%s stop=%s channel=%s><title>%s</title></programme>"
            % (quoteattr(fmt_time(start)), quoteattr(fmt_time(stop)), quoteattr(cid), escape(title)))


def cablecast(chans, guide, win_start, win_end, fmt_time, log, state=None):
    """Stations on Cablecast publish their schedule through a standard API.
    The playlist stream path (/live-N/) tells us which station channel it is."""
    found, stations = {}, {}
    for c in chans:
        if c["id"] in guide:
            continue
        m = re.search(r"//reflect-([^./]+)\.cablecast\.tv(/live-\d+/)", c.get("url", ""))
        if m:
            stations.setdefault(m.group(1), []).append((c["id"], m.group(2), c["url"]))
    day = timedelta(days=1)
    for slug, wanted in stations.items():
        streams, base = [], None
        for host in ("reflect-%s.cablecast.tv" % slug, slug + ".cablecast.tv"):
            base = "https://%s/cablecastapi/v1/" % host
            try:
                streams = get_json(base + "livestreams", 10).get("liveStreams") or []
            except Exception:  # noqa: BLE001
                streams = []
            if streams:
                break
        if not streams:
            continue
        for cid, seg, url in wanted:
            chan = None
            for s in streams:
                if seg in (s.get("streamUrl") or s.get("url") or ""):
                    chan = s.get("channel")
            if chan is None:
                # The stream moved. Find it again by the station channel number we saved.
                old = (state or {}).get(cid)
                if old and old[0] == slug:
                    for s in streams:
                        m = re.search(r"/live-\d+/", s.get("streamUrl") or s.get("url") or "")
                        if s.get("channel") == old[1] and m:
                            chan = old[1]
                            CC_URLS[cid] = re.sub(r"/live-\d+/", m.group(0), url, count=1)
            if chan is None:
                continue
            CC_STATE[cid] = [slug, chan]
            try:
                data = get_json(
                    "%sscheduleitems?channel=%s&start=%s&end=%s&include=show&page_size=1000"
                    % (base, chan, (win_start - day).strftime("%Y-%m-%d"),
                       (win_end + day).strftime("%Y-%m-%d")), 30)
            except Exception as e:  # noqa: BLE001
                log("cablecast", slug, "failed:", e)
                continue
            titles = {s.get("id"): (s.get("cgTitle") or s.get("title") or "").strip()
                      for s in data.get("shows") or []}
            runs = []
            for it in data.get("scheduleItems") or []:
                if it.get("deleted"):
                    continue
                try:
                    t = datetime.fromisoformat(it["runDateTime"]).astimezone(timezone.utc)
                except Exception:  # noqa: BLE001
                    continue
                runs.append((t, titles.get(it.get("show")) or "Scheduled program"))
            runs.sort()
            progs = []
            for i, (t, title) in enumerate(runs):
                end = runs[i + 1][0] if i + 1 < len(runs) else t + timedelta(minutes=30)
                end = min(end, t + timedelta(hours=6))
                if end > t and end > win_start and t < win_end:
                    progs.append(prog(cid, t, end, title, fmt_time))
            if progs:
                found[cid] = progs
    return found


def guide_rows(chans, guide, url):
    """Rows of the iptv-org guide index for channels that still have no listings."""
    need = {c["id"] for c in chans if c["id"] not in guide}
    rows = {}
    for r in get_json(url, 180):
        ch = r.get("channel")
        if ch in need and r.get("site") and r.get("site_id"):
            rows.setdefault(ch, []).append(r)
    return rows


def mjh_files(rows):
    """i.mjh.nz files named in the index: [(path, {their channel id: our id})]."""
    files = {}
    for ch, rs in rows.items():
        for r in rs:
            if r["site"] == "i.mjh.nz" and "#" in r["site_id"]:
                path, sid = r["site_id"].split("#", 1)
                files.setdefault(path + ".xml", {})[sid] = ch
    return sorted(files.items(), key=lambda kv: (not kv[0].endswith("/us.xml"), kv[0]))


def grab(rows, chans, guide, epg_dir, log):
    """Runs the iptv-org grabber for channels that only a listing site covers.
    Returns the path of the XMLTV file it wrote, or None."""
    if not epg_dir or not os.path.isdir(epg_dir):
        log("listing sites skipped: grabber not installed")
        return None
    names = {c["id"]: c["name"] for c in chans}
    lines, per_site = [], {}
    for ch, rs in sorted(rows.items()):
        if ch in guide:
            continue
        best = None
        for site in GRAB_SITES:
            best = next((r for r in rs if r["site"] == site), None)
            if best:
                break
        if not best:
            continue
        per_site[best["site"]] = per_site.get(best["site"], 0) + 1
        lines.append("  <channel site=%s lang=%s xmltv_id=%s site_id=%s>%s</channel>"
                     % (quoteattr(best["site"]), quoteattr(best.get("lang") or "en"),
                        quoteattr(ch), quoteattr(best["site_id"]), escape(names.get(ch, ch))))
    if not lines:
        return None
    log("listing sites to grab:", per_site)
    tmp = tempfile.mkdtemp()
    cfile, out = os.path.join(tmp, "custom.channels.xml"), os.path.join(tmp, "grab.xml")
    with open(cfile, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<channels>\n%s\n</channels>\n' % "\n".join(lines))
    cmd = ["npm", "run", "grab", "---", "--channels=" + cfile, "--output=" + out,
           "--days=2", "--maxConnections=3", "--timeout=10000"]
    p = subprocess.Popen(cmd, cwd=epg_dir, start_new_session=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        tail, _ = p.communicate(timeout=GRAB_MINUTES * 60)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        tail, _ = p.communicate()
        log("grabber stopped at the time limit")
    log("\n".join((tail or "").splitlines()[-30:]))
    return out if os.path.exists(out) and os.path.getsize(out) > 200 else None
