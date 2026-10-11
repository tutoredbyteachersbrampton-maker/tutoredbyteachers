#!/usr/bin/env python3
"""Generate assets/data/availability.json for the Make-Up Session page.

Reads the current month's schedule grid tab from the "2026-27 Tutoring
Schedule" Google Sheet and emits the open/full group slots (counts only —
never student names) for dates from today onward.

Parsing mirrors sync_oct_list.py: week blocks start on rows where
col A == "Mr. G" and col E == "Mrs. Brar"; slot rows sit at block
offsets +2 (4:00 pm), +8 (5:00 pm), +14 (6:00 pm), +20 (7:00 pm);
tutor names are in header cols 0, 4, 8, 12, 16, 20; students occupy the
5 rows under each slot offset, day column = tutor_col + day_index
(Mon=0 .. Thu=3). Group capacity is 4.

Prints "changed" (and rewrites the JSON) when the availability content
differs from the existing file, otherwise prints "unchanged".
"""
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

SID = "1Kx1YHh80gdjQ7t3HurXpPK4mYgkF4A5YB2i8u_suGPQ"
FALLBACK_TAB = "Oct 26"
CAPACITY = 4
TZ = ZoneInfo("America/Toronto")

# Dates (full "Oct 12, 2026" form) when the business is closed, with the
# reason shown to parents. Closed days render with no bookable slots.
CLOSED_DATES = {"Oct 12, 2026": "Thanksgiving"}

SLOTS = [(2, "4:00 pm"), (8, "5:00 pm"), (14, "6:00 pm"), (20, "7:00 pm")]

SITE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(SITE_DIR, "assets", "data", "availability.json")


def gws(*args, retries=3):
    """Call hatch_gws_cli, retrying transient failures with backoff."""
    last_err = "unknown error"
    for attempt in range(retries):
        try:
            r = subprocess.run(["hatch_gws_cli"] + list(args),
                               capture_output=True, text=True, timeout=120)
        except Exception as e:  # noqa: BLE001 - transport-level failure
            last_err = f"transport error: {e}"
            time.sleep(2 * (attempt + 1))
            continue
        out = r.stdout or ""
        start, end = out.find("{"), out.rfind("}")
        if start != -1 and end != -1:
            try:
                data = json.loads(out[start:end + 1])
            except json.JSONDecodeError:
                last_err = f"bad JSON: {out[:200]}"
            else:
                if isinstance(data, dict) and data.get("error"):
                    last_err = f"API error: {data['error']}"
                else:
                    return data
        else:
            last_err = (r.stderr or out)[:300] or f"exit {r.returncode}"
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Sheets API call failed after {retries} tries: {last_err}\n"
                       f"args: {' '.join(args[:4])}...")


def get_values(tab, range_):
    d = gws("sheets", "spreadsheets", "values", "get", "--params",
            json.dumps({"spreadsheetId": SID, "range": f"'{tab}'!{range_}"}))
    return d.get("values", [])


def list_tabs():
    d = gws("sheets", "spreadsheets", "get", "--params",
            json.dumps({"spreadsheetId": SID,
                        "fields": "sheets.properties.title"}))
    return [s["properties"]["title"] for s in d.get("sheets", [])]


def month_tab(today):
    """Current month's tab, e.g. 'Oct 26'; fall back to FALLBACK_TAB."""
    want = today.strftime("%b %y")
    try:
        tabs = list_tabs()
    except Exception as e:  # noqa: BLE001
        print(f"warning: could not list tabs ({e}); using {want}", file=sys.stderr)
        return want
    if want in tabs:
        return want
    print(f"warning: tab '{want}' not found; falling back to '{FALLBACK_TAB}'",
          file=sys.stderr)
    return FALLBACK_TAB


def month_mondays(year, month):
    """All Mondays that fall inside the given month."""
    first = date(year, month, 1)
    mon = first + timedelta(days=(7 - first.weekday()) % 7)
    out = []
    while mon.month == month:
        out.append(mon)
        mon += timedelta(days=7)
    return out


def build_availability(vals, tab_date, today):
    """Turn grid values into the availability structure (counts only)."""
    def cell(r, c):
        if r < len(vals) and c < len(vals[r]):
            return vals[r][c].strip()
        return ""

    blocks = []
    for i in range(len(vals)):
        if cell(i, 0) == "Mr. G" and cell(i, 4) == "Mrs. Brar":
            blocks.append(i)
    if not blocks:
        raise RuntimeError("No schedule blocks found in grid")

    mondays = month_mondays(tab_date.year, tab_date.month)
    if len(blocks) != len(mondays):
        print(f"warning: {len(blocks)} grid blocks vs {len(mondays)} Mondays "
              f"in {tab_date:%B %Y}; pairing in order", file=sys.stderr)

    weeks = []
    for bi, b in enumerate(blocks):
        if bi >= len(mondays):
            break
        monday = mondays[bi]
        tnames = [cell(b, c) for c in [0, 4, 8, 12, 16, 20]]
        days = []
        for d in range(4):  # Mon..Thu
            day_date = monday + timedelta(days=d)
            if day_date < today:
                continue
            full = day_date.strftime("%b %-d, %Y")
            entry = {
                "date_label": day_date.strftime("%a, %b %-d"),
                "date_full": day_date.strftime("%A, %B %-d, %Y"),
                "closed": full in CLOSED_DATES,
                "slots": [],
            }
            if entry["closed"]:
                entry["closed_reason"] = CLOSED_DATES[full]
                days.append(entry)
                continue
            for off, tlabel in SLOTS:
                for t, tn in enumerate(tnames):
                    if not tn:
                        continue
                    students = set()
                    for srow in range(1, 6):
                        nm = cell(b + off + srow, t * 4 + d)
                        if nm and nm != ".":
                            students.add(nm)
                    enrolled = len(students)
                    if enrolled < 1:
                        continue  # no group runs at this slot
                    spots = max(0, CAPACITY - enrolled)
                    entry["slots"].append({
                        "time": tlabel,
                        "teacher": tn,
                        "spots_left": spots,
                        "full": spots == 0,
                    })
            entry["slots"].sort(key=lambda s: (SLOTS.index(
                next(x for x in SLOTS if x[1] == s["time"])), s["teacher"]))
            if entry["slots"]:
                days.append(entry)
        if days:
            weeks.append({
                "label": f"Week of {monday.strftime('%b %-d')}",
                "days": days,
            })
    return weeks


def strip_generated(obj):
    if isinstance(obj, dict):
        return {k: strip_generated(v) for k, v in obj.items()
                if not k.startswith("generated_at")}
    if isinstance(obj, list):
        return [strip_generated(v) for v in obj]
    return obj


def main():
    now = datetime.now(TZ)
    today = now.date()
    tab = month_tab(today)
    # Dates in the tab belong to the tab's own month (relevant only when
    # the fallback tab is used outside its month).
    tab_date = today
    if tab != today.strftime("%b %y"):
        mon_abbr, yr = tab.split()
        tab_date = datetime.strptime(f"1 {mon_abbr} {yr}", "%d %b %y").date()

    vals = get_values(tab, "A1:AZ160")
    weeks = build_availability(vals, tab_date, today)

    payload = {
        "generated_at": now.isoformat(timespec="minutes"),
        "generated_at_display": now.strftime("%b %-d, %Y, %-I:%M %p %Z"),
        "weeks": weeks,
    }

    existing = None
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH) as f:
                existing = json.load(f)
        except Exception:  # noqa: BLE001
            existing = None

    if existing is not None and strip_generated(existing) == strip_generated(payload):
        print("unchanged")
        return 0

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    print("changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
