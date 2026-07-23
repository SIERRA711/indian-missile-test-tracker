#!/usr/bin/env python3
"""
Indian Missile Test Tracker — Local Updater
============================================
Monitors PIB MoD RSS feed for new defence press releases,
classifies missile tests using rule-based logic, and writes
candidate rows to candidates.csv for your review.

Usage:
    python missile_tracker_updater.py             # run once, check for new releases
    python missile_tracker_updater.py --merge     # merge reviewed candidates into main CSV
    python missile_tracker_updater.py --daemon    # poll every N hours (set POLL_HOURS below)
    python missile_tracker_updater.py --test-prid 2273160  # force-parse a specific PRID

Requirements:
    pip install requests feedparser beautifulsoup4

Config (edit the block below):
"""

import csv
import json
import os
import re
import sys
import time
import argparse
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from pathlib import Path

import requests
import feedparser
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────────────────────
# CONFIG — edit these paths to match your local repo layout
# ─────────────────────────────────────────────────────────────
MAIN_CSV      = Path("normalized_missiles.csv")
CANDIDATES    = Path("candidates.csv")
STATE_FILE    = Path(".tracker_state.json")   # stores seen PRIDs + last run time
POLL_HOURS    = 6                              # interval for --daemon mode

PIB_RSS_MOD   = "https://www.pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3"
PIB_RELEASE   = "https://www.pib.gov.in/PressReleasePage.aspx?PRID={prid}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ─────────────────────────────────────────────────────────────
# LOCATION TABLE  (extend as needed)
# ─────────────────────────────────────────────────────────────
LOCATION_MAP = {
    "LOC-001": ["chandipur", "balasore", "integrated test range", "itr"],
    "LOC-002": ["abdul kalam island", "wheeler island", "apj", "bhapur"],
    "LOC-003": ["pokhran", "jaisalmer", "chandan"],
    "LOC-004": ["andaman", "nicobar", "anc", "port blair"],
    "LOC-005": ["arabian sea"],
    "LOC-006": ["bay of bengal"],
    "LOC-007": ["kurnool", "noar", "national open area range"],
    "LOC-008": ["ahmednagar", "kk range"],
    "LOC-009": ["gopalpur"],
    "LOC-010": ["ladakh", "leh", "dras", "siachen"],
    "LOC-011": ["mhow", "madhya pradesh"],
}

