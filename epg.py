#!/usr/bin/env python3
# Copyright (C) 2026 Justin, G8YTZ / EI3IOB
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""epg.py - the programme guide, from Bitfocus Companion's triggers.

The repeater's playout is run by Companion triggers, so the schedule
already exists there. A trigger whose name starts with "EPG:" (or "EPG "
then the title, any case) is a programme:

    EPG: Philips PM5644 Test Card          60 minutes
    EPG: Film Night (2h)                   2 hours
    EPG: Repeater News (30m)               30 minutes

Its "time of day" events give the start and the days. The title is the
name without the marker and the length; the start is put on the minute
(a trigger at 00:00:20 is a programme at 00:00); a programme never runs
into the next one. Disabled triggers and disabled events are not
programmes. Everything else in Companion is ignored.

Companion 5.1 serves its triggers at /int/export/triggers/all, the same
address its own Export button uses. It is not a documented API, so the
full export is tried next, and if neither answers the last programmes
fetched are kept (see lynx_tx.py), never a blank guide.

The guide goes out through TSDuck's eitinject (p/f and 8 days of
schedule), which reads the XML written here and takes "now" from the
stream's own TDT (tsprio.py).

From the shell:  python3 epg.py 192.168.40.103:8000      (what it finds)
                 python3 epg.py export.companionconfig    (the same, from a file)
"""
from __future__ import annotations

import datetime
import gzip
import json
import re
import sys
import urllib.request
from xml.sax.saxutils import escape

MARK = re.compile(r"^\s*EPG(?::|\s)\s*(.+?)\s*$", re.I)
LENGTH = re.compile(r"\s*\(\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?:in)?)?\s*\)\s*$", re.I)
DEFAULT_MINUTES = 60
PATHS = ("/int/export/triggers/all?format=json&includeSecrets=false",
         "/int/export/full?format=json")


def fetch(address: str, timeout: float = 10.0):
    """Companion's triggers, as {id: trigger}. address: host:port."""
    base = address.strip().rstrip("/")
    if not base.startswith("http"):
        base = "http://" + base
    last = None
    for path in PATHS:
        try:
            with urllib.request.urlopen(base + path, timeout=timeout) as r:
                data = r.read()
            if data[:2] == b"\x1f\x8b":            # json-gz, Companion's default
                data = gzip.decompress(data)
            triggers = json.loads(data).get("triggers")
            if isinstance(triggers, dict):
                return triggers, path.split("?")[0]
            last = ValueError(f"{path.split('?')[0]}: no triggers in the reply")
        except Exception as e:                     # try the next way in
            last = e
    raise RuntimeError(f"Companion at {address} did not answer: {last}")


def load_file(path: str) -> dict:
    data = open(path, "rb").read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return json.loads(data).get("triggers") or {}


def title_and_minutes(name: str):
    """'EPG: Film Night (2h)' -> ('Film Night', 120); not a programme -> None."""
    m = MARK.match(name or "")
    if not m:
        return None
    title, minutes = m.group(1), None
    lm = LENGTH.search(title)
    if lm and (lm.group(1) or lm.group(2)):
        minutes = int(lm.group(1) or 0) * 60 + int(lm.group(2) or 0)
        title = title[:lm.start()].rstrip()
    return (title, minutes) if title else None


def programmes(triggers: dict) -> list:
    """Every enabled programme slot: title, minutes (None = default),
    hh, mm and the days (0 = Sunday, Companion's numbering)."""
    out = []
    for tid, t in (triggers or {}).items():
        opts = t.get("options") or {}
        if not opts.get("enabled", True):
            continue
        tm = title_and_minutes(opts.get("name", ""))
        if not tm:
            continue
        for ev in t.get("events") or []:
            if ev.get("type") != "timeofday" or not ev.get("enabled", True):
                continue
            o = ev.get("options") or {}
            try:
                hh, mm = (int(x) for x in str(o.get("time", "")).split(":")[:2])
            except ValueError:
                continue
            days = sorted({int(d) for d in (o.get("days") or []) if 0 <= int(d) <= 6})
            if days:
                out.append({"title": tm[0], "minutes": tm[1], "hh": hh, "mm": mm,
                            "days": days, "trigger": tid})
    out.sort(key=lambda p: (p["hh"], p["mm"], p["title"]))
    return out


def schedule(progs: list, tz, now: datetime.datetime, days: int = 8) -> list:
    """The events from the one running now to `days` ahead: start (UTC),
    minutes, title. Local times through tz, so BST is right on both sides
    of a change."""
    utc = datetime.timezone.utc
    today = now.astimezone(tz).date()
    starts = []
    for n in range(-1, days + 1):
        d = today + datetime.timedelta(days=n)
        js_day = (d.weekday() + 1) % 7               # Python Monday 0 -> JS Sunday 0
        for p in progs:
            if js_day in p["days"]:
                local = datetime.datetime(d.year, d.month, d.day, p["hh"], p["mm"], tzinfo=tz)
                starts.append((local.astimezone(utc), p))
    starts.sort(key=lambda s: s[0])
    events = []
    horizon = now + datetime.timedelta(days=days)
    for i, (start, p) in enumerate(starts):
        minutes = p["minutes"] or DEFAULT_MINUTES
        end = start + datetime.timedelta(minutes=minutes)
        if i + 1 < len(starts) and starts[i + 1][0] < end:
            end = starts[i + 1][0]                   # never into the next one
        if end <= now or start >= horizon or end <= start:
            continue
        events.append({"start": start, "minutes": int((end - start).total_seconds() // 60),
                       "title": p["title"]})
    return events


def event_id(start: datetime.datetime) -> int:
    """Stable for the same slot, unique within the 8 days (minutes, mod 65536:
    repeats every 45 days)."""
    return int(start.timestamp() // 60) % 0xFFFF + 1


def eit_xml(events: list, service_id: int, ts_id: int, onid: int) -> str:
    """TSDuck XML for eitinject: one EIT holding every event. eitinject
    reorganises them into p/f and schedule itself."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<tsduck>",
             f'  <EIT type="0" version="0" current="true" actual="true" '
             f'service_id="{service_id}" transport_stream_id="{ts_id}" '
             f'original_network_id="{onid}" last_table_id="0x50">']
    for e in events:
        h, m = divmod(e["minutes"], 60)
        lines += [f'    <event event_id="{event_id(e["start"])}" '
                  f'start_time="{e["start"]:%Y-%m-%d %H:%M:%S}" duration="{h:02d}:{m:02d}:00" '
                  f'running_status="undefined" CA_mode="false">',
                  '      <short_event_descriptor language_code="eng">',
                  f'        <event_name>{escape(e["title"])}</event_name>',
                  "        <text></text>",
                  "      </short_event_descriptor>",
                  "    </event>"]
    lines += ["  </EIT>", "</tsduck>", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    import zoneinfo
    src = sys.argv[1] if len(sys.argv) > 1 else ""
    trig = load_file(src) if src.endswith(".companionconfig") or src.endswith(".json") \
        else fetch(src)[0]
    progs = programmes(trig)
    print(f"{len(progs)} programme slots from {len(trig)} triggers")
    now = datetime.datetime.now(datetime.timezone.utc)
    tz = zoneinfo.ZoneInfo("Europe/London")
    for e in schedule(progs, tz, now, days=1)[:30]:
        print(f"  {e['start'].astimezone(tz):%a %H:%M}  {e['minutes']:3d} min  {e['title']}")
