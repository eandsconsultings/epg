"""Extra guide sources: Cablecast public access stations and the sources
registered in the iptv-org guide index (i.mjh.nz files and listing sites)."""
import json
import os
import re
import signal
import subprocess
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape, quoteattr

UA = {"User-Agent": "Mozilla/5.0 (tv-guide builder)"}
GRAB_SITES = ["tvpassport.com", "tvguide.com", "mi.tv", "gatotv.com"]
GRAB_MINUTES = 18


def get_json(url, timeout=15):
    if url.startswith("file://"):
        with open(url[7:]) as f:
            return json.load(f)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def prog(cid, start, stop, title, fmt_time):
    return (start, "<programme start=%s stop=%s channel=%s><title>%s</title></programme>"
            % (quoteattr(fmt_time(start)), quoteattr(fmt_time(stop)), quoteattr(cid), escape(title)))


def cablecast(chans, guide, win_start, win_end, fmt_time, log):
    """Stations on Cablecast publish their schedule through a standard API.
    The playlist stream path (/live-N/) tells us which station channel it is."""
    found, stations = {}, {}
    for c in chans:
        if c["id"] in guide:
            continue
        m = re.search(r"//reflect-([^./]+)\.cablecast\.tv(/live-\d+/)", c.get("url", ""))
        if m:
            stations.setdefault(m.group(1), []).append((c["id"], m.group(2)))
    day = timedelta(days=1)
    for slug, wanted in stations.items():
        base = "https://%s.cablecast.tv/cablecastapi/v1/" % slug
        try:
            streams = get_json(base + "livestreams", 10).get("liveStreams") or []
        except Exception:  # noqa: BLE001
            continue
        for cid, seg in wanted:
            chan = None
            for s in streams:
                if seg in (s.get("streamUrl") or s.get("url") or ""):
                    chan = s.get("channel")
            if chan is None:
                continue
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