# ─────────────────────────────────────────────────────────────
# MISSILE FAMILY RULES
# Each entry: (family, variant, category_hint, keywords)
# First match wins — order from most specific to least specific
# ─────────────────────────────────────────────────────────────
FAMILY_RULES = [
    # Ballistic — strategic
    ("Agni",        "V MIRV",   "Ballistic",            ["agni.*v.*mirv", "mirv.*agni"]),
    ("Agni",        "V",        "Ballistic",            ["agni.?v\\b", "agni.?5\\b"]),
    ("Agni",        "Prime",    "Ballistic",            ["agni.?p\\b", "agni.*prime"]),
    ("Agni",        "4",        "Ballistic",            ["agni.?4\\b"]),
    ("Agni",        "3",        "Ballistic",            ["agni.?3\\b"]),
    ("Agni",        "1",        "Ballistic",            ["agni.?1\\b", "agni.?i\\b"]),
    ("Prithvi",     "II",       "Ballistic",            ["prithvi"]),
    ("Pralay",      "Base",     "Ballistic",            ["pralay"]),
    ("K4",          "Base",     "Sub-Launched Ballistic",["k.?4\\b"]),
    ("K-15",        "SLBM",     "Sub-Launched Ballistic",["k.?15\\b", "k15", "bo-5"]),
    # BMD
    ("AD1",         "Base",     "Ballistic Missile Defence", ["\\bad1\\b", "ad-1\\b", "phase.?2.*intercept", "bmd.*phase.?2"]),
    ("AD2",         "Base",     "Ballistic Missile Defence", ["\\bad2\\b", "ad-2\\b"]),
    ("Sea-Based",   "Endo-Atmospheric Interceptor", "Ballistic Missile Defence", ["sea.based.*endo", "naval.*bmd", "endo.atmospheric.*naval"]),
    # Cruise / LACM
    ("LR-LACM",     "Base",     "Air-Launched Cruise",  ["lr.?lacm", "lrlacm", "long range land attack cruise"]),
    ("LR-LACM",     "LRLACM-01","Air-Launched Cruise",  ["lrlacm.?01"]),
    ("BrahMos",     "ALCM",     "Air-Launched Cruise",  ["brahmos.*alcm", "alcm.*brahmos"]),
    ("BrahMos",     "ER",       "Anti-Ship/Cruise",     ["brahmos.*er\\b", "brahmos.*extended"]),
    ("BrahMos",     "Base",     "Anti-Ship/Cruise",     ["brahmos"]),
    ("ITCM",        "Base",     "Air-Launched Cruise",  ["itcm", "indigenous technology cruise"]),
    ("SLCM",        "Base",     "Air-Launched Cruise",  ["\\bslcm\\b", "sub.?marine.*cruise", "underwater.*cruise"]),
    ("RudraM",      "2",        "Air-Launched Cruise",  ["rudram.?2", "rudra.?m.?2"]),
    ("RudraM",      "1",        "Air-Launched Cruise",  ["rudram.?1", "rudra.?m.?1", "\\rudram\\b"]),
    ("ULPGM",       "V3",       "Air-Launched Cruise",  ["ulpgm"]),
    ("Gaurav",      "LRGB",     "Glide Bomb",           ["gaurav"]),
    # SAM
    ("Kusha",       "Base",     "Surface-to-Air",       ["kusha", "xrsam"]),
    ("Akash",       "NG",       "Surface-to-Air",       ["akash.?ng", "akash.*next gen"]),
    ("Akash",       "Prime",    "Surface-to-Air",       ["akash.*prime"]),
    ("Akash",       "Mk1",      "Surface-to-Air",       ["akash.*mk.?1"]),
    ("Akash",       "Base",     "Surface-to-Air",       ["\\bakash\\b"]),
    ("VL-SRSAM",    "Base",     "Surface-to-Air",       ["vl.?srsam", "vl srsam"]),
    ("MRSAM",       "Base",     "Surface-to-Air",       ["\\bmrsam\\b", "medium range surface to air"]),
    ("QRSAM",       "Base",     "Surface-to-Air",       ["\\bqrsam\\b", "quick reaction surface"]),
    ("VSHORADS",    "Base",     "Surface-to-Air",       ["vshorads", "vshorad\\b"]),
    ("SFDR",        "Base",     "Surface-to-Air",       ["\\bsfdr\\b", "solid fuel ducted"]),
    ("IADWS",       "Base",     "Surface-to-Air",       ["\\biadws\\b", "integrated air defence"]),
    ("SAMAR",       "Base",     "Surface-to-Air",       ["\\bsamar\\b"]),
    # AAM
    ("Astra",       "Mk2",      "Air-to-Air",           ["astra.*mk.?2"]),
    ("Astra",       "Mk1",      "Air-to-Air",           ["astra.*mk.?1", "\\bastra\\b"]),
    # ATGM
    ("HELINA",      "Base",     "Anti-Tank",            ["\\bhelina\\b"]),
    ("Dhruvastra",  "Base",     "Anti-Tank",            ["dhruvastra", "dhruv.*atgm"]),
    ("MPATGM",      "Base",     "Anti-Tank",            ["\\bmpatgm\\b", "man portable anti tank"]),
    ("Nag",         "Mk2",      "Anti-Tank",            ["nag.*mk.?2"]),
    ("Nag",         "Base",     "Anti-Tank",            ["\\bnag\\b"]),
    ("SAMHO",       "Base",     "Anti-Tank",            ["\\bsamho\\b"]),
    ("NGCCM",       "Base",     "Anti-Tank",            ["ngccm", "next gen.*close combat"]),
    ("AMOGHA",      "III",      "Anti-Tank",            ["amogha"]),
    ("Milan-2T",    "ATGM",     "Anti-Tank",            ["milan"]),
    ("ATGM",        "MBT Arjun Mk IA", "Anti-Tank",    ["atgm.*arjun", "arjun.*atgm"]),
    # Anti-ship
    ("NASM-MR",     "Base",     "Anti-Ship/Cruise",     ["nasm.?mr", "nasm.*medium"]),
    ("NASM-SR",     "Base",     "Anti-Ship/Cruise",     ["nasm.?sr", "nasm.*short"]),
    ("LR-AShM",     "Base",     "Anti-Ship/Cruise",     ["lr.?ashm", "long range.*anti.?ship"]),
    # Glide / precision
    ("SAAW",        "EO-SAAW Mk1", "Glide Bomb",        ["eo.saaw", "saaw.*eo"]),
    ("SAAW",        "Base",     "Glide Bomb",           ["\\bsaaw\\b", "smart anti airfield"]),
    ("SANT",        "Base",     "Glide Bomb",           ["\\bsant\\b", "standoff anti tank"]),
    # Rocket / arty
    ("Pinaka",      "LRGR 120", "Rocket/Artillery",     ["pinaka.*120", "lrgr.*120"]),
    ("Pinaka",      "LRGR 120", "Rocket/Artillery",     ["\\blrgr\\b", "long range guided rocket"]),
    ("Pinaka",      "Mk1 Enhanced", "Rocket/Artillery", ["pinaka.*mk.?1.*enh", "pinaka.*enhanced"]),
    ("Pinaka",      "Base",     "Rocket/Artillery",     ["\\bpinaka\\b"]),
    ("122mm Rocket","Base",     "Rocket/Artillery",     ["122\\s*mm"]),
    ("ERASR",       "Base",     "Rocket/Artillery",     ["\\berasr\\b"]),
    # Torpedo
    ("SMART",       "Base",     "Torpedo",              ["\\bsmart\\b", "supersonc missile assisted"]),
    # Hypersonic
    ("ET LDHCM",    "Base",     "Hypersonic",           ["ldhcm", "long range.*hypersonic.*cruise", "et.?ldhcm"]),
    ("HSTDV",       "Base",     "Hypersonic",           ["\\bhstdv\\b", "hypersonic tech demo"]),
    ("Hypersonic",  "Vehicle",  "Hypersonic",           ["hypersonic.*vehicle", "hypersonic.*missile"]),
    ("LR-AShM",     "Base",     "Anti-Ship/Cruise",     ["long range.*anti.ship"]),
    # Catch-all
    ("RUDRAM-II",   "Base",     "Air-Launched Cruise",  ["rudram"]),
]

# ─────────────────────────────────────────────────────────────
# SERVICE RULES
# ─────────────────────────────────────────────────────────────
SERVICE_RULES = [
    ("SFC",          ["strategic forces command", "\\bsfc\\b"]),
    ("IAF",          ["indian air force", "\\biaf\\b", "air force"]),
    ("Indian Navy",  ["indian navy", "ins ", "naval", "\\bnavy\\b"]),
    ("Indian Army",  ["indian army", "\\barmy\\b", "corps", "regiment"]),
    ("DRDO",         ["drdo", "defence research"]),
    ("BDL",          ["bharat dynamics", "\\bbdl\\b"]),
    ("ISRO",         ["\\bisro\\b"]),
]

# ─────────────────────────────────────────────────────────────
# PLATFORM RULES
# ─────────────────────────────────────────────────────────────
PLATFORM_RULES = [
    ("Su-30MKI",        ["su.30", "sukhoi"]),
    ("Tejas LCA",       ["tejas", "lca"]),
    ("Hawk-i",          ["hawk"]),
    ("Sea King",        ["sea king"]),
    ("ALH MK-IV",       ["alh", "advanced light helicopter"]),
    ("Arjun MBT",       ["arjun"]),
    ("INS Arihant",     ["arihant"]),
    ("INS Arighaat",    ["arighaat"]),
    ("Underwater Platform", ["underwater", "submarine"]),
    ("Ship",            ["\\bship\\b", "\\bins \\w+", "destroyer", "frigate", "corvette"]),
    ("Ground Launcher", []),   # default
]

# ─────────────────────────────────────────────────────────────
# EVENT TYPE RULES
# ─────────────────────────────────────────────────────────────
EVENT_TYPE_RULES = [
    ("User Trial",              ["user trial", "user evaluation", "user validation", "induction trial"]),
    ("Training Launch",         ["training launch", "user training"]),
    ("Technology Demonstration",["technology demonstration", "tech demo", "demonstrated"]),
    ("Operational Launch",      ["operational launch", "operational firing"]),
    ("Development Test",        []),   # default
]

# Keywords that signal a missile test (to filter non-test PIB releases)
MISSILE_KEYWORDS = [
    r"\bmissiles?\b", r"\brocket(?:s|ry)?\b", r"\bprojectile\b", r"\binterceptor\b",
    r"flight test", r"flight trial", r"test fired", r"test-fired",
    r"successfully fired", r"successfully test", r"cruise missile", r"ballistic missile",
    r"anti-tank guided", r"anti-ship", r"surface-to-air", r"air-to-air",
    r"\bbmd\b", r"\batgm\b", r"\baam\b", r"\bashm\b", r"\balcm\b", r"\bsam\b",
    r"guided missile", r"\btorpedo\b", r"hypersonic",
]

# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    text = f"[{ts}] {msg}"
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8", errors="replace"))


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen_prids": [], "last_run": None}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def next_event_id(year: int, main_csv: Path) -> str:
    """Find the next sequential MTI-YYYY-NNNN id for a given year."""
    max_seq = 0
    if main_csv.exists():
        with open(main_csv, newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                m = re.match(rf"MTI-{year}-(\d+)", row[0])
                if m:
                    max_seq = max(max_seq, int(m.group(1)))
    # Also check candidates file
    if CANDIDATES.exists():
        with open(CANDIDATES, newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                m = re.match(rf"MTI-{year}-(\d+)", row[0])
                if m:
                    max_seq = max(max_seq, int(m.group(1)))
    return f"MTI-{year}-{max_seq + 1:04d}"


def match_first(text: str, rules) -> str | None:
    text_lower = text.lower()
    for pattern in rules:
        if re.search(pattern, text_lower):
            return True
    return False


def classify_family(text: str):
    text_lower = text.lower()
    for family, variant, _cat, patterns in FAMILY_RULES:
        for p in patterns:
            if re.search(p, text_lower):
                return family, variant
    return "UNKNOWN", "Base"


def classify_service(text: str) -> str:
    lower = text.lower()

    # Pass 1 — explicit conductor phrases (who ran the test)
    conductor_patterns = [
        ("SFC",         [r"(?:fired|tested|launched|conducted)\s+by\s+(?:the\s+)?(?:strategic forces|sfc)\b",
                         r"\bsfc\b.*(?:fired|test|launch|conduct)",
                         r"under the aegis of (?:the\s+)?sfc",
                         r"strategic forces command"]),
        ("IAF",         [r"indian air force.*(?:fired|test|conduct|launch)",
                         r"(?:fired|tested|launched|conducted)\s+by\s+(?:the\s+)?(?:indian air force|iaf)\b",
                         r"\biaf\b.*(?:fired|test|launch|conduct|trial|exercise)",
                         r"ex-vayushakti", r"ex-astrashakti"]),
        ("Indian Navy", [r"indian navy.*(?:fired|test|conduct|launch)",
                         r"(?:fired|tested|launched|conducted)\s+by\s+(?:the\s+)?(?:indian navy)",
                         r"from\s+ins\s+\w+",
                         r"naval.*(?:fired|tested|launched) from",
                         r"indian navy seaking", r"sea king.*in529"]),
        ("Indian Army", [r"indian army.*(?:fired|test|conduct|launch|trial|evaluation)",
                         r"(?:fired|tested|launched|conducted)\s+by\s+(?:the\s+)?(?:indian army)",
                         r"user trials?\s+by\s+(?:the\s+)?(?:indian army)",
                         r"kharga corps", r"western command.*army"]),
        ("DRDO",        [r"drdo\s+(?:conducted|carried out|successfully|flight.tested)",
                         r"(?:conducted|carried out)\s+by\s+(?:drdo|defence research)",
                         r"defence research.*(?:conducted|tested|flight)"]),
        ("BDL",         [r"bharat dynamics", r"\bbdl\b.*(?:test|trial|fire)"]),
    ]
    for service, patterns in conductor_patterns:
        for p in patterns:
            if re.search(p, lower):
                return service

    # Pass 2 — entity presence fallback
    for service, patterns in [
        ("SFC",         [r"\bsfc\b"]),
        ("IAF",         [r"\biaf\b"]),
        ("Indian Navy", [r"\bnavy\b", r"\bins \w"]),
        ("Indian Army", [r"\barmy\b"]),
        ("DRDO",        [r"\bdrdo\b"]),
    ]:
        for p in patterns:
            if re.search(p, lower):
                return service

    return "DRDO"


def classify_platform(text: str) -> str:
    text_lower = text.lower()
    for platform, patterns in PLATFORM_RULES:
        for p in patterns:
            if re.search(p, text_lower):
                return platform
    return "Ground Launcher"


def classify_location(text: str) -> str:
    text_lower = text.lower()
    # Most-specific first — Abdul Kalam Island must beat ITR/Chandipur
    location_map = [
        ("LOC-002", ["abdul kalam island", "wheeler island", "apj", "bhapur"]),
        ("LOC-004", ["andaman", "nicobar", "port blair"]),
        ("LOC-007", ["kurnool", "noar", "national open area range"]),
        ("LOC-008", ["ahmednagar", "kk range"]),
        ("LOC-009", ["gopalpur"]),
        ("LOC-010", ["ladakh", "leh", "dras", "siachen"]),
        ("LOC-011", ["mhow"]),
        ("LOC-005", ["arabian sea"]),
        ("LOC-006", ["bay of bengal"]),
        ("LOC-003", ["pokhran", "jaisalmer", "chandan"]),
        ("LOC-001", ["chandipur", "balasore", "integrated test range", " itr "]),  # last — most generic
    ]
    for loc_id, keywords in location_map:
        for kw in keywords:
            if kw in text_lower:
                return loc_id
    return "LOC-UNK"


def classify_event_type(text: str) -> str:
    text_lower = text.lower()
    for etype, patterns in EVENT_TYPE_RULES:
        for p in patterns:
            if re.search(p, text_lower):
                return etype
    return "Development Test"


def classify_result(text: str) -> str:
    text_lower = text.lower()
    fail_signals = ["unsuccessful", "failed", "failure", "not successful",
                    "technical snag", "aborted", "could not", "did not"]
    for sig in fail_signals:
        if sig in text_lower:
            return "Failure"
    return "Success"


def classify_confidence(source_type: str) -> str:
    if source_type == "Official":
        return "High"
    if source_type in ("OSINT", "Media"):
        return "Medium"
    return "Low"


def extract_notes(text: str, max_chars: int = 220) -> str:
    """Pull the first substantive sentence(s) from the release body."""
    # Strip boilerplate opener patterns
    text = re.sub(r'^.*?(?:New Delhi[^.]*\.\s*|PIB[^.]*\.\s*)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text).strip()
    # Take up to max_chars, cut at sentence boundary
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    last_period = cut.rfind('.')
    if last_period > 80:
        return cut[:last_period + 1]
    return cut.rstrip() + '…'


def is_missile_test(title: str, body: str) -> bool:
    combined = (title + ' ' + body).lower()
    return any(re.search(kw, combined) for kw in MISSILE_KEYWORDS)


# ─────────────────────────────────────────────────────────────
# PIB FETCHERS
# ─────────────────────────────────────────────────────────────

def fetch_rss_prids() -> list[dict]:
    """Fetch MoD RSS feed and return list of {prid, title, date} dicts."""
    log(f"Fetching RSS: {PIB_RSS_MOD}")
    try:
        r = requests.get(PIB_RSS_MOD, headers=HEADERS, timeout=15)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception as e:
        log(f"[!] Could not fetch RSS: {e}")
        return []

    if not feed.entries:
        log("[!] RSS returned no entries -- PIB may be blocking or down")
        return []
    results = []
    for entry in feed.entries:
        link = entry.get("link", "")
        m = re.search(r"PRID=(\d+)", link)
        if not m:
            continue
        prid = m.group(1)
        pub = entry.get("published", "")
        results.append({
            "prid": prid,
            "title": entry.get("title", ""),
            "link": link,
            "published": pub,
        })
    log(f"  RSS: {len(results)} entries found")
    return results


def fetch_release_body(prid: str) -> tuple[str, str, str]:
    """Fetch a PIB press release and return (title, body_text, ministry)."""
    url = PIB_RELEASE.format(prid=prid)
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        log(f"  [!] Could not fetch PRID {prid}: {e}")
        return "", ""

    soup = BeautifulSoup(r.text, "html.parser")

    # Title — try OG tag first, then <title>
    title = ""
    og = soup.find("meta", property="og:title")
    if og:
        title = og.get("content", "")
    if not title:
        t = soup.find("title")
        title = t.get_text(strip=True) if t else ""

    # Body — PIB uses different wrapper divs depending on era
    body_div = (
        soup.find("div", class_="innner-page-content") or           # 2021+ English
        soup.find("div", id="contentDiv") or                        # alternate
        soup.find("div", class_="content-area") or                  # alternate
        soup.find("div", class_="innner-page-main-about-us-content-right-part")  # 2020-era
    )
    if body_div:
        body = body_div.get_text(separator=" ", strip=True)
    else:
        # Fallback: grab all <p> tags
        body = " ".join(p.get_text(strip=True) for p in soup.find_all("p"))

    ministry = ""
    min_div = soup.find("div", class_="MinistryNameSubhead")
    if min_div:
        ministry = min_div.get_text(separator=" ", strip=True)

    return title, body, ministry


def parse_date_from_body(body: str, fallback: str) -> str:
    """Try to extract YYYY-MM-DD from press release body text."""
    months = {
        "january":"01","february":"02","march":"03","april":"04",
        "may":"05","june":"06","july":"07","august":"08",
        "september":"09","october":"10","november":"11","december":"12"
    }
    # Pattern: "June 15, 2026" or "15 June 2026"
    m = re.search(
        r'(\d{1,2})\s+(January|February|March|April|May|June|July|August|'
        r'September|October|November|December)\s+(\d{4})',
        body, re.IGNORECASE
    )
    if m:
        day, mon, yr = m.group(1), m.group(2).lower(), m.group(3)
        return f"{yr}-{months[mon]}-{int(day):02d}"
    m = re.search(
        r'(January|February|March|April|May|June|July|August|'
        r'September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})',
        body, re.IGNORECASE
    )
    if m:
        mon, day, yr = m.group(1).lower(), m.group(2), m.group(3)
        return f"{yr}-{months[mon]}-{int(day):02d}"
    return fallback


# ─────────────────────────────────────────────────────────────
# CORE: process one PRID into a candidate row
# ─────────────────────────────────────────────────────────────

def process_prid(prid: str, rss_title: str = "", rss_date: str = "") -> dict | None:
    title, body, ministry = fetch_release_body(prid)
    if not title and rss_title:
        title = rss_title

    if not body and not title:
        log(f"  [X] PRID {prid}: could not fetch content")
        return None

    if ministry:
        ministry_lower = ministry.lower()
        # English or Hindi Ministry of Defence
        if not re.search(r"ministry of defence|रक्षा मंत्रालय", ministry_lower):
            log(f"  [-] PRID {prid}: skipping (Ministry: {ministry[:40]})")
            return None

    combined = title + " " + body

    if not is_missile_test(title, body):
        log(f"  [-] PRID {prid}: not a missile test ({title[:60]})")
        return None

    log(f"  [OK] PRID {prid}: missile test detected -- {title[:70]}")

    # Date
    event_date = parse_date_from_body(body, rss_date or "")
    if not event_date:
        # Fall back to current year-month
        event_date = date.today().strftime("%Y-%m")
    year = int(event_date[:4]) if event_date and event_date[:4].isdigit() else date.today().year

    # Classification
    family, variant      = classify_family(combined)
    service              = classify_service(combined)
    platform             = classify_platform(combined)
    location_id          = classify_location(combined)
    event_type           = classify_event_type(combined)
    result               = classify_result(combined)
    source_url           = PIB_RELEASE.format(prid=prid)
    confidence           = "High"  # PIB is always Official
    notes                = extract_notes(body)
    event_id             = next_event_id(year, MAIN_CSV)

    return {
        "event_id":   event_id,
        "date":       event_date,
        "family":     family,
        "variant":    variant,
        "service":    service,
        "platform":   platform,
        "location_id":location_id,
        "event_type": event_type,
        "result":     result,
        "source_type":"Official",
        "source_url": source_url,
        "confidence": confidence,
        "notes":      notes,
        "_title":     title,     # for display only, stripped before writing
        "_prid":      prid,
    }


# ─────────────────────────────────────────────────────────────
# WRITE CANDIDATES
# ─────────────────────────────────────────────────────────────

FIELDNAMES = [
    "event_id","date","family","variant","service","platform",
    "location_id","event_type","result","source_type","source_url",
    "confidence","notes"
]

def write_candidate(row: dict):
    exists = CANDIDATES.exists()
    with open(CANDIDATES, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)
    log(f"  -> Written to {CANDIDATES}: {row['event_id']} | {row['family']} {row['variant']} | {row['date']}")


# ─────────────────────────────────────────────────────────────
# MERGE: candidates.csv → normalized_missiles.csv
# ─────────────────────────────────────────────────────────────

def merge_candidates():
    if not CANDIDATES.exists():
        log("No candidates.csv found -- nothing to merge.")
        return

    with open(CANDIDATES, newline="", encoding="utf-8") as f:
        candidates = list(csv.DictReader(f))

    if not candidates:
        log("candidates.csv is empty.")
        return

    print(f"\n{'-'*70}")
    print(f"  {'#':<3} {'EVENT_ID':<18} {'DATE':<12} {'FAMILY':<14} {'VARIANT':<16} {'SERVICE'}")
    print(f"{'-'*70}")
    for i, row in enumerate(candidates):
        print(f"  {i+1:<3} {row['event_id']:<18} {row['date']:<12} {row['family']:<14} {row['variant']:<16} {row['service']}")
    print(f"{'-'*70}")
    print(f"  {len(candidates)} candidate(s). Review candidates.csv, edit if needed, then re-run --merge.")
    ans = input("\n  Merge all into normalized_missiles.csv? [y/N] ").strip().lower()
    if ans != "y":
        log("Merge cancelled.")
        return

    # Read main CSV, prepend new rows (newest first), write back
    existing = []
    if MAIN_CSV.exists():
        with open(MAIN_CSV, newline="", encoding="utf-8") as f:
            existing = list(csv.DictReader(f))

    # Deduplicate by event_id
    existing_ids = {r["event_id"] for r in existing}
    new_rows = [r for r in candidates if r["event_id"] not in existing_ids]
    if not new_rows:
        log("All candidates already present in main CSV.")
    else:
        merged = new_rows + existing   # newest first
        with open(MAIN_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerows(merged)
        log(f"[OK] Merged {len(new_rows)} row(s) into {MAIN_CSV}")

    # Archive and clear candidates
    archive = CANDIDATES.with_suffix(f".merged_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    CANDIDATES.rename(archive)
    log(f"  Archived candidates to {archive}")


# ─────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────

def run_once(force_prid: str = None):
    state = load_state()
    seen  = set(state.get("seen_prids", []))
    new_candidates = 0

    if force_prid:
        entries = [{"prid": force_prid, "title": "", "published": ""}]
        seen.discard(force_prid)   # force re-process
    else:
        entries = fetch_rss_prids()

    for entry in entries:
        prid = entry["prid"]
        if prid in seen:
            continue

        row = process_prid(prid, entry.get("title",""), entry.get("published",""))
        seen.add(prid)

        if row:
            write_candidate(row)
            print(f"\n  +- CANDIDATE PREVIEW --------------------------------------")
            print(f"  |  Title    : {row['_title'][:65]}")
            print(f"  |  ID       : {row['event_id']}")
            print(f"  |  Date     : {row['date']}")
            print(f"  |  Family   : {row['family']}  Variant: {row['variant']}")
            print(f"  |  Service  : {row['service']}  Platform: {row['platform']}")
            print(f"  |  Location : {row['location_id']}")
            print(f"  |  Type     : {row['event_type']}  Result: {row['result']}")
            print(f"  |  Notes    : {row['notes'][:65]}...")
            print(f"  +------------------------------------------------------\n")
            new_candidates += 1

    state["seen_prids"] = list(seen)
    state["last_run"] = datetime.now().isoformat()
    save_state(state)

    if new_candidates:
        log(f"[OK] {new_candidates} candidate(s) written to {CANDIDATES}")
        log(f"  Review, edit if needed, then run:  python {__file__} --merge")
    else:
        log("No new missile tests found.")


def run_daemon():
    log(f"Daemon mode: polling every {POLL_HOURS}h. Ctrl-C to stop.")
    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            log("Stopped.")
            break
        except Exception as e:
            log(f"Error during run: {e}")
        next_run = datetime.now().strftime("%H:%M")
        log(f"Sleeping {POLL_HOURS}h (next check ~{next_run}). Ctrl-C to stop.")
        time.sleep(POLL_HOURS * 3600)


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def run_scan_range(start: int, end: int, workers: int = 10, delay: float = 0.0, ignore_seen: bool = False):
    """Parallel brute-force scan every PRID in [start, end] for missile tests."""
    state    = load_state()
    seen     = set(state.get("seen_prids", []))
    lock     = threading.Lock()   # guards seen-set, state file, and candidates file
    counters = {"done": 0, "hits": 0}
    total    = end - start + 1

    # Filter to only unseen PRIDs (unless --ignore-seen)
    if ignore_seen:
        todo = [str(p) for p in range(start, end + 1)]
        log(f"--ignore-seen: re-checking all {total} PRIDs in range (ignoring state).")
    else:
        todo = [str(p) for p in range(start, end + 1) if str(p) not in seen]
    skipped = total - len(todo)

    log(f"Scanning PRIDs {start}–{end} ({total} total, {skipped} already seen → {len(todo)} to fetch).")
    log(f"  Workers={workers}  Delay={delay}s  Ctrl-C to stop cleanly.")

    def fetch_one(prid: str):
        if delay:
            time.sleep(delay)
        return prid, process_prid(prid)

    def flush_state():
        with lock:
            state["seen_prids"] = list(seen)
            state["last_run"]   = datetime.now().isoformat()
            save_state(state)

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch_one, prid): prid for prid in todo}
            for future in as_completed(futures):
                try:
                    prid, row = future.result()
                except Exception as e:
                    prid = futures[future]
                    log(f"  [!] PRID {prid} error: {e}")
                    row = None

                with lock:
                    seen.add(prid)
                    counters["done"] += 1
                    done = counters["done"]

                if row:
                    with lock:
                        write_candidate(row)
                        counters["hits"] += 1
                    log(f"HIT ({done}/{len(todo)}) {row['date']} | {row['family']} {row['variant']} | {row['service']} | {row['location_id']}")
                    log(f"  Title : {row['_title'][:80]}")
                    log(f"  Notes : {row['notes'][:80]}")

                # Checkpoint state every 100 completions
                if done % 100 == 0:
                    flush_state()
                    log(f"  Progress: {done}/{len(todo)} fetched, {counters['hits']} hits so far.")

    except KeyboardInterrupt:
        log("Scan interrupted — saving progress...")

    flush_state()
    log(f"[OK] Scan complete. {counters['hits']} missile test(s) found across {counters['done']} PRIDs checked.")
    if counters["hits"]:
        log(f"  Review candidates.csv, edit if needed, then run:  python {__file__} --merge")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Indian Missile Test Tracker — local CSV updater"
    )
    parser.add_argument("--merge",       action="store_true", help="Merge candidates.csv into main CSV")
    parser.add_argument("--daemon",      action="store_true", help=f"Poll every {POLL_HOURS}h")
    parser.add_argument("--test-prid",   metavar="PRID",      help="Force-process a specific PIB PRID")
    parser.add_argument("--scan-range",   nargs=2, metavar=("START", "END"),
                        help="Parallel scan PRIDs from START to END (e.g. --scan-range 1640000 1691000)")
    parser.add_argument("--scan-workers", type=int, default=10, metavar="N",
                        help="Parallel workers for --scan-range (default: 10)")
    parser.add_argument("--ignore-seen",  action="store_true",
                        help="Re-check PRIDs even if already in state (use after rate-limit issues)")
    parser.add_argument("--scan-delay",   type=float, default=0.0, metavar="SECONDS",
                        help="Per-worker delay between requests (default: 0 — no delay)")
    parser.add_argument("--csv",          default=str(MAIN_CSV), help="Path to main CSV (default: normalized_missiles.csv)")
    args = parser.parse_args()

    MAIN_CSV = Path(args.csv)

    if args.merge:
        merge_candidates()
    elif args.daemon:
        run_daemon()
    elif args.test_prid:
        run_once(force_prid=args.test_prid)
    elif args.scan_range:
        run_scan_range(int(args.scan_range[0]), int(args.scan_range[1]),
                       workers=args.scan_workers, delay=args.scan_delay,
                       ignore_seen=args.ignore_seen)
    else:
        run_once()
