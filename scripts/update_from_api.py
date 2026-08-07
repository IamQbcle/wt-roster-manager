# -*- coding: utf-8 -*-
"""
War Thunder Roster Manager HTML updater v2.82

What it does:
1) Downloads vehicles from WT Vehicles API into data/vehicles_api_raw.json.
2) Builds data/vehicles.json as a future data source for the HTML app.
3) Caches vehicle thumbnails locally in assets/vehicles/ and assets/vehicles/slots/.

v2.11 image downloader changes:
- parallel downloads (default 24 workers);
- resumable cache via data/vehicle_image_cache_manifest.json;
- skips already downloaded images;
- remembers missing images so repeated runs do not spend hours on the same failures;
- use --retry-missing to re-check missing entries;
- stronger candidate generation from API id, vehicle name, nation prefix, and known stale/demo ids.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import html
import json
import os
import re
import sys
import subprocess
import threading
import time
import urllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    print("Missing dependency: requests")
    print("Run: python -m pip install requests")
    raise

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
VEH_DIR = ROOT / "assets" / "vehicles"
SLOT_DIR = VEH_DIR / "slots"
API_BASE = "https://wtvehiclesapi.duckdns.org/api/vehicles"
WIKI_IMG = "https://static.encyclopedia.warthunder.com/images/"
WIKI_SLOT = "https://static.encyclopedia.warthunder.com/slots/"
WIKI_UNIT = "https://wiki.warthunder.com/unit/"
WIKI_UNIT_RU = "https://wiki.warthunder.ru/unit/"
LIMIT = 200
TIMEOUT = (5, 14)  # connect, read
IMAGE_LOGIC_VERSION = "v2.25-cache-conflicts-class-ui"
MANIFEST_FILE = DATA_DIR / "vehicle_image_cache_manifest.json"
IMAGES_JSON = DATA_DIR / "vehicle_images.json"
STATUS_JSON = DATA_DIR / "vehicle_image_status.json"
TREE_LINKS_JSON = DATA_DIR / "tech_tree_links.json"
TREE_STATUS_JSON = DATA_DIR / "tech_tree_link_status.json"
DISPLAY_NAMES_JSON = DATA_DIR / "display_names.json"
DISPLAY_NAME_STATUS_JSON = DATA_DIR / "display_name_status.json"
DISPLAY_NAME_MAP: dict[str, str] = {}
NATION_PREFIX_RE = re.compile(r"^(us|ussr|germ|uk|jp|cn|it|fr|sw|il)[_-]", re.I)
INVALID_IMAGE_URL_MARKERS = ("Category:", "old-wiki", "List_of_vehicle_battle_ratings")
THREAD_LOCAL = threading.local()
MANIFEST_LOCK = threading.Lock()

# Known stale/demo ids where visible short names do not match current unit/image ids.
STALE_ID_MAP = {
    "us_m4a1": "us_m4a1_1942_sherman",
    "us_m22": "us_m22_locust",
    "us_m18": "us_m18_hellcat",
    "us_m26": "us_m26_pershing",
    "us_m13_mgmc": "us_halftrack_m13",
    "us_m15_cgmc": "us_halftrack_m15",
    "us_m16_mgmc": "us_halftrack_m16",
    "us_a10a": "a_10a_late",
}

NATION_TO_PREFIX = {
    "usa": "us", "us": "us", "u.s.a.": "us", "united states": "us", "america": "us",
    "ussr": "ussr", "u.s.s.r.": "ussr", "soviet union": "ussr", "russia": "ussr",
    "germany": "germ", "germ": "germ",
    "britain": "uk", "great britain": "uk", "uk": "uk",
    "japan": "jp", "china": "cn", "italy": "it", "france": "fr", "sweden": "sw", "israel": "il",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    VEH_DIR.mkdir(parents=True, exist_ok=True)
    SLOT_DIR.mkdir(parents=True, exist_ok=True)


def get_session() -> requests.Session:
    s = getattr(THREAD_LOCAL, "session", None)
    if s is None:
        s = requests.Session()
        retry = Retry(
            total=2,
            connect=2,
            read=1,
            backoff_factor=0.25,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
        )
        adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=retry)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36 WT-Roster-Manager/2.16",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
            "Cache-Control": "no-cache",
        })
        THREAD_LOCAL.session = s
    return s


def get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    r = get_session().get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "results", "vehicles"):
            val = payload.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
    return []


def fetch_all_vehicles() -> list[dict[str, Any]]:
    print("Downloading vehicles from WT Vehicles API...")
    for mode in ("page", "offset"):
        all_rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for i in range(0, 10000):
            params = {"limit": LIMIT, mode: i if mode == "page" else i * LIMIT}
            try:
                payload = get_json(API_BASE, params)
            except Exception as e:
                if i == 0:
                    print(f"  {mode} pagination failed at first request: {e}")
                break
            items = extract_items(payload)
            if not items:
                break
            added_this_page = 0
            for row in items:
                vid = row_identifier(row)
                if not vid or vid in seen_ids:
                    continue
                seen_ids.add(vid)
                all_rows.append(row)
                added_this_page += 1
            print(f"  {mode} {i}: {len(items)} rows, {added_this_page} new")
            if len(items) < LIMIT or added_this_page == 0:
                break
        if len(all_rows) > 100:
            print(f"Downloaded {len(all_rows)} vehicles using {mode} pagination.")
            return all_rows
    raise RuntimeError("Could not download vehicles from API; API shape may have changed.")


def norm_id(s: str) -> str:
    return str(s or "").strip().lower().replace(" ", "_").replace("/", "_")


def safe_file_stem(s: str) -> str:
    v = norm_id(s)
    return re.sub(r"[^a-z0-9_.-]+", "_", v).strip("._-") or "vehicle"


def strip_prefix_id(s: str) -> str:
    return NATION_PREFIX_RE.sub("", norm_id(s))


def clean_title_for_id(name: str) -> str:
    s = str(name or "").lower().strip()
    s = s.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    s = re.sub(r"[’'\"()\[\],.:;]", "", s)
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"-+", "_", s)
    s = re.sub(r"_+", "_", s)
    return s.strip("_")


def add_variant(vals: list[str], value: str) -> None:
    v = safe_file_stem(value)
    if not v:
        return
    forms = [
        v,
        v.replace("_", "-"),
        v.replace("-", "_"),
        re.sub(r"[_-]+", "", v),
    ]
    for f in forms:
        if f and f not in vals:
            vals.append(f)


def row_identifier(row: dict[str, Any]) -> str:
    return str(row.get("identifier") or row.get("id") or row.get("vehicle_id") or row.get("gameId") or "").strip()


def pretty_name_from_id(vid: str) -> str:
    s = str(vid or '').strip()
    s = re.sub(r'^(us|usa|ussr|germ|germany|uk|britain|jp|japan|cn|china|it|italy|fr|france|sw|sweden|il|israel)_', '', s, flags=re.I)
    s = s.replace('_', '-')
    s = re.sub(r'-+', '-', s)
    def cap(m: re.Match[str]) -> str:
        return m.group(1).upper()
    s = re.sub(r'\b([a-z])', cap, s)
    return s or str(vid or '')



FRIENDLY_ID_NAME = {
    "tbd-1_1938": "TBD-1",
    "us_halftrack_m13": "M13 MGMC",
    "us_halftrack_m15": "M15 CGMC",
    "us_halftrack_m16": "M16 MGMC",
    "us_m22_locust": "M22 Locust",
    "us_m4a1_1942_sherman": "M4A1",
    "us_m18_hellcat": "M18 Hellcat",
    "us_m26_pershing": "M26 Pershing",
    "lvt_a_1": "LVT(A)(1)",
    "us_lvt_a_1": "LVT(A)(1)",
    "lvt_a_1_trb": "LVT(A)(1)",
    "us_lvt_a_1_trb": "LVT(A)(1)",
    "uk_saint_chamond_event": "Saint-Chamond",
    "germ_a7v_event": "A7V",
    "uk_mark_v_event": "Mark V",
    "uk_garford_putilov_event": "Garford-Putilov",
    "germ_garford_putilov_event": "Garford-Putilov",
    "f_4e_event": "F-4E",
    "mig-21_bis_event": "МиГ-21 бис",
    "ah_1f_event": "AH-1F",
}

BAD_NAME_RE = re.compile(r"^[a-z]{1,4}[_-]|[_-](event|1938|locust|sherman|halftrack)|^[a-z0-9]+[_-][a-z0-9]", re.I)


def display_name_from_id(vid: str) -> str:
    raw = norm_id(vid)
    if raw in FRIENDLY_ID_NAME:
        return FRIENDLY_ID_NAME[raw]
    s = strip_prefix_id(raw)
    if s in FRIENDLY_ID_NAME:
        return FRIENDLY_ID_NAME[s]
    if re.fullmatch(r"halftrack_m(13|16)", s):
        return f"M{re.findall(r'\d+', s)[0]} MGMC"
    if re.fullmatch(r"halftrack_m15", s):
        return "M15 CGMC"
    m = re.fullmatch(r"tbd[-_]1(?:_1938)?", s)
    if m:
        return "TBD-1"
    m = re.fullmatch(r"lvt[_-]?a[_-]?1", s)
    if m:
        return "LVT(A)(1)"
    # readable aviation/ground names: p_26a_33 -> P-26A-33, a_10a_late -> A-10A late
    s = s.replace("_", "-")
    s = re.sub(r"-+", "-", s).strip("-")
    parts = []
    for part in s.split("-"):
        if not part:
            continue
        if part in {"early", "late", "event", "premium"}:
            parts.append(part.capitalize())
        elif re.fullmatch(r"[a-z]+\d*[a-z]*", part):
            parts.append(part.upper())
        else:
            parts.append(part.upper() if len(part) <= 3 else part.capitalize())
    return "-".join(parts) if parts else str(vid or "")


def is_bad_display_name(name: str, vid: str) -> bool:
    n = str(name or "").strip()
    if not n:
        return True
    low = n.lower().replace(" ", "_")
    if low == norm_id(vid) or low == strip_prefix_id(vid):
        return True
    if "halftrack-m" in low or "halftrack_m" in low:
        return True
    if re.search(r"\btbd[-_ ]?1[-_ ]?1938\b", low):
        return True
    if low.startswith("lvt-a-1") or low.startswith("lvt_a_1"):
        return True
    return False

def row_name(row: dict[str, Any]) -> str:
    vid = row_identifier(row)
    for k in ("name", "title", "vehicle", "display_name", "displayName", "shortName", "fullName", "vehicle_name", "unit_name", "wikiTitle"):
        v = row.get(k)
        if v not in (None, ""):
            name = str(v).strip()
            if is_bad_display_name(name, vid):
                return display_name_from_id(vid)
            return name
    return display_name_from_id(vid)


def nation_prefix(row: dict[str, Any]) -> str:
    n = str(row.get("country") or row.get("nation") or row.get("tree") or row.get("nation_name") or "").strip().lower()
    return NATION_TO_PREFIX.get(n, "")


def nation_normalized(row: dict[str, Any]) -> str:
    n = str(first_defined(row, ["country", "nation", "tree", "nation_name"], "")).strip().lower()
    return {
        "usa":"USA", "us":"USA", "united states":"USA", "u.s.a.":"USA",
        "ussr":"USSR", "soviet union":"USSR", "russia":"USSR",
        "germany":"Germany", "germ":"Germany", "britain":"Britain", "uk":"Britain", "great britain":"Britain",
        "japan":"Japan", "china":"China", "italy":"Italy", "france":"France", "sweden":"Sweden", "israel":"Israel",
    }.get(n, str(first_defined(row, ["country", "nation", "tree", "nation_name"], "")).strip() or "Unknown")


def soviet_display_name(name: str, nation: str) -> str:
    if str(nation).upper() != "USSR":
        return name
    s = str(name or "")
    s = s.replace("_", "-")
    repl = [
        (r"^I-?", "И-"), (r"^IL-?", "Ил-"), (r"^SU-?", "Су-"), (r"^MIG-?", "МиГ-"), (r"^YAK-?", "Як-"),
        (r"^PE-?", "Пе-"), (r"^LA-?", "Ла-"), (r"^LAGG-?", "ЛаГГ-"), (r"^TU-?", "Ту-"),
        (r"^YER-?", "Ер-"), (r"^PO-?", "По-"), (r"^DB-?", "ДБ-"), (r"^AR-?", "Ар-"), (r"^BB-?", "ББ-"),
        (r"^BT-?(\d)", r"БТ-\1"), (r"^T-?(\d)", r"Т-\1"), (r"^KV-?(\d)", r"КВ-\1"),
    ]
    for pat, rep in repl:
        s = re.sub(pat, rep, s, flags=re.I)
    s = re.sub(r"\bBIS\b", "бис", s, flags=re.I)
    s = re.sub(r"TYPE\s*(\d+)", r"тип \1", s, flags=re.I)
    s = re.sub(r"\s*[-_]\s*", "-", s)
    return s


def soviet_search_aliases(name: str, vid: str, nation: str) -> str:
    if str(nation).upper() != "USSR":
        return ""
    base = f"{name} {vid}".lower()
    vals: set[str] = set()
    pairs = [("yak","як"),("mig","миг"),("su","су"),("il","ил"),("bt","бт"),("kv","кв")]
    for en, ru in pairs:
        if en in base:
            vals.add(base.replace(en, ru))
    pref_map = {"bt":"бт", "t":"т", "kv":"кв", "su":"су", "il":"ил", "yak":"як", "mig":"миг"}
    for m in re.finditer(r"\b(bt|t|kv|su|il|yak|mig)[-_ ]?(\d+[a-zа-я0-9]*)", base, flags=re.I):
        pref = pref_map.get(m.group(1).lower(), m.group(1).lower())
        num = m.group(2)
        vals.update({f"{pref}{num}", f"{pref}-{num}", f"{pref} {num}"})
    return " ".join(sorted(vals))


def blob_text(row: dict[str, Any]) -> str:
    bits: list[str] = []
    for key in ("identifier", "id", "vehicle_id", "gameId", "name", "title", "vehicle", "display_name", "search", "url"):
        val = row.get(key)
        if isinstance(val, str):
            bits.append(val)
    return " ".join(bits).lower().replace("_", " ").replace("-", " ")


def family_variants(vals: list[str], row: dict[str, Any], raw: str, stripped: str) -> None:
    name = row_name(row).lower()
    blob = blob_text(row)
    pref = nation_prefix(row)

    # stale/demo direct map
    for candidate in (raw, stripped):
        mapped = STALE_ID_MAP.get(candidate)
        if mapped:
            add_variant(vals, mapped)

    # M13/M15/M16 half-track family
    numbers: set[str] = set()
    for m in re.finditer(r"\bm\s*(13|15|16)\s*(?:mgmc|cgmc|gmc)?\b", blob):
        numbers.add(m.group(1))
    for m in re.finditer(r"halftrack\s*m\s*(13|15|16)", blob):
        numbers.add(m.group(1))
    for n in sorted(numbers):
        add_variant(vals, f"us_halftrack_m{n}")
        add_variant(vals, f"halftrack_m{n}")
        add_variant(vals, f"m{n}_mgmc")
        if n == "15":
            add_variant(vals, "m15_cgmc")

    # other common US shorthand rows where API id is longer than visible name
    if pref == "us" or raw.startswith("us_") or " usa " in f" {blob} ":
        if re.search(r"\bm\s*4\s*a\s*1\b", blob):
            add_variant(vals, "us_m4a1_1942_sherman")
        if re.search(r"\bm\s*18\b", blob) and "hellcat" in blob:
            add_variant(vals, "us_m18_hellcat")
        if re.search(r"\bm\s*22\b", blob) and "locust" in blob:
            add_variant(vals, "us_m22_locust")
        if re.search(r"\bm\s*26\b", blob) and "pershing" in blob:
            add_variant(vals, "us_m26_pershing")

    # TBD-1 is shown as a grouped tree folder on Wiki; slot image uses a special suffix.
    if re.search(r"\btbd\s*1\b", blob) or stripped in {"tbd_1", "tbd-1", "tbd1"}:
        add_variant(vals, "tbd-1_1938")

    # Event/pack/market titles often include suffixes not present in the image id.
    title = clean_title_for_id(name)
    if title:
        add_variant(vals, title)
        no_suffix = re.sub(r"_(event|pack|premium|market|gift|squadron|early|late)$", "", title)
        if no_suffix != title:
            add_variant(vals, no_suffix)
        if pref:
            add_variant(vals, f"{pref}_{title}")
            add_variant(vals, f"{pref}_{no_suffix}")


def id_variants(identifier: str, row: dict[str, Any] | None = None) -> list[str]:
    row = row or {}
    vals: list[str] = []
    raw = norm_id(identifier)
    stripped = strip_prefix_id(raw)
    add_variant(vals, raw)
    add_variant(vals, stripped)

    # If this is a stale id, put the proper id early.
    if raw in STALE_ID_MAP:
        add_variant(vals, STALE_ID_MAP[raw])
    if stripped in STALE_ID_MAP:
        add_variant(vals, STALE_ID_MAP[stripped])

    name = row_name(row)
    pref = nation_prefix(row)
    title = clean_title_for_id(name)
    if title:
        add_variant(vals, title)
        if pref:
            add_variant(vals, f"{pref}_{title}")
        # Preserve useful aircraft punctuation forms too: f-4e -> f_4e and f-4e.
        raw_title = str(name or "").lower().strip().replace(" ", "_")
        add_variant(vals, raw_title)

    search = str(row.get("search") or "").lower()
    if search:
        add_variant(vals, search)

    family_variants(vals, row, raw, stripped)
    return list(dict.fromkeys([v for v in vals if v]))


def row_image_url(row: dict[str, Any]) -> str:
    for key in ("image", "image_url", "img", "thumbnail", "picture", "icon", "url"):
        val = row.get(key)
        if not isinstance(val, str) or not val.startswith("http"):
            continue
        if any(marker.lower() in val.lower() for marker in INVALID_IMAGE_URL_MARKERS):
            continue
        # These are navigation/category pages, not image URLs.
        if val.rstrip("/").endswith(("/aviation", "/ground", "/bluewater", "/coastal", "/helicopters")):
            continue
        return val
    return ""


def local_existing_path(vid: str) -> str:
    safe = safe_file_stem(vid)
    for folder in (SLOT_DIR, VEH_DIR):
        p = folder / f"{safe}.png"
        if p.exists() and p.stat().st_size > 500:
            return str(p.relative_to(ROOT)).replace("\\", "/")
    return ""


def candidate_urls(row: dict[str, Any]) -> list[tuple[str, str]]:
    vid = row_identifier(row)
    out: list[tuple[str, str]] = []
    # Slot thumbnails are usually exactly what the tech tree needs.
    for v in id_variants(vid, row):
        out.append((WIKI_SLOT + quote(v) + ".png", "slot"))
    # API URL can be useful, but old category URLs are filtered out.
    api = row_image_url(row)
    if api:
        out.append((api, "vehicle"))
    for v in id_variants(vid, row):
        out.append((WIKI_IMG + quote(v) + ".png", "vehicle"))
    return list(dict.fromkeys(out))


def download_binary(url: str, dest: Path, force: bool = False) -> bool:
    if not force and dest.exists() and dest.stat().st_size > 500:
        return True
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        r = get_session().get(url, timeout=TIMEOUT, stream=True)
        if r.status_code != 200:
            return False
        ctype = r.headers.get("content-type", "").lower()
        if "image" not in ctype and not url.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            return False
        data = r.content
        if not data or len(data) < 500:
            return False
        tmp.write_bytes(data)
        tmp.replace(dest)
        return True
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return False


def parse_unit_page_for_images(identifier: str, row: dict[str, Any] | None = None) -> list[str]:
    urls: list[str] = []
    for v in id_variants(identifier, row or {}):
        try:
            r = get_session().get(WIKI_UNIT + quote(v), timeout=TIMEOUT)
            if r.status_code != 200:
                continue
            html = r.text
            for m in re.finditer(r"https://static\.encyclopedia\.warthunder\.com/(?:images|slots)/[^'\" )<>]+\.(?:png|jpg|jpeg|webp)", html):
                urls.append(m.group(0))
            if urls:
                break
        except Exception:
            pass
    return list(dict.fromkeys(urls))


def load_manifest() -> dict[str, Any]:
    if not MANIFEST_FILE.exists():
        return {"logic_version": IMAGE_LOGIC_VERSION, "items": {}}
    try:
        data = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        data.setdefault("items", {})
        return data
    except Exception:
        return {"logic_version": IMAGE_LOGIC_VERSION, "items": {}}


def save_manifest(manifest: dict[str, Any]) -> None:
    tmp = MANIFEST_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(MANIFEST_FILE)


def save_vehicle_image(row: dict[str, Any], manifest: dict[str, Any], retry_missing: bool = False, force: bool = False) -> tuple[str, str, str]:
    vid = row_identifier(row)
    if not vid:
        return "", "missing", "no identifier"
    safe = safe_file_stem(vid)

    existing = local_existing_path(vid)
    if existing and not force:
        return existing, "cached", "already on disk"

    item = manifest.get("items", {}).get(vid, {})
    if (
        not retry_missing
        and not force
        and item.get("logic_version") == IMAGE_LOGIC_VERSION
        and item.get("status") == "missing"
    ):
        return "", "skipped_missing", item.get("reason", "known missing")
    if (
        not force
        and item.get("logic_version") == IMAGE_LOGIC_VERSION
        and item.get("status") == "ok"
    ):
        p = str(item.get("path") or "")
        if p and (ROOT / p).exists() and (ROOT / p).stat().st_size > 500:
            return p, "cached", "manifest"

    tried = 0
    for url, kind in candidate_urls(row):
        tried += 1
        dest = (SLOT_DIR if kind == "slot" else VEH_DIR) / f"{safe}.png"
        if download_binary(url, dest, force=force):
            return str(dest.relative_to(ROOT)).replace("\\", "/"), "ok", url

    # Slower fallback: parse unit page only after direct static URLs failed.
    for url in parse_unit_page_for_images(vid, row):
        tried += 1
        kind = "slot" if "/slots/" in url else "vehicle"
        dest = (SLOT_DIR if kind == "slot" else VEH_DIR) / f"{safe}.png"
        if download_binary(url, dest, force=force):
            return str(dest.relative_to(ROOT)).replace("\\", "/"), "ok", url

    return "", "missing", f"no image after {tried} candidates"


def process_row(args: tuple[dict[str, Any], dict[str, Any], bool, bool]) -> tuple[str, str, str, str]:
    row, manifest, retry_missing, force = args
    vid = row_identifier(row)
    path, status, reason = save_vehicle_image(row, manifest, retry_missing=retry_missing, force=force)
    return vid, path, status, reason



RANK_MAP = {"1":"I","2":"II","3":"III","4":"IV","5":"V","6":"VI","7":"VII","8":"VIII","9":"IX",1:"I",2:"II",3:"III",4:"IV",5:"V",6:"VI",7:"VII",8:"VIII",9:"IX"}
CLASS_NAMES = {"air":"Авиация","ground":"Наземка","heli":"Вертолёты","coastal":"Малый флот","bluewater":"Большой флот"}


def first_defined(row: dict[str, Any], keys: Iterable[str], default: Any = "") -> Any:
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return default


def to_float(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "."))
    except Exception:
        return None


def normalize_rank(v: Any) -> str:
    """Normalize API rank values like 1, "1", "I", "Rank I", "tier_3" to Roman numerals."""
    if v in RANK_MAP:
        return RANK_MAP[v]
    s = str(v or "I").strip().upper()
    s = re.sub(r"^(RANK|TIER)\s*", "", s)
    m = re.search(r"(VIII|VII|VI|IV|V|III|II|IX|I|[1-9])", s)
    if m:
        return RANK_MAP.get(m.group(1), m.group(1))
    return "I"


def normalize_vehicle_class(row: dict[str, Any]) -> str:
    blob = " ".join(str(first_defined(row, ["type", "vehicle_type", "class", "unitClass", "category", "kind"], "")).lower().split())
    if "heli" in blob:
        return "heli"
    if any(x in blob for x in ("ship", "boat", "destroyer", "cruiser", "battleship", "naval", "bluewater", "coastal", "submarine", "fleet", "barge", "frigate")):
        if any(x in blob for x in ("destroyer", "cruiser", "battleship", "bluewater", "submarine", "frigate")):
            return "bluewater"
        return "coastal"
    if any(x in blob for x in ("tank", "ground", "spaa", "atgm", "army", "armored", "armoured", "tank_destroyer", "light_tank", "medium_tank", "heavy_tank")):
        return "ground"
    return "air"


def role_short(row: dict[str, Any]) -> tuple[str, str]:
    raw = " ".join(str(first_defined(row, ["role", "roleName", "main_role", "mainRole", "type", "vehicle_type", "unitClass", "class", "tags"], "")).lower().replace("_", " ").split())
    name = row_name(row).lower()
    s = raw + " " + name
    if re.search(r"spaa|anti.?air|air defense|sam|зенит|зсу", s): return "ЗСУ", "SPAA"
    if re.search(r"tank destroyer|пт.?сау", s): return "ПТ-САУ", "Tank destroyer"
    if "heavy tank" in s or "тяж" in s: return "ТТ", "Heavy tank"
    if "light tank" in s or "лёг" in s or "легк" in s: return "ЛТ", "Light tank"
    if "medium tank" in s or "main battle tank" in s or "mbt" in s or "сред" in s or re.search(r"\btank\b", s): return "СТ", "Medium tank"
    if "dive bomber" in s or "пикир" in s: return "Пикировщик", "Dive bomber"
    if "torpedo" in s or "bomber" in s or "бомб" in s or "frontline" in s: return "Бомбер", "Bomber"
    if "attacker" in s or "assault" in s or "strike" in s or "штурм" in s or "удар" in s: return "Штурмовик", "Attacker"
    if "interceptor" in s or "fighter" in s or "истреб" in s: return "Истреб.", "Fighter"
    if "battlecruiser" in s: return "Лин. кр.", "Battlecruiser"
    if "battleship" in s: return "ЛК", "Battleship"
    if "light cruiser" in s: return "Лёгк. КР", "Light cruiser"
    if "cruiser" in s: return "КР", "Cruiser"
    if "destroyer" in s: return "ЭСМ", "Destroyer"
    if "torpedo" in s: return "Торп.", "Torpedo boat"
    if "gun boat" in s or "gunboat" in s: return "Канон.", "Gun boat"
    if "barge" in s: return "Баржа", "Barge"
    if "frigate" in s: return "Фрег.", "Frigate"
    if "boat" in s: return "Катер", "Boat"
    if "helicopter" in s or "heli" in s: return "Ударный верт.", "Helicopter"
    return str(first_defined(row, ["role", "type"], "Прочее")), str(first_defined(row, ["role", "type"], ""))


def _truthy_status(row: dict[str, Any], keys: list[str]) -> bool:
    """Return True only for explicit truthy fields.
    Do not scan JSON key names: fields like isPremium:false previously made every row premium.
    """
    for k in keys:
        if k not in row:
            continue
        v = row.get(k)
        if isinstance(v, bool):
            if v:
                return True
            continue
        if isinstance(v, (int, float)):
            if v != 0:
                return True
            continue
        txt = str(v).strip().lower()
        if txt in {"1", "true", "yes", "y", "да"}:
            return True
    return False


def _text_status(row: dict[str, Any]) -> str:
    fields = []
    for k in ("status", "obtain", "obtain_method", "obtainMethod", "availability", "source", "event", "category"):
        v = row.get(k)
        if isinstance(v, str):
            fields.append(v.lower())
    return " ".join(fields)


def is_special_event_unit(row: dict[str, Any]) -> bool:
    vid = row_identifier(row).lower()
    nm = row_name(row).lower()
    blob = (vid + " " + nm + " " + _text_status(row)).replace("_", "-")
    return any(x in blob for x in ("-event", "tutorial", "sub-event", "sdi-minotaur", "destroyer-heavy-tank"))

def normalize_status(row: dict[str, Any]) -> str:
    txt = _text_status(row)
    if is_special_event_unit(row):
        return "Спец."
    # Explicit booleans first. Never classify from JSON key names alone.
    if _truthy_status(row, ["isSquadron", "squadron", "squadronVehicle", "is_squadron"]):
        return "Полковая"
    if _truthy_status(row, ["isPack", "pack", "is_pack", "bundle"]):
        return "Пакетная"
    if _truthy_status(row, ["isMarket", "market", "is_market", "coupon"]):
        return "Маркет"
    if _truthy_status(row, ["isPremium", "premium", "is_premium"]):
        return "Премиум"
    if _truthy_status(row, ["isEvent", "event", "is_event", "gift"]):
        return "Акционная"

    # Then textual status-like values, not arbitrary keys.
    if any(x in txt for x in ("squadron", "полков")): return "Полковая"
    if any(x in txt for x in ("pack", "bundle", "пакет")): return "Пакетная"
    if any(x in txt for x in ("market", "coupon", "маркет")): return "Маркет"
    if any(x in txt for x in ("premium", "прем")): return "Премиум"
    if any(x in txt for x in ("event", "gift", "акцион")): return "Акционная"
    return "Обычная"


def br_values(row: dict[str, Any]) -> dict[str, float]:
    if isinstance(row.get("br"), dict):
        b = row["br"]
        ab = to_float(b.get("ab")); rb = to_float(b.get("rb")); sb = to_float(b.get("sb"))
        grb = to_float(b.get("ground_rb") or b.get("realistic_ground_br"))
        nrb = to_float(b.get("naval_rb") or b.get("realistic_naval_br"))
        gsb = to_float(b.get("ground_sb") or b.get("simulator_ground_br"))
    else:
        ab = to_float(first_defined(row, ["br_ab", "brAB", "arcade_br", "arcadeBattleRating", "battle_rating_arcade", "arcade"], None))
        rb = to_float(first_defined(row, ["br_rb", "brRB", "realistic_br", "realisticBattleRating", "battle_rating_realistic", "realistic"], None))
        sb = to_float(first_defined(row, ["br_sb", "brSB", "simulator_br", "simulatorBattleRating", "battle_rating_simulator", "simulator"], None))
        grb = to_float(first_defined(row, ["ground_rb", "realistic_ground_br", "battle_rating_realistic_ground"], None))
        nrb = to_float(first_defined(row, ["naval_rb", "realistic_naval_br", "battle_rating_realistic_naval"], None))
        gsb = to_float(first_defined(row, ["ground_sb", "simulator_ground_br", "battle_rating_simulator_ground"], None))
    any_br = to_float(first_defined(row, ["br", "battle_rating", "battleRating"], None))
    ab = ab if ab is not None else any_br if any_br is not None else 1.0
    rb = rb if rb is not None else ab
    sb = sb if sb is not None else rb
    out = {"ab": ab, "rb": rb, "sb": sb}
    if grb is not None:
        out["ground_rb"] = grb
    if nrb is not None:
        out["naval_rb"] = nrb
    if gsb is not None:
        out["ground_sb"] = gsb
    return out




def clean_wiki_title_text(s: str) -> str:
    import html as _html
    s = _html.unescape(str(s or "")).replace("\xa0", " ")
    s = re.sub(r"\s*\|\s*War Thunder Wiki.*$", "", s, flags=re.I)
    s = re.sub(r"^\s*War Thunder\s*[-–—:]\s*", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def extract_display_name_from_unit_html(html: str) -> str:
    # Prefer title/H1 visible on unit page. The page title is usually exactly the player-facing unit name.
    for pat in [r"<title[^>]*>(.*?)</title>", r"<h1[^>]*>(.*?)</h1>"]:
        m = re.search(pat, html or "", flags=re.I | re.S)
        if m:
            txt = re.sub(r"<[^>]+>", " ", m.group(1))
            txt = clean_wiki_title_text(txt)
            if txt and not re.search(r"^(Aviation|Ground Vehicles|Bluewater|Coastal|Helicopters)$", txt, re.I):
                return txt
    return ""



def fetch_display_name_from_wiki(identifier: str, row: dict[str, Any] | None = None) -> tuple[str, str]:
    """Optional, slow and unreliable: Wiki may throttle/block scripted unit page requests.
    v2.19 does NOT use this by default. It is kept only for manual experiments.
    """
    for v in id_variants(identifier, row or {}):
        try:
            r = get_session().get(WIKI_UNIT + quote(v), timeout=TIMEOUT)
            if r.status_code != 200 or not r.text:
                continue
            name = extract_display_name_from_unit_html(r.text)
            if name:
                return name, v
        except Exception:
            pass
    return "", "not found"



DISPLAY_NAME_OVERRIDES_RU = DATA_DIR / "display_name_overrides_ru.json"
DISPLAY_NAME_OVERRIDES_EN = DATA_DIR / "display_name_overrides_en.json"

DEFAULT_RU_OVERRIDES = {
    # Soviet named/event aircraft and common lend-lease names.
    # This file is intentionally a portable human-curated layer: identifier -> display name.
    "i-153_m62_zhukovskiy": "И-153 М-62 (Жуковский)",
    "i-15bis_krasnolutsky": "И-15 бис (Краснолуцкий)",
    "mig_3_series_1_15": "МиГ-3 серия 1-15",
    "mig_3_series_1_15_bk_pod": "МиГ-3 серия 1-15 (БК)",
    "mig_3_series_34": "МиГ-3 серия 34",
    "hp52_hampden_tbmk1_ussr_utk1": "Hampden TB Mk I (СССР)",
    "pby-5a_ussr": "PBY-5A (СССР)",
    "p-40e_ussr": "P-40E (СССР)",
    "pe-3_early": "Пе-3 ранний",
    "tandem_mai": "Тандем МАИ",
    "yak_2_kabb": "Як-2 КАББ",
    "su-2_tss1": "Су-2 ТСС-1",
}

RU_SUFFIX_MAP = {
    "MOSCOW": "Москва",
    "ZHUKOVSKIY": "Жуковский", "ZHUKOVSKY": "Жуковский", "JUKOVSKY": "Жуковский",
    "KRASNOLUTSKY": "Краснолуцкий", "KRASNOLUTSKIY": "Краснолуцкий",
    "POKRYSHKIN": "Покрышкин", "GOLOVATYI": "Головатый", "GOLOVATY": "Головатый",
    "DOLGUSHIN": "Долгушин", "SAFRONOV": "Сафронов", "KATYUSHA": "Катюша",
}

_LATIN_TO_CYR_MULTI = [
    ("shch", "щ"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ch", "ч"), ("sh", "ш"), ("yu", "ю"), ("ya", "я"), ("yo", "ё"), ("ye", "е"),
]
_LATIN_TO_CYR_SINGLE = str.maketrans({
    "a":"а","b":"б","v":"в","g":"г","d":"д","e":"е","z":"з","i":"и","y":"й","j":"й","k":"к","l":"л","m":"м","n":"н","o":"о","p":"п","r":"р","s":"с","t":"т","u":"у","f":"ф","h":"х","c":"к","w":"в","q":"к","x":"кс",
})

def simple_ru_translit_token(token: str) -> str:
    src = str(token or "").strip("-_ ")
    if not src:
        return ""
    up = src.upper()
    if up in RU_SUFFIX_MAP:
        return RU_SUFFIX_MAP[up]
    low = src.lower()
    for a, b in _LATIN_TO_CYR_MULTI:
        low = low.replace(a, b)
    out = low.translate(_LATIN_TO_CYR_SINGLE)
    out = re.sub(r"и[йи]$", "ий", out)
    return out[:1].upper() + out[1:] if out else src


def load_name_overrides() -> tuple[dict[str, str], dict[str, str]]:
    ensure_dirs()
    def read(path: Path) -> dict[str, str]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in data.items() if str(v).strip()}
        except Exception:
            return {}
    ru = read(DISPLAY_NAME_OVERRIDES_RU)
    en = read(DISPLAY_NAME_OVERRIDES_EN)
    changed = False
    for k, v in DEFAULT_RU_OVERRIDES.items():
        if k not in ru:
            ru[k] = v
            changed = True
    if changed or not DISPLAY_NAME_OVERRIDES_RU.exists():
        DISPLAY_NAME_OVERRIDES_RU.write_text(json.dumps(ru, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    if not DISPLAY_NAME_OVERRIDES_EN.exists():
        DISPLAY_NAME_OVERRIDES_EN.write_text(json.dumps(en, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return ru, en


def override_display_name(row: dict[str, Any], nation: str) -> str:
    vid = row_identifier(row)
    raw = norm_id(vid)
    stripped = strip_prefix_id(raw)
    ru, en = load_name_overrides()
    pool = ru if str(nation).upper() == "USSR" else en
    return pool.get(raw) or pool.get(stripped) or ""

def base_display_name_from_row(row: dict[str, Any]) -> str:
    """A no-network display name source.
    Prefer API/Wiki title fields if they are human-readable; otherwise derive from identifier.
    """
    vid = row_identifier(row)
    raw_id = norm_id(vid)
    stripped = strip_prefix_id(raw_id)
    if raw_id in FRIENDLY_ID_NAME:
        return FRIENDLY_ID_NAME[raw_id]
    if stripped in FRIENDLY_ID_NAME:
        return FRIENDLY_ID_NAME[stripped]
    for k in ("name", "title", "vehicle", "display_name", "displayName", "shortName", "fullName", "vehicle_name", "unit_name", "wikiTitle"):
        v = row.get(k)
        if v not in (None, ""):
            name = str(v).strip()
            # Reject raw ids and technical/generated titles, but keep normal game titles.
            if not is_bad_display_name(name, vid):
                return name
    return display_name_from_id(vid)


def deterministic_display_name(row: dict[str, Any]) -> str:
    vid = row_identifier(row)
    nation = nation_normalized(row)
    raw = base_display_name_from_row(row)
    return soviet_display_name_v2(raw, vid, nation)


def build_display_name_map(rows: list[dict[str, Any]], workers: int = 12, retry_missing: bool = False, fetch_wiki: bool = False) -> dict[str, str]:
    """Build a complete identifier -> display name map.

    v2.19 deliberately does this locally from API fields and identifier rules.
    Earlier versions tried to visit every Wiki unit page; after ~1400 successful names the Wiki
    started returning misses/throttling, which produced a half-good, half-broken name map.
    """
    names: dict[str, str] = {}
    status: dict[str, Any] = {"logic_version": IMAGE_LOGIC_VERSION, "items": {}}
    print("Building local display name map from API fields and identifier rules...")
    for i, row in enumerate(rows, 1):
        vid = row_identifier(row)
        if not vid:
            continue
        nm = deterministic_display_name(row)
        names[vid] = nm
        status["items"][vid] = {
            "status": "ok" if nm else "missing",
            "name": nm,
            "reason": "local/api-name-normalizer",
            "logic_version": IMAGE_LOGIC_VERSION,
            "updated_at": now_iso(),
        }
        if i % 500 == 0:
            print(f"  display names {i}/{len(rows)} processed | named {len(names)}")
    DISPLAY_NAME_STATUS_JSON.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    DISPLAY_NAMES_JSON.write_text(json.dumps(names, ensure_ascii=False, indent=2), encoding="utf-8")
    return names


def fix_lvt_name(name: str, vid: str) -> str:
    base = f"{name} {vid}".lower().replace("_", "-")
    m = re.search(r"lvt[-\s]?\(?a\)?[-\s]?\(?([124])\)?", base, flags=re.I)
    if m:
        return f"LVT(A)({m.group(1)})"
    return name


def titlecase_vehicle_token(tok: str) -> str:
    if re.fullmatch(r"[a-z]{1,4}\d+[a-z0-9]*", tok, re.I) or re.fullmatch(r"[a-z]+", tok, re.I) and len(tok) <= 4:
        return tok.upper()
    if re.fullmatch(r"m\d+[a-z0-9]*", tok, re.I):
        return tok.upper()
    return tok[:1].upper()+tok[1:] if tok else tok


def normalize_latin_display_name(name: str, vid: str) -> str:
    if not name or is_bad_display_name(name, vid):
        name = display_name_from_id(vid)
    name = clean_wiki_title_text(str(name)).replace("_", "-")
    name = fix_lvt_name(name, vid)
    if norm_id(vid) in FRIENDLY_ID_NAME:
        name = FRIENDLY_ID_NAME[norm_id(vid)]
    # Fix common wrong casing from API: m2a2 -> M2A2, f2a-1 -> F2A-1, i-15bis -> I-15bis.
    def repl(m: re.Match[str]) -> str:
        return titlecase_vehicle_token(m.group(0))
    name = re.sub(r"\b[a-z]{1,4}\d+[a-z0-9]*\b", repl, name, flags=re.I)
    # Normalize separators but keep parentheses and spaces from Wiki titles.
    name = re.sub(r"\s*-\s*", "-", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def soviet_display_name_v2(name: str, vid: str, nation: str) -> str:
    if str(nation).upper() != "USSR":
        return normalize_latin_display_name(name, vid)
    s = normalize_latin_display_name(name, vid)
    # Latin -> Russian prefixes for Soviet machines. Keep model suffixes readable.
    repl = [
        (r"\bI[-\s]?(?=\d)", "И-"), (r"\bIL[-\s]?(?=\d)", "Ил-"), (r"\bSU[-\s]?(?=\d)", "Су-"),
        (r"\bMIG[-\s]?(?=\d)", "МиГ-"), (r"\bYAK[-\s]?(?=\d)", "Як-"), (r"\bPE[-\s]?(?=\d)", "Пе-"),
        (r"\bLAGG[-\s]?(?=\d)", "ЛаГГ-"), (r"\bLA[-\s]?(?=\d)", "Ла-"), (r"\bTU[-\s]?(?=\d)", "Ту-"),
        (r"\bPO[-\s]?(?=\d)", "По-"), (r"\bAR[-\s]?(?=\d)", "Ар-"), (r"\bBB[-\s]?(?=\d)", "ББ-"),
        (r"\bDB[-\s]?(?=\d)", "ДБ-"), (r"\bSB[-\s]?(?=\d)", "СБ-"), (r"\bKOR[-\s]?(?=\d)", "Кор-"),
        (r"\bBT[-\s]?(\d)", r"БТ-\1"), (r"\bT[-\s]?(\d)", r"Т-\1"), (r"\bKV[-\s]?(\d)", r"КВ-\1"),
    ]
    for pat, rep in repl:
        s = re.sub(pat, rep, s, flags=re.I)
    s = re.sub(r"(И-\d+)\s*BIS\b", r"\1 бис", s, flags=re.I)
    s = re.sub(r"(И-\d+)-?BIS\b", r"\1 бис", s, flags=re.I)
    s = re.sub(r"\bBIS\b", "бис", s, flags=re.I)
    s = re.sub(r"\bTYPE\s*(\d+)", r"тип \1", s, flags=re.I)
    s = re.sub(r"\bTSS[-\s]?(\d+)\b", r"ТСС-\1", s, flags=re.I)
    s = re.sub(r"\bMV[-\s]?(\d+)\b", r"МВ-\1", s, flags=re.I)
    s = re.sub(r"\bM[-\s]?(\d+)\b", r"М-\1", s, flags=re.I)
    # Named/event suffixes: I-15bis-KRASNOLUTSKY -> И-15 бис (Краснолуцкий)
    def suffix_repl(m: re.Match[str]) -> str:
        token = m.group(1)
        return " (" + simple_ru_translit_token(token) + ")"
    s = re.sub(r"-([A-Z][A-Z]{3,})(?=$|[\s)])", suffix_repl, s)
    # Known suffixes that may be left as standalone tokens.
    for latin, ru in RU_SUFFIX_MAP.items():
        s = re.sub(r"\b" + re.escape(latin) + r"\b", ru, s, flags=re.I)
    s = re.sub(r"-(бис|тип\s*\d+|Москва)", r" \1", s, flags=re.I)
    s = re.sub(r"-(ТСС-\d+|МВ-\d+|М-\d+)", r" \1", s)
    s = re.sub(r"-(\d{4})(?=$|\s|\()", r" \1", s)
    # Normalise country suffix and common leftover English tokens.
    s = re.sub(r"\((?:Усср|USSR)\)", "(СССР)", s, flags=re.I)
    s = re.sub(r"\bUSSR\b", "СССР", s, flags=re.I)
    s = re.sub(r"\bEARLY\b", "ранний", s, flags=re.I)
    s = re.sub(r"\bSERIES[-\s]?(\d+)[-\s]?(\d+)?\b", lambda m: "серия " + m.group(1) + (("-" + m.group(2)) if m.group(2) else ""), s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def final_display_name(row: dict[str, Any], vid: str, nation: str) -> str:
    ov = override_display_name(row, nation)
    if ov:
        return ov
    raw = DISPLAY_NAME_MAP.get(vid) or row_name(row) or vid
    return soviet_display_name_v2(raw, vid, nation)

def normalize_for_ui(row: dict[str, Any], image_map: dict[str, str]) -> dict[str, Any] | None:
    vid = row_identifier(row)
    if not vid:
        return None
    nation_norm = nation_normalized(row)
    name = final_display_name(row, vid, nation_norm)
    cls = normalize_vehicle_class(row)
    role, role_en = role_short(row)
    search = " ".join(str(x) for x in [vid, name, pretty_name_from_id(vid), nation_norm, cls, role, role_en, first_defined(row, ["type", "vehicle_type", "tags"], ""), soviet_search_aliases(name, vid, nation_norm)] if x)
    return {
        "id": vid,
        "name": name,
        "nation": nation_norm,
        "class": cls,
        "className": CLASS_NAMES.get(cls, cls),
        "role": "Истреб." if role == "Истр." else role,
        "roleEn": role_en,
        "rank": normalize_rank(first_defined(row, ["rank", "tier", "vehicle_rank"], "I")),
        "br": br_values(row),
        "status": normalize_status(row),
        "source": "WT Vehicles API",
        "url": row_image_url(row),
        "local_image": image_map.get(vid, ""),
        "search": search,
    }


def write_lightweight_vehicles(rows: list[dict[str, Any]], image_map: dict[str, str]) -> None:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        item = normalize_for_ui(row, image_map)
        if not item or item["id"] in seen:
            continue
        seen.add(item["id"])
        out.append(item)
    out.sort(key=lambda x: (x.get("nation",""), x.get("class",""), list(RANK_MAP.values()).index(x.get("rank","I")) if x.get("rank","I") in list(RANK_MAP.values()) else 99, x.get("br",{}).get("ab",99), x.get("name","")))
    (DATA_DIR / "vehicles.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_research_order_links(identifier: str, row: dict[str, Any] | None = None) -> tuple[list[str], str]:
    """Return outgoing Research order links from a Wiki unit page.
    The Wiki unit page has a visible 'Research order:' section; for P-26A-34 M2 it links onward to P-26A-33.
    """
    for v in id_variants(identifier, row or {}):
        try:
            r = get_session().get(WIKI_UNIT + quote(v), timeout=TIMEOUT)
            if r.status_code != 200:
                continue
            html = r.text
            m = re.search(r"Research order\s*:|Research order", html, re.I)
            if not m:
                continue
            chunk = html[m.start():m.start()+5000]
            stop = re.search(r"Modifications|Flight performance|Economy|Rating by players|</section>", chunk, re.I)
            if stop:
                chunk = chunk[:stop.start()]
            links = []
            for lm in re.finditer(r"href=[\"']/unit/([^\"'#?]+)", chunk, re.I):
                target = lm.group(1).strip()
                if target and target != identifier and target not in links:
                    links.append(target)
            # Some page builds store absolute URLs or escaped slashes.
            for lm in re.finditer(r"/unit/([a-zA-Z0-9_.\-]+)", chunk):
                target = lm.group(1).strip()
                if target and target != identifier and target not in links:
                    links.append(target)
            if links:
                return links, v
        except Exception:
            pass
    return [], "not found"


def build_research_links(rows: list[dict[str, Any]], workers: int = 12, retry_missing: bool = False) -> dict[str, list[str]]:
    existing: dict[str, Any] = {"items": {}}
    if TREE_STATUS_JSON.exists():
        try:
            existing = json.loads(TREE_STATUS_JSON.read_text(encoding="utf-8"))
            existing.setdefault("items", {})
        except Exception:
            existing = {"items": {}}
    id_lookup = {row_identifier(r).lower(): row_identifier(r) for r in rows if row_identifier(r)}
    id_set = set(id_lookup.values())

    def canon_key(x: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(x or "").lower())

    # Wiki links often use slugs that differ from API identifiers by hyphen/underscore or display name.
    # Build a resolver so /unit/p-26a-33 can resolve to p-26a_33, etc.
    canon_to_id: dict[str, str] = {}
    for r in rows:
        vid = row_identifier(r)
        if not vid:
            continue
        keys = {vid, vid.replace("_", "-"), vid.replace("-", "_"), row_name(r)}
        for k in list(keys):
            keys.add(str(k).replace(" ", "_"))
            keys.add(str(k).replace(" ", "-"))
        for k in keys:
            ck = canon_key(k)
            if ck and ck not in canon_to_id:
                canon_to_id[ck] = vid

    def resolve_link(target: str) -> str | None:
        if target in id_set:
            return target
        t = target.strip()
        variants = [t, t.replace("-", "_"), t.replace("_", "-"), unquote(t), unquote(t).replace("-", "_"), unquote(t).replace("_", "-")]
        for v in variants:
            if v in id_set:
                return v
            ck = canon_key(v)
            if ck in canon_to_id:
                return canon_to_id[ck]
        return None

    links: dict[str, list[str]] = {}
    for vid, item in existing.get("items", {}).items():
        if item.get("status") == "ok" and isinstance(item.get("links"), list):
            links[vid] = [x for x in item["links"] if x in id_set]

    tasks = []
    for row in rows:
        vid = row_identifier(row)
        if not vid:
            continue
        item = existing.get("items", {}).get(vid, {})
        if not retry_missing and item.get("status") in {"ok", "missing"}:
            continue
        # Premium/event/special vehicles usually do not have research order; still allow if Wiki has one.
        tasks.append(row)

    if not tasks:
        TREE_LINKS_JSON.write_text(json.dumps({"logic_version": IMAGE_LOGIC_VERSION, "links": links}, ensure_ascii=False, indent=2), encoding="utf-8")
        return links

    print(f"Downloading/caching Wiki research links with {workers} workers...")
    done = 0
    lock = threading.Lock()
    def worker(row: dict[str, Any]) -> tuple[str, list[str], str]:
        vid = row_identifier(row)
        raw_links, reason = parse_research_order_links(vid, row)
        resolved: list[str] = []
        for x in raw_links:
            rx = resolve_link(x)
            if rx and rx != vid and rx not in resolved:
                resolved.append(rx)
        detail = reason if not raw_links else f"{reason}; raw={len(raw_links)} resolved={len(resolved)}"
        return vid, resolved, detail
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(worker, r) for r in tasks]
        for fut in cf.as_completed(futures):
            vid, nxt, reason = fut.result()
            done += 1
            if nxt:
                links[vid] = nxt
            existing["items"][vid] = {"status":"ok" if nxt else "missing", "links":nxt, "reason":reason, "logic_version":IMAGE_LOGIC_VERSION, "updated_at":now_iso()}
            if done % 50 == 0:
                with lock:
                    TREE_STATUS_JSON.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
                    TREE_LINKS_JSON.write_text(json.dumps({"logic_version": IMAGE_LOGIC_VERSION, "links": links}, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"  research links {done}/{len(tasks)} processed | linked {len(links)}")
    TREE_STATUS_JSON.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    TREE_LINKS_JSON.write_text(json.dumps({"logic_version": IMAGE_LOGIC_VERSION, "links": links}, ensure_ascii=False, indent=2), encoding="utf-8")
    return links



TREE_ORDER_JSON = DATA_DIR / "tech_tree_order.json"
TREE_ORDER_STATUS_JSON = DATA_DIR / "tech_tree_order_status.json"
TREE_GROUP_ICONS_JSON = DATA_DIR / "tech_tree_group_icons.json"
NATION_ORDER = ["USA", "Germany", "USSR", "Britain", "Japan", "China", "Italy", "France", "Sweden", "Israel"]
NATION_PARAM = {"USA":"usa", "Germany":"germany", "USSR":"ussr", "Britain":"britain", "Japan":"japan", "China":"china", "Italy":"italy", "France":"france", "Sweden":"sweden", "Israel":"israel"}
TREE_PAGE = {"air":"aviation", "ground":"ground", "heli":"helicopters", "bluewater":"ships", "coastal":"boats"}


def _strip_html_lines(html: str) -> list[str]:
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.I)
    html = re.sub(r"<br\s*/?>|</div>|</li>|</h\d>|</p>|</section>", "\n", html, flags=re.I)
    txt = re.sub(r"<[^>]+>", " ", html)
    import html as _html
    txt = _html.unescape(txt).replace("\xa0", " ")
    return [re.sub(r"\s+", " ", x).strip() for x in txt.splitlines() if re.sub(r"\s+", " ", x).strip()]


def _name_key(s: str) -> str:
    return re.sub(r"[^a-z0-9а-я]+", "", str(s or "").lower().replace("ё", "е"))


def build_name_resolver(rows: list[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, str]]:
    res: dict[tuple[str, str, str], dict[str, str]] = {}
    for row in rows:
        vid = row_identifier(row)
        if not vid:
            continue
        n = nation_normalized(row); c = normalize_vehicle_class(row)
        item = normalize_for_ui(row, {})
        names = {row_name(row), display_name_from_id(vid), pretty_name_from_id(vid), vid, vid.replace("_", "-"), vid.replace("-", "_")}
        if item:
            names.add(item.get("name", ""))
        bucket = res.setdefault((n, c, "names"), {})
        for name in names:
            k = _name_key(name)
            if k and k not in bucket:
                bucket[k] = vid
    return res


def _clean_wiki_visible_name(line: str) -> str:
    import html as _html
    s = _html.unescape(str(line or "")).replace("\xa0", " ")
    s = re.sub(r"^[^A-Za-z0-9А-Яа-я]+", "", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _tree_page_url(page: str, nation: str | None = None) -> str:
    if nation:
        return f"https://wiki.warthunder.com/{page}?t_c={NATION_PARAM.get(nation, nation.lower())}&v=t"
    return f"https://wiki.warthunder.com/{page}?v=t"


def _resolve_tree_name(name: str, bucket: dict[str, str]) -> str | None:
    clean = _clean_wiki_visible_name(name)
    if not clean:
        return None
    candidates = [clean]
    # Group/folder labels can be like TBD/B-18 or TBF/SBD; child names often follow, but try split too.
    if "/" in clean:
        candidates.extend(p.strip() for p in clean.split("/") if p.strip())
    # Remove country/operator prefix symbols already stripped, quote suffixes, and common descriptions.
    for cand in list(candidates):
        candidates.append(re.sub(r"\s+Catalina$", "", cand, flags=re.I))
        candidates.append(re.sub(r"\s+Phantom\s+II$", "", cand, flags=re.I))
        candidates.append(re.sub(r"\s+Abrams$", "", cand, flags=re.I))
    for cand in candidates:
        k = _name_key(cand)
        if k in bucket:
            return bucket[k]
    # Last resort: if a visible line includes a known exact key as substring, use the longest match.
    ck = _name_key(clean)
    if len(ck) >= 4:
        hits = [(k, v) for k, v in bucket.items() if len(k) >= 4 and (k in ck or ck in k)]
        if hits:
            hits.sort(key=lambda kv: len(kv[0]), reverse=True)
            return hits[0][1]
    return None


def _extract_order_from_lines(lines: list[str], nation: str, cls: str, resolver: dict[tuple[str, str, str], dict[str, str]]) -> list[str]:
    bucket = resolver.get((nation, cls, "names"), {})
    if not bucket:
        return []
    # Prefer each Researchable vehicles block; stop before Premium vehicles.
    starts = [i for i, line in enumerate(lines) if "Researchable vehicles" in line]
    blocks: list[list[str]] = []
    if starts:
        for bi, start in enumerate(starts):
            end = starts[bi + 1] if bi + 1 < len(starts) else len(lines)
            block = lines[start:end]
            # Do not cut at "Premium vehicles": in Wiki text extraction both column headers
            # appear before rank rows, so cutting there would drop the entire tree.
            blocks.append(block)
    else:
        blocks = [lines]
    # On all-nations pages, block order follows NATION_ORDER. On t_c pages, the selected nation may be first or only.
    chosen_blocks: list[list[str]] = []
    if len(blocks) >= len(NATION_ORDER) and nation in NATION_ORDER:
        chosen_blocks.append(blocks[NATION_ORDER.index(nation)])
    chosen_blocks.extend(blocks[:1])
    ids: list[str] = []
    for block in chosen_blocks:
        for line in block:
            clean = _clean_wiki_visible_name(line)
            if not clean:
                continue
            if clean.startswith("Rank ") or clean in {"Researchable vehicles", "Premium vehicles", "Tree", "List"}:
                continue
            # Skip obvious filter/collection text and weaponry lines.
            if re.search(r"vehicles$|Found:|Filters|Country|Roles|Battle Rating|Arcade Battles|Realistic Battles|Simulator Battles", clean, re.I):
                continue
            if len(clean) > 70:
                continue
            vid = _resolve_tree_name(clean, bucket)
            if vid and vid not in ids:
                ids.append(vid)
        if ids:
            break
    return ids






def fetch_text_with_fallback(url: str) -> tuple[int, str, str]:
    """Fetch HTML. Some Wiki tree pages return 405 to Python requests; curl often behaves closer to browser/network stack."""
    try:
        r = get_session().get(url, timeout=(10, 45), allow_redirects=True)
        if r.status_code == 200 and r.text:
            return r.status_code, r.text, "requests"
        first = f"requests HTTP {r.status_code}"
    except Exception as e:
        first = f"requests {type(e).__name__}: {e}"
    try:
        cmd = ["curl", "-L", "--compressed", "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36", "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "--max-time", "45", url]
        cp = subprocess.run(cmd, capture_output=True, timeout=55)
        txt = cp.stdout.decode("utf-8", errors="replace")
        if cp.returncode == 0 and txt.strip():
            return 200, txt, f"curl fallback after {first}"
        return 0, "", f"{first}; curl rc={cp.returncode} stderr={cp.stderr.decode('utf-8', errors='replace')[:160]}"
    except Exception as e:
        return 0, "", f"{first}; curl {type(e).__name__}: {e}"

def fetch_wiki_tree_lines(page: str, nation: str | None = None) -> tuple[list[str], str]:
    """Fetch a Wiki tree page with browser-like headers.
    The Wiki currently returns readable HTML for normal browser GET URLs such as
    /aviation?t_c=ussr&v=t. Older updater builds sometimes received HTTP 405;
    this function retries both query orders and records the exact reason.
    """
    urls = []
    if nation:
        nc = NATION_PARAM.get(nation, nation.lower())
        urls.extend([
            f"https://wiki.warthunder.com/{page}?t_c={nc}&v=t",
            f"https://wiki.warthunder.com/{page}?v=t&t_c={nc}",
        ])
    urls.append(f"https://wiki.warthunder.com/{page}?v=t")
    reasons = []
    for url in urls:
        try:
            code, text, how = fetch_text_with_fallback(url)
            reasons.append(f"{url} -> {how if code==200 else how}")
            if code == 200 and text:
                lines = _strip_html_lines(text)
                if lines:
                    return lines, f"HTTP 200 lines={len(lines)} via {how} url={url}"
        except Exception as e:
            reasons.append(f"{url} -> {type(e).__name__}: {e}")
    return [], "; ".join(reasons)


def parse_wiki_tree_group_icons(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """v2.70: parse folder/group slot icons from Wiki tree pages.

    The UI still has safe fallbacks, but this writes data/tech_tree_group_icons.json so
    renderer code can use the exact static.encyclopedia.warthunder.com/slots/*_group.png
    URLs discovered on the tree page instead of guessing every folder icon by hand.
    """
    out: dict[str, Any] = {"logic_version": "v2.70", "items": {}, "by_slug": {}, "status": {}}
    label_re = re.compile(r">\s*([^<>]{2,60}?)\s*<")
    icon_re = re.compile(r"https://static\.encyclopedia\.warthunder\.com/slots/([A-Za-z0-9_\-]+_group)\.png")
    for cls, page in TREE_PAGE.items():
        for nation in NATION_ORDER:
            key = f"{nation}|{cls}"
            urls = [
                f"https://wiki.warthunder.com/{page}?t_c={NATION_PARAM.get(nation, nation.lower())}&v=t",
                f"https://wiki.warthunder.com/{page}?v=t&t_c={NATION_PARAM.get(nation, nation.lower())}",
            ]
            found: dict[str, str] = {}
            reason = ""
            for url in urls:
                code, html, how = fetch_text_with_fallback(url)
                reason += f"{url} -> {how}; "
                if code != 200 or not html:
                    continue
                for m in icon_re.finditer(html):
                    slug = m.group(1)
                    icon_url = f"https://static.encyclopedia.warthunder.com/slots/{slug}.png"
                    out["by_slug"][slug] = icon_url
                    # Try to infer visible folder label from nearby HTML; this is best-effort.
                    window = html[max(0, m.start()-700):min(len(html), m.end()+700)]
                    labels = []
                    for lm in label_re.finditer(window):
                        lab = re.sub(r"\s+", " ", lm.group(1).replace("\xa0", " ")).strip()
                        if lab and not re.search(r"Image|Rank|Researchable|Premium|vehicles|Battle Rating", lab, re.I):
                            labels.append(lab)
                    # Slug-derived key is always present; direct labels are added when guessed.
                    slug_label = re.sub(r"_group$", "", slug).replace("_", " ").strip()
                    found.setdefault(slug_label, icon_url)
                    for lab in labels[-3:]:
                        found.setdefault(lab, icon_url)
                if found:
                    break
            out["items"][key] = found
            out["status"][key] = {"count": len(found), "reason": reason.strip(), "updated_at": now_iso()}
    try:
        TREE_GROUP_ICONS_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wiki tree group icons parsed: {sum(len(v) for v in out['items'].values())} labels, {len(out['by_slug'])} slugs.")
    except Exception as e:
        print("WARNING: could not write tech_tree_group_icons.json:", e)
    return out

def parse_wiki_tree_order(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Extract visible Wiki tree order by nation/class.
    v2.16: use browser-like GET, per-nation pages first, and text order from the Wiki page.
    This still cannot perfectly infer exact branch geometry, but it should no longer return zero trees
    when the page is reachable in a browser.
    """
    resolver = build_name_resolver(rows)
    out: dict[str, list[str]] = {}
    status: dict[str, Any] = {"logic_version": IMAGE_LOGIC_VERSION, "items": {}}
    for cls, page in TREE_PAGE.items():
        for nation in NATION_ORDER:
            key = f"{nation}|{cls}"
            lines, fetch_reason = fetch_wiki_tree_lines(page, nation)
            ids: list[str] = []
            reason = fetch_reason
            if lines:
                ids = _extract_order_from_lines(lines, nation, cls, resolver)
                reason = f"{fetch_reason}; matched ids={len(ids)}"
            # Fallback to all-nations page only if per-nation matched nothing.
            if not ids:
                all_lines, all_reason = fetch_wiki_tree_lines(page, None)
                if all_lines:
                    ids = _extract_order_from_lines(all_lines, nation, cls, resolver)
                    reason += f"; all-page fallback: {all_reason}; matched ids={len(ids)}"
                else:
                    reason += f"; all-page fallback failed: {all_reason}"
            out[key] = ids
            status["items"][key] = {"count": len(ids), "reason": reason, "updated_at": now_iso()}
    TREE_ORDER_JSON.write_text(json.dumps({"logic_version": IMAGE_LOGIC_VERSION, "order": out}, ensure_ascii=False, indent=2), encoding="utf-8")
    TREE_ORDER_STATUS_JSON.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    nonempty = {k: v for k, v in out.items() if v}
    print(f"Wiki tree order loaded for {len(nonempty)} non-empty nation/class trees out of {len(out)}.")
    if not nonempty:
        print("WARNING: Wiki tree order parser found 0 non-empty trees. This may mean Wiki blocks scripted HTML requests; see data/tech_tree_order_status.json.")
    return out

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Update War Thunder roster data and local image cache.")
    p.add_argument("--workers", type=int, default=int(os.environ.get("WT_RM_IMAGE_WORKERS", "24")), help="parallel image download workers; default 24")
    p.add_argument("--retry-missing", action="store_true", help="retry images previously marked missing")
    p.add_argument("--force-images", action="store_true", help="redownload images even if cached")
    p.add_argument("--skip-images", action="store_true", help="download API data only; do not cache vehicle images")
    p.add_argument("--skip-tree-links", action="store_true", help="do not fetch Wiki Research order links")
    p.add_argument("--tree-workers", type=int, default=int(os.environ.get("WT_RM_TREE_WORKERS", "24")), help="parallel Wiki Research order workers; default 24")
    p.add_argument("--retry-tree-missing", action="store_true", help="retry Wiki pages previously marked as having no Research order links")
    p.add_argument("--skip-tree-order", action="store_true", help="do not fetch Wiki tree page order")
    p.add_argument("--skip-names", action="store_true", help="do not fetch Wiki display names")
    p.add_argument("--retry-names", action="store_true", help="retry Wiki display names previously missing")
    p.add_argument("--fetch-wiki-names", action="store_true", default=True, help="fetch display names and basic metadata from real Wiki unit pages (default: on)")
    p.add_argument("--no-wiki-names", action="store_false", dest="fetch_wiki_names", help="do not fetch Wiki unit names; use local deterministic normalization only")
    p.add_argument("--clear-name-cache", action="store_true", help="delete cached Wiki/display-name metadata before rebuilding names")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    ensure_dirs()
    if getattr(args, "clear_name_cache", False):
        for f in (DISPLAY_NAMES_JSON, DISPLAY_NAME_STATUS_JSON, DATA_DIR / "wiki_unit_meta_status.json", DATA_DIR / "vehicle_data_conflicts.json", DATA_DIR / "display_names_ru.json", DATA_DIR / "display_names_en.json", DATA_DIR / "wiki_unit_meta_status_ru.json", DATA_DIR / "wiki_unit_meta_status_en.json"):
            
            try:
                if f.exists():
                    f.unlink()
                    print(f"Deleted name/cache file: {f}")
            except Exception as e:
                print(f"WARNING: could not delete {f}: {e}")
    rows = fetch_all_vehicles()
    (DATA_DIR / "vehicles_api_raw.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    manifest = load_manifest()
    manifest["logic_version"] = IMAGE_LOGIC_VERSION
    manifest.setdefault("items", {})
    image_map: dict[str, str] = {}

    if args.skip_images:
        print("Skipping image cache by request.")
        write_lightweight_vehicles(rows, image_map)
        return 0

    print(f"Downloading/caching vehicle images with {args.workers} workers...")
    print("Resume is enabled: existing files and successful manifest entries are skipped.")
    if not args.retry_missing:
        print("Previously missing images are skipped. Use --retry-missing to re-check them.")

    done = 0
    ok = cached = missing = skipped_missing = 0
    last_save = time.time()
    tasks = [(row, manifest, args.retry_missing, args.force_images) for row in rows if row_identifier(row)]
    with cf.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(process_row, t) for t in tasks]
        for fut in cf.as_completed(futures):
            vid, path, status, reason = fut.result()
            done += 1
            if path:
                image_map[vid] = path
            if status == "ok":
                ok += 1
            elif status == "cached":
                cached += 1
            elif status == "skipped_missing":
                skipped_missing += 1
            else:
                missing += 1
            manifest["items"][vid] = {
                "status": "ok" if path else "missing",
                "path": path,
                "reason": reason,
                "logic_version": IMAGE_LOGIC_VERSION,
                "updated_at": now_iso(),
            }
            if done % 25 == 0 or time.time() - last_save > 10:
                with MANIFEST_LOCK:
                    save_manifest(manifest)
                last_save = time.time()
                print(f"  {done}/{len(tasks)} processed | ok {ok} | cached {cached} | missing {missing} | skipped missing {skipped_missing}")

    save_manifest(manifest)
    for vid, item in manifest.get("items", {}).items():
        if item.get("status") == "ok" and item.get("path"):
            p = ROOT / str(item["path"])
            if p.exists() and p.stat().st_size > 500:
                image_map[vid] = str(item["path"])
    IMAGES_JSON.write_text(json.dumps(image_map, ensure_ascii=False, indent=2), encoding="utf-8")
    STATUS_JSON.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    global DISPLAY_NAME_MAP
    if not args.skip_names:
        DISPLAY_NAME_MAP = build_display_name_map(rows, workers=args.tree_workers, retry_missing=args.retry_names, fetch_wiki=args.fetch_wiki_names)
        print(f"Display names available: {len(DISPLAY_NAME_MAP)} vehicles.")
    elif DISPLAY_NAMES_JSON.exists():
        try:
            DISPLAY_NAME_MAP = json.loads(DISPLAY_NAMES_JSON.read_text(encoding="utf-8"))
        except Exception:
            DISPLAY_NAME_MAP = {}
    global TREE_PLACEMENT_MAP
    if not args.skip_tree_order:
        try:
            TREE_PLACEMENT_MAP = build_wiki_tree_placement_v245(rows)
        except Exception as e:
            TREE_PLACEMENT_MAP = {}
            print("WARNING: Wiki tree placement failed:", e)
    write_lightweight_vehicles(rows, image_map)
    if not args.skip_tree_links:
        links = build_research_links(rows, workers=args.tree_workers, retry_missing=args.retry_tree_missing)
        print(f"Research links available: {len(links)} source vehicles.")
    if not args.skip_tree_order:
        order = parse_wiki_tree_order(rows)
        print(f"Wiki tree order non-empty trees: {sum(1 for v in order.values() if v)} / {len(order)}.")
        try:
            parse_wiki_tree_group_icons(rows)
        except Exception as e:
            print("WARNING: Wiki tree group icon parser failed:", e)
    print(f"Done. Vehicles: {len(rows)}. Images available: {len(image_map)}. Newly downloaded: {ok}. Cached: {cached}. Missing: {missing}.")
    print("Local folders are portable: assets/vehicles, data, and the manifest can be copied into future versions.")
    return 0


# --- v2.22 robust display-name layer: stop showing raw identifiers in UI ---
TECHNICAL_WORDS_DROP = {
    'destroyer', 'cruiser', 'battleship', 'battlecruiser', 'frigate', 'boat', 'gun', 'heavy', 'light',
    'tank', 'spaa', 'bomber', 'fighter', 'attacker', 'assault', 'helicopter', 'premium', 'event'
}
COUNTRY_SUFFIXES = {
    'usa': 'USA', 'us': 'USA', 'ussr': 'СССР', 'germany': 'Germany', 'germ': 'Germany',
    'uk': 'Great Britain', 'britain': 'Great Britain', 'jp': 'Japan', 'japan': 'Japan',
    'cn': 'China', 'china': 'China', 'it': 'Italy', 'italy': 'Italy', 'fr': 'France',
    'france': 'France', 'sw': 'Sweden', 'sweden': 'Sweden', 'il': 'Israel', 'israel': 'Israel'
}
KNOWN_ID_DISPLAY = {
    'uk_chieftain_marksman': 'Chieftain Marksman',
    'chieftain_marksman': 'Chieftain Marksman',
    'ki_61_1a_otsu_usa': 'Ki-61-Ia otsu (USA)',
    'ki_61_1a_hei_usa': 'Ki-61-Ia hei (USA)',
    'saab_j35a': 'Saab J35A',
    'saab_j35d': 'Saab J35D',
    'jp_destroyer_harukaze': 'Harukaze',
    'destroyer_harukaze': 'Harukaze',
    'jp_destroyer_ayanami': 'Ayanami',
    'jp_destroyer_yugumo': 'Yugumo',
    'jp_destroyer_mutsuki': 'Mutsuki',
    'jp_destroyer_akizuki': 'Akizuki',
    'jp_destroyer_kiyoshimo': 'Kiyoshimo',
    'jp_destroyer_shimakaze': 'Shimakaze',
    'jp_cruiser_furutaka': 'Furutaka',
    'jp_cruiser_ikoma': 'Ikoma',
}

ROMAN_JP = {'1a':'Ia','1b':'Ib','1c':'Ic','2a':'IIa','2b':'IIb','2c':'IIc'}
LOWER_MODEL_WORDS = {'otsu','ko','hei','tei','mod','late','early','bis','ter','series'}


def _split_id_words(x: str) -> list[str]:
    t = norm_id(x)
    t = NATION_PREFIX_RE.sub('', t)
    return [w for w in re.split(r'[_\-\s]+', t) if w]


def _format_code_token(tok: str) -> str:
    low = tok.lower()
    if low in ROMAN_JP:
        return ROMAN_JP[low]
    if low in LOWER_MODEL_WORDS:
        return low
    if re.fullmatch(r'[a-z]{1,3}\d+[a-z0-9]*', tok, re.I):
        return tok.upper()
    if re.fullmatch(r'[a-z]+\d+[a-z]*', tok, re.I):
        # saab -> handled separately; a7m, p51, j35a should be uppercase as model codes.
        return tok.upper()
    if re.fullmatch(r'm\d+[a-z0-9]*', tok, re.I):
        return tok.upper()
    if re.fullmatch(r'[a-z]{1,4}', tok, re.I) and len(tok) <= 3:
        return tok.upper()
    if low == 'saab':
        return 'Saab'
    return tok[:1].upper()+tok[1:] if tok else tok


def display_name_from_identifier_v222(vid: str, row: dict[str, Any] | None = None) -> str:
    raw = norm_id(vid)
    stripped = strip_prefix_id(raw)
    for key in (raw, stripped):
        if key in FRIENDLY_ID_NAME:
            return FRIENDLY_ID_NAME[key]
        if key in KNOWN_ID_DISPLAY:
            return KNOWN_ID_DISPLAY[key]
    words = _split_id_words(raw)
    # Drop leading naval/ground class words that are not part of the visible name.
    if words and words[0] in {'destroyer','cruiser','battleship','battlecruiser','frigate'} and len(words) > 1:
        words = words[1:]
    if len(words) >= 2 and words[0] in {'heavy','light'} and words[1] in {'tank','cruiser'}:
        words = words[2:]
    # Country suffix at the end: ki_61_1a_otsu_usa -> Ki-61-Ia otsu (USA)
    country_suffix = ''
    if words and words[-1].lower() in COUNTRY_SUFFIXES:
        country_suffix = COUNTRY_SUFFIXES[words[-1].lower()]
        words = words[:-1]
    # Event suffix: keep as normal text only when there is no better name.
    special_suffix = ''
    if words and words[-1] == 'event':
        special_suffix = ' Event'
        words = words[:-1]
    # Aircraft model family with numeric model parts: ki 61 1a otsu -> Ki-61-Ia otsu
    if len(words) >= 2 and re.fullmatch(r'[a-z]{1,4}', words[0], re.I) and re.fullmatch(r'\d+[a-z]*', words[1], re.I):
        head = _format_code_token(words[0]) + '-' + words[1].upper()
        rest = [_format_code_token(w) for w in words[2:]]
        if rest:
            name = head + '-' + rest[0] + ((' ' + ' '.join(rest[1:])) if len(rest)>1 else '')
        else:
            name = head
    else:
        out=[]
        for w in words:
            if w in TECHNICAL_WORDS_DROP and len(words) > 1:
                continue
            out.append(_format_code_token(w))
        # Use spaces for word names, but hyphen model-code runs like M4A1/BT-5 are preserved by token formatter.
        name = ' '.join(out).strip()
    name = re.sub(r'\bSaab\s+J', 'Saab J', name)
    name = re.sub(r'\s+', ' ', name).strip() or display_name_from_id(vid)
    if country_suffix:
        name += f' ({country_suffix})'
    if special_suffix and 'Event' not in name:
        name += special_suffix
    return name


def is_bad_display_name_v222(name: str, vid: str) -> bool:
    n = str(name or '').strip()
    if not n:
        return True
    low = norm_id(n)
    raw = norm_id(vid)
    stripped = strip_prefix_id(raw)
    if low in {raw, stripped}:
        return True
    if re.match(r'^(us|ussr|germ|uk|jp|cn|it|fr|sw|il)[_\-\s]+', low):
        return True
    if '_' in n:
        return True
    if re.search(r'\b(uk|jp|germ|ussr|cn|sw|il)[\s\-]+(destroyer|cruiser|chieftain|tank|fighter|bomber)\b', low):
        return True
    if 'halftrack' in low or 'lvt-a-1' in low or 'tbd-1-1938' in low:
        return True
    return False


def base_display_name_from_row(row: dict[str, Any]) -> str:  # override earlier definition
    vid = row_identifier(row)
    raw_id = norm_id(vid)
    stripped = strip_prefix_id(raw_id)
    for key in (raw_id, stripped):
        if key in FRIENDLY_ID_NAME:
            return FRIENDLY_ID_NAME[key]
        if key in KNOWN_ID_DISPLAY:
            return KNOWN_ID_DISPLAY[key]
    for k in ('name','title','vehicle','display_name','displayName','shortName','fullName','vehicle_name','unit_name','wikiTitle'):
        v = row.get(k)
        if v not in (None, ''):
            name = str(v).strip()
            if not is_bad_display_name_v222(name, vid):
                return name
    return display_name_from_identifier_v222(vid, row)


def normalize_latin_display_name(name: str, vid: str) -> str:  # override earlier definition
    if not name or is_bad_display_name_v222(name, vid):
        name = display_name_from_identifier_v222(vid)
    name = clean_wiki_title_text(str(name)).replace('_', ' ')
    name = fix_lvt_name(name, vid)
    if norm_id(vid) in FRIENDLY_ID_NAME:
        name = FRIENDLY_ID_NAME[norm_id(vid)]
    if norm_id(vid) in KNOWN_ID_DISPLAY:
        name = KNOWN_ID_DISPLAY[norm_id(vid)]
    # Correct casing without destroying official spaces.
    def repl(m: re.Match[str]) -> str:
        return titlecase_vehicle_token(m.group(0))
    name = re.sub(r'\b[a-z]{1,4}\d+[a-z0-9]*\b', repl, name, flags=re.I)
    name = re.sub(r'\bsaab\b', 'Saab', name, flags=re.I)
    name = re.sub(r'\((?:Усср|ussr)\)', '(СССР)', name, flags=re.I)
    # Keep common human-readable multiword names as spaces, not hyphenated identifiers.
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def deterministic_display_name(row: dict[str, Any]) -> str:  # override earlier definition
    vid = row_identifier(row)
    nation = nation_normalized(row)
    raw = base_display_name_from_row(row)
    return soviet_display_name_v2(raw, vid, nation)
# --- end v2.22 robust display-name layer ---



# --- v2.23 final deterministic display-name layer ---
# This layer is intentionally local/offline. Wiki tree/name parsing stays optional because Wiki tree pages
# were not stable for scripted requests. Manual overrides in data/display_name_overrides_*.json still win.
KNOWN_ID_DISPLAY_V223 = {
    'uk_chieftain_marksman': 'Chieftain Marksman', 'chieftain_marksman': 'Chieftain Marksman',
    'ki_61_1a_otsu_usa': 'Ki-61-Ia otsu (USA)', 'ki_61_1a_hei_usa': 'Ki-61-Ia hei (USA)',
    'ki_61_1a_ko': 'Ki-61-Ia ko', 'ki_61_1a_otsu': 'Ki-61-Ia otsu', 'ki_61_1a_hei': 'Ki-61-Ia hei',
    'saab_j35a': 'Saab J35A', 'saab_j35d': 'Saab J35D', 'saab_j35xs': 'Saab J35XS',
    'jp_destroyer_harukaze': 'Harukaze', 'destroyer_harukaze': 'Harukaze',
    'jp_destroyer_ayanami': 'Ayanami', 'jp_destroyer_yugumo': 'Yugumo', 'jp_destroyer_mutsuki': 'Mutsuki',
    'jp_destroyer_akizuki': 'Akizuki', 'jp_destroyer_kiyoshimo': 'Kiyoshimo', 'jp_destroyer_shimakaze': 'Shimakaze',
    'lvt_a_1': 'LVT(A)(1)', 'us_lvt_a_1': 'LVT(A)(1)', 'lvt_a_1_trb': 'LVT(A)(1)', 'us_lvt_a_1_trb': 'LVT(A)(1)',
    'us_halftrack_m13': 'M13 MGMC', 'us_halftrack_m15': 'M15 CGMC', 'us_halftrack_m16': 'M16 MGMC', 'tbd-1_1938': 'TBD-1',
    'i-153_m62_zhukovskiy': 'И-153 М-62 (Жуковский)', 'i-15bis_krasnolutsky': 'И-15 бис (Краснолуцкий)',
    'mig_3_series_1_15': 'МиГ-3 серия 1-15', 'hp52_hampden_tbmk1_ussr_utk1': 'Hampden TB Mk I (СССР)',
    'pby-5a_ussr': 'PBY-5A (СССР)', 'p-40e_ussr': 'P-40E (СССР)', 'pe-3_early': 'Пе-3 ранний',
    'tandem_mai': 'Тандем МАИ', 'yak_2_kabb': 'Як-2 КАББ',
}
COUNTRY_SUFFIXES_V223 = {
    'usa':'USA','us':'USA','ussr':'СССР','germany':'Germany','germ':'Germany','uk':'Great Britain','britain':'Great Britain',
    'jp':'Japan','japan':'Japan','cn':'China','china':'China','it':'Italy','italy':'Italy','fr':'France','france':'France','sw':'Sweden','sweden':'Sweden','il':'Israel','israel':'Israel'
}
TECHNICAL_WORDS_DROP_V223 = {'destroyer','cruiser','battleship','battlecruiser','frigate','boat','gun','heavy','light','tank','spaa','bomber','fighter','attacker','assault','helicopter','premium','event','naval','aircraft'}
LOWER_MODEL_WORDS_V223 = {'otsu','ko','hei','tei','late','early','bis','ter','mod','series','prototype'}
ROMAN_JP_V223 = {'1a':'Ia','1b':'Ib','1c':'Ic','2a':'IIa','2b':'IIb','2c':'IIc','3a':'IIIa','3b':'IIIb'}
RU_SUFFIX_V223 = {'MOSCOW':'Москва','ZHUKOVSKIY':'Жуковский','ZHUKOVSKY':'Жуковский','KRASNOLUTSKY':'Краснолуцкий','KRASNOLUTSKIY':'Краснолуцкий','POKRYSHKIN':'Покрышкин','GOLOVATYI':'Головатый','GOLOVATY':'Головатый','DOLGUSHIN':'Долгушин','SAFRONOV':'Сафронов'}

def _norm_v223(x: str) -> str:
    return re.sub(r'[\s/]+', '_', str(x or '').strip().lower().replace('‐','-').replace('‑','-').replace('‒','-').replace('–','-').replace('—','-').replace('−','-'))

def _strip_nation_v223(x: str) -> str:
    return re.sub(r'^(us|usa|ussr|germ|germany|uk|britain|jp|japan|cn|china|it|italy|fr|france|sw|sweden|il|israel)[_\-\s]+', '', _norm_v223(x), flags=re.I)

def _token_v223(tok: str) -> str:
    low = str(tok or '').lower()
    if not low: return ''
    if low in ROMAN_JP_V223: return ROMAN_JP_V223[low]
    if low in LOWER_MODEL_WORDS_V223: return low
    if low == 'saab': return 'Saab'
    if re.fullmatch(r'm\d+[a-z0-9]*', tok, re.I): return tok.upper()
    if re.fullmatch(r'[a-z]{1,4}\d+[a-z0-9]*', tok, re.I): return tok.upper()
    if re.fullmatch(r'[a-z]{1,3}', tok, re.I): return tok.upper()
    return tok[:1].upper() + tok[1:] if tok else ''

def display_name_from_identifier_v223(vid: str, row: dict[str, Any] | None = None) -> str:
    raw = _norm_v223(vid)
    stripped = _strip_nation_v223(raw)
    for key in (raw, stripped):
        if key in FRIENDLY_ID_NAME: return FRIENDLY_ID_NAME[key]
        if key in KNOWN_ID_DISPLAY_V223: return KNOWN_ID_DISPLAY_V223[key]
    words = [w for w in re.split(r'[_\-\s]+', stripped) if w]
    if words and words[0] in {'destroyer','cruiser','battleship','battlecruiser','frigate'} and len(words) > 1:
        words = words[1:]
    if len(words) >= 2 and words[0] in {'heavy','light'} and words[1] in {'tank','cruiser'}:
        words = words[2:]
    country = ''
    if words and words[-1].lower() in COUNTRY_SUFFIXES_V223:
        country = COUNTRY_SUFFIXES_V223[words.pop().lower()]
    event = False
    if words and words[-1] == 'event':
        event = True; words = words[:-1]
    if len(words) >= 2 and re.fullmatch(r'[a-z]{1,4}', words[0], re.I) and re.fullmatch(r'\d+[a-z]*', words[1], re.I):
        name = _token_v223(words[0]) + '-' + words[1].upper()
        rest = [_token_v223(w) for w in words[2:]]
        if rest: name += '-' + rest[0] + ((' ' + ' '.join(rest[1:])) if len(rest) > 1 else '')
    else:
        kept = [w for w in words if not (w in TECHNICAL_WORDS_DROP_V223 and len(words) > 1)]
        name = ' '.join(_token_v223(w) for w in kept).strip()
    name = re.sub(r'\bM(\d+[a-z0-9]*)\b', lambda m: 'M'+m.group(1).upper(), name, flags=re.I)
    name = re.sub(r'\bSaab\s+J', 'Saab J', name, flags=re.I)
    name = re.sub(r'\s+', ' ', name).strip() or display_name_from_id(vid)
    if country: name += f' ({country})'
    if event and 'Event' not in name: name += ' Event'
    return name

def is_bad_display_name_v223(name: str, vid: str) -> bool:
    n = str(name or '').strip()
    if not n: return True
    low = _norm_v223(n); raw = _norm_v223(vid); stripped = _strip_nation_v223(raw)
    if low in {raw, stripped}: return True
    if '_' in n: return True
    if re.match(r'^(us|usa|ussr|germ|germany|uk|britain|jp|japan|cn|china|it|italy|fr|france|sw|sweden|il|israel)[_\-\s]+', n, re.I): return True
    if re.search(r'\b(uk|jp|germ|ussr|cn|sw|il)[\s\-]+(destroyer|cruiser|chieftain|tank|fighter|bomber)\b', n, re.I): return True
    if re.search(r'halftrack|lvt-a-1|tbd-1-1938', n, re.I): return True
    if re.match(r'^[a-z]{2,}\s+[a-z0-9][a-z0-9\s\-]+$', n) and n[0].islower(): return True
    return False

def _fix_ru_v223(name: str, nation: str) -> str:
    s = str(name or '')
    if str(nation or '').upper() == 'USSR':
        replacements = [(r'\bMIG[-\s]?(?=\d)','МиГ-'),(r'\bYAK[-\s]?(?=\d)','Як-'),(r'\bPE[-\s]?(?=\d)','Пе-'),(r'\bSU[-\s]?(?=\d)','Су-'),(r'\bIL[-\s]?(?=\d)','Ил-'),(r'\bKOR[-\s]?(?=\d)','Кор-'),(r'\bLA[-\s]?(?=\d)','Ла-'),(r'\bI[-\s]?(?=\d)','И-'),(r'\bBB[-\s]?(?=\d)','ББ-')]
        for a,b in replacements: s = re.sub(a,b,s,flags=re.I)
        s = re.sub(r'\bBT[-\s]?(\d)', r'БТ-\1', s, flags=re.I)
        s = re.sub(r'\bKV[-\s]?(\d)', r'КВ-\1', s, flags=re.I)
        s = re.sub(r'\bT[-\s]?(\d)', r'Т-\1', s)
        s = re.sub(r'(И-\d+)\s*BIS\b', r'\1 бис', s, flags=re.I)
        s = re.sub(r'(И-\d+)-?BIS\b', r'\1 бис', s, flags=re.I)
        s = re.sub(r'\bTSS[-\s]?(\d+)\b', r'ТСС-\1', s, flags=re.I)
        s = re.sub(r'\bSERIES[-\s]?(\d+)[-\s]?(\d+)?\b', lambda m: 'серия '+m.group(1)+(('-'+m.group(2)) if m.group(2) else ''), s, flags=re.I)
        for a,b in RU_SUFFIX_V223.items(): s = re.sub(r'\b'+re.escape(a)+r'\b', b, s, flags=re.I)
        s = re.sub(r'-([А-ЯЁ][а-яё]+)$', r' (\1)', s)
        s = re.sub(r'-(бис|серия\s*\d+(?:-\d+)?|ТСС-\d+)', r' \1', s, flags=re.I)
    s = re.sub(r'\((?:Усср|ussr)\)', '(СССР)', s, flags=re.I)
    return re.sub(r'\s+', ' ', s).strip()

def base_display_name_from_row(row: dict[str, Any]) -> str:  # v2.23 override
    vid = row_identifier(row)
    raw = _norm_v223(vid); stripped = _strip_nation_v223(raw)
    for key in (raw, stripped):
        if key in FRIENDLY_ID_NAME: return FRIENDLY_ID_NAME[key]
        if key in KNOWN_ID_DISPLAY_V223: return KNOWN_ID_DISPLAY_V223[key]
    for k in ('name','title','vehicle','display_name','displayName','shortName','fullName','vehicle_name','unit_name','wikiTitle'):
        val = row.get(k)
        if val not in (None, ''):
            nm = str(val).strip()
            if not is_bad_display_name_v223(nm, vid): return nm
    return display_name_from_identifier_v223(vid, row)

def normalize_latin_display_name(name: str, vid: str) -> str:  # v2.23 override
    if not name or is_bad_display_name_v223(name, vid): name = display_name_from_identifier_v223(vid)
    name = clean_wiki_title_text(str(name)).replace('_', ' ')
    name = fix_lvt_name(name, vid)
    raw = _norm_v223(vid)
    if raw in FRIENDLY_ID_NAME: name = FRIENDLY_ID_NAME[raw]
    if raw in KNOWN_ID_DISPLAY_V223: name = KNOWN_ID_DISPLAY_V223[raw]
    name = re.sub(r'\b[a-z]{1,4}\d+[a-z0-9]*\b', lambda m: titlecase_vehicle_token(m.group(0)), name, flags=re.I)
    name = re.sub(r'\bsaab\b', 'Saab', name, flags=re.I)
    name = re.sub(r'\((?:Усср|ussr)\)', '(СССР)', name, flags=re.I)
    return re.sub(r'\s+', ' ', name).strip()

def deterministic_display_name(row: dict[str, Any]) -> str:  # v2.23 override
    vid = row_identifier(row)
    nation = nation_normalized(row)
    ov = override_display_name(row, nation)
    if ov: return ov
    raw = base_display_name_from_row(row)
    if is_bad_display_name_v223(raw, vid): raw = display_name_from_identifier_v223(vid, row)
    return _fix_ru_v223(normalize_latin_display_name(raw, vid), nation)

def final_display_name(row: dict[str, Any], vid: str, nation: str) -> str:  # v2.23/v2.24 override
    ov = override_display_name(row, nation)
    if ov: return ov
    try:
        local = LOCAL_DISPLAY_V224.get(norm_id(vid))
        if local: return local
    except Exception:
        pass
    raw = base_display_name_from_row(row)
    return _fix_ru_v223(normalize_latin_display_name(raw, vid), nation)
# --- end v2.23 final deterministic display-name layer ---



# --- v2.24 Wiki unit metadata/name layer ---
# This is deliberately NOT Wiki tree parsing. It opens each real /unit/<identifier> page,
# reads the visible page title and small metadata block, and then falls back locally if the unit page is missing.
WIKI_UNIT_META_STATUS_JSON = DATA_DIR / "wiki_unit_meta_status.json"
WIKI_UNIT_META_MAP: dict[str, dict[str, Any]] = {}

LOCAL_DISPLAY_V224 = {
    "uk_destroyer_st_class_saumarez": "HMS Saumarez",
    "uk_a_22f_mk_7_churchill_1944": "Churchill VII",
    "uk_a_22f_mk_7_churchill_crocodile": "Churchill Crocodile",
    "ki-45_hei_tei_china": "Ki-45 Hei/Tei (China)",
    "late_298d": "Late 298D",
    "ussr_ba_11": "БА-11",
    "ussr_d3_tk126": "ТК-126",
    "ussr_su_76m_5st_kav_corps": "СУ-76М (5гв.Кав.Корп)",
    "ussr_su_76m_1943": "СУ-76М",
}
LOCAL_RANK_V224 = {"ussr_su_76m_5st_kav_corps": "II", "ussr_su_76m_1943": "II"}
LOCAL_CLASS_V224 = {"ussr_su_76m_5st_kav_corps": "ground", "ussr_su_76m_1943": "ground", "ussr_ba_11": "ground"}

CLASS_FROM_WIKI_CATEGORY = {
    "aviation": "air", "aircraft": "air", "самолёты": "air", "авиация": "air",
    "ground vehicles": "ground", "ground": "ground", "наземная техника": "ground", "танки": "ground",
    "helicopters": "heli", "вертолёты": "heli",
    "bluewater fleet": "bluewater", "bluewater": "bluewater", "большой флот": "bluewater",
    "coastal fleet": "coastal", "coastal": "coastal", "малый флот": "coastal",
}


def _unit_slug_candidates_v224(identifier: str, row: dict[str, Any] | None = None) -> list[str]:
    vals: list[str] = []
    for v in id_variants(identifier, row or {}):
        for f in (v, v.replace("_", "-"), v.replace("-", "_")):
            if f and f not in vals:
                vals.append(f)
    return vals


def _parse_wiki_unit_meta_v224(html: str, lang: str, slug: str) -> dict[str, Any]:
    title = extract_display_name_from_unit_html(html)
    lines = _strip_html_lines(html)
    meta: dict[str, Any] = {"title": title, "lang": lang, "slug": slug}
    # Category is normally just before the title in the page header.
    for line in lines[:80]:
        key = line.strip().lower().replace("ё", "е")
        if key in CLASS_FROM_WIKI_CATEGORY:
            meta["class"] = CLASS_FROM_WIKI_CATEGORY[key]
            meta["category"] = line.strip()
            break
    # Rank: the header usually has a roman numeral adjacent to the word Rank/Ранг.
    for i, line in enumerate(lines[:100]):
        if re.fullmatch(r"(?:VIII|VII|VI|IV|V|III|II|I|[1-8])", line.strip(), flags=re.I):
            window = " ".join(lines[max(0, i-3):i+4]).lower()
            if "rank" in window or "ранг" in window or i < 45:
                meta["rank"] = normalize_rank(line.strip())
                break
    # BR values in header are useful when API data is stale. Keep conservative.
    for mode in ("AB", "RB", "SB"):
        for i, line in enumerate(lines[:120]):
            if line.strip().upper() == mode:
                for cand in lines[i+1:i+5]:
                    val = to_float(cand)
                    if val is not None:
                        meta.setdefault("br", {})[mode.lower()] = val
                        break
                break
    # Role labels vary by language. Use if close to the main-role label.
    for i, line in enumerate(lines[:120]):
        low = line.lower().replace("ё", "е")
        if "main role" in low or "основная роль" in low:
            # In extracted text the role is often the previous line in EN, next/previous in RU.
            candidates = lines[max(0, i-3):i+4]
            for cand in candidates:
                c = cand.strip()
                if c and c != line and not re.search(r"rank|ранг|battle rating|боевой рейтинг|research country|страна", c, re.I):
                    if len(c) <= 40:
                        meta["role_label"] = c
                        return meta
    return meta


def fetch_wiki_unit_meta_v224(identifier: str, row: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    nation = nation_normalized(row or {})
    # USSR benefits most from the Russian Wiki: names like СУ-76М (5гв.Кав.Корп) are official there.
    bases: list[tuple[str, str]] = []
    if str(nation).upper() == "USSR":
        bases.append(("ru", WIKI_UNIT_RU))
        bases.append(("en", WIKI_UNIT))
    else:
        bases.append(("en", WIKI_UNIT))
        bases.append(("ru", WIKI_UNIT_RU))
    last = "not found"
    for lang, base in bases:
        for slug in _unit_slug_candidates_v224(identifier, row):
            try:
                r = get_session().get(base + quote(slug), timeout=TIMEOUT, allow_redirects=True)
                if r.status_code != 200 or not r.text:
                    last = f"{lang}:{slug}:{r.status_code}"
                    continue
                meta = _parse_wiki_unit_meta_v224(r.text, lang, slug)
                title = str(meta.get("title") or "").strip()
                # Guard against nav/category pages and hard 404 shells.
                if title and not re.search(r"^(Aviation|Ground Vehicles|Bluewater|Coastal|Helicopters|War Thunder Wiki)$", title, re.I):
                    return meta, f"{lang}:{slug}"
                last = f"{lang}:{slug}:no title"
            except Exception as e:
                last = f"{lang}:{slug}:{e}"
    return {}, last


def _load_wiki_meta_cache_v224() -> dict[str, Any]:
    try:
        data = json.loads(WIKI_UNIT_META_STATUS_JSON.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("items", {})
            return data
    except Exception:
        pass
    return {"logic_version": IMAGE_LOGIC_VERSION, "items": {}}


def _save_wiki_meta_cache_v224(cache: dict[str, Any]) -> None:
    WIKI_UNIT_META_STATUS_JSON.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _name_from_wiki_or_local_v224(row: dict[str, Any], fetch_wiki: bool, retry_missing: bool, cache: dict[str, Any]) -> tuple[str, dict[str, Any], str, str]:
    vid = row_identifier(row)
    nation = nation_normalized(row)
    if not fetch_wiki:
        local = LOCAL_DISPLAY_V224.get(norm_id(vid)) or deterministic_display_name(row)
        return local, {}, "local", "wiki disabled"
    item = cache.get("items", {}).get(vid, {})
    if item.get("status") == "ok" and isinstance(item.get("meta"), dict):
        meta = item["meta"]
        title = str(meta.get("title") or "").strip()
        if title:
            return _fix_ru_v223(normalize_latin_display_name(title, vid), nation), meta, "wiki-cache", item.get("reason", "cached")
    if item.get("status") == "missing" and not retry_missing:
        return (LOCAL_DISPLAY_V224.get(norm_id(vid)) or deterministic_display_name(row)), {}, "local-after-missing", item.get("reason", "cached missing")
    meta, reason = fetch_wiki_unit_meta_v224(vid, row)
    title = str(meta.get("title") or "").strip()
    if title:
        return _fix_ru_v223(normalize_latin_display_name(title, vid), nation), meta, "wiki", reason
    return (LOCAL_DISPLAY_V224.get(norm_id(vid)) or deterministic_display_name(row)), {}, "local-fallback", reason


def build_display_name_map(rows: list[dict[str, Any]], workers: int = 12, retry_missing: bool = False, fetch_wiki: bool = True) -> dict[str, str]:  # v2.24 override
    names: dict[str, str] = {}
    global WIKI_UNIT_META_MAP
    WIKI_UNIT_META_MAP = {}
    status: dict[str, Any] = {"logic_version": IMAGE_LOGIC_VERSION, "items": {}}
    cache = _load_wiki_meta_cache_v224()
    cache["logic_version"] = IMAGE_LOGIC_VERSION
    cache.setdefault("items", {})
    print("Building display name map from real Wiki unit pages when available; local normalizer is fallback.")
    lock = threading.Lock()
    done = 0
    def worker(row: dict[str, Any]) -> tuple[str, str, dict[str, Any], str, str]:
        vid = row_identifier(row)
        nm, meta, source, reason = _name_from_wiki_or_local_v224(row, fetch_wiki, retry_missing, cache)
        return vid, nm, meta, source, reason
    tasks = [r for r in rows if row_identifier(r)]
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(worker, r) for r in tasks]
        for fut in cf.as_completed(futures):
            vid, nm, meta, source, reason = fut.result()
            done += 1
            if not nm:
                nm = display_name_from_identifier_v223(vid)
            names[vid] = nm
            if meta:
                WIKI_UNIT_META_MAP[vid] = meta
            status["items"][vid] = {"status": "ok" if nm else "missing", "name": nm, "source": source, "reason": reason, "logic_version": IMAGE_LOGIC_VERSION, "updated_at": now_iso()}
            cache["items"][vid] = {"status": "ok" if meta else "missing", "meta": meta, "reason": reason, "logic_version": IMAGE_LOGIC_VERSION, "updated_at": now_iso()}
            if done % 100 == 0:
                with lock:
                    DISPLAY_NAME_STATUS_JSON.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
                    DISPLAY_NAMES_JSON.write_text(json.dumps(names, ensure_ascii=False, indent=2), encoding="utf-8")
                    _save_wiki_meta_cache_v224(cache)
                print(f"  display/wiki names {done}/{len(tasks)} processed | wiki meta {len(WIKI_UNIT_META_MAP)}")
    DISPLAY_NAME_STATUS_JSON.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    DISPLAY_NAMES_JSON.write_text(json.dumps(names, ensure_ascii=False, indent=2), encoding="utf-8")
    _save_wiki_meta_cache_v224(cache)
    return names


def _class_from_wiki_or_api_v224(row: dict[str, Any]) -> str:
    vid = row_identifier(row)
    meta = WIKI_UNIT_META_MAP.get(vid) or {}
    cls = meta.get("class")
    if cls in CLASS_NAMES:
        return str(cls)
    rid = norm_id(vid)
    if rid in LOCAL_CLASS_V224:
        return LOCAL_CLASS_V224[rid]
    # Defensive correction: Soviet SU/СУ self-propelled guns can never be bluewater fleet.
    if rid.startswith("ussr_su_") or rid.startswith("ussr_su-"):
        return "ground"
    return normalize_vehicle_class(row)


def _rank_from_wiki_or_api_v224(row: dict[str, Any]) -> str:
    vid = row_identifier(row)
    meta = WIKI_UNIT_META_MAP.get(vid) or {}
    if meta.get("rank"):
        return normalize_rank(meta["rank"])
    rid = norm_id(row_identifier(row))
    if rid in LOCAL_RANK_V224:
        return LOCAL_RANK_V224[rid]
    return normalize_rank(first_defined(row, ["rank", "tier", "vehicle_rank"], "I"))


def _br_from_wiki_or_api_v224(row: dict[str, Any]) -> dict[str, float]:
    br = br_values(row)
    meta = WIKI_UNIT_META_MAP.get(row_identifier(row)) or {}
    mbr = meta.get("br") if isinstance(meta.get("br"), dict) else {}
    for k in ("ab", "rb", "sb"):
        if to_float(mbr.get(k)) is not None:
            br[k] = to_float(mbr.get(k))  # type: ignore[assignment]
    return br


def normalize_for_ui(row: dict[str, Any], image_map: dict[str, str]) -> dict[str, Any] | None:  # v2.24 override
    vid = row_identifier(row)
    if not vid:
        return None
    nation_norm = nation_normalized(row)
    name = final_display_name(row, vid, nation_norm)
    # Prefer the already-built name map because it may come from Wiki unit pages.
    if DISPLAY_NAME_MAP.get(vid):
        name = DISPLAY_NAME_MAP[vid]
    cls = _class_from_wiki_or_api_v224(row)
    role, role_en = role_short(row)
    rank = _rank_from_wiki_or_api_v224(row)
    search = " ".join(str(x) for x in [vid, name, pretty_name_from_id(vid), nation_norm, cls, role, role_en, first_defined(row, ["type", "vehicle_type", "tags"], ""), soviet_search_aliases(name, vid, nation_norm)] if x)
    return {
        "id": vid,
        "name": name,
        "nation": nation_norm,
        "class": cls,
        "className": CLASS_NAMES.get(cls, cls),
        "role": "Истреб." if role == "Истр." else role,
        "roleEn": role_en,
        "rank": rank,
        "br": _br_from_wiki_or_api_v224(row),
        "status": normalize_status(row),
        "source": "WT Vehicles API + Wiki unit page" if WIKI_UNIT_META_MAP.get(vid) else "WT Vehicles API",
        "url": row_image_url(row),
        "local_image": image_map.get(vid, ""),
        "search": search,
    }
# --- end v2.24 Wiki unit metadata/name layer ---


# --- v2.25 cached Wiki names, conflict diagnostics, stricter class/name finalizer ---
VEHICLE_DATA_CONFLICTS_JSON = DATA_DIR / "vehicle_data_conflicts.json"

RU_LOCAL_DISPLAY_V225 = {
    "po-2m": "ПО-2М", "ussr_po-2m": "ПО-2М", "po_2m": "ПО-2М", "ussr_po_2m": "ПО-2М",
    "ussr_ya_5m": "Я-5М", "ya_5m": "Я-5М", "ussr_ya-5m": "Я-5М", "ya-5m": "Я-5М",
    "ussr_btr_152a": "БТР-152А", "btr_152a": "БТР-152А", "ussr_btr-152a": "БТР-152А", "btr-152a": "БТР-152А",
    "lagg-3-66": "ЛАГГ-3-66", "ussr_lagg-3-66": "ЛАГГ-3-66", "lagg_3_66": "ЛАГГ-3-66", "ussr_lagg_3_66": "ЛАГГ-3-66",
    "ussr_ba_11": "БА-11", "ba_11": "БА-11", "ussr_d3_tk126": "ТК-126", "d3_tk126": "ТК-126",
    "ussr_su_76m_5st_kav_corps": "СУ-76М (5гв.Кав.Корп)", "ussr_su_76m_1943": "СУ-76М",
}

def _compact_id_v225(x: str) -> str:
    return norm_id(x).replace("-", "_")

def _api_class_guess_v225(row: dict[str, Any]) -> str:
    """Infer class from API fields without trusting broad Wiki/nav words."""
    vid = norm_id(row_identifier(row))
    blob = " ".join(str(row.get(k, "")) for k in ("type","vehicle_type","class","unitClass","category","kind","tags","role","roleName","main_role","mainRole","name","title")).lower().replace("_", " ")
    blob += " " + vid.replace("_", " ").replace("-", " ")
    # Strong ID/family hints first.
    if re.search(r"\b(heli|helicopter)\b", blob): return "heli"
    if re.search(r"\b(destroyer|cruiser|battleship|battlecruiser|bluewater|frigate)\b", blob): return "bluewater"
    if re.search(r"\b(coastal|gun boat|gunboat|torpedo boat|submarine chaser|barge|naval ferry|boat)\b", blob): return "coastal"
    if re.search(r"\b(tank|ground|spaa|sam|atgm|army|armou?red|tank destroyer|light tank|medium tank|heavy tank|средний танк|легкий танк|лёгкий танк|тяжелый танк|тяжёлый танк|зсу|пт сау|сау)\b", blob): return "ground"
    if re.search(r"\b(fighter|bomber|attacker|assault|interceptor|aircraft|aviation|dive bomber|torpedo bomber)\b", blob): return "air"
    # Identifier families that API sometimes describes too tersely.
    if re.match(r"^(ussr|us|germ|uk|jp|cn|it|fr|sw|il)_(btr|ba|bt|kv|is_|is-|t_|t-|su_|su-|zis|gaz|m\d|lvt|halftrack)", vid):
        return "ground"
    return normalize_vehicle_class(row)

def _class_from_role_v225(row: dict[str, Any]) -> str | None:
    role, role_en = role_short(row)
    r = (role + " " + role_en).lower()
    if re.search(r"зсу|пт|сау|танк|spaa|tank|destroyer", r): return "ground"
    if re.search(r"верт|helicopter", r): return "heli"
    if re.search(r"эсм|крейсер|лин|фрег|катер|баржа|destroyer|cruiser|battleship|battlecruiser|frigate|boat|barge", r):
        if re.search(r"катер|баржа|boat|barge", r): return "coastal"
        return "bluewater"
    if re.search(r"истреб|бомб|штурм|пикир|fighter|bomber|attacker", r): return "air"
    return None

def _wiki_class_safe_v225(row: dict[str, Any]) -> str | None:
    vid = row_identifier(row)
    meta = WIKI_UNIT_META_MAP.get(vid) or {}
    cls = meta.get("class")
    if cls in CLASS_NAMES:
        return str(cls)
    return None

def final_class_v225(row: dict[str, Any]) -> tuple[str, dict[str, str]]:
    vid = row_identifier(row)
    local = LOCAL_CLASS_V224.get(norm_id(vid))
    role_cls = _class_from_role_v225(row)
    api_cls = _api_class_guess_v225(row)
    wiki_cls = _wiki_class_safe_v225(row)
    # v2.24 could be polluted by generic Wiki navigation lines. Trust role/API before Wiki unless API is absent.
    final = local or role_cls or api_cls or wiki_cls or "air"
    # Hard guards against impossible classes.
    rid = norm_id(vid)
    if rid.startswith(("ussr_su_", "ussr_su-", "ussr_btr", "ussr_ba_", "ussr_t_", "ussr_kv_", "ussr_is_")):
        final = "ground"
    return final, {"apiClass": api_cls, "wikiClass": wiki_cls or "", "roleClass": role_cls or "", "localClass": local or ""}

def _rank_sources_v225(row: dict[str, Any]) -> tuple[str, dict[str, str]]:
    vid = row_identifier(row)
    api_rank = normalize_rank(first_defined(row, ["rank", "tier", "vehicle_rank"], "I"))
    meta = WIKI_UNIT_META_MAP.get(vid) or {}
    wiki_rank = normalize_rank(meta.get("rank")) if meta.get("rank") else ""
    local_rank = LOCAL_RANK_V224.get(norm_id(vid), "")
    final = local_rank or wiki_rank or api_rank
    return final, {"apiRank": api_rank, "wikiRank": wiki_rank, "localRank": local_rank}

def _br_sources_v225(row: dict[str, Any]) -> tuple[dict[str, float], dict[str, Any]]:
    api_br = br_values(row)
    final = dict(api_br)
    meta = WIKI_UNIT_META_MAP.get(row_identifier(row)) or {}
    wiki_br = meta.get("br") if isinstance(meta.get("br"), dict) else {}
    for k in ("ab", "rb", "sb"):
        val = to_float(wiki_br.get(k))
        if val is not None:
            final[k] = val
    return final, {"apiBR": api_br, "wikiBR": wiki_br or {}}

def _cyr_model_suffix_v225(s: str) -> str:
    # Convert common Russian-model Latin suffix letters without damaging NATO names in non-USSR rows.
    repl = {"A":"А", "B":"Б", "V":"В", "G":"Г", "D":"Д", "E":"Е", "K":"К", "M":"М", "N":"Н", "P":"П", "R":"Р", "S":"С", "T":"Т", "U":"У"}
    def one(m: re.Match[str]) -> str:
        return m.group(1) + repl.get(m.group(2).upper(), m.group(2))
    return re.sub(r"(\d)([ABVGDEKMNPRSTU])\b", one, s, flags=re.I)

def _ru_name_final_v225(name: str, vid: str, nation: str) -> str:
    rid = _compact_id_v225(vid)
    for key in (norm_id(vid), rid, norm_id(vid).replace("_", "-")):
        if key in RU_LOCAL_DISPLAY_V225:
            return RU_LOCAL_DISPLAY_V225[key]
    if str(nation).upper() != "USSR":
        return name
    s = str(name or "").strip()
    # These are vehicle designations in Russian UI; user explicitly wants all-caps forms like ПО/Я/БТР/ЛАГГ.
    replacements = [
        (r"\bPO[-\s]?(?=\d)", "ПО-"), (r"\bPo[-\s]?(?=\d)", "ПО-"),
        (r"\bYA[-\s]?(?=\d)", "Я-"), (r"\bYa[-\s]?(?=\d)", "Я-"),
        (r"\bBTR[-\s]?(?=\d)", "БТР-"), (r"\bBA[-\s]?(?=\d)", "БА-"),
        (r"\bLAGG[-\s]?(?=\d)", "ЛАГГ-"), (r"\bLaGG[-\s]?(?=\d)", "ЛАГГ-"),
        (r"\bSU[-\s]?(?=\d)", "СУ-"), (r"\bSu[-\s]?(?=\d)", "СУ-"),
        (r"\bIL[-\s]?(?=\d)", "ИЛ-"), (r"\bMIG[-\s]?(?=\d)", "МиГ-"), (r"\bYAK[-\s]?(?=\d)", "Як-"),
    ]
    for a,b in replacements:
        s = re.sub(a,b,s,flags=re.I)
    s = _cyr_model_suffix_v225(s)
    s = re.sub(r"\((?:Усср|USSR|ussr)\)", "(СССР)", s)
    return re.sub(r"\s+", " ", s).strip()

def final_display_name(row: dict[str, Any], vid: str, nation: str) -> str:  # v2.25 override
    ov = override_display_name(row, nation)
    if ov:
        return ov
    raw = DISPLAY_NAME_MAP.get(vid) or LOCAL_DISPLAY_V224.get(norm_id(vid)) or base_display_name_from_row(row)
    name = _fix_ru_v223(normalize_latin_display_name(raw, vid), nation)
    return _ru_name_final_v225(name, vid, nation)

def _conflict_entry_v225(row: dict[str, Any], item: dict[str, Any], src: dict[str, Any]) -> dict[str, Any] | None:
    conflicts: list[str] = []
    api_cls, wiki_cls, final_cls = src.get("apiClass",""), src.get("wikiClass",""), item.get("class","")
    api_rank, wiki_rank, final_rank = src.get("apiRank",""), src.get("wikiRank",""), item.get("rank","")
    if wiki_cls and api_cls and wiki_cls != api_cls:
        conflicts.append("class_api_vs_wiki")
    if final_cls and api_cls and final_cls != api_cls:
        conflicts.append("class_final_vs_api")
    if final_cls and wiki_cls and final_cls != wiki_cls:
        conflicts.append("class_final_vs_wiki")
    if wiki_rank and api_rank and wiki_rank != api_rank:
        conflicts.append("rank_api_vs_wiki")
    if final_rank and api_rank and final_rank != api_rank:
        conflicts.append("rank_final_vs_api")
    if final_rank and wiki_rank and final_rank != wiki_rank:
        conflicts.append("rank_final_vs_wiki")
    name = str(item.get("name", ""))
    if is_bad_display_name_v223(name, row_identifier(row)):
        conflicts.append("suspicious_display_name")
    if not conflicts:
        return None
    meta = WIKI_UNIT_META_MAP.get(row_identifier(row)) or {}
    return {
        "identifier": row_identifier(row),
        "displayName": name,
        "nation": item.get("nation"),
        "apiClass": api_cls,
        "wikiClass": wiki_cls,
        "roleClass": src.get("roleClass", ""),
        "finalClass": final_cls,
        "apiRank": api_rank,
        "wikiRank": wiki_rank,
        "finalRank": final_rank,
        "apiBR": src.get("apiBR", {}),
        "wikiBR": src.get("wikiBR", {}),
        "finalBR": item.get("br", {}),
        "conflictType": sorted(set(conflicts)),
        "resolutionSource": "local/role/api guarded finalizer; Wiki used when safe",
        "wikiTitle": meta.get("title", ""),
        "wikiLang": meta.get("lang", ""),
    }

def normalize_for_ui(row: dict[str, Any], image_map: dict[str, str]) -> dict[str, Any] | None:  # v2.25 override
    vid = row_identifier(row)
    if not vid:
        return None
    nation_norm = nation_normalized(row)
    name = final_display_name(row, vid, nation_norm)
    cls, class_src = final_class_v225(row)
    role, role_en = role_short(row)
    rank, rank_src = _rank_sources_v225(row)
    br, br_src = _br_sources_v225(row)
    search = " ".join(str(x) for x in [vid, name, pretty_name_from_id(vid), nation_norm, cls, role, role_en, first_defined(row, ["type", "vehicle_type", "tags"], ""), soviet_search_aliases(name, vid, nation_norm)] if x)
    item = {
        "id": vid, "name": name, "nation": nation_norm, "class": cls, "className": CLASS_NAMES.get(cls, cls),
        "role": "Истреб." if role == "Истр." else role, "roleEn": role_en, "rank": rank, "br": br,
        "status": normalize_status(row), "source": "WT Vehicles API + cached Wiki unit page" if WIKI_UNIT_META_MAP.get(vid) else "WT Vehicles API",
        "url": row_image_url(row), "local_image": image_map.get(vid, ""), "search": search,
    }
    item["_debugSources"] = {**class_src, **rank_src, **br_src}
    return item

def write_lightweight_vehicles(rows: list[dict[str, Any]], image_map: dict[str, str]) -> None:  # v2.25 override
    out: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        item = normalize_for_ui(row, image_map)
        if not item or item["id"] in seen:
            continue
        seen.add(item["id"])
        src = item.pop("_debugSources", {})
        ce = _conflict_entry_v225(row, item, src)
        if ce:
            conflicts.append(ce)
        out.append(item)
    ranks = list(RANK_MAP.values())
    out.sort(key=lambda x: (x.get("nation",""), x.get("class",""), ranks.index(x.get("rank","I")) if x.get("rank","I") in ranks else 99, x.get("br",{}).get("ab",99), x.get("name","")))
    (DATA_DIR / "vehicles.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    VEHICLE_DATA_CONFLICTS_JSON.write_text(json.dumps({"logic_version": IMAGE_LOGIC_VERSION, "count": len(conflicts), "items": conflicts}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Vehicle data conflict report: {len(conflicts)} potential conflicts written to data/vehicle_data_conflicts.json")
# --- end v2.25 layer ---


# --- v2.26 dual Wiki locale names + cleaner conflict/class finalizer ---
DISPLAY_NAMES_RU_JSON = DATA_DIR / "display_names_ru.json"
DISPLAY_NAMES_EN_JSON = DATA_DIR / "display_names_en.json"
WIKI_UNIT_META_STATUS_RU_JSON = DATA_DIR / "wiki_unit_meta_status_ru.json"
WIKI_UNIT_META_STATUS_EN_JSON = DATA_DIR / "wiki_unit_meta_status_en.json"
DISPLAY_NAME_MAP_RU: dict[str, str] = {}
DISPLAY_NAME_MAP_EN: dict[str, str] = {}


def _load_lang_cache_v226(path: Path) -> dict[str, Any]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("items", {})
            return d
    except Exception:
        pass
    return {"logic_version": IMAGE_LOGIC_VERSION, "items": {}}


def _save_lang_cache_v226(path: Path, cache: dict[str, Any]) -> None:
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _fetch_one_lang_unit_meta_v226(identifier: str, row: dict[str, Any], lang: str, retry_missing: bool, cache: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    vid = row_identifier(row)
    item = cache.get("items", {}).get(vid, {})
    if item.get("status") == "ok" and isinstance(item.get("meta"), dict) and item.get("meta", {}).get("title"):
        return item["meta"], "wiki-cache-" + lang, item.get("reason", "cached")
    if item.get("status") == "missing" and not retry_missing:
        return {}, "missing-cache-" + lang, item.get("reason", "cached missing")
    base = WIKI_UNIT_RU if lang == "ru" else WIKI_UNIT
    last = "not found"
    for slug in _unit_slug_candidates_v224(identifier, row):
        try:
            r = get_session().get(base + quote(slug), timeout=TIMEOUT, allow_redirects=True)
            if r.status_code != 200 or not r.text:
                last = f"{lang}:{slug}:{r.status_code}"
                continue
            meta = _parse_wiki_unit_meta_v224(r.text, lang, slug)
            title = str(meta.get("title") or "").strip()
            if title and not re.search(r"^(Aviation|Ground Vehicles|Bluewater|Coastal|Helicopters|War Thunder Wiki)$", title, re.I):
                return meta, "wiki-" + lang, f"{lang}:{slug}"
            last = f"{lang}:{slug}:no title"
        except Exception as e:
            last = f"{lang}:{slug}:{e}"
    return {}, "local-" + lang, last


def _title_to_name_v226(title: str, vid: str, nation: str, lang: str) -> str:
    if not title:
        return ""
    if lang == "ru":
        return _ru_name_final_v225(_fix_ru_v223(str(title).strip(), nation), vid, nation)
    return normalize_latin_display_name(str(title).strip(), vid)


def build_display_name_map(rows: list[dict[str, Any]], workers: int = 12, retry_missing: bool = False, fetch_wiki: bool = True) -> dict[str, str]:  # v2.26 override
    """Build two official Wiki-name maps: RU from wiki.warthunder.ru, EN from wiki.warthunder.com.
    display_names.json stays RU for backward compatibility with the current Russian UI.
    Existing per-language cache entries are reused, so future updates do not re-download known unit pages.
    """
    global WIKI_UNIT_META_MAP, DISPLAY_NAME_MAP_RU, DISPLAY_NAME_MAP_EN
    WIKI_UNIT_META_MAP = {}
    DISPLAY_NAME_MAP_RU, DISPLAY_NAME_MAP_EN = {}, {}
    ru_cache = _load_lang_cache_v226(WIKI_UNIT_META_STATUS_RU_JSON)
    en_cache = _load_lang_cache_v226(WIKI_UNIT_META_STATUS_EN_JSON)
    status: dict[str, Any] = {"logic_version": IMAGE_LOGIC_VERSION, "items": {}}
    tasks = [r for r in rows if row_identifier(r)]
    print("Building RU/EN display name maps from real Wiki unit pages; cached known unit pages are skipped.")
    done = 0
    lock = threading.Lock()
    def worker(row: dict[str, Any]) -> tuple[str, str, str, dict[str, Any], dict[str, Any], str, str]:
        vid = row_identifier(row)
        nation = nation_normalized(row)
        if not fetch_wiki:
            local = deterministic_display_name(row)
            return vid, _ru_name_final_v225(local, vid, nation), local, {}, {}, "local", "wiki disabled"
        ru_meta, ru_src, ru_reason = _fetch_one_lang_unit_meta_v226(vid, row, "ru", retry_missing, ru_cache)
        en_meta, en_src, en_reason = _fetch_one_lang_unit_meta_v226(vid, row, "en", retry_missing, en_cache)
        ru_title = str(ru_meta.get("title") or "").strip()
        en_title = str(en_meta.get("title") or "").strip()
        local_ru = RU_LOCAL_DISPLAY_V225.get(norm_id(vid)) or RU_LOCAL_DISPLAY_V225.get(_compact_id_v225(vid)) or ""
        ru_name = local_ru or _title_to_name_v226(ru_title, vid, nation, "ru") or _ru_name_final_v225(deterministic_display_name(row), vid, nation)
        en_name = _title_to_name_v226(en_title, vid, nation, "en") or normalize_latin_display_name(deterministic_display_name(row), vid)
        return vid, ru_name, en_name, ru_meta, en_meta, ru_reason, en_reason
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(worker, r) for r in tasks]
        for fut in cf.as_completed(futures):
            vid, ru_name, en_name, ru_meta, en_meta, ru_reason, en_reason = fut.result()
            done += 1
            DISPLAY_NAME_MAP_RU[vid] = ru_name
            DISPLAY_NAME_MAP_EN[vid] = en_name
            # Keep one meta map for class/rank; prefer EN metadata if available because class labels are parsed more reliably, RU title remains stored separately.
            meta = dict(en_meta or ru_meta or {})
            if ru_meta.get("title"):
                meta["title_ru"] = ru_meta.get("title")
            if en_meta.get("title"):
                meta["title_en"] = en_meta.get("title")
            if meta:
                WIKI_UNIT_META_MAP[vid] = meta
            ru_cache["items"][vid] = {"status": "ok" if ru_meta else "missing", "meta": ru_meta, "name": ru_name, "reason": ru_reason, "logic_version": IMAGE_LOGIC_VERSION, "updated_at": now_iso()}
            en_cache["items"][vid] = {"status": "ok" if en_meta else "missing", "meta": en_meta, "name": en_name, "reason": en_reason, "logic_version": IMAGE_LOGIC_VERSION, "updated_at": now_iso()}
            status["items"][vid] = {"status": "ok", "name_ru": ru_name, "name_en": en_name, "ru_reason": ru_reason, "en_reason": en_reason, "logic_version": IMAGE_LOGIC_VERSION, "updated_at": now_iso()}
            if done % 100 == 0:
                with lock:
                    DISPLAY_NAMES_RU_JSON.write_text(json.dumps(DISPLAY_NAME_MAP_RU, ensure_ascii=False, indent=2), encoding="utf-8")
                    DISPLAY_NAMES_EN_JSON.write_text(json.dumps(DISPLAY_NAME_MAP_EN, ensure_ascii=False, indent=2), encoding="utf-8")
                    DISPLAY_NAMES_JSON.write_text(json.dumps(DISPLAY_NAME_MAP_RU, ensure_ascii=False, indent=2), encoding="utf-8")
                    DISPLAY_NAME_STATUS_JSON.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
                    _save_lang_cache_v226(WIKI_UNIT_META_STATUS_RU_JSON, ru_cache)
                    _save_lang_cache_v226(WIKI_UNIT_META_STATUS_EN_JSON, en_cache)
                print(f"  RU/EN wiki names {done}/{len(tasks)} processed | cached ru {sum(1 for x in ru_cache['items'].values() if x.get('status')=='ok')} | cached en {sum(1 for x in en_cache['items'].values() if x.get('status')=='ok')}")
    DISPLAY_NAMES_RU_JSON.write_text(json.dumps(DISPLAY_NAME_MAP_RU, ensure_ascii=False, indent=2), encoding="utf-8")
    DISPLAY_NAMES_EN_JSON.write_text(json.dumps(DISPLAY_NAME_MAP_EN, ensure_ascii=False, indent=2), encoding="utf-8")
    DISPLAY_NAMES_JSON.write_text(json.dumps(DISPLAY_NAME_MAP_RU, ensure_ascii=False, indent=2), encoding="utf-8")
    DISPLAY_NAME_STATUS_JSON.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    _save_lang_cache_v226(WIKI_UNIT_META_STATUS_RU_JSON, ru_cache)
    _save_lang_cache_v226(WIKI_UNIT_META_STATUS_EN_JSON, en_cache)
    return DISPLAY_NAME_MAP_RU


def final_display_name(row: dict[str, Any], vid: str, nation: str) -> str:  # v2.26 override, Russian UI default
    ov = override_display_name(row, nation)
    if ov:
        return ov
    raw = DISPLAY_NAME_MAP_RU.get(vid) or DISPLAY_NAME_MAP.get(vid) or RU_LOCAL_DISPLAY_V225.get(norm_id(vid)) or base_display_name_from_row(row)
    return _ru_name_final_v225(_fix_ru_v223(str(raw), nation), vid, nation)


def final_class_v225(row: dict[str, Any]) -> tuple[str, dict[str, str]]:  # v2.26 override: API/ID before broad role fallback
    vid = row_identifier(row)
    local = LOCAL_CLASS_V224.get(norm_id(vid))
    api_cls = _api_class_guess_v225(row)
    role_cls = _class_from_role_v225(row)
    wiki_cls = _wiki_class_safe_v225(row)
    final = local or api_cls or role_cls or wiki_cls or "air"
    rid = norm_id(vid)
    if rid.startswith(("ussr_su_", "ussr_su-", "ussr_btr", "ussr_ba_", "ussr_t_", "ussr_kv_", "ussr_is_")):
        final = "ground"
    return final, {"apiClass": api_cls, "wikiClass": wiki_cls or "", "roleClass": role_cls or "", "localClass": local or ""}


def normalize_for_ui(row: dict[str, Any], image_map: dict[str, str]) -> dict[str, Any] | None:  # v2.26 override
    item = globals()["normalize_for_ui_v226_base"](row, image_map) if False else None
    vid = row_identifier(row)
    if not vid:
        return None
    nation_norm = nation_normalized(row)
    name_ru = final_display_name(row, vid, nation_norm)
    name_en = DISPLAY_NAME_MAP_EN.get(vid) or normalize_latin_display_name(base_display_name_from_row(row), vid)
    cls, class_src = final_class_v225(row)
    role, role_en = role_short(row)
    rank, rank_src = _rank_sources_v225(row)
    br, br_src = _br_sources_v225(row)
    search = " ".join(str(x) for x in [vid, name_ru, name_en, pretty_name_from_id(vid), nation_norm, cls, role, role_en, first_defined(row, ["type", "vehicle_type", "tags"], ""), soviet_search_aliases(name_ru, vid, nation_norm)] if x)
    item = {
        "id": vid, "name": name_ru, "nameRu": name_ru, "nameEn": name_en, "nation": nation_norm, "class": cls, "className": CLASS_NAMES.get(cls, cls),
        "role": "Истреб." if role == "Истр." else role, "roleEn": role_en, "rank": rank, "br": br,
        "status": normalize_status(row), "source": "WT Vehicles API + RU/EN Wiki unit pages" if WIKI_UNIT_META_MAP.get(vid) else "WT Vehicles API",
        "url": row_image_url(row), "local_image": image_map.get(vid, ""), "search": search,
    }
    item["_debugSources"] = {**class_src, **rank_src, **br_src}
    return item


def _conflict_entry_v225(row: dict[str, Any], item: dict[str, Any], src: dict[str, Any]) -> dict[str, Any] | None:  # v2.26 override: reduce false positives
    conflicts: list[str] = []
    api_cls, wiki_cls, final_cls = src.get("apiClass",""), src.get("wikiClass",""), item.get("class","")
    api_rank, wiki_rank, final_rank = src.get("apiRank",""), src.get("wikiRank",""), item.get("rank","")
    if wiki_cls and api_cls and wiki_cls != api_cls: conflicts.append("class_api_vs_wiki")
    if final_cls and api_cls and final_cls != api_cls: conflicts.append("class_final_vs_api")
    if wiki_rank and api_rank and wiki_rank != api_rank: conflicts.append("rank_api_vs_wiki")
    name = str(item.get("name", ""))
    if is_bad_display_name_v223(name, row_identifier(row)): conflicts.append("suspicious_display_name")
    if not conflicts: return None
    meta = WIKI_UNIT_META_MAP.get(row_identifier(row)) or {}
    return {"identifier": row_identifier(row), "displayName": name, "displayNameEn": item.get("nameEn", ""), "nation": item.get("nation"), "apiClass": api_cls, "wikiClass": wiki_cls, "roleClass": src.get("roleClass", ""), "finalClass": final_cls, "apiRank": api_rank, "wikiRank": wiki_rank, "finalRank": final_rank, "apiBR": src.get("apiBR", {}), "wikiBR": src.get("wikiBR", {}), "finalBR": item.get("br", {}), "conflictType": sorted(set(conflicts)), "resolutionSource": "local/API guarded finalizer; RU/EN Wiki names are cached separately", "wikiTitleRu": meta.get("title_ru", ""), "wikiTitleEn": meta.get("title_en", meta.get("title", "")), "wikiLang": meta.get("lang", "")}


def parse_args(argv: list[str]) -> argparse.Namespace:  # v2.26 override for clearing new name caches too
    p = argparse.ArgumentParser(description="Update War Thunder roster data and local image/name cache.")
    p.add_argument("--workers", type=int, default=int(os.environ.get("WT_RM_IMAGE_WORKERS", "24")))
    p.add_argument("--retry-missing", action="store_true")
    p.add_argument("--force-images", action="store_true")
    p.add_argument("--skip-images", action="store_true")
    p.add_argument("--skip-tree-links", action="store_true")
    p.add_argument("--tree-workers", type=int, default=int(os.environ.get("WT_RM_TREE_WORKERS", "24")))
    p.add_argument("--retry-tree-missing", action="store_true")
    p.add_argument("--skip-tree-order", action="store_true")
    p.add_argument("--skip-names", action="store_true")
    p.add_argument("--retry-names", action="store_true")
    p.add_argument("--fetch-wiki-names", action="store_true", default=True)
    p.add_argument("--no-wiki-names", action="store_false", dest="fetch_wiki_names")
    p.add_argument("--clear-name-cache", action="store_true")
    return p.parse_args(argv)
# --- end v2.26 layer ---


# --- v2.27 class/name finalizer fixes ---
def _api_class_guess_v225(row: dict[str, Any]) -> str:  # v2.27 override: tank_destroyer before naval destroyer
    vid = norm_id(row_identifier(row))
    blob = " ".join(str(row.get(k, "")) for k in ("type","vehicle_type","class","unitClass","category","kind","tags","role","roleName","main_role","mainRole","name","title")).lower().replace("_", " ")
    blob += " " + vid.replace("_", " ").replace("-", " ")
    # Ground roles first: otherwise "tank destroyer" was matched as naval destroyer.
    if re.search(r"\b(tank destroyer|tank_destroyer|пт\s*сау|сау|self.?propelled|spaa|anti.?air|sam|atgm|light tank|medium tank|heavy tank|main battle tank|armou?red|tank|ground|army|зсу)\b", blob):
        return "ground"
    if re.match(r"^(ussr|us|germ|uk|jp|cn|it|fr|sw|il)_(m50|asu|bm_|btr|ba_|bt_|kv_|is_|t_|su_|object_|zis|gaz|ya_|zsu|m\d|t\d|lvt|halftrack)", vid):
        return "ground"
    if re.match(r"^(m50|asu|bm_|btr|ba_|bt_|kv_|is_|t_|su_|object_|zis|gaz|ya_|zsu)", vid):
        return "ground"
    if re.search(r"\b(heli|helicopter)\b", blob): return "heli"
    if re.search(r"\b(battlecruiser|battleship|cruiser|destroyer|bluewater|frigate)\b", blob): return "bluewater"
    if re.search(r"\b(coastal|gun boat|gunboat|torpedo boat|submarine chaser|barge|naval ferry|boat)\b", blob): return "coastal"
    if re.search(r"\b(fighter|bomber|attacker|assault|interceptor|aircraft|aviation|dive bomber|torpedo bomber)\b", blob): return "air"
    return normalize_vehicle_class(row)


def _class_from_role_v225(row: dict[str, Any]) -> str | None:  # v2.27 override
    role, role_en = role_short(row)
    r = (role + " " + role_en).lower()
    if re.search(r"пт|сау|зсу|spaa|tank destroyer|light tank|medium tank|heavy tank|tank\b", r): return "ground"
    if re.search(r"верт|helicopter", r): return "heli"
    if re.search(r"эсм|крейсер|лин|фрег|катер|баржа|destroyer|cruiser|battleship|battlecruiser|frigate|boat|barge", r):
        if re.search(r"катер|баржа|boat|barge", r): return "coastal"
        return "bluewater"
    if re.search(r"истреб|бомб|штурм|пикир|fighter|bomber|attacker", r): return "air"
    return None


def final_class_v225(row: dict[str, Any]) -> tuple[str, dict[str, str]]:  # v2.27 override
    vid = row_identifier(row)
    rid = norm_id(vid)
    local = LOCAL_CLASS_V224.get(rid)
    api_cls = _api_class_guess_v225(row)
    role_cls = _class_from_role_v225(row)
    wiki_cls = _wiki_class_safe_v225(row)
    final = local or api_cls or role_cls or wiki_cls or "air"
    if re.match(r"^(ussr|us|germ|uk|jp|cn|it|fr|sw|il)_(m50|asu|bm_|btr|ba_|bt_|kv_|is_|t_|su_|object_|zis|gaz|ya_|zsu|m\d|t\d|lvt|halftrack)", rid):
        final = "ground"
    if rid.startswith(("ussr_su_", "ussr_su-", "ussr_btr", "ussr_ba_", "ussr_t_", "ussr_kv_", "ussr_is_")):
        final = "ground"
    return final, {"apiClass": api_cls, "wikiClass": wiki_cls or "", "roleClass": role_cls or "", "localClass": local or ""}


def final_display_name(row: dict[str, Any], vid: str, nation: str) -> str:  # v2.27 override: strip country prefixes after Wiki/local selection
    ov = override_display_name(row, nation)
    raw = ov or DISPLAY_NAME_MAP_RU.get(vid) or DISPLAY_NAME_MAP.get(vid) or RU_LOCAL_DISPLAY_V225.get(norm_id(vid)) or base_display_name_from_row(row)
    name = _ru_name_final_v225(_fix_ru_v223(str(raw), nation), vid, nation)
    name = re.sub(r"^(?:us|usa|ussr|uk|jp|cn|germ|fr|it|sw|il)\s+", "", name, flags=re.I).strip()
    name = re.sub(r"^(?:USA|USSR)\s+", "", name).strip()
    return name
# --- end v2.27 layer ---



# --- v2.30 robust API update guard: never overwrite a full roster with a partial API response ---
MIN_REASONABLE_VEHICLES_V230 = int(os.environ.get("WT_RM_MIN_VEHICLES", "1000"))
FULL_ROSTER_EXPECTED_HINT_V230 = int(os.environ.get("WT_RM_EXPECTED_VEHICLES", "3000"))


def _read_json_list_count_v230(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return len(data.get("items", []))
    except Exception:
        pass
    return 0


def _existing_roster_count_v230() -> int:
    return max(
        _read_json_list_count_v230(DATA_DIR / "vehicles.json"),
        _read_json_list_count_v230(DATA_DIR / "vehicles_api_raw.json"),
    )


def _fetch_mode_v230(mode: str, first_page_retries: int = 3) -> list[dict[str, Any]]:
    all_rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    duplicate_pages = 0
    for i in range(0, 10000):
        params = {"limit": LIMIT, mode: i if mode == "page" else i * LIMIT}
        attempt = 0
        while True:
            try:
                payload = get_json(API_BASE, params)
                break
            except Exception as e:
                attempt += 1
                if i == 0 and attempt < first_page_retries:
                    print(f"  {mode} pagination first request failed, retry {attempt}/{first_page_retries - 1}: {e}")
                    time.sleep(1.0 * attempt)
                    continue
                if i == 0:
                    print(f"  {mode} pagination failed at first request: {e}")
                else:
                    print(f"  {mode} pagination stopped at page {i}: {e}")
                return all_rows
        items = extract_items(payload)
        if not items:
            break
        added_this_page = 0
        for row in items:
            vid = row_identifier(row)
            if not vid or vid in seen_ids:
                continue
            seen_ids.add(vid)
            all_rows.append(row)
            added_this_page += 1
        print(f"  {mode} {i}: {len(items)} rows, {added_this_page} new")
        if added_this_page == 0:
            duplicate_pages += 1
            # API ignored the paging parameter or returned the same page again.
            print(f"  {mode} pagination returned no new vehicles; treating this mode as exhausted.")
            break
        if len(items) < LIMIT:
            break
    return all_rows


def fetch_all_vehicles() -> list[dict[str, Any]]:  # v2.30 override
    print("Downloading vehicles from WT Vehicles API...")
    candidates: list[tuple[str, list[dict[str, Any]]]] = []
    for mode in ("page", "offset"):
        rows = _fetch_mode_v230(mode)
        if rows:
            candidates.append((mode, rows))
            print(f"  {mode} pagination candidate: {len(rows)} unique vehicles.")
        # If page pagination produced a full-looking roster, do not waste time on offset.
        if mode == "page" and len(rows) >= FULL_ROSTER_EXPECTED_HINT_V230:
            print(f"Downloaded {len(rows)} vehicles using page pagination.")
            return rows
    if not candidates:
        # v2.48: keep the local cache usable when the community API is down.
        for cached_path in (DATA_DIR / "vehicles_api_raw.json", DATA_DIR / "vehicles.json"):
            try:
                if cached_path.exists():
                    cached_rows = json.loads(cached_path.read_text(encoding="utf-8"))
                    if isinstance(cached_rows, list) and len(cached_rows) >= MIN_REASONABLE_VEHICLES_V230:
                        print("WARNING: WT Vehicles API is unavailable; using existing local cache:", cached_path)
                        print(f"  local cache vehicles: {len(cached_rows)}")
                        return cached_rows
            except Exception as e:
                print(f"WARNING: could not use local cache {cached_path}: {e}")
        raise RuntimeError("Could not download vehicles from API; no pagination mode returned data and no usable local cache was found. Copy a previous data/ folder into this build or try again later.")
    mode, rows = max(candidates, key=lambda x: len(x[1]))
    print(f"Best API candidate: {mode} pagination with {len(rows)} unique vehicles.")
    return rows


def _validate_roster_size_v230(rows: list[dict[str, Any]]) -> None:
    new_count = len(rows)
    old_count = _existing_roster_count_v230()
    required = MIN_REASONABLE_VEHICLES_V230
    if old_count >= MIN_REASONABLE_VEHICLES_V230:
        required = max(required, int(old_count * 0.80))
    if new_count < required:
        raise RuntimeError(
            "Partial API update refused: downloaded "
            f"{new_count} vehicles, existing roster has {old_count}. "
            "Keeping previous data. Try update_from_api.bat again later; the WT Vehicles API likely timed out or returned only the first page."
        )
    if new_count < FULL_ROSTER_EXPECTED_HINT_V230:
        print(
            "WARNING: downloaded roster is smaller than the usual full roster "
            f"({new_count} < {FULL_ROSTER_EXPECTED_HINT_V230}). Continuing because no safer existing full roster was found."
        )


def _write_json_atomic_v230(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:  # v2.30 override
    args = parse_args(argv or sys.argv[1:])
    ensure_dirs()
    if getattr(args, "clear_name_cache", False):
        for f in (DISPLAY_NAMES_JSON, DISPLAY_NAME_STATUS_JSON, DATA_DIR / "wiki_unit_meta_status.json", DATA_DIR / "vehicle_data_conflicts.json", DATA_DIR / "display_names_ru.json", DATA_DIR / "display_names_en.json", DATA_DIR / "wiki_unit_meta_status_ru.json", DATA_DIR / "wiki_unit_meta_status_en.json"):
            try:
                if f.exists():
                    f.unlink()
                    print(f"Deleted name/cache file: {f}")
            except Exception as e:
                print(f"WARNING: could not delete {f}: {e}")

    rows = fetch_all_vehicles()
    _validate_roster_size_v230(rows)
    _write_json_atomic_v230(DATA_DIR / "vehicles_api_raw.json", rows)

    manifest = load_manifest()
    manifest["logic_version"] = IMAGE_LOGIC_VERSION
    manifest.setdefault("items", {})
    image_map: dict[str, str] = {}

    if args.skip_images:
        print("Skipping image cache by request.")
        write_lightweight_vehicles(rows, image_map)
        return 0

    print(f"Downloading/caching vehicle images with {args.workers} workers...")
    print("Resume is enabled: existing files and successful manifest entries are skipped.")
    if not args.retry_missing:
        print("Previously missing images are skipped. Use --retry-missing to re-check them.")

    done = 0
    ok = cached = missing = skipped_missing = 0
    last_save = time.time()
    tasks = [(row, manifest, args.retry_missing, args.force_images) for row in rows if row_identifier(row)]
    with cf.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(process_row, t) for t in tasks]
        for fut in cf.as_completed(futures):
            vid, path, status, reason = fut.result()
            done += 1
            if path:
                image_map[vid] = path
            if status == "ok":
                ok += 1
            elif status == "cached":
                cached += 1
            elif status == "skipped_missing":
                skipped_missing += 1
            else:
                missing += 1
            manifest["items"][vid] = {
                "status": "ok" if path else "missing",
                "path": path,
                "reason": reason,
                "logic_version": IMAGE_LOGIC_VERSION,
                "updated_at": now_iso(),
            }
            if done % 25 == 0 or time.time() - last_save > 10:
                with MANIFEST_LOCK:
                    save_manifest(manifest)
                last_save = time.time()
                print(f"  {done}/{len(tasks)} processed | ok {ok} | cached {cached} | missing {missing} | skipped missing {skipped_missing}")

    save_manifest(manifest)
    for vid, item in manifest.get("items", {}).items():
        if item.get("status") == "ok" and item.get("path"):
            p = ROOT / str(item["path"])
            if p.exists() and p.stat().st_size > 500:
                image_map[vid] = str(item["path"])
    _write_json_atomic_v230(IMAGES_JSON, image_map)
    _write_json_atomic_v230(STATUS_JSON, manifest)

    global DISPLAY_NAME_MAP
    if not args.skip_names:
        DISPLAY_NAME_MAP = build_display_name_map(rows, workers=args.tree_workers, retry_missing=args.retry_names, fetch_wiki=args.fetch_wiki_names)
        print(f"Display names available: {len(DISPLAY_NAME_MAP)} vehicles.")
    elif DISPLAY_NAMES_JSON.exists():
        try:
            DISPLAY_NAME_MAP = json.loads(DISPLAY_NAMES_JSON.read_text(encoding="utf-8"))
        except Exception:
            DISPLAY_NAME_MAP = {}
    global TREE_PLACEMENT_MAP
    if not args.skip_tree_order:
        try:
            TREE_PLACEMENT_MAP = build_wiki_tree_placement_v245(rows)
        except Exception as e:
            TREE_PLACEMENT_MAP = {}
            print("WARNING: Wiki tree placement failed:", e)
    write_lightweight_vehicles(rows, image_map)
    if not args.skip_tree_links:
        links = build_research_links(rows, workers=args.tree_workers, retry_missing=args.retry_tree_missing)
        print(f"Research links available: {len(links)} source vehicles.")
    if not args.skip_tree_order:
        order = parse_wiki_tree_order(rows)
        print(f"Wiki tree order non-empty trees: {sum(1 for v in order.values() if v)} / {len(order)}.")
        try:
            parse_wiki_tree_group_icons(rows)
        except Exception as e:
            print("WARNING: Wiki tree group icon parser failed:", e)
    print(f"Done. Vehicles: {len(rows)}. Images available: {len(image_map)}. Newly downloaded: {ok}. Cached: {cached}. Missing: {missing}.")
    print("Local folders are portable: assets/vehicles, data, and the manifest can be copied into future versions.")
    return 0
# --- end v2.30 layer ---


# --- v2.31 Wiki EN cache hygiene + safer rank fallback ---
def _is_good_cached_meta_v231(item: dict[str, Any]) -> bool:
    if not isinstance(item, dict) or item.get("status") != "ok":
        return False
    meta = item.get("meta")
    if not isinstance(meta, dict):
        return False
    title = str(meta.get("title") or "").strip()
    if not title:
        return False
    if re.search(r"^(Aviation|Ground Vehicles|Bluewater|Coastal|Helicopters|War Thunder Wiki)$", title, re.I):
        return False
    return True


def _cache_stats_v231(cache: dict[str, Any]) -> dict[str, int]:
    stats = {"ok": 0, "missing": 0, "empty": 0, "failed": 0, "bad_cached": 0, "total": 0}
    for item in (cache.get("items") or {}).values():
        stats["total"] += 1
        st = str(item.get("status") or "").lower()
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        title = str(meta.get("title") or "").strip()
        reason = str(item.get("reason") or "").lower()
        if _is_good_cached_meta_v231(item):
            stats["ok"] += 1
        elif st == "missing":
            stats["missing"] += 1
        elif not title:
            stats["empty"] += 1
        elif "error" in reason or "timed" in reason or "exception" in reason:
            stats["failed"] += 1
        else:
            stats["bad_cached"] += 1
    return stats


def _fetch_one_lang_unit_meta_v231(identifier: str, row: dict[str, Any], lang: str, retry_missing: bool, cache: dict[str, Any]) -> tuple[dict[str, Any], str, str, bool]:
    """Return meta, source, reason, network_used.
    v2.31 deliberately re-checks failed/empty/missing cache entries instead of treating them as final.
    Only cache entries with a non-empty valid title are considered reusable.
    """
    vid = row_identifier(row)
    item = (cache.get("items") or {}).get(vid, {})
    if _is_good_cached_meta_v231(item):
        return item["meta"], "wiki-cache-" + lang, item.get("reason", "cached ok"), False
    # Missing/empty/bad cache is not trusted anymore. It will be retried on every run.
    base = WIKI_UNIT_RU if lang == "ru" else WIKI_UNIT
    last = "not found"
    for slug in _unit_slug_candidates_v224(identifier, row):
        try:
            r = get_session().get(base + quote(slug), timeout=TIMEOUT, allow_redirects=True)
            if r.status_code != 200 or not r.text:
                last = f"{lang}:{slug}:{r.status_code}"
                continue
            meta = _parse_wiki_unit_meta_v224(r.text, lang, slug)
            title = str(meta.get("title") or "").strip()
            if title and not re.search(r"^(Aviation|Ground Vehicles|Bluewater|Coastal|Helicopters|War Thunder Wiki)$", title, re.I):
                return meta, "wiki-" + lang, f"{lang}:{slug}", True
            last = f"{lang}:{slug}:empty title"
        except Exception as e:
            last = f"{lang}:{slug}:error:{e}"
    return {}, "local-" + lang, last, True


def _rank_by_br_guard_v231(rank: str, br: dict[str, float], vid: str) -> str:
    """Last-resort guard for obvious rank-I pollution from incomplete API/Wiki rank data.
    Does not try to be official; it only prevents top-tier vehicles from falling into Rank I.
    """
    try:
        b = max(float(x) for x in (br or {}).values() if x is not None)
    except Exception:
        return rank
    if normalize_rank(rank) != "I" or b < 5.0:
        return rank
    if b >= 11.0: return "VIII"
    if b >= 9.0: return "VII"
    if b >= 7.0: return "VI"
    if b >= 6.0: return "V"
    return "IV"


def build_display_name_map(rows: list[dict[str, Any]], workers: int = 12, retry_missing: bool = False, fetch_wiki: bool = True) -> dict[str, str]:  # v2.31 override
    global WIKI_UNIT_META_MAP, DISPLAY_NAME_MAP_RU, DISPLAY_NAME_MAP_EN
    WIKI_UNIT_META_MAP = {}
    DISPLAY_NAME_MAP_RU, DISPLAY_NAME_MAP_EN = {}, {}
    ru_cache = _load_lang_cache_v226(WIKI_UNIT_META_STATUS_RU_JSON)
    en_cache = _load_lang_cache_v226(WIKI_UNIT_META_STATUS_EN_JSON)
    status: dict[str, Any] = {"logic_version": IMAGE_LOGIC_VERSION, "items": {}, "cache_policy": "v2.31 retries failed/empty/missing wiki-name entries"}
    tasks = [r for r in rows if row_identifier(r)]
    before_ru = _cache_stats_v231(ru_cache)
    before_en = _cache_stats_v231(en_cache)
    print("Building RU/EN display name maps from real Wiki unit pages; only valid title cache entries are reused.")
    print(f"  RU wiki cache before: ok {before_ru['ok']} | missing {before_ru['missing']} | empty {before_ru['empty']} | bad {before_ru['bad_cached']} | total {before_ru['total']}")
    print(f"  EN wiki cache before: ok {before_en['ok']} | missing {before_en['missing']} | empty {before_en['empty']} | bad {before_en['bad_cached']} | total {before_en['total']}")
    done = 0
    net_ru = net_en = ok_ru = ok_en = fallback_ru = fallback_en = 0
    lock = threading.Lock()

    def worker(row: dict[str, Any]) -> tuple[str, str, str, dict[str, Any], dict[str, Any], str, str, bool, bool]:
        vid = row_identifier(row)
        nation = nation_normalized(row)
        if not fetch_wiki:
            local = deterministic_display_name(row)
            return vid, _ru_name_final_v225(local, vid, nation), local, {}, {}, "local", "wiki disabled", False, False
        ru_meta, ru_src, ru_reason, ru_net = _fetch_one_lang_unit_meta_v231(vid, row, "ru", retry_missing, ru_cache)
        en_meta, en_src, en_reason, en_net = _fetch_one_lang_unit_meta_v231(vid, row, "en", retry_missing, en_cache)
        ru_title = str(ru_meta.get("title") or "").strip()
        en_title = str(en_meta.get("title") or "").strip()
        local_ru = RU_LOCAL_DISPLAY_V225.get(norm_id(vid)) or RU_LOCAL_DISPLAY_V225.get(_compact_id_v225(vid)) or ""
        ru_name = local_ru or _title_to_name_v226(ru_title, vid, nation, "ru") or _ru_name_final_v225(deterministic_display_name(row), vid, nation)
        en_name = _title_to_name_v226(en_title, vid, nation, "en") or normalize_latin_display_name(deterministic_display_name(row), vid)
        return vid, ru_name, en_name, ru_meta, en_meta, ru_reason, en_reason, ru_net, en_net

    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(worker, r) for r in tasks]
        for fut in cf.as_completed(futures):
            vid, ru_name, en_name, ru_meta, en_meta, ru_reason, en_reason, ru_net, en_net = fut.result()
            done += 1
            if ru_net: net_ru += 1
            if en_net: net_en += 1
            if ru_meta.get("title"): ok_ru += 1
            else: fallback_ru += 1
            if en_meta.get("title"): ok_en += 1
            else: fallback_en += 1
            DISPLAY_NAME_MAP_RU[vid] = ru_name
            DISPLAY_NAME_MAP_EN[vid] = en_name
            meta = dict(en_meta or ru_meta or {})
            if ru_meta.get("title"):
                meta["title_ru"] = ru_meta.get("title")
            if en_meta.get("title"):
                meta["title_en"] = en_meta.get("title")
            if meta:
                WIKI_UNIT_META_MAP[vid] = meta
            ru_cache["items"][vid] = {"status": "ok" if ru_meta.get("title") else "missing", "meta": ru_meta, "name": ru_name, "reason": ru_reason, "logic_version": IMAGE_LOGIC_VERSION, "updated_at": now_iso()}
            en_cache["items"][vid] = {"status": "ok" if en_meta.get("title") else "missing", "meta": en_meta, "name": en_name, "reason": en_reason, "logic_version": IMAGE_LOGIC_VERSION, "updated_at": now_iso()}
            status["items"][vid] = {"status": "ok", "name_ru": ru_name, "name_en": en_name, "ru_reason": ru_reason, "en_reason": en_reason, "logic_version": IMAGE_LOGIC_VERSION, "updated_at": now_iso()}
            if done % 100 == 0:
                with lock:
                    DISPLAY_NAMES_RU_JSON.write_text(json.dumps(DISPLAY_NAME_MAP_RU, ensure_ascii=False, indent=2), encoding="utf-8")
                    DISPLAY_NAMES_EN_JSON.write_text(json.dumps(DISPLAY_NAME_MAP_EN, ensure_ascii=False, indent=2), encoding="utf-8")
                    DISPLAY_NAMES_JSON.write_text(json.dumps(DISPLAY_NAME_MAP_RU, ensure_ascii=False, indent=2), encoding="utf-8")
                    DISPLAY_NAME_STATUS_JSON.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
                    _save_lang_cache_v226(WIKI_UNIT_META_STATUS_RU_JSON, ru_cache)
                    _save_lang_cache_v226(WIKI_UNIT_META_STATUS_EN_JSON, en_cache)
                print(f"  RU/EN wiki names {done}/{len(tasks)} | RU ok {ok_ru} fallback {fallback_ru} net {net_ru} | EN ok {ok_en} fallback {fallback_en} net {net_en}")
    DISPLAY_NAMES_RU_JSON.write_text(json.dumps(DISPLAY_NAME_MAP_RU, ensure_ascii=False, indent=2), encoding="utf-8")
    DISPLAY_NAMES_EN_JSON.write_text(json.dumps(DISPLAY_NAME_MAP_EN, ensure_ascii=False, indent=2), encoding="utf-8")
    DISPLAY_NAMES_JSON.write_text(json.dumps(DISPLAY_NAME_MAP_RU, ensure_ascii=False, indent=2), encoding="utf-8")
    DISPLAY_NAME_STATUS_JSON.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    _save_lang_cache_v226(WIKI_UNIT_META_STATUS_RU_JSON, ru_cache)
    _save_lang_cache_v226(WIKI_UNIT_META_STATUS_EN_JSON, en_cache)
    after_ru = _cache_stats_v231(ru_cache)
    after_en = _cache_stats_v231(en_cache)
    print(f"  RU wiki cache after: ok {after_ru['ok']} | missing {after_ru['missing']} | empty {after_ru['empty']} | bad {after_ru['bad_cached']} | total {after_ru['total']}")
    print(f"  EN wiki cache after: ok {after_en['ok']} | missing {after_en['missing']} | empty {after_en['empty']} | bad {after_en['bad_cached']} | total {after_en['total']}")
    return DISPLAY_NAME_MAP_RU


_old_normalize_for_ui_v231 = normalize_for_ui
def normalize_for_ui(row: dict[str, Any], image_map: dict[str, str]) -> dict[str, Any] | None:  # v2.31 override
    item = _old_normalize_for_ui_v231(row, image_map)
    if item:
        item["rank"] = _rank_by_br_guard_v231(item.get("rank", "I"), item.get("br", {}), item.get("id", ""))
    return item
# --- end v2.31 layer ---


# --- v2.32: correct Sukhoi aircraft vs Soviet SU self-propelled guns; affiliate special status ---
_SOVIET_SU_AIRCRAFT_RE_V232 = re.compile(r"^ussr[_-]su[_-]?(?:2|6|7|9|11|15|17|22|24|25|27|30|33|34|39)(?:[_-]|$)", re.I)
_SOVIET_SU_GROUND_RE_V232 = re.compile(r"^ussr[_-]su[_-]?(?:5|57|76|85|100|122|152)(?:[_-]|$)", re.I)


def _is_p36c_badger_v232(row: dict[str, Any]) -> bool:
    rid = norm_id(row_identifier(row)).replace("-", "_")
    blob = (rid + " " + str(row_name(row) or "")).lower()
    return rid in {"p_36c_rb", "us_p_36c_rb"} or "therussianbadger" in blob or "russianbadger" in blob


_old_normalize_status_v232 = normalize_status
def normalize_status(row: dict[str, Any]) -> str:  # v2.32 override
    # P-36C (TheRussianBadger) is a hidden/affiliate unique vehicle, not regular researchable tech.
    if _is_p36c_badger_v232(row):
        return "Акционная"
    return _old_normalize_status_v232(row)


_old_api_class_guess_v232 = _api_class_guess_v225
def _api_class_guess_v225(row: dict[str, Any]) -> str:  # v2.32 override: Sukhoi Su aircraft are air, not Soviet SU SPGs.
    rid = norm_id(row_identifier(row))
    rid2 = rid.replace("-", "_")
    blob = " ".join(str(row.get(k, "")) for k in ("type","vehicle_type","class","unitClass","category","kind","tags","role","roleName","main_role","mainRole","name","title")).lower().replace("_", " ")
    if _SOVIET_SU_AIRCRAFT_RE_V232.match(rid2):
        return "air"
    if _SOVIET_SU_GROUND_RE_V232.match(rid2):
        return "ground"
    return _old_api_class_guess_v232(row)


_old_final_class_v232 = final_class_v225
def final_class_v225(row: dict[str, Any]) -> tuple[str, dict[str, str]]:  # v2.32 override
    rid = norm_id(row_identifier(row)).replace("-", "_")
    if _SOVIET_SU_AIRCRAFT_RE_V232.match(rid):
        api_cls = _api_class_guess_v225(row)
        role_cls = _class_from_role_v225(row)
        wiki_cls = _wiki_class_safe_v225(row)
        return "air", {"apiClass": api_cls, "wikiClass": wiki_cls or "", "roleClass": role_cls or "", "localClass": "sukhoi-aircraft-guard"}
    if _SOVIET_SU_GROUND_RE_V232.match(rid):
        api_cls = _api_class_guess_v225(row)
        role_cls = _class_from_role_v225(row)
        wiki_cls = _wiki_class_safe_v225(row)
        return "ground", {"apiClass": api_cls, "wikiClass": wiki_cls or "", "roleClass": role_cls or "", "localClass": "soviet-su-spg-guard"}
    return _old_final_class_v232(row)
# --- end v2.32 layer ---

# --- v2.33: fix optional-prefix Soviet Sukhoi aircraft and Ya-5 coastal boat classification ---
# Some WT API identifiers omit the nation prefix (e.g. su_2_m82, su_2_mv5), while older guards
# only matched ussr_su_*. A broad ground guard also caught ya_5m as ground. Keep these as
# late final guards so they override older fallback layers.
_SOVIET_SUKHOI_AIRCRAFT_RE_V233 = re.compile(
    r"^(?:ussr[_-])?su[_-]?(?:2|6|7|9|11|15|17|20|22|24|25|27|30|33|34|35|39|47)(?:[_-]|$)",
    re.I,
)
_SOVIET_SU_SPG_RE_V233 = re.compile(
    r"^(?:ussr[_-])?su[_-]?(?:5|57b|57|76|85|100|122|152)(?:[_-]|$)",
    re.I,
)
_SOVIET_YA_BOAT_RE_V233 = re.compile(r"^(?:ussr[_-])?ya[_-]?5(?:m)?(?:[_-]|$)", re.I)


def _rid_v233(row: dict[str, Any]) -> str:
    return norm_id(row_identifier(row)).replace("-", "_")


_old_api_class_guess_v233 = _api_class_guess_v225
def _api_class_guess_v225(row: dict[str, Any]) -> str:  # v2.33 override
    rid = _rid_v233(row)
    if _SOVIET_YA_BOAT_RE_V233.match(rid):
        return "coastal"
    # Important: test aircraft before SPG, but do not treat SU-57/SU-57B SPGs as Sukhoi jets.
    if _SOVIET_SUKHOI_AIRCRAFT_RE_V233.match(rid):
        return "air"
    if _SOVIET_SU_SPG_RE_V233.match(rid):
        return "ground"
    return _old_api_class_guess_v233(row)


_old_final_class_v233 = final_class_v225
def final_class_v225(row: dict[str, Any]) -> tuple[str, dict[str, str]]:  # v2.33 override
    rid = _rid_v233(row)
    api_cls = _api_class_guess_v225(row)
    role_cls = _class_from_role_v225(row)
    wiki_cls = _wiki_class_safe_v225(row)
    if _SOVIET_YA_BOAT_RE_V233.match(rid):
        return "coastal", {"apiClass": api_cls, "wikiClass": wiki_cls or "", "roleClass": role_cls or "", "localClass": "ya-5-coastal-guard"}
    if _SOVIET_SUKHOI_AIRCRAFT_RE_V233.match(rid):
        return "air", {"apiClass": api_cls, "wikiClass": wiki_cls or "", "roleClass": role_cls or "", "localClass": "sukhoi-aircraft-optional-prefix-guard"}
    if _SOVIET_SU_SPG_RE_V233.match(rid):
        return "ground", {"apiClass": api_cls, "wikiClass": wiki_cls or "", "roleClass": role_cls or "", "localClass": "soviet-su-spg-optional-prefix-guard"}
    return _old_final_class_v233(row)
# --- end v2.33 layer ---



# --- v2.34: broad class guards, official RU titles, creator/affiliate specials ---
def _clean_official_wiki_title_v234(title: str) -> str:
    """Strip War Thunder nation/hidden markers but keep the official Wiki spelling.
    Important: do not transliterate official RU Wiki titles. Export aircraft can be officially written
    as Su-22M3 / Su-30MK2 AMV on the Russian Wiki, and that spelling must be preserved.
    """
    t = str(title or "").strip().replace("\u00a0", " ")
    t = re.sub(r"^[\s○◯◌●•▄▃▂▁◔◘◢◣◤◥␗␙⭐★☆]+", "", t).strip()
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _title_to_name_v226(title: str, vid: str, nation: str, lang: str) -> str:  # v2.34 override
    t = _clean_official_wiki_title_v234(title)
    if not t:
        return ""
    if lang == "ru":
        # Use the RU Wiki title as the authority. Do not run the older Cyrillic autocorrect layer here.
        return t
    return normalize_latin_display_name(t, vid)


def _vid_blob_v234(row: dict[str, Any]) -> str:
    rid = norm_id(row_identifier(row)).replace("-", "_").lower()
    name_bits = " ".join(str(first_defined(row, ["name", "title", "type", "vehicle_type", "class", "unitClass", "category", "kind", "tags", "role", "roleName", "main_role", "mainRole"], "")) for _ in [0]).lower()
    return rid + " " + name_bits.replace("-", "_")


def _class_guard_v234(row: dict[str, Any]) -> str | None:
    """Strong but narrow ID guards. These run before old broad 'su_ -> ground' rules."""
    rid = norm_id(row_identifier(row)).replace("-", "_").lower()
    blob = _vid_blob_v234(row)
    # Sukhoi aircraft in any tree: USSR, Germany, China, Japan, export/event variants, and IDs with no nation prefix.
    # Do not include 57 here: SU-57/SU-57B are WWII SPGs in this roster context.
    if re.search(r"(?:^|_)su_?(?:2|6|7|8|9|11|15|17|20|22|24|25|27|30|33|34|35|39|47)(?!\d)[a-z0-9_]*(?:$|_)", rid):
        return "air"
    # Other aircraft families that were being dragged into ground by old broad guards.
    if re.search(r"(?:^|_)(ba_65|md_460_sambad|sambad|super_mystere|mystere|t129|t_129)(?:$|_)", rid):
        return "heli" if re.search(r"(?:^|_)(t129|t_129)(?:$|_)", rid) else "air"
    # Japanese naval units that old type/T rules misclassified as tanks.
    if re.search(r"(?:^|_)(jds_murasame|murasame|dd_107|destroyer_murasame|jp_destroyer)", rid):
        return "bluewater"
    if re.search(r"(?:^|_)(type_t_51|type_t_14|t_51a|t_51b|t_14)(?:$|_|mod)", rid) and (rid.startswith("jp_") or "type_t" in rid):
        return "coastal"
    # Submarines are not normal tech-tree vehicles, but if present they are naval, not ground.
    if re.search(r"(?:^|_)(sub|submarine|u_boat|type_7|type_vii)(?:$|_)", rid):
        return "bluewater"
    # Specific ground SPGs/tanks, after aircraft/naval exceptions.
    if re.search(r"(?:^|_)su_?(?:5|57b|57|76|85|100|122|152)(?:$|_)", rid):
        return "ground"
    return None


_old_api_class_guess_v234 = _api_class_guess_v225
def _api_class_guess_v225(row: dict[str, Any]) -> str:  # v2.34 override
    g = _class_guard_v234(row)
    if g:
        return g
    return _old_api_class_guess_v234(row)


_old_final_class_v234 = final_class_v225
def final_class_v225(row: dict[str, Any]) -> tuple[str, dict[str, str]]:  # v2.34 override
    g = _class_guard_v234(row)
    api_cls = _api_class_guess_v225(row)
    role_cls = _class_from_role_v225(row)
    wiki_cls = _wiki_class_safe_v225(row)
    if g:
        return g, {"apiClass": api_cls, "wikiClass": wiki_cls or "", "roleClass": role_cls or "", "localClass": "v2.34-id-class-guard"}
    return _old_final_class_v234(row)


def _wiki_title_for_v234(row: dict[str, Any]) -> str:
    vid = row_identifier(row)
    meta = WIKI_UNIT_META_MAP.get(vid) or {}
    return " ".join(str(x or "") for x in [meta.get("title_ru"), meta.get("title_en"), meta.get("title"), DISPLAY_NAME_MAP_RU.get(vid), DISPLAY_NAME_MAP_EN.get(vid)])


def _is_creator_or_hidden_special_v234(row: dict[str, Any]) -> bool:
    rid = norm_id(row_identifier(row)).replace("-", "_").lower()
    title_blob = _wiki_title_for_v234(row)
    text_blob = (title_blob + " " + rid + " " + str(row_name(row) or "") + " " + _text_status(row)).lower()
    if re.search(r"(^|\s)[○◯◌]", title_blob):
        return True
    if any(x in text_blob for x in ("therussianbadger", "russianbadger", "affiliate", "партнер", "партнёр", "author support", "поддержки авторов")):
        return True
    # Known affiliate/creator suffixes seen in WT ids. Keep narrow to avoid marking every RB/Russian vehicle.
    if rid.endswith(("_trb", "_rb")) and any(base in rid for base in ("p_36c", "lvt_a_1")):
        return True
    return False


_old_normalize_status_v234 = normalize_status
def normalize_status(row: dict[str, Any]) -> str:  # v2.34 override
    if _is_creator_or_hidden_special_v234(row):
        return "Акционная"
    rid = norm_id(row_identifier(row)).replace("-", "_").lower()
    if re.search(r"(?:^|_)(sub_type_7|submarine_type_7|type_vii_sub|type_7_sub)(?:$|_)", rid):
        return "Спец."
    return _old_normalize_status_v234(row)


_old_normalize_for_ui_v234 = normalize_for_ui
def normalize_for_ui(row: dict[str, Any], image_map: dict[str, str]) -> dict[str, Any] | None:  # v2.34 override
    item = _old_normalize_for_ui_v234(row, image_map)
    if not item:
        return item
    g = _class_guard_v234(row)
    if g:
        item["class"] = g
        item["className"] = CLASS_NAMES.get(g, g)
        src = item.setdefault("_debugSources", {})
        src["localClass"] = "v2.34-id-class-guard"
    rid = norm_id(row_identifier(row)).replace("-", "_").lower()
    if item.get("class") == "bluewater" and re.search(r"(murasame|destroyer|dd_107|jp_destroyer)", rid):
        item["role"], item["roleEn"] = "ЭСМ", "Destroyer"
    elif item.get("class") == "coastal" and re.search(r"(type_t_51|type_t_14|t_51|t_14|boat|gunboat)", rid):
        item["role"], item["roleEn"] = "Катер", "Boat"
    elif item.get("class") == "air" and re.search(r"(sambad|md_460|super_mystere|su_|ba_65)", rid):
        # Avoid leftover ground roles after class guard.
        if str(item.get("role", "")).lower() in {"зсу", "пт-сау", "лт", "ст", "тт"}:
            item["role"], item["roleEn"] = "Истреб.", "Fighter"
    return item
# --- end v2.34 layer ---


# --- v2.35: safer class guards, special creator status, clean official names ---
def _name_blob_v235(row: dict[str, Any]) -> str:
    vid = norm_id(row_identifier(row)).replace('-', '_').lower()
    bits = [vid]
    for k in ('name','title','type','vehicle_type','class','unitClass','category','kind','tags','role','roleName','main_role','mainRole'):
        v = row.get(k)
        if isinstance(v, (list, tuple, set)):
            bits.extend(str(x) for x in v)
        elif v is not None:
            bits.append(str(v))
    try:
        m = WIKI_UNIT_META_MAP.get(row_identifier(row)) or {}
        bits.extend(str(m.get(k) or '') for k in ('title_ru','title_en','title','class','rank'))
    except Exception:
        pass
    return ' '.join(bits).lower().replace('-', '_').replace('–','_')


def _vehicle_class_guard_v235(row: dict[str, Any]) -> str | None:
    b = _name_blob_v235(row)
    rid = norm_id(row_identifier(row)).replace('-', '_').lower()
    # Explicit non-ground exceptions must run before the old broad t_/su_/sub_ ground guards.
    if re.search(r'(?:^|_)sub[_ ]?i[_ ]?ii(?:$|_)|sub_i_ii|sub\s*i\s*[- ]\s*ii', b):
        return 'ground'
    if re.search(r'(?:^|_)(type[_ ]?t[_ ]?51[ab]?|t[_ ]?51[ab]|type[_ ]?t[_ ]?14|t[_ ]?14)(?:$|_|\s|mod)', b):
        return 'coastal'
    if re.search(r'(jds[_ ]?murasame|murasame|dd[_ -]?107|jp[_ ]?destroyer)', b):
        return 'bluewater'
    if re.search(r'(?:^|_)(t129|t_129|a129|aw129)(?:$|_)|\bt129\b', b):
        return 'heli'
    if re.search(r'(ba[_ .]?65|sambad|md[_ ]?460|super[_ ]?mystere|mystere)', b):
        return 'air'
    # Sukhoi aircraft in any nation/tree. Do not match SU-57/SU-57B SPGs here.
    if re.search(r'(?:^|_)su[_ ]?(?:2|6|7|8|9|11|15|17|20|22|24|25|27|30|33|34|35|39|47)(?:[a-z_]|$)', b):
        return 'air'
    # Yak aircraft were disappearing when stale JS/Python guards treated prefixes too aggressively.
    if re.search(r'(?:^|_)(yak|i_?15|i_?16|i_?180|lagg|la_?5|la_?7|mig|pe_?2|pe_?3|il_?2|il_?4|db_?3|tu_?2|sb_?2|tb_?3|mbr_?2|kor_?2)(?:$|_)', rid):
        return 'air'
    if re.search(r'(?:^|_)(sub[_ ]?type[_ ]?7|type[_ ]?7[_ ]?sub|type[_ ]?vii|u[_ ]?boat|submarine)(?:$|_)', b):
        return 'bluewater'
    if re.search(r'(?:^|_)su[_ ]?(?:5|57b|57|76|85|100|122|152)(?:$|_)', b):
        return 'ground'
    return None


def _role_guard_v235(item: dict[str, Any]) -> None:
    rid = str(item.get('id','')).lower().replace('-', '_')
    if item.get('class') == 'ground' and re.search(r'(?:^|_)sub_i_ii(?:$|_)|sub_i_ii', rid):
        item['role'], item['roleEn'] = 'ЗСУ', 'SPAA'
    elif item.get('class') == 'bluewater' and re.search(r'(murasame|dd_107|destroyer)', rid):
        item['role'], item['roleEn'] = 'ЭСМ', 'Destroyer'
    elif item.get('class') == 'coastal' and re.search(r'(type_t_51|t_51|type_t_14|t_14)', rid):
        item['role'], item['roleEn'] = 'Катер', 'Boat'
    elif item.get('class') == 'heli' and re.search(r'(t129|t_129|a129|aw129)', rid):
        item['role'], item['roleEn'] = 'Ударный вертолёт', 'Attack helicopter'
    elif item.get('class') == 'air' and re.search(r'(su_|ba_65|sambad|md_460|mystere)', rid):
        if str(item.get('role') or '').lower() in {'зсу','пт-сау','лт','ст','тт','катер','эсм'}:
            item['role'], item['roleEn'] = 'Истреб.', 'Fighter'


def _is_creator_or_hidden_special_v235(row: dict[str, Any]) -> bool:
    rid = norm_id(row_identifier(row)).replace('-', '_').lower()
    t = _wiki_title_for_v234(row)
    blob = (t + ' ' + rid + ' ' + str(row_name(row) or '') + ' ' + _text_status(row)).lower()
    if re.search(r'[○◯◌]', t):
        return True
    if any(x in blob for x in ('therussianbadger','russianbadger','affiliate','партнер','партнёр','поддержки авторов','author support')):
        return True
    if rid.endswith(('_trb','_rb')) and any(x in rid for x in ('p_36c','lvt_a_1')):
        return True
    return False


_old_api_class_guess_v235 = _api_class_guess_v225
def _api_class_guess_v225(row: dict[str, Any]) -> str:  # v2.35 override
    g = _vehicle_class_guard_v235(row)
    if g:
        return g
    return _old_api_class_guess_v235(row)


_old_final_class_v235 = final_class_v225
def final_class_v225(row: dict[str, Any]) -> tuple[str, dict[str, str]]:  # v2.35 override
    g = _vehicle_class_guard_v235(row)
    if g:
        api_cls = _old_api_class_guess_v235(row)
        role_cls = _class_from_role_v225(row)
        wiki_cls = _wiki_class_safe_v225(row)
        return g, {'apiClass': api_cls, 'wikiClass': wiki_cls or '', 'roleClass': role_cls or '', 'localClass': 'v2.35-class-guard'}
    return _old_final_class_v235(row)


_old_norm_status_v235 = normalize_status
def normalize_status(row: dict[str, Any]) -> str:  # v2.35 override
    if _is_creator_or_hidden_special_v235(row):
        return 'Акционная'
    return _old_norm_status_v235(row)


def _official_name_cleanup_v235(name: str, vid: str, nation: str) -> str:
    n = str(name or '').strip()
    # Streamer/creator variants should not get a misleading country suffix in the visible name.
    rid = norm_id(vid).replace('-', '_').lower()
    if rid == 'p_63c_5_kingcobra_animal_version' or 'kingcobra_animal' in rid:
        return re.sub(r'\s*\((?:США|USA|US)\)\s*$', ' (Animal)', n, flags=re.I) if 'Animal' not in n else n
    return n


_old_title_to_name_v235 = _title_to_name_v226
def _title_to_name_v226(title: str, vid: str, nation: str, lang: str) -> str:  # v2.35 override
    n = _old_title_to_name_v235(title, vid, nation, lang)
    return _official_name_cleanup_v235(n, vid, nation)


_old_norm_ui_v235 = normalize_for_ui
def normalize_for_ui(row: dict[str, Any], image_map: dict[str, str]) -> dict[str, Any] | None:  # v2.35 override
    item = _old_norm_ui_v235(row, image_map)
    if not item:
        return item
    g = _vehicle_class_guard_v235(row)
    if g:
        item['class'] = g
        item['className'] = CLASS_NAMES.get(g, g)
        item.setdefault('_debugSources', {})['localClass'] = 'v2.35-class-guard'
    if _is_creator_or_hidden_special_v235(row):
        item['status'] = 'Акционная'
    for k in ('name','nameRu','nameEn'):
        if item.get(k):
            item[k] = _official_name_cleanup_v235(item[k], item.get('id',''), item.get('nation',''))
    _role_guard_v235(item)
    return item
# --- end v2.35 layer ---



# --- v2.36: stricter class guards, Ya-5M/SUB-I-II fixes, fail partial API rosters ---
def _vehicle_class_guard_v236(row: dict[str, Any]) -> str | None:
    b = _name_blob_v235(row)
    rid = norm_id(row_identifier(row)).replace('-', '_').lower()
    # Boat Ya-5M / Я-5М: do not let broad ya_/y prefix rules classify it as ground.
    if re.search(r'(?:^|_)ya[_ -]?5m(?:$|_)|\bя[_ -]?5м\b', b):
        return 'coastal'
    # Italian SUB-I-II is SPAA, not a submarine/bluewater vessel.
    if re.search(r'(?:^|_)sub[_ -]?i[_ -]?ii(?:$|_)|\bsub\s*i\s*ii\b|sub_i_ii', b):
        return 'ground'
    # Japanese torpedo boats / patrol craft. Several API ids omit underscores.
    if re.search(r'(?:^|_)(?:jp_)?(?:type[_ ]?t[_ ]?51[ab]?|type[_ ]?t51[ab]?|t[_ ]?51[ab]|t51[ab])(?:$|_|\s)', b):
        return 'coastal'
    if re.search(r'(?:^|_)(?:jp_)?(?:type[_ ]?t[_ ]?14|type[_ ]?t14|t[_ ]?14|t14)(?:$|_|\s|mod)', b):
        return 'coastal'
    if re.search(r'(?:^|_)(t129|t_129|a129|aw129)(?:$|_)|\bt129\b', b):
        return 'heli'
    if re.search(r'(ba[_ .]?65|sambad|md[_ ]?460|super[_ ]?mystere|mystere)', b):
        return 'air'
    # Sukhoi aircraft in any nation/tree. Keep SU-57/SU-76/SU-85 SPGs out of this branch.
    if re.search(r'(?:^|_)su[_ -]?(?:2|6|7|8|9|11|15|17|20|22|24|25|27|30|33|34|35|39|47)(?:[^0-9]|$)', b):
        return 'air'
    if re.search(r'(?:^|_)(yak|i_?15|i_?16|i_?180|lagg|la_?5|la_?7|mig|pe_?2|pe_?3|il_?2|il_?4|db_?3|tu_?2|sb_?2|tb_?3|mbr_?2|kor_?2)(?:$|_)', rid):
        return 'air'
    if re.search(r'(murasame|dd[_ -]?107|jp[_ ]?destroyer)', b):
        return 'bluewater'
    if re.search(r'(?:^|_)(sub[_ ]?type[_ ]?7|type[_ ]?7[_ ]?sub|type[_ ]?vii|u[_ ]?boat|submarine)(?:$|_)', b):
        return 'bluewater'
    if re.search(r'(?:^|_)su[_ -]?(?:5|57b|57|76|85|100|122|152)(?:$|_)', b):
        return 'ground'
    return None

_old_api_class_guess_v236 = _api_class_guess_v225
def _api_class_guess_v225(row: dict[str, Any]) -> str:  # v2.36 override
    g = _vehicle_class_guard_v236(row)
    if g:
        return g
    return _old_api_class_guess_v236(row)

_old_final_class_v236 = final_class_v225
def final_class_v225(row: dict[str, Any]) -> tuple[str, dict[str, str]]:  # v2.36 override
    g = _vehicle_class_guard_v236(row)
    if g:
        return g, {'apiClass': _old_api_class_guess_v236(row), 'wikiClass': _wiki_class_safe_v225(row) or '', 'roleClass': _class_from_role_v225(row) or '', 'localClass': 'v2.36-class-guard'}
    return _old_final_class_v236(row)

_old_norm_ui_v236 = normalize_for_ui
def normalize_for_ui(row: dict[str, Any], image_map: dict[str, str]) -> dict[str, Any] | None:  # v2.36 override
    item = _old_norm_ui_v236(row, image_map)
    if not item:
        return item
    g = _vehicle_class_guard_v236(row)
    if g:
        item['class'] = g
        item['className'] = CLASS_NAMES.get(g, g)
        item.setdefault('_debugSources', {})['localClass'] = 'v2.36-class-guard'
    rid = str(item.get('id','')).lower().replace('-', '_')
    if item.get('class') == 'coastal' and re.search(r'(ya_5m|type_t_51|t_51|type_t51|t51|type_t_14|t_14|type_t14|t14)', rid):
        item['role'], item['roleEn'] = 'Катер', 'Boat'
    if item.get('class') == 'ground' and re.search(r'sub_i_ii', rid):
        item['role'], item['roleEn'] = 'ЗСУ', 'SPAA'
    return item

# Never silently write a 2600/200 vehicle roster when the public API is timing out.
def _validate_roster_size_v230(rows: list[dict[str, Any]]) -> None:  # v2.36 override
    new_count = len(rows)
    old_count = _existing_roster_count_v230()
    if new_count < FULL_ROSTER_EXPECTED_HINT_V230:
        raise RuntimeError(
            'Partial API update refused: downloaded '
            f'{new_count} vehicles, expected at least {FULL_ROSTER_EXPECTED_HINT_V230}. '
            f'Existing roster count: {old_count}. Keeping previous data; run update again later.'
        )
# --- end v2.36 layer ---


# --- v2.37: final class/status guards and research_links.json compatibility ---
def _vehicle_class_guard_v237(row: dict[str, Any]) -> str | None:
    b = _name_blob_v235(row)
    rid = norm_id(row_identifier(row)).replace('-', '_').lower()
    blob = (b + ' ' + rid).lower().replace('-', '_')
    if re.search(r'(?:^|_)ya[_ ]?5m(?:$|_)|\bя[_ ]?5м\b|ya_5m', blob):
        return 'coastal'
    if re.search(r'(?:^|_)sub[_ ]?i[_ ]?ii(?:$|_)|\bsub\s*i\s*ii\b|sub_i_ii', blob):
        return 'ground'
    if re.search(r'(?:^|_)(?:jp_)?(?:type[_ ]?t[_ ]?51[ab]?|type[_ ]?t51[ab]?|t[_ ]?51[ab]|t51[ab])(?:$|_|\s)', blob):
        return 'coastal'
    if re.search(r'(?:^|_)(?:jp_)?(?:type[_ ]?t[_ ]?14|type[_ ]?t14|t[_ ]?14|t14)(?:$|_|\s|mod)', blob):
        return 'coastal'
    if re.search(r'(?:^|_)(t129|t_129|a129|aw129)(?:$|_)|\bt129\b', blob):
        return 'heli'
    if re.search(r'(ba[_ .]?65|sambad|md[_ ]?460|super[_ ]?mystere|mystere)', blob):
        return 'air'
    if re.search(r'(?:^|_)su[_ ]?(?:2|6|7|8|9|11|15|17|20|22|24|25|27|30|33|34|35|39|47)(?:[^0-9]|$)', blob):
        return 'air'
    if re.search(r'(?:^|_)(yak|i_?15|i_?16|i_?180|lagg|la_?5|la_?7|mig|pe_?2|pe_?3|il_?2|il_?4|db_?3|tu_?2|sb_?2|tb_?3|mbr_?2|kor_?2)(?:$|_)', rid):
        return 'air'
    if re.search(r'(murasame|dd[_ ]?107|jp[_ ]?destroyer)', blob):
        return 'bluewater'
    if re.search(r'(?:^|_)(sub[_ ]?type[_ ]?7|type[_ ]?7[_ ]?sub|type[_ ]?vii|u[_ ]?boat|submarine)(?:$|_)', blob):
        return 'bluewater'
    if re.search(r'(?:^|_)su[_ ]?(?:5|57b|57|76|85|100|122|152)(?:$|_)', blob):
        return 'ground'
    return None


def _special_status_guard_v237(row: dict[str, Any]) -> str | None:
    rid = norm_id(row_identifier(row)).replace('-', '_').lower()
    title = _wiki_title_for_v234(row) if '_wiki_title_for_v234' in globals() else ''
    blob = (rid + ' ' + str(row_name(row) or '') + ' ' + str(title or '') + ' ' + _text_status(row)).lower()
    if any(x in rid for x in ('p_36c_rb','lvt_a_1_trb','lvt_a_4_zis_2','m5a1_td','kingcobra_animal')):
        return 'Акционная'
    if re.search(r'[○◯◌◔]', str(title or '')):
        return 'Акционная'
    if any(x in blob for x in ('affiliate','therussianbadger','russianbadger','партнер','партнёр','поддержки авторов','special event','gift vehicle')):
        return 'Акционная'
    return None

_old_api_class_guess_v237 = _api_class_guess_v225
def _api_class_guess_v225(row: dict[str, Any]) -> str:  # v2.37 override
    g = _vehicle_class_guard_v237(row)
    if g:
        return g
    return _old_api_class_guess_v237(row)

_old_final_class_v237 = final_class_v225
def final_class_v225(row: dict[str, Any]) -> tuple[str, dict[str, str]]:  # v2.37 override
    g = _vehicle_class_guard_v237(row)
    if g:
        return g, {'apiClass': _old_api_class_guess_v237(row), 'wikiClass': _wiki_class_safe_v225(row) or '', 'roleClass': _class_from_role_v225(row) or '', 'localClass': 'v2.37-class-guard'}
    return _old_final_class_v237(row)

_old_norm_status_v237 = normalize_status
def normalize_status(row: dict[str, Any]) -> str:  # v2.37 override
    s = _special_status_guard_v237(row)
    if s:
        return s
    return _old_norm_status_v237(row)

_old_title_to_name_v237 = _title_to_name_v226
def _title_to_name_v226(title: str, vid: str, nation: str, lang: str) -> str:  # v2.37 override
    n = _old_title_to_name_v237(title, vid, nation, lang)
    rid = norm_id(vid).replace('-', '_').lower()
    if 'lvt_a_1_trb' in rid:
        return 'LVT(A)(1) (TheRussianBadger)'
    if 'p_63c_5_kingcobra_animal' in rid or 'kingcobra_animal' in rid:
        return 'Kingcobra (Animal)'
    return n

_old_norm_ui_v237 = normalize_for_ui
def normalize_for_ui(row: dict[str, Any], image_map: dict[str, str]) -> dict[str, Any] | None:  # v2.37 override
    item = _old_norm_ui_v237(row, image_map)
    if not item:
        return item
    g = _vehicle_class_guard_v237(row)
    if g:
        item['class'] = g
        item['className'] = CLASS_NAMES.get(g, g)
        item.setdefault('_debugSources', {})['localClass'] = 'v2.37-class-guard'
    sg = _special_status_guard_v237(row)
    if sg:
        item['status'] = sg
    rid = str(item.get('id','')).lower().replace('-', '_')
    if 'lvt_a_1_trb' in rid:
        item['name'] = item['nameRu'] = item['nameEn'] = 'LVT(A)(1) (TheRussianBadger)'
    if 'p_63c_5_kingcobra_animal' in rid:
        item['name'] = item['nameRu'] = item['nameEn'] = 'Kingcobra (Animal)'
    if item.get('class') == 'coastal' and re.search(r'(ya_5m|type_t_51|t51|type_t_14|t14)', rid):
        item['role'], item['roleEn'] = 'Катер', 'Boat'
    if item.get('class') == 'ground' and re.search(r'sub_i_ii', rid):
        item['role'], item['roleEn'] = 'ЗСУ', 'SPAA'
    return item

_old_build_research_links_v237 = build_research_links
def build_research_links(rows: list[dict[str, Any]], workers: int = 12, retry_missing: bool = False) -> dict[str, list[str]]:  # v2.37 override
    links = _old_build_research_links_v237(rows, workers=workers, retry_missing=retry_missing)
    out = {
        'logic_version': IMAGE_LOGIC_VERSION,
        'updated_at': now_iso(),
        'links': links,
        'note': 'Parsed from Wiki unit Research order / Порядок исследования where available. Group/folder thumbnails can use assets/vehicles/slots/<group_id>.png or static.encyclopedia.warthunder.com/slots/<group_id>.png.'
    }
    try:
        (DATA_DIR / 'research_links.json').write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        print(f'WARNING: could not write research_links.json: {e}')
    return links
# --- end v2.37 layer ---


# --- v2.38: stronger right-column/special detection and class guards ---
def _vehicle_class_guard_v238(row: dict[str, Any]) -> str | None:
    rid = norm_id(row_identifier(row)).replace('-', '_').lower()
    blob = (_name_blob_v235(row) + ' ' + rid).lower().replace('-', '_')
    if re.search(r'(?:^|_)ya[_ ]?5m(?:$|_)|\bya[_ -]?5m\b|я[_ -]?5м', blob):
        return 'coastal'
    if re.search(r'(?:^|_)sub[_ ]?i[_ ]?ii(?:$|_)|\bsub\s*i\s*ii\b|sub_i_ii', blob):
        return 'ground'
    if re.search(r'(?:^|_)(?:jp_)?(?:type[_ ]?t[_ ]?51[ab]?|type[_ ]?t51[ab]?|t[_ ]?51[ab]|t51[ab])(?:$|_|\s)', blob):
        return 'coastal'
    if re.search(r'(?:^|_)(?:jp_)?(?:type[_ ]?t[_ ]?14|type[_ ]?t14|t[_ ]?14|t14)(?:$|_|\s|mod)', blob):
        return 'coastal'
    if re.search(r'(?:^|_)(t129|t_129|a129|aw129)(?:$|_)|\bt129\b', blob):
        return 'heli'
    if re.search(r'(ba[_ .]?65|sambad|md[_ ]?460|super[_ ]?mystere|mystere)', blob):
        return 'air'
    if re.search(r'(?:^|_)su[_ ]?(?:2|6|7|8|9|11|15|17|20|22|24|25|27|30|33|34|35|39|47)(?:[^0-9]|$)', blob):
        return 'air'
    if re.search(r'(?:^|_)(yak|i_?15|i_?16|i_?180|lagg|la_?5|la_?7|mig|pe_?2|pe_?3|il_?2|il_?4|db_?3|tu_?2|sb_?2|tb_?3|mbr_?2|kor_?2)(?:$|_)', rid):
        return 'air'
    if re.search(r'(murasame|dd[_ ]?107|jp[_ ]?destroyer)', blob):
        return 'bluewater'
    if re.search(r'(?:^|_)su[_ ]?(?:5|57b|57|76|85|100|122|152)(?:$|_)', blob):
        return 'ground'
    return None


def _special_status_guard_v238(row: dict[str, Any]) -> str | None:
    rid = norm_id(row_identifier(row)).replace('-', '_').lower()
    title = _wiki_title_for_v234(row) if '_wiki_title_for_v234' in globals() else ''
    blob = (rid + ' ' + str(row_name(row) or '') + ' ' + str(title or '') + ' ' + _text_status(row)).lower()
    # Systemic sources: explicit Wiki symbols/text and right-column style ids used by Gaijin for creator/event/gift vehicles.
    if re.search(r'[○◯◌◔]', str(title or '')):
        return 'Акционная'
    if any(x in blob for x in ('affiliate','therussianbadger','russianbadger','партнер','партнёр','поддержки авторов','special event','gift vehicle','unique camouflage','уникальном камуфляже')):
        return 'Акционная'
    if re.search(r'(_trb|_rb|kingcobra_animal|bt_7_1937_td|m5a1_td|lvt_a_4_zis_2|lvt_4_zis_2)$', rid):
        return 'Акционная'
    return None

_old_api_class_guess_v238 = _api_class_guess_v225
def _api_class_guess_v225(row: dict[str, Any]) -> str:  # v2.38 override
    g = _vehicle_class_guard_v238(row)
    if g:
        return g
    return _old_api_class_guess_v238(row)

_old_final_class_v238 = final_class_v225
def final_class_v225(row: dict[str, Any]) -> tuple[str, dict[str, str]]:  # v2.38 override
    g = _vehicle_class_guard_v238(row)
    if g:
        return g, {'apiClass': _old_api_class_guess_v238(row), 'wikiClass': _wiki_class_safe_v225(row) or '', 'roleClass': _class_from_role_v225(row) or '', 'localClass': 'v2.38-class-guard'}
    return _old_final_class_v238(row)

_old_norm_status_v238 = normalize_status
def normalize_status(row: dict[str, Any]) -> str:  # v2.38 override
    s = _special_status_guard_v238(row)
    if s:
        return s
    return _old_norm_status_v238(row)

_old_title_to_name_v238 = _title_to_name_v226
def _title_to_name_v226(title: str, vid: str, nation: str, lang: str) -> str:  # v2.38 override
    n = _old_title_to_name_v238(title, vid, nation, lang)
    rid = norm_id(vid).replace('-', '_').lower()
    if 'lvt_a_1_trb' in rid:
        return 'LVT(A)(1) (TheRussianBadger)'
    if 'p_63c_5_kingcobra_animal' in rid or 'kingcobra_animal' in rid:
        return 'Kingcobra (Animal)'
    return n

_old_norm_ui_v238 = normalize_for_ui
def normalize_for_ui(row: dict[str, Any], image_map: dict[str, str]) -> dict[str, Any] | None:  # v2.38 override
    item = _old_norm_ui_v238(row, image_map)
    if not item:
        return item
    g = _vehicle_class_guard_v238(row)
    if g:
        item['class'] = g
        item['className'] = CLASS_NAMES.get(g, g)
        item.setdefault('_debugSources', {})['localClass'] = 'v2.38-class-guard'
    sg = _special_status_guard_v238(row)
    if sg:
        item['status'] = sg
    rid = str(item.get('id','')).lower().replace('-', '_')
    if 'lvt_a_1_trb' in rid:
        item['name'] = item['nameRu'] = item['nameEn'] = 'LVT(A)(1) (TheRussianBadger)'
    if 'p_63c_5_kingcobra_animal' in rid:
        item['name'] = item['nameRu'] = item['nameEn'] = 'Kingcobra (Animal)'
    if item.get('class') == 'coastal' and re.search(r'(ya_5m|type_t_51|t51|type_t_14|t14)', rid):
        item['role'], item['roleEn'] = 'Катер', 'Boat'
    if item.get('class') == 'ground' and re.search(r'sub_i_ii', rid):
        item['role'], item['roleEn'] = 'ЗСУ', 'SPAA'
    return item
# --- end v2.38 layer ---


# --- v2.39: less noisy conflict report and final class/status guard refresh ---
IMAGE_LOGIC_VERSION = "v2.39-unified-data-conflict-clean"

def _norm_conflict_class_v239(x: object) -> str:
    s = str(x or '').strip().lower()
    return {
        'aviation':'air','aircraft':'air','plane':'air','самолёт':'air','самолет':'air','авиация':'air','air':'air',
        'ground':'ground','tank':'ground','наземка':'ground','наземная':'ground',
        'helicopter':'heli','helicopters':'heli','вертолёты':'heli','вертолеты':'heli','heli':'heli',
        'coastal':'coastal','coastal fleet':'coastal','малый флот':'coastal',
        'bluewater':'bluewater','bluewater fleet':'bluewater','большой флот':'bluewater'
    }.get(s, s)

def _rank_idx_v239(x: object) -> int:
    r = str(x or '').strip().upper()
    vals = ['I','II','III','IV','V','VI','VII','VIII']
    return vals.index(r) if r in vals else -1

def _conflict_entry_v225(row: dict[str, Any], item: dict[str, Any], src: dict[str, Any]) -> dict[str, Any] | None:  # v2.39 override: signal, not noise
    conflicts: list[str] = []
    rid = row_identifier(row)
    final_cls = _norm_conflict_class_v239(item.get('class',''))
    api_cls = _norm_conflict_class_v239(src.get('apiClass',''))
    wiki_cls = _norm_conflict_class_v239(src.get('wikiClass',''))
    role_cls = _norm_conflict_class_v239(src.get('roleClass',''))
    # Do not count ordinary API-vs-Wiki wording differences. Report only when the final class contradicts both useful sources.
    useful = [x for x in (api_cls, wiki_cls, role_cls) if x and x not in ('unknown','none','')]
    if useful and final_cls and len(set(useful + [final_cls])) > 1:
        if useful.count(final_cls) == 0:
            conflicts.append('final_class_contradicts_sources')
    name = str(item.get('name',''))
    try:
        if is_bad_display_name_v223(name, rid):
            conflicts.append('suspicious_display_name')
    except Exception:
        pass
    # Severe rank-only checks: high BR in rank I/II is useful; normal API/wiki Roman formatting differences are not.
    br = item.get('br') or {}
    try:
        max_br = max(float(br.get(k) or 0) for k in ('ab','rb','sb'))
    except Exception:
        max_br = 0
    if max_br >= 7.0 and _rank_idx_v239(item.get('rank')) <= 1:
        conflicts.append('high_br_low_rank')
    if not conflicts:
        return None
    meta = WIKI_UNIT_META_MAP.get(rid) or {}
    return {
        'identifier': rid,
        'displayName': name,
        'displayNameEn': item.get('nameEn',''),
        'nation': item.get('nation'),
        'apiClass': api_cls,
        'wikiClass': wiki_cls,
        'roleClass': role_cls,
        'finalClass': final_cls,
        'finalRank': item.get('rank'),
        'finalBR': item.get('br',{}),
        'conflictType': sorted(set(conflicts)),
        'resolutionSource': 'v2.39 cleaned diagnostic: only severe contradictions are reported',
        'wikiTitleRu': meta.get('title_ru',''),
        'wikiTitleEn': meta.get('title_en', meta.get('title','')),
    }

# --- end v2.39 layer ---

# --- v2.43 official unit-name/rank/class guard based on live unit pages and user-found cases ---
# Rationale: when Wiki fetch fails transiently, do not let local fallback names/ranks win for known unit pages.
V243_OFFICIAL_UNIT_OVERRIDES = {
    "us_m4a5_ram_2": {"ru": "M4A5", "en": "M4A5", "rank": "II"},
    "uk_m4a5_ram_2": {"ru": "M4A5", "en": "M4A5", "rank": "II"},
    "us_t18_e2": {"ru": "T18E2", "en": "T18E2", "rank": "II"},
    "us_mk1_grant": {"ru": "Grant I (США)", "en": "Grant I (USA)", "rank": "II"},
    "uk_mk1_grant": {"ru": "Grant I", "en": "Grant I", "rank": "II"},
    "us_lvt_a_1": {"ru": "LVT(A)(1)", "en": "LVT(A)(1)", "rank": "I"},
    "us_lvt_a_1_trb": {"ru": "LVT(A)(1) (TheRussianBadger)", "en": "LVT(A)(1) (TheRussianBadger)", "rank": "I", "status": "Акционная"},
    "us_lvt_4_zis_2": {"ru": "LVT(A)(4) (ZiS-2) (США)", "en": "LVT(A)(4) (ZIS-2) (USA)", "rank": "II", "status": "Акционная"},
    "cn_lvt_4_zis_2": {"ru": "LVT(A)(4) (ZiS-2)", "en": "LVT(A)(4) (ZIS-2)", "rank": "II", "status": "Акционная"},
    "us_m8_greyhound": {"ru": "M8 LAC", "en": "M8 LAC"},
    "cn_m8_greyhound": {"ru": "M8 LAC (Китай)", "en": "M8 LAC (China)"},
    "us_m3a1_stuart_usmc": {"ru": "M3A1 (USMC)", "en": "M3A1 (USMC)"},
    "us_m4_sherman_promo": {"ru": "M4", "en": "M4", "rank": "II", "status": "Акционная"},
    "us_m24_chaffee_tl": {"ru": "M24 (TL)", "en": "M24 (TL)", "rank": "II", "status": "Акционная"},
    "us_m5a1_stuart_td": {"ru": "M5A1 TD", "en": "M5A1 TD", "rank": "II", "status": "Акционная"},
    "p-63c-5_kingcobra_animal_version": {"ru": "Kingcobra (Animal)", "en": "Kingcobra (Animal)", "status": "Акционная"},
    "ussr_ya_5m": {"ru": "Я-5М", "en": "Ya-5M", "class": "coastal", "role": "Катер", "rank": "I"},
    "ussr_zsu_29k": {"ru": "ЯГ-10 (29-К)", "en": "YaG-10 (29-K)", "class": "ground", "role": "ПТ-САУ", "rank": "II"},
    "jp_sub_i_ii_20mm": {"ru": "SUB-I-II", "en": "SUB-I-II", "class": "ground", "role": "ЗСУ", "rank": "IV"},
}

_old_unit_slug_candidates_v243 = _unit_slug_candidates_v224
def _unit_slug_candidates_v224(identifier: str, row: dict[str, Any] | None = None) -> list[str]:  # v2.43 override
    # Always try the exact API identifier first; many WT unit pages are exactly /unit/<identifier>.
    raw = norm_id(identifier)
    vals = []
    def add(x):
        if x and x not in vals:
            vals.append(x)
    add(raw)
    if row:
        add(row_identifier(row))
    for x in _old_unit_slug_candidates_v243(identifier, row):
        add(x)
    return vals

_old_build_display_name_map_v243 = build_display_name_map
def build_display_name_map(rows: list[dict[str, Any]], workers: int = 12, retry_missing: bool = False, fetch_wiki: bool = True) -> dict[str, str]:  # v2.43 override
    names = _old_build_display_name_map_v243(rows, workers=workers, retry_missing=retry_missing, fetch_wiki=fetch_wiki)
    try:
        global DISPLAY_NAME_MAP_RU, DISPLAY_NAME_MAP_EN
        for vid, o in V243_OFFICIAL_UNIT_OVERRIDES.items():
            if o.get('ru'):
                DISPLAY_NAME_MAP_RU[vid] = o['ru']
                names[vid] = o['ru']
            if o.get('en'):
                DISPLAY_NAME_MAP_EN[vid] = o['en']
        DISPLAY_NAMES_RU_JSON.write_text(json.dumps(DISPLAY_NAME_MAP_RU, ensure_ascii=False, indent=2), encoding='utf-8')
        DISPLAY_NAMES_EN_JSON.write_text(json.dumps(DISPLAY_NAME_MAP_EN, ensure_ascii=False, indent=2), encoding='utf-8')
        DISPLAY_NAMES_JSON.write_text(json.dumps(DISPLAY_NAME_MAP_RU, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"v2.44 official-name overrides applied: {len(V243_OFFICIAL_UNIT_OVERRIDES)} units")
    except Exception as e:
        print("WARNING: v2.43 official-name override write failed:", e)
    return names

_old_normalize_for_ui_v243 = normalize_for_ui
def normalize_for_ui(row: dict[str, Any], image_map: dict[str, str]) -> dict[str, Any] | None:  # v2.43 override
    item = _old_normalize_for_ui_v243(row, image_map)
    if not item:
        return item
    rid = str(item.get('id') or row_identifier(row)).strip()
    o = V243_OFFICIAL_UNIT_OVERRIDES.get(rid)
    if o:
        item['name'] = o.get('ru') or item.get('name')
        item['nameRu'] = o.get('ru') or item.get('nameRu') or item.get('name')
        item['nameEn'] = o.get('en') or item.get('nameEn') or item.get('name')
        if o.get('rank'):
            item['rank'] = o['rank']
        if o.get('class'):
            item['class'] = o['class']
            item['className'] = CLASS_NAMES.get(o['class'], o['class'])
        if o.get('role'):
            item['role'] = o['role']
            if o['role'] == 'ЗСУ': item['roleEn'] = 'SPAA'
            if o['role'] == 'Катер': item['roleEn'] = 'Boat'
        if o.get('status'):
            item['status'] = o['status']
    return item

# v2.43: removed broken make_conflict_report_item override; base script has no such hook in this branch.

# --- end v2.43 layer ---


# --- v2.45: official Wiki tree placement parser (left researchable vs right special column) ---
# Goal: stop guessing right-column vehicles by id suffixes.  The primary source is the
# visible official Wiki tree pages (/ground, /aviation, ...).  Old-Wiki gift/premium
# category pages are used only as a fallback/status hint when the new tree text loses
# the visual column boundary in plain text.
TREE_PLACEMENT_JSON = DATA_DIR / "wiki_tree_placement.json"
TREE_PLACEMENT_STATUS_JSON = DATA_DIR / "wiki_tree_placement_status.json"
TREE_PLACEMENT_MAP: dict[str, dict[str, Any]] = {}

RIGHT_COLUMN_SYMBOLS_V245 = tuple("○◔◍◌␗␙▄▃▀▂◄★☆")
OLD_WIKI_SPECIAL_CATEGORY_URLS_V245 = [
    ("gift_ground", "https://old-wiki.warthunder.com/Category:Gift_ground_vehicles"),
    ("premium_ground", "https://old-wiki.warthunder.com/Category:Premium_ground_vehicles"),
    ("gift_air", "https://old-wiki.warthunder.com/Category:Gift_aircraft"),
    ("premium_air", "https://old-wiki.warthunder.com/Category:Premium_aircraft"),
    ("gift_bluewater", "https://old-wiki.warthunder.com/Category:Gift_ships"),
    ("premium_bluewater", "https://old-wiki.warthunder.com/Category:Premium_ships"),
    ("gift_coastal", "https://old-wiki.warthunder.com/Category:Gift_boats"),
    ("premium_coastal", "https://old-wiki.warthunder.com/Category:Premium_boats"),
    ("gift_heli", "https://old-wiki.warthunder.com/Category:Gift_helicopters"),
    ("premium_heli", "https://old-wiki.warthunder.com/Category:Premium_helicopters"),
]

def _tree_clean_name_v245(line: str) -> tuple[str, bool]:
    import html as _html
    raw = _html.unescape(str(line or '')).replace('\xa0', ' ').strip()
    has_marker = bool(raw and raw[0] in RIGHT_COLUMN_SYMBOLS_V245)
    clean = re.sub(r"^[^A-Za-z0-9А-Яа-я]+", "", raw).strip()
    clean = re.sub(r"\s+", " ", clean)
    return clean, has_marker

def _canon_tree_key_v245(s: str) -> str:
    s = str(s or '').lower().replace('ё','е')
    return re.sub(r"[^a-z0-9а-я]+", "", s)

def _load_old_wiki_special_ids_v245(id_set: set[str]) -> dict[str, str]:
    """Return ids mentioned by old official Wiki gift/premium category pages.
    This is a fallback to support cases where the new Wiki tree text is reachable but
    plain-text extraction loses exact column geometry.  It is intentionally cached.
    """
    cache = DATA_DIR / 'old_wiki_special_status_v245.json'
    out: dict[str, str] = {}
    existing = {}
    if cache.exists():
        try: existing = json.loads(cache.read_text(encoding='utf-8'))
        except Exception: existing = {}
    items = existing.get('items', {}) if isinstance(existing, dict) else {}
    changed = False
    for kind, url in OLD_WIKI_SPECIAL_CATEGORY_URLS_V245:
        cached = items.get(kind)
        html = None
        if isinstance(cached, dict) and cached.get('status') == 'ok' and cached.get('ids'):
            for vid in cached.get('ids', []):
                if vid in id_set: out[vid] = kind
            continue
        try:
            code, text, how = fetch_text_with_fallback(url)
            if code == 200 and text:
                html = text
        except Exception:
            html = None
        ids=[]
        if html:
            # image filenames are usually exact API ids: us_lvt_4_zis_2.png, us_m5a1_stuart_td.png, etc.
            for m in re.finditer(r"([a-z0-9][a-z0-9_\-]{2,})\.(?:png|webp|jpg)", html, flags=re.I):
                cand = m.group(1).lower().replace('-', '_')
                if cand in id_set and cand not in ids:
                    ids.append(cand)
                    out[cand] = kind
            items[kind] = {'status':'ok' if ids else 'missing', 'url':url, 'ids':ids, 'count':len(ids), 'updated_at':now_iso()}
            changed = True
        else:
            items[kind] = {'status':'failed', 'url':url, 'ids':[], 'count':0, 'updated_at':now_iso()}
            changed = True
    if changed:
        try: cache.write_text(json.dumps({'items':items}, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception: pass
    return out

def _build_tree_name_resolver_v245(rows: list[dict[str, Any]]) -> dict[tuple[str,str], dict[str, list[str]]]:
    res: dict[tuple[str,str], dict[str, list[str]]] = {}
    for row in rows:
        vid = row_identifier(row)
        if not vid: continue
        nation = nation_normalized(row); cls = normalize_vehicle_class(row)
        # Use official maps if already built, then fallbacks.
        names = {vid, vid.replace('_','-'), vid.replace('-','_'), row_name(row), display_name_from_id(vid), pretty_name_from_id(vid)}
        try:
            names.add(DISPLAY_NAME_MAP_RU.get(vid,'')); names.add(DISPLAY_NAME_MAP_EN.get(vid,'')); names.add(DISPLAY_NAME_MAP.get(vid,''))
        except Exception: pass
        # Try current UI item but do not depend on tree placement yet.
        try:
            item = _old_normalize_for_ui_v243(row, {}) if '_old_normalize_for_ui_v243' in globals() else None
            if item:
                names.add(item.get('name','')); names.add(item.get('nameRu','')); names.add(item.get('nameEn',''))
        except Exception: pass
        bucket = res.setdefault((nation, cls), {})
        for nm in names:
            nm = str(nm or '').strip()
            if not nm: continue
            variants = {nm}
            variants.add(re.sub(r"\s+\([^)]*\)$", "", nm).strip())
            variants.add(nm.replace(' Mk.', ' Mk '))
            variants.add(nm.replace('Mk.', 'Mk'))
            variants.add(nm.replace('ZiS', 'ZIS'))
            for v in variants:
                k=_canon_tree_key_v245(v)
                if k:
                    bucket.setdefault(k, []).append(vid)
    return res

def _resolve_tree_line_to_id_v245(clean: str, bucket: dict[str, list[str]], allowed_ids: set[str]) -> str | None:
    if not clean: return None
    candidates=[clean]
    if '/' in clean:
        # folders are handled elsewhere; still try full folder name first.
        candidates.extend(x.strip() for x in clean.split('/') if x.strip())
    for c in list(candidates):
        candidates.append(re.sub(r"\s+\([^)]*\)$", "", c).strip())
        candidates.append(c.replace('ZiS','ZIS'))
        candidates.append(c.replace('ZIS','ZiS'))
    for c in candidates:
        k=_canon_tree_key_v245(c)
        ids=[x for x in bucket.get(k, []) if x in allowed_ids]
        if len(ids)==1: return ids[0]
        if len(ids)>1:
            # prefer exact id/name length match; otherwise deterministic first.
            ids.sort(key=lambda x: (len(x), x))
            return ids[0]
    return None

def _parse_tree_blocks_v245(lines: list[str]) -> list[list[str]]:
    starts=[i for i,l in enumerate(lines) if str(l).strip()=='Researchable vehicles']
    blocks=[]
    for idx,start in enumerate(starts):
        end=starts[idx+1] if idx+1 < len(starts) else len(lines)
        block=lines[start:end]
        if any(re.match(r"Rank\s+[IVX]+", x) for x in block):
            blocks.append(block)
    return blocks

def build_wiki_tree_placement_v245(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    id_set={row_identifier(r) for r in rows if row_identifier(r)}
    id_to_row={row_identifier(r): r for r in rows if row_identifier(r)}
    resolver=_build_tree_name_resolver_v245(rows)
    old_special=_load_old_wiki_special_ids_v245(id_set)
    placements: dict[str, dict[str, Any]]={}
    status={'logic_version':'v2.45', 'pages':{}, 'oldWikiSpecialCount':len(old_special), 'updated_at':now_iso()}

    for cls,page in TREE_PAGE.items():
        # Fetch the all-nations page. It contains nation blocks in NATION_ORDER and is more stable
        # than per-nation query URLs, which the new Wiki sometimes ignores in static HTML.
        lines, reason = fetch_wiki_tree_lines(page, None)
        blocks=_parse_tree_blocks_v245(lines) if lines else []
        status['pages'][page]={'reason':reason, 'lines':len(lines), 'blocks':len(blocks)}
        for bi, block in enumerate(blocks[:len(NATION_ORDER)]):
            nation=NATION_ORDER[bi]
            bucket=resolver.get((nation, cls), {})
            allowed={vid for vid,row in id_to_row.items() if nation_normalized(row)==nation and normalize_vehicle_class(row)==cls}
            if not bucket or not allowed: continue
            current_rank=''
            for line in block:
                line=str(line or '').strip()
                m=re.match(r"Rank\s+([IVX]+)", line)
                if m:
                    current_rank=m.group(1); continue
                if not current_rank: continue
                if not line or line in {'Researchable vehicles','Premium vehicles','Tree','List'}: continue
                if re.search(r"vehicles$|Found:|Filters|Country|Roles|Battle Rating|Arcade Battles|Realistic Battles|Simulator Battles", line, re.I): continue
                if len(line)>80: continue
                clean, marker=_tree_clean_name_v245(line)
                if not clean: continue
                vid=_resolve_tree_line_to_id_v245(clean, bucket, allowed)
                if not vid: continue
                row=id_to_row.get(vid, {})
                stat=normalize_status(row)
                right = (stat != 'Обычная') or marker or (vid in old_special)
                source=[]
                if stat!='Обычная': source.append('api/status')
                if marker: source.append('wiki-tree-marker')
                if vid in old_special: source.append('old-wiki-'+old_special[vid])
                if not source: source.append('wiki-tree-left')
                # right wins over left if the same vehicle is encountered twice.
                prev=placements.get(vid)
                if (not prev) or (prev.get('placement')!='right' and right):
                    placements[vid]={'placement':'right' if right else 'left','rank':current_rank,'nation':nation,'class':cls,'visibleName':clean,'source':'+'.join(source),'page':page}
    # User-found / API-hidden cases are fallback overrides, not primary source. Keep them explicit.
    fallback_right={
        'us_lvt_a_1_trb':'affiliate/trb fallback', 'p-36c_rb':'affiliate/rb fallback',
        'us_lvt_4_zis_2':'old/event fallback', 'us_m5a1_stuart_td':'old/event fallback',
        'us_m24_chaffee_tl':'old/event fallback', 'us_m4_sherman_promo':'promo fallback',
        'ussr_bt_7_1937_td':'twitch drop fallback', 'p-63c-5_kingcobra_animal_version':'animal fallback'
    }
    for vid, reason in fallback_right.items():
        if vid in id_set:
            row=id_to_row.get(vid,{})
            placements[vid]={'placement':'right','rank':normalize_rank(first_defined(row,['rank','tier','vehicle_rank'],'I')),'nation':nation_normalized(row),'class':normalize_vehicle_class(row),'visibleName':'','source':reason,'page':'fallback'}
    try:
        TREE_PLACEMENT_JSON.write_text(json.dumps({'logic_version':'v2.45','items':placements}, ensure_ascii=False, indent=2), encoding='utf-8')
        TREE_PLACEMENT_STATUS_JSON.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        print('WARNING: could not write wiki_tree_placement:', e)
    print(f"Wiki tree placement: {sum(1 for v in placements.values() if v.get('placement')=='right')} right-column/special, {sum(1 for v in placements.values() if v.get('placement')=='left')} left/researchable, old-wiki special hints {len(old_special)}.")
    return placements

_old_normalize_for_ui_v245 = normalize_for_ui
def normalize_for_ui(row: dict[str, Any], image_map: dict[str, str]) -> dict[str, Any] | None:  # v2.45 override
    item=_old_normalize_for_ui_v245(row, image_map)
    if not item: return item
    vid=str(item.get('id') or row_identifier(row))
    tp=TREE_PLACEMENT_MAP.get(vid) if isinstance(TREE_PLACEMENT_MAP, dict) else None
    if tp:
        item['treePlacement']=tp.get('placement')
        item['treePlacementSource']=tp.get('source')
        if tp.get('rank'):
            item['treeRank']=tp.get('rank')
            # The official tree rank is safer than fallback rank when it comes from tree text.
            if tp.get('source') and 'wiki-tree' in tp.get('source',''):
                item['rank']=tp.get('rank')
        if tp.get('placement')=='right' and item.get('status')=='Обычная':
            item['status']='Акционная'
    else:
        item['treePlacement']='left' if item.get('status')=='Обычная' else 'right'
        item['treePlacementSource']='fallback-status'
    return item

# --- end v2.45 Wiki tree placement layer ---



# --- v2.59: final safe class guards before real tree work ---
# These guards fix the two dangerous broad matches found in UI testing:
# 1) "tank destroyer" / "ПТ-САУ" must never be treated as naval destroyer.
# 2) frigates / MPK / SKR / small escorts are Coastal in this roster split.
# Hidden internal items like destroyer_heavy_tank_h are dropped from user roster.
def _is_hidden_internal_v259(row: dict[str, Any]) -> bool:
    rid = row_identifier(row).lower().replace('-', '_')
    nm = row_name(row).lower().replace('-', '_')
    return bool(re.search(r'(tutorial|test_vehicle|test_veh|dummy|destroyer_heavy_tank|sub_event|sdi_minotaur|soc_1|sc_1|o3u_1|f_4e_event|ah_1f_event)', rid + ' ' + nm))

def _class_guard_v259(row: dict[str, Any]) -> str | None:
    rid = row_identifier(row).lower().replace('-', '_')
    role = ' '.join(str(row.get(k, '') or '') for k in ('role','roleEn','type','vehicle_type','vehicleType')).lower()
    name = row_name(row).lower()
    blob = f'{rid} {role} {name}'
    if re.search(r'(sub_event|sdi_minotaur|destroyer_heavy_tank)', rid):
        return None
    if rid == 'us_t14':
        return 'ground'
    if rid == 'ussr_ya_5m':
        return 'coastal'
    if rid == 'ussr_zsu_29k' or re.search(r'(^|_)zsu(_|\d)', rid) or re.search(r'\b(зсу|spaa|anti.?air)\b', role):
        return 'ground'
    if 'sub_i_ii' in rid:
        return 'ground'
    if re.search(r'\b(tank destroyer|tank_destroyer|пт\s*-?\s*сау|пт|самоход|сау|spaa|anti.?air|зсу)\b', role):
        return 'ground'
    if re.search(r'(^|_)isu[_-]', rid) or re.search(r'(^|_)su[_-](5|57|57b|76|85|100|122|152)(_|$|-)', rid) or re.search(r'(^|_)su_(76|85|100|122|152|57b)(_|$)', rid):
        return 'ground'
    if re.search(r'(^|_)(type_t_51|type_t_14|t_51a|t_51b)(_|$)', rid) or re.search(r'^jp_t14', rid):
        return 'coastal'
    if re.search(r'coastal|gun_boat|heavy_gun_boat|naval_ferry_barge|torpedo_boat|submarine_chaser', rid):
        return 'coastal'
    if re.search(r'\b(фрег|frigate|катер|канонер|coastal|boat|gun boat|gunboat|torpedo boat|submarine chaser)\b', role + ' ' + blob):
        return 'coastal'
    if re.search(r'battlecruiser|battleship|cruiser|bluewater', rid) or re.search(r'(^|_)destroyer(_|$)', rid) or re.search(r'\b(лк|крейсер|эсм|эсминец|линкор|battleship|battlecruiser|cruiser|destroyer)\b', role):
        return 'bluewater'
    return None

_old_normalize_vehicle_class_v259 = normalize_vehicle_class
def normalize_vehicle_class(row: dict[str, Any]) -> str:  # v2.59 override
    g = _class_guard_v259(row)
    if g:
        return g
    return _old_normalize_vehicle_class_v259(row)

_old_final_class_v259 = final_class_v225
def final_class_v225(row: dict[str, Any]) -> tuple[str, dict[str, str]]:  # v2.59 override
    g = _class_guard_v259(row)
    if g:
        return g, {'finalGuard': 'v2.59'}
    return _old_final_class_v259(row)

_old_normalize_for_ui_v259 = normalize_for_ui
def normalize_for_ui(row: dict[str, Any], image_map: dict[str, str]) -> dict[str, Any] | None:  # v2.59 override
    if _is_hidden_internal_v259(row):
        return None
    item = _old_normalize_for_ui_v259(row, image_map)
    if not item:
        return item
    g = _class_guard_v259(row)
    if g:
        item['class'] = g
        item['className'] = CLASS_NAMES.get(g, g)
        if g == 'coastal' and re.search(r'фрег|frigate', str(item.get('role','')) + ' ' + str(item.get('roleEn','')), re.I):
            item['role'] = item.get('role') or 'Фрег.'
            item['roleEn'] = item.get('roleEn') or 'Frigate'
    vid = str(item.get('id') or row_identifier(row))
    if vid == 'us_t14':
        item['class'] = 'ground'
        item['className'] = CLASS_NAMES.get('ground', 'ground')
        item['role'] = 'ТТ'
        item['roleEn'] = 'Heavy tank'
        item['rank'] = 'III'
        item['treeRank'] = 'III'
    if vid == 'mig-21_bis_event':
        item['nameRu'] = 'МиГ-21 бис'
        item['nameEn'] = 'MiG-21 bis'
        item['name'] = 'МиГ-21 бис'
    return item

# --- end v2.59 class guards ---


# --- v2.61: raw API status wins over fragile Wiki glyphs; safer Wiki title validation ---
IMAGE_LOGIC_VERSION = "v2.62-status-tree-unit-supplements"
SUSPICIOUS_DISPLAY_NAMES_JSON = DATA_DIR / "suspicious_display_names.json"

_BAD_WIKI_TITLE_WORDS_V261 = re.compile(
    r"(истори|гайд|руководств|обзор|тактик|категори|category|guide|history|update|обновлен|devblog|news|war thunder wiki)",
    re.I,
)


def _truthy_v261(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).strip().lower() in {"1", "true", "yes", "y", "да"}


def _raw_api_status_v261(row: dict[str, Any]) -> str:
    # Explicit API fields are safer than tree text glyphs. Order matters:
    # event+market should be shown as Акционная in the UI, not merely Маркет.
    if _truthy_v261(row.get("squadron_vehicle")) or _truthy_v261(row.get("squadronVehicle")) or _truthy_v261(row.get("isSquadron")):
        return "Полковая"
    if _truthy_v261(row.get("is_pack")) or _truthy_v261(row.get("isPack")) or _truthy_v261(row.get("pack")):
        return "Пакетная"
    if _truthy_v261(row.get("is_premium")) or _truthy_v261(row.get("isPremium")) or _truthy_v261(row.get("premium")):
        return "Премиум"
    ev = row.get("event")
    if ev is not None and str(ev).strip():
        return "Акционная"
    if _truthy_v261(row.get("on_marketplace")) or _truthy_v261(row.get("is_market")) or _truthy_v261(row.get("isMarket")):
        return "Маркет"
    return "Обычная"


_old_normalize_status_v261 = normalize_status
def normalize_status(row: dict[str, Any]) -> str:  # v2.61 override
    rid = norm_id(row_identifier(row)).replace('-', '_').lower()
    if re.search(r'(tutorial|test_vehicle|test_veh|dummy|destroyer_heavy_tank|sub_event|sdi_minotaur)', rid):
        return "Спец."
    st = _raw_api_status_v261(row)
    if st != "Обычная":
        return st
    # Creator/affiliate narrow guards from older layers still apply.
    sg = _special_status_guard_v238(row) if '_special_status_guard_v238' in globals() else None
    if sg:
        return sg
    return "Обычная"


def _is_suspicious_wiki_title_v261(title: str, vid: str, row: dict[str, Any] | None = None, lang: str = "") -> bool:
    t = clean_wiki_title_text(str(title or "")).strip()
    if not t:
        return True
    if len(t) > 52:
        return True
    if _BAD_WIKI_TITLE_WORDS_V261.search(t):
        return True
    if re.search(r"^(Aviation|Ground Vehicles|Bluewater|Coastal|Helicopters|War Thunder Wiki)$", t, re.I):
        return True
    # A normal vehicle title usually has at least one digit or a short all-caps model token;
    # don't make this fatal for named ships, but reject long sentence-like titles.
    if len(t.split()) >= 5 and not re.search(r"\d|\(|\)|-|/", t):
        return True
    return False


_old_fetch_one_lang_unit_meta_v261 = _fetch_one_lang_unit_meta_v231
def _fetch_one_lang_unit_meta_v231(identifier: str, row: dict[str, Any], lang: str, retry_missing: bool, cache: dict[str, Any]) -> tuple[dict[str, Any], str, str, bool]:  # v2.61 override
    meta, src, reason, net = _old_fetch_one_lang_unit_meta_v261(identifier, row, lang, retry_missing, cache)
    title = str((meta or {}).get('title') or '').strip()
    if meta and _is_suspicious_wiki_title_v261(title, row_identifier(row), row, lang):
        return {}, 'rejected-title-' + lang, f"{reason}: suspicious title {title!r}", net
    return meta, src, reason, net


_old_title_to_name_v261 = _title_to_name_v226
def _title_to_name_v226(title: str, vid: str, nation: str, lang: str) -> str:  # v2.61 override
    if _is_suspicious_wiki_title_v261(title, vid, None, lang):
        return ""
    return _old_title_to_name_v261(title, vid, nation, lang)


_old_build_display_name_map_v261 = build_display_name_map
def build_display_name_map(rows: list[dict[str, Any]], workers: int = 12, retry_missing: bool = False, fetch_wiki: bool = True) -> dict[str, str]:  # v2.61 override
    result = _old_build_display_name_map_v261(rows, workers=workers, retry_missing=retry_missing, fetch_wiki=fetch_wiki)
    suspicious: list[dict[str, Any]] = []
    byid = {row_identifier(r): r for r in rows if row_identifier(r)}
    for vid, row in byid.items():
        nation = nation_normalized(row)
        for lang, mp in (("ru", DISPLAY_NAME_MAP_RU), ("en", DISPLAY_NAME_MAP_EN)):
            name = str(mp.get(vid) or "").strip()
            if name and _is_suspicious_wiki_title_v261(name, vid, row, lang):
                fallback = _ru_name_final_v225(display_name_from_identifier_v223(vid, row), vid, nation) if lang == "ru" else normalize_latin_display_name(display_name_from_identifier_v223(vid, row), vid)
                suspicious.append({"id": vid, "lang": lang, "rejected": name, "fallback": fallback})
                mp[vid] = fallback
    if suspicious:
        SUSPICIOUS_DISPLAY_NAMES_JSON.write_text(json.dumps({"logic_version": IMAGE_LOGIC_VERSION, "count": len(suspicious), "items": suspicious}, ensure_ascii=False, indent=2), encoding="utf-8")
        DISPLAY_NAMES_RU_JSON.write_text(json.dumps(DISPLAY_NAME_MAP_RU, ensure_ascii=False, indent=2), encoding="utf-8")
        DISPLAY_NAMES_EN_JSON.write_text(json.dumps(DISPLAY_NAME_MAP_EN, ensure_ascii=False, indent=2), encoding="utf-8")
        DISPLAY_NAMES_JSON.write_text(json.dumps(DISPLAY_NAME_MAP_RU, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Suspicious Wiki display names rejected: {len(suspicious)} written to data/suspicious_display_names.json")
    return DISPLAY_NAME_MAP_RU


def _tree_clean_name_v245(line: str) -> tuple[str, bool]:  # v2.61 override
    import html as _html
    raw = _html.unescape(str(line or '')).replace('\xa0', ' ').strip()
    # Stars on the new Wiki can mean operator/lend-lease marker inside the research column.
    # Do not treat them as right-column markers. Right side comes from API status/real special flags.
    has_marker = bool(raw and raw[0] in tuple("○◔◍◌␗␙▄▃▀▂◄"))
    clean = re.sub(r"^[^A-Za-z0-9А-Яа-я]+", "", raw).strip()
    clean = re.sub(r"\s+", " ", clean)
    return clean, has_marker


_old_build_wiki_tree_placement_v261 = build_wiki_tree_placement_v245
def build_wiki_tree_placement_v245(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:  # v2.61 override
    placements = _old_build_wiki_tree_placement_v261(rows)
    id_to_row = {row_identifier(r): r for r in rows if row_identifier(r)}
    changed = {"ordinary_forced_left": 0, "status_forced_right": 0}
    for vid, row in id_to_row.items():
        st = _raw_api_status_v261(row)
        if st == "Обычная":
            p = placements.get(vid)
            # If the only reason was a parsed marker/star, trust raw API ordinary status.
            if p and p.get('placement') == 'right' and 'wiki-tree-marker' in str(p.get('source','')) and 'api/status' not in str(p.get('source','')):
                p['placement'] = 'left'
                p['source'] = str(p.get('source','')) + '+raw-api-ordinary-guard'
                changed['ordinary_forced_left'] += 1
        else:
            p = placements.setdefault(vid, {"rank": normalize_rank(first_defined(row, ['rank','tier','vehicle_rank'], 'I')), "nation": nation_normalized(row), "class": normalize_vehicle_class(row), "visibleName": row_name(row), "page": "raw-api"})
            if p.get('placement') != 'right':
                changed['status_forced_right'] += 1
            p['placement'] = 'right'
            p['source'] = (str(p.get('source','')) + '+raw-api-status').strip('+')
    try:
        TREE_PLACEMENT_JSON.write_text(json.dumps({'logic_version':'v2.61','items':placements,'guards':changed}, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        print('WARNING: could not rewrite v2.61 wiki_tree_placement:', e)
    print(f"v2.61 tree placement guards: ordinary forced left {changed['ordinary_forced_left']}, status forced right {changed['status_forced_right']}.")
    return placements


_old_normalize_for_ui_v261 = normalize_for_ui
def normalize_for_ui(row: dict[str, Any], image_map: dict[str, str]) -> dict[str, Any] | None:  # v2.61 override
    item = _old_normalize_for_ui_v261(row, image_map)
    if not item:
        return item
    st = _raw_api_status_v261(row)
    if st:
        item['status'] = st
    vid = row_identifier(row)
    tp = TREE_PLACEMENT_MAP.get(vid) if isinstance(TREE_PLACEMENT_MAP, dict) else None
    if st == 'Обычная':
        item['treePlacement'] = 'left'
        item['treePlacementSource'] = (tp or {}).get('source', '') + '+raw-api-ordinary-guard'
    elif st:
        item['treePlacement'] = 'right'
        item['treePlacementSource'] = 'raw-api-status'
    return item
# --- end v2.61 layer ---



# --- v2.62: unit-level status/placement corrections + Wiki-only event supplements + spawn drone hide ---
STATUS_RIGHT_OVERRIDES_V262 = {
    "ussr_t_80u_yt_cup_2019": "Акционная",
    "ussr_bt_7_1937_td": "Акционная",
    "tu-2_early": "Премиум",
    "a-26c": "Акционная",
    "p-40e_td": "Акционная",
    "p-36c_rb": "Акционная",
    "us_lvt_a_1_trb": "Акционная",
    "p-63c-5_kingcobra_animal_version": "Акционная",
    "tu_95m": "Акционная",
    "b_52h": "Акционная",
}
STATUS_LEFT_OVERRIDES_V262 = {
    "ussr_is_2_1944_321": "Обычная",
}
HIDDEN_ID_RE_V262 = re.compile(r"^(uav_|ucav_|soc_1$|sc_1$|o3u_1$|f_4e_event$|ah_1f_event$)|sub_event|sdi_minotaur|destroyer_heavy_tank|tutorial|test_vehicle|test_veh|dummy", re.I)
SUPPLEMENTAL_WIKI_UNITS_V262 = [
    {"identifier":"ussr_t_80u_yt_cup_2019","country":"ussr","vehicle_type":"medium_tank","vehicle_sub_types":[],"era":7,"arcade_br":11.7,"realistic_br":11.7,"realistic_ground_br":11.7,"simulator_br":11.7,"simulator_ground_br":11.7,"event":"youtube_cup_2019","is_premium":0,"is_pack":0,"on_marketplace":0,"squadron_vehicle":0,"visibility":"wiki_supplement"},
    {"identifier":"tu_95m","country":"ussr","vehicle_type":"bomber","vehicle_sub_types":[],"era":6,"arcade_br":8.3,"realistic_br":8.3,"realistic_ground_br":8.3,"simulator_br":8.3,"simulator_ground_br":8.3,"event":"nuclear_escalation_2026","is_premium":0,"is_pack":0,"on_marketplace":0,"squadron_vehicle":0,"visibility":"wiki_supplement"},
    {"identifier":"b_52h","country":"usa","vehicle_type":"bomber","vehicle_sub_types":[],"era":6,"arcade_br":8.7,"realistic_br":8.7,"realistic_ground_br":8.7,"simulator_br":8.7,"simulator_ground_br":8.7,"event":"nuclear_escalation_2026","is_premium":0,"is_pack":0,"on_marketplace":0,"squadron_vehicle":0,"visibility":"wiki_supplement"},
]

_old_fetch_all_vehicles_v262 = fetch_all_vehicles
def fetch_all_vehicles() -> list[dict[str, Any]]:  # v2.62 override
    rows = _old_fetch_all_vehicles_v262()
    seen = {row_identifier(r) for r in rows if row_identifier(r)}
    added = 0
    for r in SUPPLEMENTAL_WIKI_UNITS_V262:
        if r["identifier"] not in seen:
            rows.append(dict(r))
            added += 1
    if added:
        print(f"v2.62 Wiki-only supplements added: {added} vehicles not present in WT Vehicles API.")
    return rows

_old_raw_api_status_v262 = _raw_api_status_v261
def _raw_api_status_v261(row: dict[str, Any]) -> str:  # v2.62 override
    vid = row_identifier(row)
    if vid in STATUS_LEFT_OVERRIDES_V262:
        return STATUS_LEFT_OVERRIDES_V262[vid]
    if vid in STATUS_RIGHT_OVERRIDES_V262:
        return STATUS_RIGHT_OVERRIDES_V262[vid]
    # API event field is noisy for some units. Only strong status fields are safe globally;
    # event-only rows need Wiki/known-unit confirmation.
    if _truthy_v261(row.get("squadron_vehicle")) or _truthy_v261(row.get("squadronVehicle")) or _truthy_v261(row.get("isSquadron")):
        return "Полковая"
    if _truthy_v261(row.get("is_pack")) or _truthy_v261(row.get("isPack")) or _truthy_v261(row.get("pack")):
        return "Пакетная"
    if _truthy_v261(row.get("is_premium")) or _truthy_v261(row.get("isPremium")) or _truthy_v261(row.get("premium")):
        return "Премиум"
    if _truthy_v261(row.get("on_marketplace")) or _truthy_v261(row.get("is_market")) or _truthy_v261(row.get("isMarket")):
        return "Маркет"
    return "Обычная"

_old_is_hidden_internal_v262 = _is_hidden_internal_v259
def _is_hidden_internal_v259(row: dict[str, Any]) -> bool:  # v2.62 override
    rid = norm_id(row_identifier(row)).replace('-', '_').lower()
    if HIDDEN_ID_RE_V262.search(rid):
        return True
    return _old_is_hidden_internal_v262(row)

_old_build_wiki_tree_placement_v262 = build_wiki_tree_placement_v245
def build_wiki_tree_placement_v245(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:  # v2.62 override
    placements = _old_build_wiki_tree_placement_v262(rows)
    id_to_row = {row_identifier(r): r for r in rows if row_identifier(r)}
    changed = {"forced_left": 0, "forced_right": 0, "hidden_removed": 0}
    for vid, status in STATUS_LEFT_OVERRIDES_V262.items():
        if vid in id_to_row:
            p = placements.setdefault(vid, {"rank": normalize_rank(first_defined(id_to_row[vid], ['rank','tier','vehicle_rank','era'], 'I')), "nation": nation_normalized(id_to_row[vid]), "class": normalize_vehicle_class(id_to_row[vid]), "visibleName": row_name(id_to_row[vid]), "page": "unit-override"})
            if p.get('placement') != 'left': changed['forced_left'] += 1
            p['placement'] = 'left'; p['source'] = 'unit-override-left'
    for vid, status in STATUS_RIGHT_OVERRIDES_V262.items():
        if vid in id_to_row:
            p = placements.setdefault(vid, {"rank": normalize_rank(first_defined(id_to_row[vid], ['rank','tier','vehicle_rank','era'], 'I')), "nation": nation_normalized(id_to_row[vid]), "class": normalize_vehicle_class(id_to_row[vid]), "visibleName": row_name(id_to_row[vid]), "page": "unit-override"})
            if p.get('placement') != 'right': changed['forced_right'] += 1
            p['placement'] = 'right'; p['source'] = 'unit-override-right'; p['status'] = status
    for vid in list(placements.keys()):
        if HIDDEN_ID_RE_V262.search(vid):
            placements.pop(vid, None); changed['hidden_removed'] += 1
    try:
        TREE_PLACEMENT_JSON.write_text(json.dumps({'logic_version':'v2.62','items':placements,'guards':changed}, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        print('WARNING: could not rewrite v2.62 wiki_tree_placement:', e)
    print(f"v2.62 tree placement guards: forced left {changed['forced_left']}, forced right {changed['forced_right']}, hidden removed {changed['hidden_removed']}.")
    return placements

_old_normalize_for_ui_v262 = normalize_for_ui
def normalize_for_ui(row: dict[str, Any], image_map: dict[str, str]) -> dict[str, Any] | None:  # v2.62 override
    if _is_hidden_internal_v259(row):
        return None
    item = _old_normalize_for_ui_v262(row, image_map)
    if not item:
        return item
    vid = row_identifier(row)
    if vid in STATUS_LEFT_OVERRIDES_V262:
        item['status'] = STATUS_LEFT_OVERRIDES_V262[vid]
        item['treePlacement'] = 'left'
        item['treePlacementSource'] = 'unit-override-left'
    elif vid in STATUS_RIGHT_OVERRIDES_V262:
        item['status'] = STATUS_RIGHT_OVERRIDES_V262[vid]
        item['treePlacement'] = 'right'
        item['treePlacementSource'] = 'unit-override-right'
    if vid == 'p-36c_rb':
        item['nameRu'] = 'P-36C (TheRussianBadger)'; item['nameEn'] = 'P-36C (TheRussianBadger)'; item['name'] = 'P-36C (TheRussianBadger)'
    if vid == 'ussr_t_80u_yt_cup_2019':
        item['nameRu'] = 'Т-80У (YouTube Cup 2019)'; item['nameEn'] = 'T-80U (YouTube Cup 2019)'; item['name'] = 'Т-80У (YouTube Cup 2019)'
    return item
# --- end v2.62 layer ---

# --- v2.63: compact UI support + stronger unit-level special placement corrections ---
# The API alone is not enough for left/right placement: some real special/event/wiki-only
# vehicles do not expose a strong API flag, while some ordinary tree vehicles carry noisy
# event/update metadata. Keep exact unit and safe suffix rules close to the updater so
# generated data and frontend agree.
STATUS_RIGHT_OVERRIDES_V263 = dict(STATUS_RIGHT_OVERRIDES_V262)
STATUS_RIGHT_OVERRIDES_V263.update({
    "us_m4_sherman_promo": "Акционная",
    "bf-109c_1_promo": "Акционная",
    "us_m24_chaffee_tl": "Акционная",
    "p-51a_tl": "Акционная",
    "us_m5a1_stuart_td": "Акционная",
    "uk_dark_class_mtb_td": "Акционная",
    "germ_pzkpfw_II_ausf_C_td": "Акционная",
    "germ_pzkpfw_III_ausf_J_td": "Акционная",
    "germ_sdkfz_234_2_td": "Акционная",
    "jp_type_97_kai_td": "Акционная",
    "sw_strv_m39_td": "Акционная",
    "us_m1a1_abrams_yt_cup_2019": "Акционная",
    "uk_challenger_II_yt_cup_2019": "Акционная",
    "ussr_skr_pr35": "Полковая",
    "ussr_cruiser_voroshilov": "Акционная",
})

SUPPLEMENTAL_WIKI_UNITS_V263 = list(SUPPLEMENTAL_WIKI_UNITS_V262) + [
    {"identifier":"us_m1a1_abrams_yt_cup_2019","country":"usa","vehicle_type":"medium_tank","vehicle_sub_types":[],"era":7,"arcade_br":11.7,"realistic_br":11.7,"realistic_ground_br":11.7,"simulator_br":11.7,"simulator_ground_br":11.7,"event":"youtube_cup_2019","is_premium":0,"is_pack":0,"on_marketplace":0,"squadron_vehicle":0,"visibility":"wiki_supplement"},
]

_old_fetch_all_vehicles_v263 = fetch_all_vehicles
def fetch_all_vehicles() -> list[dict[str, Any]]:  # v2.63 override
    rows = _old_fetch_all_vehicles_v263()
    seen = {row_identifier(r) for r in rows if row_identifier(r)}
    added = 0
    for r in SUPPLEMENTAL_WIKI_UNITS_V263:
        if r["identifier"] not in seen:
            rows.append(dict(r)); seen.add(r["identifier"]); added += 1
    if added:
        print(f"v2.63 Wiki-only supplements added: {added} vehicles not present in WT Vehicles API.")
    return rows

_old_raw_api_status_v263 = _raw_api_status_v261
def _raw_api_status_v261(row: dict[str, Any]) -> str:  # v2.63 override
    vid = row_identifier(row)
    if vid in STATUS_LEFT_OVERRIDES_V262:
        return STATUS_LEFT_OVERRIDES_V262[vid]
    if vid in STATUS_RIGHT_OVERRIDES_V263:
        return STATUS_RIGHT_OVERRIDES_V263[vid]
    rid = norm_id(vid).replace('-', '_').lower()
    # Safe special suffixes seen in WT unit ids: promo, Twitch Drop (TD), TL, YouTube Cup.
    # Do not use generic wiki icons/stars for this decision.
    if rid.endswith('_promo') or rid.endswith('_tl') or rid.endswith('_td') or '_yt_cup_' in rid:
        return "Акционная"
    return _old_raw_api_status_v263(row)

_old_build_wiki_tree_placement_v263 = build_wiki_tree_placement_v245
def build_wiki_tree_placement_v245(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:  # v2.63 override
    placements = _old_build_wiki_tree_placement_v263(rows)
    id_to_row = {row_identifier(r): r for r in rows if row_identifier(r)}
    changed = {"forced_right": 0, "forced_left": 0, "pattern_right": 0}
    for vid, row in id_to_row.items():
        status = _raw_api_status_v261(row)
        rid = norm_id(vid).replace('-', '_').lower()
        force_right = vid in STATUS_RIGHT_OVERRIDES_V263 or rid.endswith('_promo') or rid.endswith('_tl') or rid.endswith('_td') or '_yt_cup_' in rid
        force_left = vid in STATUS_LEFT_OVERRIDES_V262
        if force_left:
            p = placements.setdefault(vid, {"rank": normalize_rank(first_defined(row, ['rank','tier','vehicle_rank','era'], 'I')), "nation": nation_normalized(row), "class": normalize_vehicle_class(row), "visibleName": row_name(row), "page": "unit-override"})
            if p.get('placement') != 'left': changed['forced_left'] += 1
            p['placement'] = 'left'; p['source'] = 'unit-override-left-v263'; p['status'] = 'Обычная'
        elif force_right:
            p = placements.setdefault(vid, {"rank": normalize_rank(first_defined(row, ['rank','tier','vehicle_rank','era'], 'I')), "nation": nation_normalized(row), "class": normalize_vehicle_class(row), "visibleName": row_name(row), "page": "unit-override"})
            if p.get('placement') != 'right': changed['forced_right'] += 1
            if vid not in STATUS_RIGHT_OVERRIDES_V263: changed['pattern_right'] += 1
            p['placement'] = 'right'; p['source'] = 'unit-override-right-v263'; p['status'] = status if status != 'Обычная' else 'Акционная'
    try:
        TREE_PLACEMENT_JSON.write_text(json.dumps({'logic_version':'v2.63','items':placements,'guards':changed}, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        print('WARNING: could not rewrite v2.63 wiki_tree_placement:', e)
    print(f"v2.63 tree placement guards: forced right {changed['forced_right']}, pattern right {changed['pattern_right']}, forced left {changed['forced_left']}.")
    return placements

_old_normalize_for_ui_v263 = normalize_for_ui
def normalize_for_ui(row: dict[str, Any], image_map: dict[str, str]) -> dict[str, Any] | None:  # v2.63 override
    item = _old_normalize_for_ui_v263(row, image_map)
    if not item:
        return item
    vid = row_identifier(row)
    rid = norm_id(vid).replace('-', '_').lower()
    if vid == 'us_m1a1_abrams_yt_cup_2019':
        item['nameRu'] = 'M1A1 (YouTube Cup 2019)'; item['nameEn'] = 'M1A1 (YouTube Cup 2019)'; item['name'] = 'M1A1 (YouTube Cup 2019)'
    if vid in STATUS_RIGHT_OVERRIDES_V263 or rid.endswith('_promo') or rid.endswith('_tl') or rid.endswith('_td') or '_yt_cup_' in rid:
        item['status'] = _raw_api_status_v261(row)
        if item['status'] == 'Обычная': item['status'] = 'Акционная'
        item['treePlacement'] = 'right'
        item['treePlacementSource'] = 'unit-override-right-v263'
    if vid in STATUS_LEFT_OVERRIDES_V262:
        item['status'] = 'Обычная'
        item['treePlacement'] = 'left'
        item['treePlacementSource'] = 'unit-override-left-v263'
    return item
# --- end v2.63 layer ---


# --- v2.73: parse Wiki tree layout geometry for USA all classes ---
TECH_TREE_LAYOUT_JSON = DATA_DIR / "tech_tree_layout.json"
TECH_TREE_LAYOUT_STATUS_JSON = DATA_DIR / "tech_tree_layout_status.json"


def _fetch_wiki_tree_html_v272(page: str, nation: str) -> tuple[str, str]:
    nc = NATION_PARAM.get(nation, nation.lower())
    urls = [
        f"https://wiki.warthunder.ru/{page}?v=t&t_c={nc}",
        f"https://wiki.warthunder.ru/{page}?t_c={nc}&v=t",
        f"https://wiki.warthunder.com/{page}?v=t&t_c={nc}",
        f"https://wiki.warthunder.com/{page}?t_c={nc}&v=t",
    ]
    reasons=[]
    for url in urls:
        try:
            code, html, how = fetch_text_with_fallback(url)
            reasons.append(f"{url} -> {how}")
            if code == 200 and html and len(html) > 1000:
                return html, f"HTTP 200 via {how} url={url}"
        except Exception as e:
            reasons.append(f"{url} -> {type(e).__name__}: {e}")
    return "", "; ".join(reasons)


def _is_tree_header_v272(line: str) -> bool:
    return line in {"Исследуемая техника", "Премиумная техника", "Researchable vehicles", "Premium vehicles"}


def _rank_from_line_v272(line: str) -> str | None:
    s=str(line or '').strip()
    m=re.match(r"^([IVX]+)\s+ранг$", s, re.I)
    if m: return m.group(1).upper()
    m=re.match(r"^Rank\s+([IVX]+)$", s, re.I)
    if m: return m.group(1).upper()
    return None


def _skip_tree_line_v272(line: str) -> bool:
    s=str(line or '').strip()
    if not s or _is_tree_header_v272(s) or _rank_from_line_v272(s): return True
    if s in set(NATION_ORDER) or s in {'Tree','List','Дерево','Список'}: return True
    if re.search(r"Found:|Filters|Фильтры|Country|Страна|Roles|Роли|Battle Rating|Боевой рейтинг|Arcade|Realistic|Simulator|Коллекции", s, re.I): return True
    if len(s)>80: return True
    return False


def _clean_layout_name_v272(line: str) -> str:
    s=_clean_wiki_visible_name(line)
    # keep nation/operator symbols out of matching, but actual display still comes from vehicle data
    s=re.sub(r"^[○◌◍␗␙␠▃▀◄■★☆]+", "", s).strip()
    return re.sub(r"\s+", " ", s)


def _group_tokens_v272(label: str) -> list[str]:
    lab=_clean_layout_name_v272(label)
    lab=re.sub(r"\b(ранний|поздний|early|late|Phantom II|Abrams)\b", " ", lab, flags=re.I)
    parts=[]
    for p in re.split(r"[/,]", lab):
        p=p.strip()
        if not p: continue
        parts.append(p)
    if not parts:
        parts=[lab]
    toks=[]
    for p in parts:
        k=_name_key(p)
        if len(k)>=2: toks.append(k)
    return toks


def _member_matches_group_v272(label: str, child_name: str) -> bool:
    ck=_name_key(_clean_layout_name_v272(child_name))
    if not ck: return False
    for t in _group_tokens_v272(label):
        if len(t)>=2 and (ck.startswith(t) or t in ck or ck in t):
            return True
    return False


def _resolve_layout_line_v272(clean: str, bucket: dict[str, str]) -> str | None:
    vid=_resolve_tree_name(clean, bucket)
    return vid


def _side_for_vid_v272(vid: str, id_to_row: dict[str, dict[str, Any]], placements: dict[str, Any]) -> str:
    p=placements.get(vid) if isinstance(placements, dict) else None
    if isinstance(p, dict) and p.get('placement') == 'right':
        return 'right'
    row=id_to_row.get(vid, {})
    try:
        st=_raw_api_status_v261(row)
    except Exception:
        st=normalize_status(row) if row else 'Обычная'
    return 'right' if st and st != 'Обычная' else 'left'


def _extract_first_tree_block_v272(lines: list[str]) -> list[str]:
    starts=[i for i,l in enumerate(lines) if _is_tree_header_v272(l)]
    if not starts:
        return lines
    # Per-nation pages still often contain all nations in static text. The selected nation is first.
    start=starts[0]
    end=len(lines)
    for j in starts[1:]:
        if j-start>25:
            end=j; break
    return lines[start:end]


def _parse_layout_nodes_from_lines_v272(lines: list[str], nation: str, cls: str, rows: list[dict[str, Any]], placements: dict[str, Any]) -> dict[str, Any]:
    resolver=build_name_resolver(rows)
    bucket=resolver.get((nation, cls, 'names'), {})
    id_to_row={row_identifier(r):r for r in rows if row_identifier(r)}
    ranks={r:{'flatLeft':[], 'flatRight':[]} for r in RANK_ROMAN_V272}
    current_rank=None
    block=_extract_first_tree_block_v272(lines)
    i=0
    while i < len(block):
        raw=block[i]
        rank=_rank_from_line_v272(raw)
        if rank:
            current_rank=rank; i+=1; continue
        if not current_rank or _skip_tree_line_v272(raw):
            i+=1; continue
        clean=_clean_layout_name_v272(raw)
        vid=_resolve_layout_line_v272(clean,bucket)
        # Group/folder: visible label followed by matching member unit lines.
        if '/' in clean or not vid:
            members=[]; j=i+1
            while j < len(block):
                r2=_rank_from_line_v272(block[j])
                if r2 or _is_tree_header_v272(block[j]): break
                if _skip_tree_line_v272(block[j]):
                    j+=1; continue
                c2=_clean_layout_name_v272(block[j])
                v2=_resolve_layout_line_v272(c2,bucket)
                if not v2: break
                if not _member_matches_group_v272(clean,c2): break
                members.append({'id':v2,'visibleName':c2}); j+=1
            if members:
                side=_side_for_vid_v272(members[0]['id'], id_to_row, placements)
                node={'type':'group','label':clean,'ids':[m['id'] for m in members]}
                ranks[current_rank]['flatRight' if side=='right' else 'flatLeft'].append(node)
                i=j; continue
        if vid:
            side=_side_for_vid_v272(vid,id_to_row,placements)
            ranks[current_rank]['flatRight' if side=='right' else 'flatLeft'].append({'type':'unit','id':vid,'visibleName':clean})
        i+=1
    return ranks

RANK_ROMAN_V272=['I','II','III','IV','V','VI','VII','VIII','IX']


def _load_research_links_v272() -> dict[str, list[str]]:
    try:
        obj=json.loads(RESEARCH_LINKS_JSON.read_text(encoding='utf-8'))
        if isinstance(obj, dict) and isinstance(obj.get('links'), dict):
            return {str(k): [str(x) for x in v] for k,v in obj['links'].items() if isinstance(v,list)}
        if isinstance(obj, dict):
            return {str(k): [str(x) for x in v] for k,v in obj.items() if isinstance(v,list)}
    except Exception:
        pass
    return {}


def _node_ids_v272(node: dict[str, Any]) -> list[str]:
    if node.get('type')=='group':
        return [str(x) for x in node.get('ids',[]) if x]
    return [str(node.get('id') or '')] if node.get('id') else []


def _assign_layout_columns_v272(flat: list[dict[str, Any]], col_for: dict[str, int], links: dict[str, list[str]], max_cols: int) -> list[list[dict[str, Any]]]:
    cols=[[] for _ in range(max_cols)]
    next_col=0
    for node in flat:
        ids=_node_ids_v272(node)
        lead=ids[0] if ids else ''
        col=None
        # A group/vehicle follows its research predecessor when that predecessor is known.
        for candidate in ids:
            for parent in links.get(candidate, []):
                if parent in col_for:
                    col=col_for[parent]; break
            if col is not None: break
        if col is None:
            # Put new roots into the shortest column, preserving page order.
            lengths=[sum(len(_node_ids_v272(n)) or 1 for n in c) for c in cols]
            col=min(range(max_cols), key=lambda c:(lengths[c], c if c>=next_col else c+max_cols))
            next_col=(col+1)%max_cols
        cols[col].append(node)
        for vid in ids:
            col_for[vid]=col
    return cols


def build_wiki_tree_layout_v272(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """v2.73: diagnostic Wiki page fetcher.

    Important: v2.72 parsed _strip_html_lines(html), which destroys the real tree geometry
    (CSS grid positions, folders, right-column order, and arrows). That produced convincing but
    wrong row-major trees. This version keeps fetched raw HTML for inspection and marks generated
    flat layouts as unsafe so the frontend will not render them as final truth.
    """
    links=_load_research_links_v272()
    placements=TREE_PLACEMENT_MAP if isinstance(TREE_PLACEMENT_MAP, dict) else {}
    out={'logic_version':'v2.73-diagnostic-flat-text-only','updated_at':now_iso(),'items':{},'status':{}}
    raw_dir = DATA_DIR / 'wiki_tree_raw'
    try:
        raw_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    nation='USA'
    for cls,page in TREE_PAGE.items():
        key=f'{nation}|{cls}'
        html, reason=_fetch_wiki_tree_html_v272(page,nation)
        raw_path = raw_dir / f'{nation}_{cls}_{page}.html'
        if html:
            try:
                raw_path.write_text(html, encoding='utf-8')
            except Exception:
                pass
        if not html:
            out['items'][key]={'ranks':{},'source':'','parser':'v2.73 diagnostic; fetch failed','unsafe_flat_text':True,'error':'fetch failed'}
            out['status'][key]={'ok':False,'reason':reason,'raw_html':str(raw_path).replace(str(DATA_DIR)+str(os.sep),'data/'),'updated_at':now_iso()}
            continue
        lines=_strip_html_lines(html)
        parsed=_parse_layout_nodes_from_lines_v272(lines,nation,cls,rows,placements)
        col_for_left={}; col_for_right={}
        ranks={}
        placed_count=0
        for r in RANK_ROMAN_V272:
            left_flat=parsed.get(r,{}).get('flatLeft',[])
            right_flat=parsed.get(r,{}).get('flatRight',[])
            left_cols=_assign_layout_columns_v272(left_flat,col_for_left,links,5)
            ranks[r]={'leftCols':left_cols,'right':right_flat}
            placed_count += sum(len(_node_ids_v272(n)) or 1 for n in left_flat+right_flat)
        out['items'][key]={
            'source':reason,
            'parser':'v2.73 diagnostic flat-text parser; NOT SAFE FOR FINAL RENDER',
            'unsafe_flat_text':True,
            'note':'Raw HTML is saved under data/wiki_tree_raw/. Need DOM/CSS geometry parser; stripped text order is not the visual tree.',
            'ranks':ranks
        }
        out['status'][key]={
            'ok':placed_count>0,
            'placedUnits':placed_count,
            'lines':len(lines),
            'reason':reason,
            'raw_html':str(raw_path).replace(str(DATA_DIR)+str(os.sep),'data/'),
            'unsafe_flat_text':True,
            'updated_at':now_iso()
        }
    try:
        TECH_TREE_LAYOUT_JSON.write_text(json.dumps({'logic_version':'v2.73-diagnostic-flat-text-only','updated_at':now_iso(),'items':out['items']}, ensure_ascii=False, indent=2), encoding='utf-8')
        TECH_TREE_LAYOUT_STATUS_JSON.write_text(json.dumps(out['status'], ensure_ascii=False, indent=2), encoding='utf-8')
        print('Wiki tree raw pages fetched for USA classes:', ', '.join(f"{k}={v.get('placedUnits',0)} unsafe" for k,v in out['status'].items()))
        print('v2.73: flat text layouts are diagnostic only; frontend will ignore unsafe_flat_text layouts.')
    except Exception as e:
        print('WARNING: could not write tech_tree_layout diagnostic files:', e)
    return out

_old_parse_wiki_tree_order_v272 = parse_wiki_tree_order
def parse_wiki_tree_order(rows: list[dict[str, Any]]) -> dict[str, list[str]]:  # v2.73 override
    order=_old_parse_wiki_tree_order_v272(rows)
    try:
        build_wiki_tree_layout_v272(rows)
    except Exception as e:
        print('WARNING: Wiki tree layout parser failed:', e)
    return order
# --- end v2.73 layer ---



# --- v2.75: DOM/CSS Wiki tree parser probe (safe only when real geometry is found) ---
TECH_TREE_LAYOUT_PROBE_JSON = DATA_DIR / "tech_tree_layout_probe.json"


def _style_number_v275(style: str, keys: list[str]) -> float | None:
    for key in keys:
        m = re.search(rf"(?:^|[;\s]){re.escape(key)}\s*:\s*(-?\d+(?:\.\d+)?)\s*px", style, re.I)
        if m:
            try: return float(m.group(1))
            except Exception: pass
    return None


def _extract_coord_from_chunk_v275(chunk: str) -> tuple[float | None, float | None, str]:
    # War Thunder Wiki tree cards have changed several times. Try many common CSS/DOM encodings.
    probes=[]
    # closest inline styles around the unit href are usually enough when SSR keeps geometry.
    for m in re.finditer(r"style\s*=\s*(['\"])(.*?)\1", chunk, re.I|re.S):
        style = html.unescape(m.group(2))
        x = _style_number_v275(style, ['left', '--x', 'x'])
        y = _style_number_v275(style, ['top', '--y', 'y'])
        if x is not None or y is not None:
            probes.append((x,y,'style:'+style[:160]))
        mt = re.search(r"translate(?:3d)?\s*\(\s*(-?\d+(?:\.\d+)?)px\s*,\s*(-?\d+(?:\.\d+)?)px", style, re.I)
        if mt:
            try: probes.append((float(mt.group(1)), float(mt.group(2)), 'transform:'+style[:160]))
            except Exception: pass
        mg = re.search(r"grid-(?:column|area)\s*:\s*(\d+)", style, re.I)
        mr = re.search(r"grid-row\s*:\s*(\d+)", style, re.I)
        if mg or mr:
            try: probes.append((float(mg.group(1)) if mg else None, float(mr.group(1)) if mr else None, 'grid:'+style[:160]))
            except Exception: pass
    # data attributes
    mdx = re.search(r"data-(?:x|left|col(?:umn)?)\s*=\s*(['\"])(-?\d+(?:\.\d+)?)\1", chunk, re.I)
    mdy = re.search(r"data-(?:y|top|row)\s*=\s*(['\"])(-?\d+(?:\.\d+)?)\1", chunk, re.I)
    if mdx or mdy:
        try: probes.append((float(mdx.group(2)) if mdx else None, float(mdy.group(2)) if mdy else None, 'data-attrs'))
        except Exception: pass
    # Pick first full x+y, otherwise first partial.
    for x,y,src in probes:
        if x is not None and y is not None:
            return x,y,src
    if probes:
        x,y,src=probes[0]; return x,y,src
    return None, None, ''


def _slug_to_id_candidates_v275(slug: str) -> list[str]:
    s = unquote(str(slug or '').strip('/'))
    s = s.split('/')[-1]
    return [s, s.replace('-', '_'), s.replace('_','-')]


def _extract_dom_candidates_v275(html_text: str, nation: str, cls: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    ids = {row_identifier(r) for r in rows if row_identifier(r)}
    resolver = build_name_resolver(rows)
    bucket = resolver.get((nation, cls, 'names'), {})
    candidates=[]
    seen_occ=set()
    # Raw HTML may contain /unit/foo, https://.../unit/foo, or escaped links.
    for m in re.finditer(r"(?:href|to)\s*=\s*(['\"])(?:https?://[^'\"]+)?/unit/([^'\"?#]+)(?:[?#][^'\"]*)?\1|/unit/([A-Za-z0-9_\-.]+)", html_text, re.I):
        slug = m.group(2) or m.group(3) or ''
        if not slug: continue
        if (slug, m.start()) in seen_occ: continue
        seen_occ.add((slug, m.start()))
        vid=None
        for cand in _slug_to_id_candidates_v275(slug):
            if cand in ids:
                vid=cand; break
        if not vid:
            # Fallback: derive visible anchor text and resolve by display name.
            after = html_text[m.end():m.end()+800]
            mt = re.search(r">\s*([^<>]{1,80})\s*</a", after, re.I|re.S)
            if mt:
                nm = _clean_layout_name_v272(_strip_html_lines(mt.group(1))[0] if _strip_html_lines(mt.group(1)) else mt.group(1))
                vid = _resolve_tree_name(nm, bucket)
        if not vid: continue
        chunk = html_text[max(0,m.start()-2200):min(len(html_text),m.end()+2200)]
        x,y,coord_src = _extract_coord_from_chunk_v275(chunk)
        # Slot / group icon near the card.
        img = ''
        mi = re.search(r"https://static\.encyclopedia\.warthunder\.com/slots/[^'\"()\s<>]+?\.png", chunk, re.I)
        if mi: img = mi.group(0)
        candidates.append({'id':vid,'slug':slug,'x':x,'y':y,'coordSource':coord_src,'icon':img,'offset':m.start()})
    # Deduplicate: keep occurrence with geometry, then first occurrence.
    best={}
    for c in candidates:
        cur=best.get(c['id'])
        score=(1 if c.get('x') is not None and c.get('y') is not None else 0, -c.get('offset',0))
        if not cur:
            best[c['id']]=c
        else:
            cur_score=(1 if cur.get('x') is not None and cur.get('y') is not None else 0, -cur.get('offset',0))
            if score>cur_score: best[c['id']]=c
    items=list(best.values())
    full=[c for c in items if c.get('x') is not None and c.get('y') is not None]
    return {'items':items,'fullGeometry':full,'count':len(items),'fullGeometryCount':len(full),'examples':items[:25]}


def _cluster_index_v275(values: list[float], value: float, tolerance: float=35.0) -> int:
    centers=[]
    for v in sorted(values):
        if not centers or abs(v-centers[-1])>tolerance:
            centers.append(v)
        else:
            centers[-1]=(centers[-1]+v)/2
    if not centers: return 0
    return min(range(len(centers)), key=lambda i: abs(value-centers[i]))


def _build_safe_layout_from_geometry_v275(cands: list[dict[str, Any]], rows: list[dict[str, Any]], nation: str, cls: str) -> dict[str, Any] | None:
    id_to_row={row_identifier(r):r for r in rows if row_identifier(r)}
    real=[c for c in cands if c.get('x') is not None and c.get('y') is not None and c.get('id') in id_to_row]
    if len(real) < 20:
        return None
    # Split sides mostly by known placement; x is still used for column order.
    ranks={r:{'leftCols':[[] for _ in range(5)], 'right':[ ]} for r in RANK_ROMAN_V272}
    by_rank={r:[] for r in RANK_ROMAN_V272}
    for c in real:
        row=id_to_row[c['id']]
        rank=str(row.get('treeRank') or row.get('rank') or '').upper()
        if rank not in by_rank: continue
        side='right' if _side_for_vid_v272(c['id'], id_to_row, TREE_PLACEMENT_MAP if isinstance(TREE_PLACEMENT_MAP,dict) else {})=='right' else 'left'
        cc=dict(c); cc['side']=side; by_rank[rank].append(cc)
    placed=0
    for rank, arr in by_rank.items():
        left=sorted([c for c in arr if c['side']=='left'], key=lambda c:(float(c['y']), float(c['x'])))
        right=sorted([c for c in arr if c['side']=='right'], key=lambda c:(float(c['y']), float(c['x'])))
        xs=[float(c['x']) for c in left]
        # Use detected x clusters as true columns. If the Wiki page is responsive, cluster count may vary; cap to 5.
        for c in left:
            col=min(4,_cluster_index_v275(xs,float(c['x']),45.0))
            ranks[rank]['leftCols'][col].append({'type':'unit','id':c['id'],'x':c['x'],'y':c['y']})
            placed+=1
        for c in right:
            ranks[rank]['right'].append({'type':'unit','id':c['id'],'x':c['x'],'y':c['y']})
            placed+=1
    if placed < 20: return None
    return {'ranks':ranks,'placedUnits':placed}


def build_wiki_tree_layout_v275(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """First real parser probe: read raw HTML and only render if actual DOM/CSS coordinates are found.
    It deliberately refuses to fall back to stripped text, because that caused fake trees in v2.72.
    """
    out={'logic_version':'v2.75-dom-geometry-probe','updated_at':now_iso(),'items':{},'status':{}}
    probe={'logic_version':'v2.75-dom-geometry-probe','updated_at':now_iso(),'items':{}}
    raw_dir = DATA_DIR / 'wiki_tree_raw'
    try: raw_dir.mkdir(parents=True, exist_ok=True)
    except Exception: pass
    nation='USA'
    for cls,page in TREE_PAGE.items():
        key=f'{nation}|{cls}'
        html_text, reason=_fetch_wiki_tree_html_v272(page,nation)
        raw_path=raw_dir / f'{nation}_{cls}_{page}.html'
        if html_text:
            try: raw_path.write_text(html_text, encoding='utf-8')
            except Exception: pass
        if not html_text:
            out['items'][key]={'ranks':{},'safe_dom_geometry':False,'error':'fetch failed'}
            out['status'][key]={'ok':False,'reason':reason,'updated_at':now_iso()}
            continue
        parsed=_extract_dom_candidates_v275(html_text,nation,cls,rows)
        probe['items'][key]=parsed
        safe=_build_safe_layout_from_geometry_v275(parsed['items'],rows,nation,cls)
        if safe:
            out['items'][key]={
                'source':reason,
                'parser':'v2.75 DOM/CSS geometry parser',
                'safe_dom_geometry':True,
                'ranks':safe['ranks']
            }
            out['status'][key]={'ok':True,'safe_dom_geometry':True,'placedUnits':safe['placedUnits'],'candidates':parsed['count'],'fullGeometry':parsed['fullGeometryCount'],'reason':reason,'raw_html':str(raw_path).replace(str(DATA_DIR)+str(os.sep),'data/'),'updated_at':now_iso()}
        else:
            out['items'][key]={
                'source':reason,
                'parser':'v2.75 DOM/CSS geometry probe; geometry not found, not safe for rendering',
                'safe_dom_geometry':False,
                'unsafe_no_geometry':True,
                'ranks':{}
            }
            out['status'][key]={'ok':False,'safe_dom_geometry':False,'candidates':parsed['count'],'fullGeometry':parsed['fullGeometryCount'],'reason':'No reliable x/y DOM geometry found; raw HTML and probe saved','raw_html':str(raw_path).replace(str(DATA_DIR)+str(os.sep),'data/'),'updated_at':now_iso()}
    try:
        TECH_TREE_LAYOUT_JSON.write_text(json.dumps({'logic_version':'v2.75-dom-geometry-probe','updated_at':now_iso(),'items':out['items']}, ensure_ascii=False, indent=2), encoding='utf-8')
        TECH_TREE_LAYOUT_STATUS_JSON.write_text(json.dumps(out['status'], ensure_ascii=False, indent=2), encoding='utf-8')
        TECH_TREE_LAYOUT_PROBE_JSON.write_text(json.dumps(probe, ensure_ascii=False, indent=2), encoding='utf-8')
        print('Wiki DOM geometry probe for USA classes:', ', '.join(f"{k}=candidates {v.get('candidates',0)} geom {v.get('fullGeometry',0)} safe {v.get('safe_dom_geometry',False)}" for k,v in out['status'].items()))
        print('v2.75: tech_tree_layout is used only when safe_dom_geometry=true; probe saved to data/tech_tree_layout_probe.json')
    except Exception as e:
        print('WARNING: could not write v2.75 Wiki DOM geometry files:', e)
    return out

# Override v2.73 diagnostic builder with the safer DOM probe.
def build_wiki_tree_layout_v272(rows: list[dict[str, Any]]) -> dict[str, Any]:  # v2.75 override name used by parse_wiki_tree_order wrapper
    return build_wiki_tree_layout_v275(rows)
# --- end v2.75 layer ---



# --- v2.76: real Wiki table DOM parser (rank/table/tr/td/group aware) ---
def _find_balanced_div_end_v276(text: str, start: int) -> int:
    depth = 0
    pos = start
    token_re = re.compile(r"</?div\b[^>]*>", re.I|re.S)
    for m in token_re.finditer(text, start):
        tok = m.group(0)
        if tok.startswith('</'):
            depth -= 1
            if depth == 0:
                return m.end()
        else:
            if not tok.rstrip().endswith('/>'):
                depth += 1
    return len(text)


def _extract_balanced_div_v276(text: str, start: int) -> tuple[str, int]:
    end = _find_balanced_div_end_v276(text, start)
    return text[start:end], end


def _strip_tags_v276(x: str) -> str:
    x = html.unescape(str(x or ''))
    x = re.sub(r"<[^>]+>", " ", x)
    x = re.sub(r"\s+", " ", x).strip()
    return x


def _attrs_v276(tag: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(r"([a-zA-Z0-9_:\-]+)\s*=\s*(['\"])(.*?)\2", tag, re.S):
        out[m.group(1).lower()] = html.unescape(m.group(3))
    return out


def _first_icon_v276(chunk: str) -> str:
    m = re.search(r"https://static\.encyclopedia\.warthunder\.com/slots/[^'\"()\s<>]+?\.png", html.unescape(chunk), re.I)
    return m.group(0) if m else ''


def _first_text_v276(chunk: str) -> str:
    m = re.search(r"<div[^>]+class\s*=\s*['\"][^'\"]*wt-tree_item-text[^'\"]*['\"][^>]*>\s*<span[^>]*>(.*?)</span>", chunk, re.I|re.S)
    if not m:
        m = re.search(r"<span[^>]*>(.*?)</span>", chunk, re.I|re.S)
    return _strip_tags_v276(m.group(1) if m else '')


def _iter_html_cells_v276(row_html: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"<td\b[^>]*>(.*?)</td>", row_html, re.I|re.S)]


def _iter_html_rows_v276(table_html: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", table_html, re.I|re.S)]


def _node_from_cell_v276(cell: str, id_lookup: dict[str, str]) -> dict[str, Any] | None:
    # Group/folder node, including real group icon and expanded child units.
    mg = re.search(r"<div\b([^>]*class\s*=\s*['\"][^'\"]*wt-tree_group[^'\"]*['\"][^>]*)>", cell, re.I|re.S)
    if mg:
        attrs = _attrs_v276(mg.group(1))
        gid = attrs.get('data-unit-id','')
        req_raw = attrs.get('data-unit-req','')
        req = id_lookup.get(req_raw.lower(), req_raw)
        label = ''
        # Prefer label only from the folder header, before group-items.
        before_items = cell.split('wt-tree_group-items',1)[0]
        label = _first_text_v276(before_items) or gid.replace('_group','').replace('-', ' ')
        icon = _first_icon_v276(before_items) or _first_icon_v276(cell)
        ids = []
        member_icons = {}
        for mi in re.finditer(r"<div\b([^>]*class\s*=\s*['\"][^'\"]*wt-tree_item\b[^'\"]*['\"][^>]*)>", cell, re.I|re.S):
            a = _attrs_v276(mi.group(1))
            vid = a.get('data-unit-id','')
            canon = id_lookup.get(vid.lower())
            if canon:
                try:
                    item_block, _ = _extract_balanced_div_v276(cell, mi.start())
                    ico = _first_icon_v276(item_block)
                    if ico:
                        member_icons[canon] = ico
                except Exception:
                    pass
            if canon and canon not in ids:
                ids.append(canon)
        if ids:
            return {'type':'group','id':gid,'label':label,'name':label,'ids':ids,'icon':icon,'memberIcons':member_icons,'req':req}
        return None
    # Ordinary unit card.
    mi = re.search(r"<div\b([^>]*class\s*=\s*['\"][^'\"]*wt-tree_item\b[^'\"]*['\"][^>]*)>", cell, re.I|re.S)
    if mi:
        attrs = _attrs_v276(mi.group(1))
        vid = attrs.get('data-unit-id','')
        canon = id_lookup.get(vid.lower())
        if canon:
            req_raw = attrs.get('data-unit-req','')
            req = id_lookup.get(req_raw.lower(), req_raw)
            return {'type':'unit','id':canon,'req':req, 'icon':_first_icon_v276(cell), 'label':_first_text_v276(cell)}
    return None


def _extract_tree_instance_v276(html_text: str, nation_code: str='usa') -> str:
    m = re.search(rf"<div\b[^>]*class\s*=\s*['\"][^'\"]*unit-tree[^'\"]*['\"][^>]*data-tree-id\s*=\s*['\"]{re.escape(nation_code.lower())}['\"][^>]*>", html_text, re.I|re.S)
    if not m:
        return html_text
    chunk, _ = _extract_balanced_div_v276(html_text, m.start())
    return chunk


def _parse_wiki_table_layout_v276(html_text: str, nation: str, cls: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    id_lookup = {row_identifier(r).lower(): row_identifier(r) for r in rows if row_identifier(r)}
    tree = _extract_tree_instance_v276(html_text, nation.lower())
    ranks = {r:{'leftCols':[[] for _ in range(5)], 'right':[]} for r in RANK_ROMAN_V272}
    rank_order = RANK_ROMAN_V272[:]
    current_rank_idx = -1
    placed = 0
    groups = 0
    # Walk rank headers and following rank blocks in source order.
    header_or_rank = re.compile(r"<div\b[^>]*class\s*=\s*['\"][^'\"]*(wt-tree_r-header|wt-tree_rank)[^'\"]*['\"][^>]*>", re.I|re.S)
    pos = 0
    while True:
        mh = header_or_rank.search(tree, pos)
        if not mh:
            break
        tag = mh.group(0)
        cls_token = mh.group(1)
        block, end = _extract_balanced_div_v276(tree, mh.start())
        pos = end
        if cls_token == 'wt-tree_r-header':
            mlab = re.search(r"<span[^>]*>\s*([IVX]+)\s*</span>\s*ранг", block, re.I)
            if mlab:
                rank = mlab.group(1).upper()
                if rank in rank_order:
                    current_rank_idx = rank_order.index(rank)
            continue
        if cls_token != 'wt-tree_rank' or current_rank_idx < 0:
            continue
        rank = rank_order[current_rank_idx]
        # Rank block contains two direct grid-column wrappers. Match each wrapper by grid-column.
        for side_re, side in [(r"grid-column\s*:\s*1\s*/\s*3", 'left'), (r"grid-column\s*:\s*4\s*/\s*6", 'right')]:
            ms = re.search(rf"<div\b[^>]*style\s*=\s*['\"][^'\"]*{side_re}[^'\"]*['\"][^>]*>", block, re.I|re.S)
            if not ms:
                continue
            side_block, _ = _extract_balanced_div_v276(block, ms.start())
            rows_html = _iter_html_rows_v276(side_block)
            for row_i, row_html in enumerate(rows_html):
                cells = _iter_html_cells_v276(row_html)
                for col_i, cell in enumerate(cells):
                    node = _node_from_cell_v276(cell, id_lookup)
                    if not node:
                        continue
                    node['row'] = row_i
                    node['col'] = col_i
                    if node.get('type') == 'group':
                        groups += 1
                    if side == 'left':
                        col = min(4, col_i)
                        ranks[rank]['leftCols'][col].append(node)
                    else:
                        ranks[rank]['right'].append(node)
                    placed += len(node.get('ids') or [node.get('id')])
        current_rank_idx += 1
    return {'ranks':ranks, 'placedUnits':placed, 'groups':groups}


def build_wiki_tree_layout_v276(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out={'logic_version':'v2.83-wiki-table-parser-all-nations','updated_at':now_iso(),'items':{},'status':{}}
    raw_dir = DATA_DIR / 'wiki_tree_raw'
    try: raw_dir.mkdir(parents=True, exist_ok=True)
    except Exception: pass
    total_ok = 0
    total_seen = 0
    for nation in NATION_ORDER:
        for cls,page in TREE_PAGE.items():
            key=f'{nation}|{cls}'
            total_seen += 1
            html_text, reason = _fetch_wiki_tree_html_v272(page,nation)
            raw_path = raw_dir / f'{nation}_{cls}_{page}.html'
            if html_text:
                try: raw_path.write_text(html_text, encoding='utf-8')
                except Exception: pass
            if not html_text:
                out['items'][key]={'ranks':{},'safe_dom_geometry':False,'safe_table_layout':False,'error':'fetch failed'}
                out['status'][key]={'ok':False,'safe_dom_geometry':False,'safe_table_layout':False,'placedUnits':0,'groups':0,'reason':reason,'updated_at':now_iso()}
                continue
            parsed = _parse_wiki_table_layout_v276(html_text,nation,cls,rows)
            ok = parsed['placedUnits'] >= 1
            if ok: total_ok += 1
            out['items'][key]={
                'source':reason,
                'parser':'v2.83 Wiki table DOM parser: all nations/classes; wt-tree_rank/table/tr/td/group + canonical id lookup',
                'safe_dom_geometry':ok,
                'safe_table_layout':ok,
                'ranks':parsed['ranks'] if ok else {}
            }
            out['status'][key]={
                'ok':ok,
                'safe_dom_geometry':ok,
                'safe_table_layout':ok,
                'placedUnits':parsed['placedUnits'],
                'groups':parsed['groups'],
                'reason':reason,
                'raw_html':str(raw_path).replace(str(DATA_DIR)+str(os.sep),'data/'),
                'updated_at':now_iso()
            }
    try:
        TECH_TREE_LAYOUT_JSON.write_text(json.dumps({'logic_version':'v2.83-wiki-table-parser-all-nations','updated_at':now_iso(),'items':out['items']}, ensure_ascii=False, indent=2), encoding='utf-8')
        TECH_TREE_LAYOUT_STATUS_JSON.write_text(json.dumps(out['status'], ensure_ascii=False, indent=2), encoding='utf-8')
        sample=', '.join(f"{k}={v.get('placedUnits',0)}u/{v.get('groups',0)}g" for k,v in list(out['status'].items())[:10])
        print(f'Wiki table layout parsed for all nations/classes: {total_ok}/{total_seen} safe trees. Sample: {sample}')
    except Exception as e:
        print('WARNING: could not write v2.83 Wiki table layout files:', e)
    return out

# Override previous geometry probe with real table parser.
def build_wiki_tree_layout_v272(rows: list[dict[str, Any]]) -> dict[str, Any]:
    # v2.77: keep this alias because parse_wiki_tree_order() was wrapped earlier.
    # It must call the table DOM parser, not the old flat-text diagnostic parser.
    return build_wiki_tree_layout_v276(rows)
# --- end v2.76/v2.77/v2.78 layer ---


# --- v2.84: all-nation tree cleanup guards ---
# Items below are present in API-like data but are not normal selectable roster vehicles:
# aircraft carried by ships, old event/tutorial placeholders, submarines, WWI event vehicles,
# and duplicate event variants already represented by real tree units.
HIDDEN_EXACT_IDS_V284 = {
    'fokker_d7','zeppelin','germ_a7v','germ_a7v_event','germ_beutepanzer_mk_iv','germ_beutepanzer_mk_iv_event',
    'germ_garford_putilov','germ_garford_putilov_event','germ_sub_type_7','mig-21_bis_event',
    'kor_1','kor_2','spad_13','fairey_3f_mk3b','osprey_mk4','walrus_mk1','uk_garford_putilov_event',
    'ussr_garford_putilov','uk_mark_v','uk_mark_v_event','uk_saint_chamond_event','aichi_e13a','e7k2','e8n2',
    'pe-2-359_china','cn_bt_5','cn_type_95_ha_go','ro_43','re_2000_ga_ep','gl_832','loire_130',
}
# Keep these if/when they are correctly present in the Wiki tree. The user reported them as unplaced/internal only.
# a6m5_zero_china is kept; re_2000_ga_ep is hidden as an unplaced/internal oddity per user test.
_old_is_hidden_internal_v284 = _is_hidden_internal_v259
def _is_hidden_internal_v259(row: dict[str, Any]) -> bool:  # v2.84 override
    rid = norm_id(row_identifier(row)).replace('-', '_').lower()
    if rid in {x.replace('-', '_').lower() for x in HIDDEN_EXACT_IDS_V284}:
        return True
    return _old_is_hidden_internal_v284(row)

_old_class_guard_v284 = _class_guard_v259
def _class_guard_v259(row: dict[str, Any]) -> str | None:  # v2.84 override
    rid = norm_id(row_identifier(row)).replace('-', '_').lower()
    # Ram I id contains "cruiser" in the identifier, but it is a ground medium tank, not bluewater fleet.
    if rid == 'uk_cruiser_ram_1':
        return 'ground'
    return _old_class_guard_v284(row)

_old_normalize_for_ui_v284 = normalize_for_ui
def normalize_for_ui(row: dict[str, Any], image_map: dict[str, str]) -> dict[str, Any] | None:  # v2.84 override
    if _is_hidden_internal_v259(row):
        return None
    item = _old_normalize_for_ui_v284(row, image_map)
    if not item:
        return item
    vid = norm_id(str(item.get('id') or row_identifier(row))).replace('-', '_').lower()
    if vid == 'uk_cruiser_ram_1':
        item['class'] = 'ground'; item['className'] = CLASS_NAMES.get('ground','ground')
        item['role'] = item.get('role') or 'СТ'; item['roleEn'] = item.get('roleEn') or 'Medium tank'
    return item

_old_build_wiki_tree_layout_v284 = build_wiki_tree_layout_v276
def build_wiki_tree_layout_v276(rows: list[dict[str, Any]]) -> dict[str, Any]:  # v2.84 wrapper
    out = _old_build_wiki_tree_layout_v284(rows)
    try:
        # Update file version labels only; parser geometry is unchanged from the stable table DOM parser.
        p = json.loads(TECH_TREE_LAYOUT_JSON.read_text(encoding='utf-8'))
        p['logic_version'] = 'v2.84-wiki-table-parser-all-nations-cleanup'
        TECH_TREE_LAYOUT_JSON.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass
    return out

# --- end v2.84 layer ---


# --- v2.85: stronger cleanup after all earlier wrappers ---
HIDDEN_EXACT_IDS_V285 = {x.replace('-', '_').lower() for x in {'re_2000_ga_ep'}}
_old_is_hidden_internal_v285 = _is_hidden_internal_v259
def _is_hidden_internal_v259(row: dict[str, Any]) -> bool:  # v2.85 override
    rid = norm_id(row_identifier(row)).replace('-', '_').lower()
    if rid in HIDDEN_EXACT_IDS_V285:
        return True
    return _old_is_hidden_internal_v285(row)

_old_normalize_for_ui_v285 = normalize_for_ui
def normalize_for_ui(row: dict[str, Any], image_map: dict[str, str]) -> dict[str, Any] | None:  # v2.85 override
    rid = norm_id(row_identifier(row)).replace('-', '_').lower()
    if rid in HIDDEN_EXACT_IDS_V285:
        return None
    item = _old_normalize_for_ui_v285(row, image_map)
    if not item:
        return item
    vid = norm_id(str(item.get('id') or row_identifier(row))).replace('-', '_').lower()
    if vid == 'uk_cruiser_ram_1':
        item['class'] = 'ground'
        item['className'] = CLASS_NAMES.get('ground', 'ground')
        item['role'] = item.get('role') or 'СТ'
        item['roleEn'] = item.get('roleEn') or 'Medium tank'
        item['search'] = re.sub(r'\bbluewater\b|Большой флот', 'ground', str(item.get('search') or ''), flags=re.I)
    return item
# --- end v2.85 layer ---



# --- v2.86: final cleanup wrapper for Ram I and hidden oddities ---
def _force_v286_vehicle_cleanup_item(item: dict[str, Any]) -> dict[str, Any]:
    vid = norm_id(str(item.get('id') or item.get('identifier') or '')).replace('-', '_').lower()
    if vid == 'uk_cruiser_ram_1':
        item['class'] = 'ground'
        item['className'] = CLASS_NAMES.get('ground', 'ground')
        item['role'] = 'СТ'
        item['roleEn'] = 'Medium tank'
        item['treePlacement'] = 'left'
        item['treePlacementSource'] = 'v2.86 ram-ground-guard'
        item['search'] = re.sub(r'\bbluewater\b|Большой флот|Bluewater fleet', 'ground Наземка Ground', str(item.get('search') or ''), flags=re.I)
    return item

_old_write_lightweight_vehicles_v286 = write_lightweight_vehicles
def write_lightweight_vehicles(rows: list[dict[str, Any]], image_map: dict[str, str]) -> None:  # v2.86 override
    _old_write_lightweight_vehicles_v286(rows, image_map)
    try:
        p = DATA_DIR / 'vehicles.json'
        data = json.loads(p.read_text(encoding='utf-8'))
        changed = False
        for item in data:
            before = json.dumps(item, ensure_ascii=False, sort_keys=True)
            _force_v286_vehicle_cleanup_item(item)
            after = json.dumps(item, ensure_ascii=False, sort_keys=True)
            changed = changed or (before != after)
        if changed:
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        print('WARNING: v2.86 post-write cleanup failed:', e)

_old_build_wiki_tree_layout_v286 = build_wiki_tree_layout_v276
def build_wiki_tree_layout_v276(rows: list[dict[str, Any]]) -> dict[str, Any]:  # v2.86 wrapper
    out = _old_build_wiki_tree_layout_v286(rows)
    try:
        p = json.loads(TECH_TREE_LAYOUT_JSON.read_text(encoding='utf-8'))
        p['logic_version'] = 'v2.86-i18n-lineup-image-ram-cleanup'
        TECH_TREE_LAYOUT_JSON.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass
    return out
# --- end v2.86 layer ---


# --- v2.87: English display-name Cyrillic guard ---
_CYR_MAP_V287 = {
    'А':'A','а':'a','Б':'B','б':'b','В':'V','в':'v','Г':'G','г':'g','Д':'D','д':'d','Е':'E','е':'e','Ё':'Yo','ё':'yo',
    'Ж':'Zh','ж':'zh','З':'Z','з':'z','И':'I','и':'i','Й':'Y','й':'y','К':'K','к':'k','Л':'L','л':'l','М':'M','м':'m',
    'Н':'N','н':'n','О':'O','о':'o','П':'P','п':'p','Р':'R','р':'r','С':'S','с':'s','Т':'T','т':'t','У':'U','у':'u',
    'Ф':'F','ф':'f','Х':'Kh','х':'kh','Ц':'Ts','ц':'ts','Ч':'Ch','ч':'ch','Ш':'Sh','ш':'sh','Щ':'Shch','щ':'shch',
    'Ъ':'','ъ':'','Ы':'Y','ы':'y','Ь':'','ь':'','Э':'E','э':'e','Ю':'Yu','ю':'yu','Я':'Ya','я':'ya'
}
def _latinize_cyrillic_name_v287(name: str, vid: str = '') -> str:
    s = str(name or '')
    repl = [
        ('МиГ','MiG'),('миг','MiG'),('ЛаГГ','LaGG'),('лагг','LaGG'),('Як','Yak'),('як','Yak'),('Су','Su'),('су','Su'),
        ('Ил','Il'),('ил','Il'),('Ту','Tu'),('ту','Tu'),('Ла','La'),('ла','La'),('Пе','Pe'),('пе','Pe'),('СБ','SB'),
        ('БТ','BT'),('ИС-','IS-'),('Т-','T-'),('ЗиС','ZiS'),('ЗиЛ','ZiL'),('ГАЗ','GAZ'),('БТР','BTR'),('Объект','Object')
    ]
    for a,b in repl:
        s = s.replace(a,b)
    s = ''.join(_CYR_MAP_V287.get(ch, ch) for ch in s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s or pretty_name_from_id(vid)

_old_build_display_name_map_v287 = build_display_name_map
def build_display_name_map(rows: list[dict[str, Any]], workers: int = 12, retry_missing: bool = False, fetch_wiki: bool = True) -> dict[str, str]:
    out = _old_build_display_name_map_v287(rows, workers=workers, retry_missing=retry_missing, fetch_wiki=fetch_wiki)
    try:
        changed = 0
        for vid, name in list(DISPLAY_NAME_MAP_EN.items()):
            if re.search(r'[А-Яа-яЁё]', str(name or '')):
                DISPLAY_NAME_MAP_EN[vid] = _latinize_cyrillic_name_v287(name, vid)
                changed += 1
        if changed:
            DISPLAY_NAMES_EN_JSON.write_text(json.dumps(DISPLAY_NAME_MAP_EN, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f"v2.87 English display-name Cyrillic guard: latinized {changed} names.")
    except Exception as e:
        print('WARNING: v2.87 English name guard failed:', e)
    return out
# --- end v2.87 layer ---


# --- v2.88/v2.90: stronger English-name guard and grouped unit icon export ---
def _pretty_en_from_id_v288(vid: str) -> str:
    s = str(vid or '').lower()
    s = re.sub(r'^(ussr|germ|germany|us|uk|jp|jpn|cn|china|it|italy|fr|france|sw|sweden|israel)_', '', s)
    s = s.replace('_', ' ')
    fixes = [
        (r'\bil\s*(\d)', r'Il-\1'), (r'\byak\s*(\d)', r'Yak-\1'), (r'\bmig\s*(\d)', r'MiG-\1'),
        (r'\bsu\s*(\d)', r'Su-\1'), (r'\btu\s*(\d)', r'Tu-\1'), (r'\bpe\s*(\d)', r'Pe-\1'),
        (r'\blagg\s*3', r'LaGG-3'), (r'\bla\s*(\d)', r'La-\1'), (r'\bi\s*(\d)', r'I-\1'),
    ]
    for a,b in fixes:
        s = re.sub(a,b,s)
    out = []
    for w in s.split():
        if re.match(r'^(Il|Yak|MiG|Su|Tu|Pe|LaGG|La|I)-', w, re.I):
            out.append(re.sub(r'^[a-z]+', lambda m: m.group(0)[0].upper()+m.group(0)[1:], w))
        elif w.isupper():
            out.append(w)
        else:
            out.append(w[:1].upper()+w[1:])
    return ' '.join(out).strip() or pretty_name_from_id(vid)

def _english_name_guard_v288(name: str, vid: str) -> str:
    s = _latinize_cyrillic_name_v287(name, vid) if re.search(r'[А-Яа-яЁё]', str(name or '')) else str(name or '')
    s = re.sub(r'^[\s▁▂▃▄▅▆▇█▀]+', '', s).strip()
    if re.match(r'^\d', s or '') or len(s) < 2:
        return _pretty_en_from_id_v288(vid)
    # If transliteration lost the aircraft designator, prefer the canonical id-derived name.
    low_id = str(vid or '').lower()
    if any(x in low_id for x in ('il_2','yak_','mig_','lagg','pe_','db_','sb_','i_15','i_16')) and not re.search(r'\b(Il|Yak|MiG|LaGG|Pe|DB|SB|I)-?\d', s, re.I):
        return _pretty_en_from_id_v288(vid)
    return s

_old_build_display_name_map_v288 = build_display_name_map
def build_display_name_map(rows: list[dict[str, Any]], workers: int = 12, retry_missing: bool = False, fetch_wiki: bool = True) -> dict[str, str]:
    out = _old_build_display_name_map_v288(rows, workers=workers, retry_missing=retry_missing, fetch_wiki=fetch_wiki)
    try:
        changed = 0
        for vid, name in list(DISPLAY_NAME_MAP_EN.items()):
            fixed = _english_name_guard_v288(name, vid)
            if fixed != name:
                DISPLAY_NAME_MAP_EN[vid] = fixed
                changed += 1
        if changed:
            DISPLAY_NAMES_EN_JSON.write_text(json.dumps(DISPLAY_NAME_MAP_EN, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f"v2.88 English display-name guard: fixed {changed} names.")
    except Exception as e:
        print('WARNING: v2.88 English name guard failed:', e)
    return out
# --- end v2.88 layer ---


# --- v2.90: repair English labels that lost Soviet/Italian designators like Il-2 -> "2" ---
def _english_name_guard_v289(name: str, vid: str) -> str:
    s = _english_name_guard_v288(name, vid)
    vid_l = str(vid or '').lower()
    # Some upstream/wiki title cleanups may strip the alphabetic prefix (e.g. Il-2 -> "2 1941").
    if re.match(r'^\s*\d', str(s or '')) and re.match(r'^(il|i|yak|mig|su|tu|pe|lagg|la|db|sb)[_-]', vid_l):
        return _pretty_en_from_id_v288(vid)
    if re.search(r'[А-Яа-яЁё]', str(s or '')):
        return _latinize_cyrillic_name_v287(s, vid)
    return s

_old_build_display_name_map_v289 = build_display_name_map
def build_display_name_map(rows: list[dict[str, Any]], workers: int = 12, retry_missing: bool = False, fetch_wiki: bool = True) -> dict[str, str]:
    out = _old_build_display_name_map_v289(rows, workers=workers, retry_missing=retry_missing, fetch_wiki=fetch_wiki)
    try:
        changed = 0
        for vid, name in list(DISPLAY_NAME_MAP_EN.items()):
            fixed = _english_name_guard_v289(name, vid)
            if fixed != name:
                DISPLAY_NAME_MAP_EN[vid] = fixed
                changed += 1
        if changed:
            DISPLAY_NAMES_EN_JSON.write_text(json.dumps(DISPLAY_NAME_MAP_EN, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f"v2.90 English display-name guard: fixed {changed} names.")
    except Exception as e:
        print('WARNING: v2.90 English name guard failed:', e)
    return out
# --- end v2.90 layer ---


# --- v2.90: English names must prefer EN Wiki title / id, not transliteration of RU names ---
def _english_name_guard_v290(name: str, vid: str) -> str:
    raw = str(name or '').strip()
    # If an English field still contains Cyrillic, treat it as a failed EN lookup/cache entry.
    # Prefer deterministic id-derived English label over transliteration.
    if re.search(r'[А-Яа-яЁё]', raw):
        return _pretty_en_from_id_v288(vid) or _latinize_cyrillic_name_v287(raw, vid)
    fixed = _english_name_guard_v289(raw, vid)
    if re.match(r'^\s*\d', str(fixed or '')):
        return _pretty_en_from_id_v288(vid) or fixed
    return fixed

_old_build_display_name_map_v290 = build_display_name_map
def build_display_name_map(rows: list[dict[str, Any]], workers: int = 12, retry_missing: bool = False, fetch_wiki: bool = True) -> dict[str, str]:
    out = _old_build_display_name_map_v290(rows, workers=workers, retry_missing=retry_missing, fetch_wiki=fetch_wiki)
    try:
        changed = 0
        for vid, name in list(DISPLAY_NAME_MAP_EN.items()):
            fixed = _english_name_guard_v290(name, vid)
            if fixed != name:
                DISPLAY_NAME_MAP_EN[vid] = fixed
                changed += 1
        if changed:
            DISPLAY_NAMES_EN_JSON.write_text(json.dumps(DISPLAY_NAME_MAP_EN, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f"v2.90 English display-name guard: id-fixed {changed} names.")
    except Exception as e:
        print('WARNING: v2.90 English name guard failed:', e)
    return out
# --- end v2.90 layer ---

# --- v3.00: patch snapshots and lineup-relevant diff history ---
PATCH_HISTORY_JSON = DATA_DIR / "patch_history.json"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"

def _json_load_v300(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default

def _json_write_v300(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

def _safe_version_slug_v300(version: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(version or "").strip())
    return s.strip("._-") or datetime.now(timezone.utc).strftime("snapshot_%Y_%m_%d")

def _detect_game_version_v300() -> str:
    # Manual override is intentionally first: War Thunder public APIs do not always expose a stable client version.
    for p in (DATA_DIR / "game_version.txt", ROOT / "game_version.txt"):
        try:
            if p.exists():
                s = p.read_text(encoding="utf-8").strip()
                if s:
                    return s
        except Exception:
            pass
    env = os.environ.get("WT_GAME_VERSION") or os.environ.get("WAR_THUNDER_VERSION")
    if env:
        return env.strip()
    # Fallback is still useful: it creates deterministic daily snapshots and keeps history functional.
    return "unknown-" + datetime.now(timezone.utc).strftime("%Y-%m-%d")

def _vehicle_by_id_v300(items: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(items, list):
        return {}
    out = {}
    for it in items:
        if isinstance(it, dict):
            vid = str(it.get("id") or it.get("identifier") or "")
            if vid:
                out[vid] = it
    return out

def _br_map_v300(v: dict[str, Any]) -> dict[str, Any]:
    b = v.get("br", {})
    if isinstance(b, dict):
        return {k: b.get(k) for k in ("ab", "rb", "sb") if b.get(k) not in (None, "")}
    return {"ab": b} if b not in (None, "") else {}

def _layout_index_v300(layout: Any) -> dict[str, dict[str, Any]]:
    data = layout.get("items") if isinstance(layout, dict) and isinstance(layout.get("items"), dict) else layout
    out: dict[str, dict[str, Any]] = {}
    def walk(node: Any, ctx: dict[str, Any]) -> None:
        if isinstance(node, dict):
            nid = node.get("id") or node.get("unit_id") or node.get("identifier") or node.get("vehicle_id")
            if nid:
                cur = dict(ctx)
                for src, dst in (("side","side"),("treeSide","side"),("rank","tree_rank"),("treeRank","tree_rank"),("row","row"),("column","column"),("col","column"),("folder_id","folder_id"),("folderId","folder_id"),("group_id","folder_id"),("groupId","folder_id"),("nation","nation"),("class","class")):
                    if src in node and node.get(src) not in (None, ""):
                        cur[dst] = node.get(src)
                out[str(nid)] = cur
            next_ctx = dict(ctx)
            for src, dst in (("side","side"),("treeSide","side"),("rank","tree_rank"),("treeRank","tree_rank"),("row","row"),("column","column"),("col","column"),("folder_id","folder_id"),("folderId","folder_id"),("group_id","folder_id"),("groupId","folder_id"),("nation","nation"),("class","class")):
                if src in node and node.get(src) not in (None, ""):
                    next_ctx[dst] = node.get(src)
            for k in ("children","items","vehicles","units","nodes","entries"):
                if isinstance(node.get(k), (list, dict)):
                    walk(node.get(k), next_ctx)
        elif isinstance(node, list):
            for x in node:
                walk(x, ctx)
    walk(data, {})
    return out

def _neq_v300(a: Any, b: Any) -> bool:
    return json.dumps(a, ensure_ascii=False, sort_keys=True) != json.dumps(b, ensure_ascii=False, sort_keys=True)

def _make_change_v300(vid: str, typ: str, old: Any, new: Any) -> dict[str, Any]:
    return {"unit_id": vid, "change_type": typ, "old": old, "new": new, "affected_user": False, "owned": False, "lineups": []}

def _diff_snapshots_v300(old_vehicles: Any, new_vehicles: Any, old_layout: Any, new_layout: Any) -> list[dict[str, Any]]:
    oldv = _vehicle_by_id_v300(old_vehicles)
    newv = _vehicle_by_id_v300(new_vehicles)
    oldl = _layout_index_v300(old_layout)
    newl = _layout_index_v300(new_layout)
    out: list[dict[str, Any]] = []
    for vid in sorted(set(oldv) | set(newv)):
        if vid not in oldv:
            out.append(_make_change_v300(vid, "added", "", "present"))
            continue
        if vid not in newv:
            out.append(_make_change_v300(vid, "removed", "present", "missing"))
            continue
        a, b = oldv[vid], newv[vid]
        field_map = [("rank","rank"),("class","class"),("role","role"),("status","status"),("nation","nation")]
        for fld, typ in field_map:
            if _neq_v300(a.get(fld), b.get(fld)):
                out.append(_make_change_v300(vid, typ, a.get(fld), b.get(fld)))
        if _neq_v300(_br_map_v300(a), _br_map_v300(b)):
            out.append(_make_change_v300(vid, "br", _br_map_v300(a), _br_map_v300(b)))
        la, lb = oldl.get(vid, {}), newl.get(vid, {})
        for fld, typ in (("side","tree_side"),("folder_id","folder"),):
            if _neq_v300(la.get(fld), lb.get(fld)):
                out.append(_make_change_v300(vid, typ, la.get(fld), lb.get(fld)))
        posa = {k: la.get(k) for k in ("tree_rank","row","column") if la.get(k) not in (None, "")}
        posb = {k: lb.get(k) for k in ("tree_rank","row","column") if lb.get(k) not in (None, "")}
        if _neq_v300(posa, posb):
            out.append(_make_change_v300(vid, "tree_position", posa, posb))
    return out

def _latest_snapshot_dir_v300(exclude_slug: str) -> Path | None:
    if not SNAPSHOTS_DIR.exists():
        return None
    candidates = [p for p in SNAPSHOTS_DIR.iterdir() if p.is_dir() and p.name != exclude_slug and (p / "vehicles_snapshot.json").exists()]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]

def update_patch_history_v300() -> None:
    vehicles_path = DATA_DIR / "vehicles.json"
    layout_path = DATA_DIR / "tech_tree_layout.json"
    if not vehicles_path.exists():
        print("v3.00 patch history: vehicles.json is missing, snapshot skipped.")
        return
    version = _detect_game_version_v300()
    slug = _safe_version_slug_v300(version)
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    snap_dir = SNAPSHOTS_DIR / slug
    old_dir = _latest_snapshot_dir_v300(slug)
    vehicles_now = _json_load_v300(vehicles_path, [])
    layout_now = _json_load_v300(layout_path, {})
    vehicle_changes: list[dict[str, Any]] = []
    if old_dir:
        old_vehicles = _json_load_v300(old_dir / "vehicles_snapshot.json", [])
        old_layout = _json_load_v300(old_dir / "tech_tree_layout_snapshot.json", {})
        vehicle_changes = _diff_snapshots_v300(old_vehicles, vehicles_now, old_layout, layout_now)
    snap_dir.mkdir(parents=True, exist_ok=True)
    _json_write_v300(snap_dir / "vehicles_snapshot.json", vehicles_now)
    _json_write_v300(snap_dir / "tech_tree_layout_snapshot.json", layout_now)
    history = _json_load_v300(PATCH_HISTORY_JSON, {"current_version": "", "patches": []})
    if not isinstance(history, dict):
        history = {"current_version": "", "patches": []}
    patches = [p for p in history.get("patches", []) if isinstance(p, dict) and str(p.get("version")) != version]
    note_file = DATA_DIR / "patch_notes" / (slug + ".json")
    note = _json_load_v300(note_file, {}) if note_file.exists() else {}
    title = note.get("title") or ("Update " + version)
    notes = note.get("text") or note.get("notes") or ""
    source_url = note.get("source_url") or ""
    if old_dir or vehicle_changes or not patches:
        patches.insert(0, {
            "version": version,
            "date": note.get("date") or datetime.now(timezone.utc).date().isoformat(),
            "title": title,
            "source_url": source_url,
            "notes": notes,
            "vehicle_changes": vehicle_changes,
        })
    history["current_version"] = version
    history["patches"] = patches[:3]
    _json_write_v300(PATCH_HISTORY_JSON, history)
    print(f"v3.00 patch history: current={version}; changes={len(vehicle_changes)}; stored patches={len(history['patches'])}.")


# --- v3.00 network hardening: adaptive API page size for fragile routes ---
# Some ISP routes/VPN paths accept the HTTPS request and headers but stall while
# transferring the large JSON body. A HEAD/curl -I check can still look healthy.
# Keep the normal fast path first, then gracefully fall back to smaller pages.
API_READ_TIMEOUT_V300 = float(os.environ.get("WT_RM_API_READ_TIMEOUT", "30"))
API_CONNECT_TIMEOUT_V300 = float(os.environ.get("WT_RM_API_CONNECT_TIMEOUT", "10"))
API_PAGE_DELAY_V300 = float(os.environ.get("WT_RM_API_PAGE_DELAY", "0.08"))


def _api_limit_candidates_v300() -> list[int]:
    raw = os.environ.get("WT_RM_API_LIMITS", "200,100,50,25,10,5")
    out: list[int] = []
    for part in raw.split(','):
        try:
            n = int(part.strip())
        except Exception:
            continue
        if 1 <= n <= 500 and n not in out:
            out.append(n)
    return out or [200, 100, 50, 25, 10, 5]


def _get_api_json_v300(params: dict[str, Any], attempt: int = 1) -> Any:
    # Use a JSON Accept header for the API endpoint, without touching the wiki/image
    # defaults used elsewhere in this updater.
    headers = {"Accept": "application/json", "User-Agent": "WT-Roster-Manager/3.00 API-Updater"}
    r = get_session().get(
        API_BASE,
        params=params,
        timeout=(API_CONNECT_TIMEOUT_V300, API_READ_TIMEOUT_V300),
        headers=headers,
    )
    r.raise_for_status()
    clen = r.headers.get("Content-Length")
    if clen and clen.isdigit() and len(r.content) != int(clen):
        raise RuntimeError(f"incomplete API response: got {len(r.content)} of {clen} bytes")
    try:
        return r.json()
    except Exception as e:
        # This is usually what happens after a truncated body: headers are 200 OK,
        # but JSON cannot be parsed because the array was cut in the middle.
        raise RuntimeError(f"invalid/truncated API JSON for params={params}: {e}") from e


def _fetch_mode_v300_network(mode: str, limit: int, first_page_retries: int = 3) -> tuple[list[dict[str, Any]], bool]:
    all_rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i in range(0, 10000):
        params = {"limit": limit, mode: i if mode == "page" else i * limit}
        payload = None
        for attempt in range(1, first_page_retries + 1):
            try:
                payload = _get_api_json_v300(params, attempt=attempt)
                break
            except Exception as e:
                where = "first request" if i == 0 else f"page {i}"
                if attempt < first_page_retries:
                    print(f"  {mode} limit={limit} {where} failed, retry {attempt}/{first_page_retries - 1}: {e}")
                    time.sleep(0.8 * attempt)
                    continue
                print(f"  {mode} limit={limit} stopped at {where}: {e}")
                return all_rows, False
        items = extract_items(payload)
        if not items:
            return all_rows, True
        added_this_page = 0
        for row in items:
            vid = row_identifier(row)
            if not vid or vid in seen_ids:
                continue
            seen_ids.add(vid)
            all_rows.append(row)
            added_this_page += 1
        if i < 5 or i % 10 == 0 or len(items) < limit:
            print(f"  {mode} limit={limit} page {i}: {len(items)} rows, {added_this_page} new, total {len(all_rows)}")
        if added_this_page == 0:
            print(f"  {mode} limit={limit}: no new vehicles; treating this mode as exhausted.")
            return all_rows, True
        if len(items) < limit:
            return all_rows, True
        if API_PAGE_DELAY_V300 > 0:
            time.sleep(API_PAGE_DELAY_V300)
    return all_rows, True


def _usable_cached_rows_v300_network() -> list[dict[str, Any]] | None:
    for cached_path in (DATA_DIR / "vehicles_api_raw.json", DATA_DIR / "vehicles.json"):
        try:
            if not cached_path.exists():
                continue
            cached_rows = json.loads(cached_path.read_text(encoding="utf-8"))
            if isinstance(cached_rows, list) and len(cached_rows) >= MIN_REASONABLE_VEHICLES_V230:
                print("WARNING: WT Vehicles API is unavailable or incomplete; using existing local cache:", cached_path)
                print(f"  local cache vehicles: {len(cached_rows)}")
                return cached_rows
        except Exception as e:
            print(f"WARNING: could not use local cache {cached_path}: {e}")
    return None


def _add_supplemental_wiki_units_v300_network(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Preserve the v2.63 supplement layer when replacing the network fetcher.
    try:
        supplements = SUPPLEMENTAL_WIKI_UNITS_V263
    except NameError:
        supplements = []
    seen = {row_identifier(r) for r in rows if row_identifier(r)}
    added = 0
    for r in supplements:
        vid = r.get("identifier")
        if vid and vid not in seen:
            rows.append(dict(r))
            seen.add(vid)
            added += 1
    if added:
        print(f"v3.00 network layer: Wiki-only supplements added: {added} vehicles not present in WT Vehicles API.")
    return rows


def fetch_all_vehicles() -> list[dict[str, Any]]:  # v3.00 network hardening override
    print("Downloading vehicles from WT Vehicles API...")
    print("  network strategy: adaptive page size", _api_limit_candidates_v300())
    candidates: list[tuple[str, int, list[dict[str, Any]], bool]] = []
    for mode in ("page", "offset"):
        for limit in _api_limit_candidates_v300():
            rows, complete = _fetch_mode_v300_network(mode, limit)
            if rows:
                candidates.append((mode, limit, rows, complete))
                state = "complete" if complete else "partial"
                print(f"  {mode} limit={limit} candidate: {len(rows)} unique vehicles ({state}).")
            if complete and len(rows) >= FULL_ROSTER_EXPECTED_HINT_V230:
                print(f"Downloaded {len(rows)} vehicles using {mode} pagination, limit={limit}.")
                return _add_supplemental_wiki_units_v300_network(rows)
            # If a full-looking first attempt succeeds, no need to try smaller limits.
            if complete and len(rows) >= MIN_REASONABLE_VEHICLES_V230:
                break
    good = [(m, l, r, c) for (m, l, r, c) in candidates if c and len(r) >= MIN_REASONABLE_VEHICLES_V230]
    if good:
        mode, limit, rows, complete = max(good, key=lambda x: len(x[2]))
        print(f"Best API candidate: {mode} pagination, limit={limit}, {len(rows)} unique vehicles.")
        return _add_supplemental_wiki_units_v300_network(rows)
    cached = _usable_cached_rows_v300_network()
    if cached is not None:
        return cached
    best = max(candidates, key=lambda x: len(x[2]), default=None)
    if best:
        mode, limit, rows, complete = best
        raise RuntimeError(
            "Could not download a complete vehicle roster from API. "
            f"Best partial result: {len(rows)} vehicles via {mode} limit={limit}. "
            "This usually means the route receives headers but stalls/truncates the JSON body. "
            "Try VPN/mobile internet or set WT_RM_API_LIMITS=10,5 before running update_from_api.bat."
        )
    raise RuntimeError(
        "Could not download vehicles from API; no pagination mode returned data and no usable local cache was found. "
        "Try VPN/mobile internet, copy a previous data/ folder, or set WT_RM_API_LIMITS=10,5."
    )
# --- end v3.00 network hardening layer ---



# --- v3.40: availability/status normalization and diff layer ---
AVAILABILITY_OVERRIDES_V336 = {
    "yp-38": ("unavailable", "Hidden pack vehicle; normally unavailable"),
    "pbm_3": ("unavailable", "Sea Voyage 2019 event reward"),
    "p-38g_metal": ("unavailable", "Hidden/owner-only premium vehicle"),
    "p-40e_td": ("unavailable", "Twitch Drops reward"),
    "f4u-4b_vmf_214": ("unavailable", "Xbox exclusive premium pack"),
    "us_m5a1_stuart_canadian_5st_arm": ("unavailable", "Temporary sale / normally hidden"),
    "us_elco_80ft_pt109_boat": ("unavailable", "Discontinued naval pack"),
    "germ_s_100_s204_lang": ("unavailable", "Discontinued preorder fleet pack"),
    "germ_destroyer_class1936_z20_karlgalster": ("unavailable", "Temporary German Navy Day sale / normally hidden"),
    "germ_destroyer_class1939_t31": ("unavailable", "Temporary German Navy Day sale / normally hidden"),
    "germ_cruiser_prinz_eugen": ("unavailable", "Temporary anniversary sale / normally hidden"),
    "fiat_cr42_marcolin": ("unavailable", "German tree legacy owner-only vehicle; Italian version remains available"),
    "he_112b_1": ("unavailable", "German version removed from sale; Italian version remains available"),
    "sm_79_1937": ("unavailable", "German tree legacy vehicle removed due redundancy"),
    "sm_79_1942": ("unavailable", "German tree legacy vehicle not shown in current client folder"),
    "hs-129b-2_romania": ("unavailable", "German tree version no longer obtainable; Italian version remains available"),
    "bf-109g-2_romania": ("unavailable", "German tree Romanian premium no longer obtainable"),
    "tempest_mkv_luftwaffe": ("unavailable", "Temporary sale / normally hidden"),
    "us_lvt_a_4": ("market_available", "Victory Day event reward; obtainable via Market when coupons are listed"),
    "germ_frigate_koln_lubeck": ("market_available", "Strategist event reward; obtainable via Market when coupons are listed"),
    "p-43a-1": ("market_available", "Event vehicle obtainable via Market when coupons are listed"),
    "ussr_kv_8": ("market_available", "Legend of Victory 2026 event reward; obtainable via Market when coupons are listed"),
    "sw_sav_fm48": ("unavailable", "Limited-time sale premium; normally owner-only"),
    "f_20a": ("unavailable", "Pack retired after May Sale 2026; owner-only unless already owned"),
}
STATUS_OVERRIDES_V336 = {
    "us_lvt_a_4": "Акционная",
    "ussr_kv_8": "Акционная",
}
MARKET_OVERRIDES_V336 = {"us_lvt_a_4", "germ_frigate_koln_lubeck", "p-43a-1", "ussr_kv_8"}
GERMAN_FOREIGN_PREFIX_V336 = {"fiat_cr42_marcolin", "he_112b_1", "sm_79_1937", "sm_79_1942", "hs-129b-2_romania", "bf-109g-2_romania", "tempest_mkv_luftwaffe"}

# v3.39 extra owner-only / market availability corrections
AVAILABILITY_OVERRIDES_V336.update({
    "germ_amd_35_kwk": ("unavailable", "Old hidden premium / owner-only"),
    "germ_sdkfz_251_22": ("unavailable", "Old hidden premium / owner-only"),
    "us_m4a2_1944_germ": ("unavailable", "Captured German Sherman; normally hidden / owner-only"),
    "germ_pzkpfw_V_ersatz_m10": ("unavailable", "Festive Quest 2017 event reward / owner-only"),
    "germ_sturmmorser_sturmtiger": ("unavailable", "10th Anniversary Dreams come true event reward / owner-only"),
    "germ_flakpanzer_V_Coelian": ("unavailable", "Hidden from research after German tree changes"),
    "germ_pzkpfw_VI_ausf_b_tiger_IIh_sla": ("unavailable", "Normally hidden / temporary-sale premium"),
    "germ_panther_II": ("unavailable", "Hidden from research after German tree changes"),
    "germ_pzkpfw_VI_ausf_b_tiger_IIh_kwk46": ("unavailable", "Hidden from research after German tree changes"),
    "germ_pzkpfw_Maus": ("unavailable", "Hidden from normal research; may return temporarily"),
    "bo_105cb2": ("unavailable", "Normally hidden / temporary-sale premium"),
    "uh_1c_xm_30": ("unavailable", "Normally hidden / temporary-sale premium"),
    "ah_64a_peten": ("unavailable", "Old USA pack; normally unavailable"),
    "tb_3_m17_32": ("unavailable", "Old gift/event vehicle / owner-only"),
    "tandem_mai": ("unavailable", "Old gift/event vehicle / owner-only"),
    "p-39q_15": ("unavailable", "Hidden USSR premium / owner-only"),
    "pe-2-205": ("unavailable", "Hidden USSR premium / owner-only"),
    "tu-2_early": ("unavailable", "Hidden USSR premium / owner-only"),
    "p-63c-5_ussr": ("unavailable", "Hidden USSR premium / owner-only"),
    "p-63a-10_ussr": ("unavailable", "Hidden USSR premium / owner-only"),
    "fw-190d-9_ussr": ("unavailable", "Hidden captured USSR premium / owner-only"),
    "spitfire_ix_ussr": ("unavailable", "Hidden captured USSR premium / owner-only"),
    "yak-3t": ("unavailable", "Hidden USSR premium / owner-only"),
    "tu-1": ("unavailable", "Hidden USSR premium / owner-only"),
    "su-7bmk": ("unavailable", "Normally hidden / temporary-sale premium"),
    "sb_2m_103u": ("unavailable", "Legacy USSR research variant absent from current tree"),
    "sb_2m_103u_mv3": ("unavailable", "Legacy USSR research variant absent from current tree"),
    "sb_2m_103_mv3": ("unavailable", "Legacy USSR research variant absent from current tree"),
    "er-2_m105_tat": ("unavailable", "Legacy USSR research variant absent from current tree"),
    "er-2_m105r_tat": ("unavailable", "Legacy USSR research variant absent from current tree"),
    "germ_spz_12_3": ("market_available", "Battle Pass vehicle obtainable via Market when coupons are listed"),
})
STATUS_OVERRIDES_V336.update({"germ_spz_12_3": "Маркет", "germ_frigate_koln_lubeck": "Маркет"})
MARKET_OVERRIDES_V336.update({"germ_spz_12_3", "germ_frigate_koln_lubeck", "us_lvt_a_4", "p-43a-1"})
# v3.39: do not inject the raw Unicode ▀ into saved names; the UI draws a CSS badge instead.
GERMAN_FOREIGN_PREFIX_V336.clear()

def _alias_id_v336(vid: str) -> list[str]:
    s = str(vid or "").lower()
    return list(dict.fromkeys([s, s.replace("-", "_"), s.replace("_", "-"), s.replace("-", "_").replace("_", "")]))

def _in_alias_v336(vid: str, coll) -> bool:
    return any(a in coll for a in _alias_id_v336(vid))

def _get_alias_v336(vid: str, mapping):
    for a in _alias_id_v336(vid):
        if a in mapping:
            return mapping[a]
    return None

def _load_availability_file_v336() -> dict[str, Any]:
    p = DATA_DIR / "availability_overrides.json"
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data.get("items", data)
    except Exception:
        pass
    return {}

def _status_is_event_like_v336(st: str) -> bool:
    return str(st or "").strip().lower() in {"акционная", "event", "gift", "twitch drops", "twitch drop", "battle pass", "battlepass"}

def _apply_availability_to_vehicle_v336(item: dict[str, Any], file_items: dict[str, Any]) -> dict[str, Any]:
    vid = str(item.get("id") or item.get("identifier") or "").lower()
    status_override = _get_alias_v336(vid, STATUS_OVERRIDES_V336)
    if status_override:
        item["status"] = status_override
    if _in_alias_v336(vid, GERMAN_FOREIGN_PREFIX_V336):
        for k in ("name", "nameRu", "nameEn"):
            if item.get(k) and not str(item[k]).startswith("▀"):
                item[k] = "▀" + str(item[k])
    rec = None
    for a in _alias_id_v336(vid):
        if a in file_items:
            rec = file_items[a]
            break
    val = ""
    note = ""
    if isinstance(rec, str):
        val = rec
    elif isinstance(rec, dict):
        val = str(rec.get("availability") or rec.get("status") or "")
        note = str(rec.get("en") or rec.get("ru") or rec.get("reason") or rec.get("availability_reason") or "")
    forced = _get_alias_v336(vid, AVAILABILITY_OVERRIDES_V336)
    if forced:
        val, default_note = forced
        note = note or default_note
    is_market = _in_alias_v336(vid, MARKET_OVERRIDES_V336) or str(item.get("status") or "") == "Маркет"
    if str(val).lower() in {"market", "market_available"}:
        is_market = True
    if is_market:
        item["market_available"] = True
    if str(val).lower() in {"unavailable", "hidden", "owned_only", "owner_only", "not_obtainable", "removed_from_sale"}:
        item["availability"] = "unavailable"
    elif is_market:
        item["availability"] = "available"
    elif _status_is_event_like_v336(str(item.get("status") or "")):
        item["availability"] = "unavailable"
        note = note or "v3.40 rule: Event/Gift/Twitch/Battle Pass vehicle without Market is owner-only by default."
    else:
        item["availability"] = item.get("availability") or "available"
    if note:
        item["availability_reason"] = note
    return item

_old_write_lightweight_vehicles_v336 = write_lightweight_vehicles
def write_lightweight_vehicles(rows: list[dict[str, Any]], image_map: dict[str, str]) -> None:  # v3.38 override
    _old_write_lightweight_vehicles_v336(rows, image_map)
    try:
        p = DATA_DIR / "vehicles.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        file_items = _load_availability_file_v336()
        changed = False
        for item in data:
            before = json.dumps(item, ensure_ascii=False, sort_keys=True)
            _apply_availability_to_vehicle_v336(item, file_items)
            after = json.dumps(item, ensure_ascii=False, sort_keys=True)
            changed = changed or before != after
        if changed:
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print("v3.38 availability normalization applied to vehicles.json")
    except Exception as e:
        print("WARNING: v3.38 availability normalization failed:", e)

_old_diff_snapshots_v300_v336 = _diff_snapshots_v300
def _diff_snapshots_v300(old_vehicles: Any, new_vehicles: Any, old_layout: Any, new_layout: Any) -> list[dict[str, Any]]:  # v3.38 override
    out = _old_diff_snapshots_v300_v336(old_vehicles, new_vehicles, old_layout, new_layout)
    oldv = _vehicle_by_id_v300(old_vehicles)
    newv = _vehicle_by_id_v300(new_vehicles)
    for vid in sorted(set(oldv) & set(newv)):
        a, b = oldv[vid], newv[vid]
        if _neq_v300(a.get("availability"), b.get("availability")):
            out.append(_make_change_v300(vid, "availability", a.get("availability"), b.get("availability")))
        if _neq_v300(a.get("market_available"), b.get("market_available")):
            out.append(_make_change_v300(vid, "market_status", a.get("market_available"), b.get("market_available")))
        if _neq_v300(a.get("availability_reason"), b.get("availability_reason")):
            out.append(_make_change_v300(vid, "availability", a.get("availability_reason"), b.get("availability_reason")))
    return out
# --- v3.45 availability and prefix-rule refinements ---
AVAILABILITY_OVERRIDES_V336.update({
    "germ_flakpanzer_v_coelian": ("unavailable", "Hidden from research after German tree changes"),
    "germ_panther_ii": ("unavailable", "Hidden from research after German tree changes"),
    "germ_pzkpfw_vi_ausf_b_tiger_iih_kwk46": ("unavailable", "Hidden from research after German tree changes"),
    "germ_pzkpfw_maus": ("unavailable", "Hidden from normal research; may return temporarily"),
    "arado-196a-3": ("unavailable", "Operation S.U.M.M.E.R. event reward / owner-only by default"),
    "sea_hawk_mk100": ("unavailable", "Removed/hidden premium; temporary sale by events"),
    "mig-21_sps_k": ("unavailable", "Removed/hidden premium; temporary anniversary sale"),
    "su_22m4_de_wtd61": ("unavailable", "Pack/platform-limited availability; absent from current PC client"),
    "ussr_g5_rct": ("unavailable", "Legacy USSR premium/event unit absent from current tree"),
    "ussr_ya_5m": ("unavailable", "Legacy USSR premium/event unit absent from current tree"),
    "ussr_1124_rct": ("unavailable", "Legacy USSR premium/event unit absent from current tree"),
    "ussr_pr_183_egypt_bm_21": ("unavailable", "Legacy USSR premium/event unit absent from current tree"),
    "ussr_destroyer_7y_stroyny": ("unavailable", "Normally hidden / temporary-sale premium"),
    "ussr_destroyer_pr41_neustrashimy": ("unavailable", "Normally hidden / temporary-sale premium"),
    "lagg-3-4": ("unavailable", "Legacy USSR hidden owner-only aircraft"),
    "lagg-3-23": ("unavailable", "Legacy USSR hidden owner-only aircraft"),
    "mig-17_cuba": ("unavailable", "Normally hidden / temporary-sale premium"),
    "ussr_t_34_1940_l_11": ("unavailable", "Hidden/owner-only premium"),
    "ussr_bm_31_12": ("unavailable", "Old event/owner-only vehicle"),
    "ussr_kv_7_u13": ("unavailable", "Old event/owner-only vehicle"),
    "ussr_type_65_aa": ("unavailable", "Hidden/owner-only USSR vehicle"),
    "ussr_t_34_85e": ("unavailable", "Normally hidden / temporary-sale premium"),
    "ussr_su_122p": ("unavailable", "Normally hidden / temporary-sale premium"),
    "ussr_is_2_1944_revenge": ("unavailable", "Hidden/owner-only premium"),
    "ussr_t_80u_yt_cup_2019": ("unavailable", "YouTube Cup 2019 prize / owner-only"),
    "ussr_t_34_85_zis_53_v80": ("market_available", "Legend of Victory 2025 event vehicle; coupon can be tradable on Market"),
})
STATUS_OVERRIDES_V336.update({"arado-196a-3": "Акционная", "ussr_t_34_85_zis_53_v80": "Акционная"})
MARKET_OVERRIDES_V336.update({"ussr_t_34_85_zis_53_v80"})

# --- v3.47 expanded availability audit from current-client checks ---
AVAILABILITY_OVERRIDES_V336.update({
    'a6m5ko': ("unavailable", 'Hidden/old Japanese premium absent from current client tree.'),
    'a_129_a': ("unavailable", 'Hidden/old Italian premium absent from current client tree.'),
    'attaker_fb2': ("unavailable", 'Hidden/old British premium absent from current client tree.'),
    'boomerang_mkii': ("unavailable", 'Hidden/old British premium absent from current client tree.'),
    'buccaneer_s1': ("unavailable", 'Event/Market aircraft absent from current client tree; only if owned/market-acquired.'),
    'cn_t_62': ("unavailable", 'Hidden/old Chinese premium absent from current client tree.'),
    'cn_type_69_2a': ("unavailable", 'Hidden/old Chinese premium absent from current client tree.'),
    'd_371_hs9': ("unavailable", 'Hidden/old French pack/event aircraft absent from current client tree.'),
    'd_520': ("unavailable", 'Hidden/old British premium/event aircraft absent from current client tree.'),
    'd_521': ("unavailable", 'Legacy British event aircraft absent from current client tree.'),
    'f-84f_iaf': ("unavailable", 'Hidden/old French/Israeli premium aircraft absent from current client tree.'),
    'fr_destroyer_le_fantasque_class_le_triomphant': ("unavailable", 'Hidden/old French naval pack absent from current client tree.'),
    'fr_lorraine_155': ("unavailable", 'Hidden/old French pack/event vehicle absent from current client tree.'),
    'fr_lvt_bofors': ("unavailable", 'Hidden/old French pack/event vehicle absent from current client tree.'),
    'fr_panhard_ebr_1954': ("unavailable", 'Hidden/old French pack/event vehicle absent from current client tree.'),
    'fr_trident_class_glaive_p671': ("unavailable", '2026 Maritime Gendarme event reward; hide by default outside ownership/event context.'),
    'fw-190a-5_japan': ("unavailable", 'Hidden/old Japanese premium absent from current client tree.'),
    'fw-190a-8_france': ("unavailable", 'Hidden/old French premium aircraft absent from current client tree.'),
    'gladiator_mk2_france': ("unavailable", 'Legacy British event/hidden aircraft absent from current client tree.'),
    'gladiator_mk2_silver': ("unavailable", 'Legacy British event/hidden aircraft absent from current client tree.'),
    'h-75m_china': ("unavailable", 'Hidden/old Chinese pack absent from current client tree.'),
    'hawk_209_indonesia': ("unavailable", 'Pack/platform-limited availability; absent from current PC client.'),
    'hp_12': ("unavailable", 'Legacy British aircraft absent from current client tree.'),
    'hurricane_mk1_late_ep': ("unavailable", 'Hidden/old British premium absent from current client tree.'),
    'iar_316b': ("unavailable", 'Hidden/old French premium helicopter absent from current client tree.'),
    'it_41m_turan_2': ("unavailable", 'Hidden/old Hungarian/Italian pack absent from current client tree.'),
    'it_44m_zrinyi_1': ("unavailable", 'Hidden/old Hungarian/Italian pack absent from current client tree.'),
    'it_destroyer_soldati_serie1': ("unavailable", 'Hidden/old Italian naval pack absent from current client tree.'),
    'it_destroyer_soldati_serie1_geniere': ("unavailable", 'Hidden/old Italian premium absent from current client tree.'),
    'it_mc_485': ("unavailable", 'Hidden/old Italian coastal pack absent from current client tree.'),
    'it_p493_freccia': ("unavailable", 'Hidden/old Italian coastal pack absent from current client tree.'),
    'it_semovente_m43_105_leoncello': ("unavailable", 'Hidden/old Italian premium absent from current client tree.'),
    'it_toldi_II_a': ("unavailable", 'Hidden/old Hungarian/Italian pack absent from current client tree.'),
    'j2m4_kai': ("unavailable", 'Hidden/old Japanese premium absent from current client tree.'),
    'j9_early': ("unavailable", 'Hidden/old Swedish pack aircraft absent from current client tree.'),
    'j_7d': ("unavailable", 'Pack/premium absent from current client tree; owner-only by default.'),
    'jp_destroyer_hayanami': ("unavailable", 'Hidden/old Japanese naval pack absent from current client tree.'),
    'jp_destroyer_kiyoshimo': ("unavailable", 'Hidden/old Japanese premium absent from current client tree.'),
    'jp_destroyer_suzutsuki': ("unavailable", 'Hidden/old Japanese naval pack absent from current client tree.'),
    'jp_kusen_tei_13_1942': ("unavailable", 'Hidden/old Japanese coastal pack absent from current client tree.'),
    'jp_t51a': ("unavailable", 'Hidden/old Japanese coastal pack absent from current client tree.'),
    'jp_type_3_ka_chi': ("unavailable", 'Hidden/old Japanese event/pack vehicle absent from current client tree.'),
    'ki-96': ("unavailable", 'Hidden/old Japanese premium absent from current client tree.'),
    'late_298d': ("unavailable", 'Hidden/old French pack/event aircraft absent from current client tree.'),
    'lightning_f53': ("unavailable", 'Hidden/old British premium absent from current client tree.'),
    'mb_151c1': ("unavailable", 'Hidden/old French pack/event aircraft absent from current client tree.'),
    'mc-202_d': ("unavailable", 'Hidden/old Italian pack absent from current client tree.'),
    'mig-21_bison': ("unavailable", 'Pack/premium absent from current client tree; owner-only by default.'),
    'mirage_milan': ("unavailable", 'Hidden/old French premium aircraft absent from current client tree.'),
    'ro_57_quadriarma': ("unavailable", 'Hidden/old Italian pack absent from current client tree.'),
    'saab_j29d': ("unavailable", 'Hidden/old Swedish premium aircraft absent from current client tree.'),
    'saab_j35a': ("unavailable", 'Hidden/old Swedish premium aircraft absent from current client tree.'),
    'so_4050_vautour_2a_iaf': ("unavailable", 'Hidden/old French/Israeli pack aircraft absent from current client tree.'),
    'so_4050_vautour_2n': ("unavailable", 'Hidden/old French premium aircraft absent from current client tree.'),
    'spitfire_lf_mk9c_cw_greece': ("unavailable", 'Hidden/old Israeli pack aircraft absent from current client tree.'),
    'swordfish_mk2': ("unavailable", 'Hidden/old British premium absent from current client tree.'),
    't2_early': ("unavailable", 'Hidden/old Japanese premium absent from current client tree.'),
    'typhoon_mk1b': ("unavailable", 'Hidden/old British premium absent from current client tree.'),
    'uk_ac1_sentinel': ("unavailable", 'Absent from current client tree in user audit; hidden/owner-only by default.'),
    'uk_centurion_mk_3_ss11': ("unavailable", 'Absent from current client tree in user audit; old pack/premium, owner-only by default.'),
    'uk_centurion_shot_kal_d': ("unavailable", 'Absent from current client tree in user audit; old pack/premium, owner-only by default.'),
    'uk_destroyer_battle_cadiz': ("unavailable", 'Hidden/old British naval pack absent from current client tree.'),
    'uk_destroyer_haida': ("unavailable", 'Hidden/old British premium absent from current client tree.'),
    'uk_fairmile_d_5001_5029': ("unavailable", 'Hidden/old British premium absent from current client tree.'),
    'uk_gay_class_archer': ("unavailable", 'Hidden/old British naval pack absent from current client tree.'),
    'uk_higgins_78ft_mtb422': ("unavailable", 'Hidden/old British naval pack absent from current client tree.'),
    'uk_tog_2': ("unavailable", 'Absent from current client tree in user audit; event/owner-only by default.'),
    'uk_vijayanta': ("unavailable", 'Absent from current client tree in user audit; treat as owner-only/hidden by default.'),
    'vl_myrsky_2_late': ("unavailable", 'Hidden/old Swedish/Finnish pack aircraft absent from current client tree.')
})
STATUS_OVERRIDES_V336.update({'fr_trident_class_glaive_p671': 'Акционная'})
# Buccaneer S.1 may be market/coupon obtainable, but is hidden from the regular client tree; keep market flag for labeling.
MARKET_OVERRIDES_V336.update({'buccaneer_s1'})
# --- end v3.47 expanded audit ---

# Normalize mixed-case legacy keys so aliases match ids like germ_flakpanzer_V_Coelian.
AVAILABILITY_OVERRIDES_V336.update({str(k).lower(): v for k, v in list(AVAILABILITY_OVERRIDES_V336.items())})
STATUS_OVERRIDES_V336.update({str(k).lower(): v for k, v in list(STATUS_OVERRIDES_V336.items())})
MARKET_OVERRIDES_V336.update({str(k).lower() for k in list(MARKET_OVERRIDES_V336)})
# --- end v3.45 layer ---

# --- end v3.38 layer ---


# --- v3.51: curated availability only ---
# News/store availability scanning was intentionally removed.
# Availability changes are now curated manually in data/availability_overrides.json.

_old_diff_snapshots_v350 = _diff_snapshots_v300
def _diff_snapshots_v300(old_vehicles: Any, new_vehicles: Any, old_layout: Any, new_layout: Any) -> list[dict[str, Any]]:  # v3.50 diff noise filter kept
    out = _old_diff_snapshots_v350(old_vehicles, new_vehicles, old_layout, new_layout)
    falsey = {None, '', False}
    clean=[]
    for c in out:
        typ=str(c.get('change_type') or '')
        if typ in {'status','market_status'} and c.get('old') in falsey and c.get('new') in falsey:
            continue
        if typ in {'availability_candidate','market_candidate'}:
            continue
        clean.append(c)
    return clean

_old_main_v300 = main
def main(argv: list[str] | None = None) -> int:
    rc = _old_main_v300(argv)
    if rc == 0:
        try:
            update_patch_history_v300()
        except Exception as e:
            print("WARNING: v3.00 patch history update failed:", e)
    return rc
# --- end v3.51 curated availability layer ---




# --- v3.80: Wings Over Water 2026 Wiki-only supplement ---
SUPPLEMENTAL_WIKI_UNITS_V380 = [
    {"identifier":"spitfire_mk5b_float","country":"britain","vehicle_type":"fighter","vehicle_sub_types":["hydroplane"],"era":2,"arcade_br":3.3,"realistic_br":3.3,"realistic_ground_br":3.3,"simulator_br":3.3,"simulator_ground_br":3.3,"event":"wings_over_water_2026","is_premium":1,"is_pack":0,"on_marketplace":0,"squadron_vehicle":0,"visibility":"wiki_supplement"},
]
_old_add_supplemental_wiki_units_v380 = _add_supplemental_wiki_units_v300_network
def _add_supplemental_wiki_units_v300_network(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:  # v3.80 override
    rows = _old_add_supplemental_wiki_units_v380(rows)
    seen = {row_identifier(r) for r in rows if row_identifier(r)}
    for r in SUPPLEMENTAL_WIKI_UNITS_V380:
        if r["identifier"] not in seen:
            rows.append(dict(r)); seen.add(r["identifier"])
    return rows


# --- v3.83: curated post-Heavy-Cavalry data corrections ---
# Keep these corrections durable across future API refreshes.  They are intentionally
# applied after vehicles.json is generated because the public Vehicles API and Wiki
# can lag behind official news/changelog/store availability.
BR_OVERRIDES_V383 = {
    "uk_fv107_scimitar_mk2": {"ab": 8.3, "rb": 8.3, "sb": 8.3, "ground_rb": 8.3, "ground_sb": 8.3},
    "hawk_209_indonesia": {"ab": 11.0, "rb": 10.7, "sb": 10.7, "ground_rb": 10.7, "ground_sb": 10.7},
}

AVAILABILITY_CURATED_V383 = {
    # Permanent return-to-sale vehicles
    "mig-21_sps_k": ("available", "Returned to sale after the MiG-21 first-flight event; available at full price after the sale."),
    "mig-21_bison": ("available", "Returned to sale after the MiG-21 first-flight event; available at full price after the sale."),
    "j_7d": ("available", "Returned to sale after the MiG-21 first-flight event; available at full price after the sale."),
    "su_22m4_de_wtd61": ("available", "Returned to sale after the Su-17 first-flight event; available at full price after the sale."),
    "av_8b_na": ("available", "Returned to sale for the USA 250th anniversary; remains available at full price after the sale."),

    # Retired packs / temporary-only sales
    "germ_pzh_2000": ("unavailable", "Pack removed from sale after the 2026 Summer Sale; owner-only/unavailable for new players."),
    "jh_7a_prototype": ("unavailable", "Pack removed from sale after the 2026 Summer Sale; owner-only/unavailable for new players."),
    "jp_type_74_red_star": ("unavailable", "Pack removed from sale after the 2026 Summer Sale; owner-only/unavailable for new players."),
    "us_m26e1_pershing": ("unavailable", "Temporary USA 250th anniversary sale; normally owner-only/unavailable."),
    "f-86f-35": ("unavailable", "Temporary USA 250th anniversary sale; normally owner-only/unavailable."),
    "us_xm1_chrysler": ("unavailable", "Temporary USA 250th anniversary pack; normally owner-only/unavailable."),
    "us_destroyer_fletcher_bennion": ("unavailable", "Temporary USA 250th anniversary pack; normally owner-only/unavailable."),

    # Event/Battle Pass/current limited-time vehicles: stable DB treats them as owner-only unless the user marks them owned.
    "uk_fv107_scimitar_mk2": ("unavailable", "Strike of the Scimitar event reward; owner-only/unavailable in the stable database."),
    "it_fiat_6616_ub": ("unavailable", "Battle Pass Season 24 vehicle; owner-only/unavailable in the stable database."),
    "iar_81c_db_605": ("unavailable", "Battle Pass Season 24 vehicle; owner-only/unavailable in the stable database."),

    # Known temporary sale vehicles kept owner-only/unavailable by default.
    "saab_j35a": ("unavailable", "Temporary event sale; normally hidden / owner-only."),
    "jp_destroyer_kiyoshimo": ("unavailable", "Temporary event sale; normally hidden / owner-only."),
    "j2m4_kai": ("unavailable", "Temporary event sale; normally hidden / owner-only."),
    "so_4050_vautour_2n": ("unavailable", "Temporary event sale; normally hidden / owner-only."),
    "su-7bmk": ("unavailable", "Temporary event sale; normally hidden / owner-only."),
    "cn_type_69_2a": ("unavailable", "Temporary event sale; normally hidden / owner-only."),
}

try:
    AVAILABILITY_OVERRIDES_V336.update(AVAILABILITY_CURATED_V383)
except Exception:
    pass

def _apply_v383_curated_item(item: dict[str, Any]) -> bool:
    vid = str(item.get("id") or item.get("identifier") or "")
    changed = False
    br = BR_OVERRIDES_V383.get(vid)
    if br:
        old = dict(item.get("br") or {})
        new = dict(old)
        new.update(br)
        if new != old:
            item["br"] = new
            changed = True
    av = AVAILABILITY_CURATED_V383.get(vid)
    if av:
        availability, note = av
        if item.get("availability") != availability:
            item["availability"] = availability
            changed = True
        if note and item.get("availability_reason") != note:
            item["availability_reason"] = note
            changed = True
    if vid == "uk_fv107_scimitar_mk2" and item.get("status") != "Акционная":
        item["status"] = "Акционная"
        changed = True
    return changed

_old_write_lightweight_vehicles_v383 = write_lightweight_vehicles
def write_lightweight_vehicles(rows: list[dict[str, Any]], image_map: dict[str, str]) -> None:  # v3.83 override
    _old_write_lightweight_vehicles_v383(rows, image_map)
    try:
        p = DATA_DIR / "vehicles.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        changed = 0
        for item in data:
            if isinstance(item, dict) and _apply_v383_curated_item(item):
                changed += 1
        if changed:
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"v3.83 curated data corrections applied: {changed} vehicles.")
    except Exception as e:
        print("WARNING: v3.83 curated data corrections failed:", e)

# --- end v3.83 layer ---


# --- v3.84: August 2026 BR changes + current rank moves ---
# Generated from the official Planned BR Changes — August 2026 spreadsheet export.
# Some spreadsheet notes explicitly say "next major update"; those are documented in patch_history/game_patch_status
# but intentionally not applied here until the relevant major update is live.
BR_OVERRIDES_V384 = {
  "f_14a_iriaf": {
    "ab": 13.0,
    "rb": 13.7
  },
  "mig_29smt_9_19": {
    "ab": 13.7
  },
  "su_34": {
    "ab": 13.7
  },
  "f_16a_block_20_mlu": {
    "ab": 13.7
  },
  "f_16a_block_15_adf": {
    "ab": 13.7,
    "ground_rb": 12.3
  },
  "f_16a_block_15_adf_italy": {
    "ab": 13.7,
    "ground_rb": 12.3
  },
  "saab_ja37di_f21": {
    "ab": 13.3,
    "ground_rb": 11.7
  },
  "saab_ja37di": {
    "ab": 13.3,
    "ground_rb": 11.7
  },
  "mirage_2000d_rmv": {
    "ground_rb": 13.0
  },
  "mirage_4000": {
    "ground_rb": 12.0
  },
  "cf_188a_canada": {
    "ground_rb": 12.0
  },
  "fa_18c_early": {
    "ground_rb": 12.0
  },
  "fa_18c_switzerland": {
    "ground_rb": 12.0
  },
  "mig_29n": {
    "ground_rb": 12.0
  },
  "kfir_c10_colombia": {
    "ground_rb": 12.0
  },
  "f-5th_thailand": {
    "ground_rb": 12.0
  },
  "j_8f": {
    "ground_rb": 11.7
  },
  "su_33": {
    "ground_rb": 11.7
  },
  "j_11": {
    "ground_rb": 11.7
  },
  "su_27": {
    "ground_rb": 11.7
  },
  "f_16a_block_15_ocu_belgium": {
    "ground_rb": 11.7
  },
  "f_14b": {
    "ab": 12.3
  },
  "f_14d": {
    "ab": 12.3,
    "ground_rb": 11.7
  },
  "f_15a": {
    "ground_rb": 11.7
  },
  "f_15a_iaf": {
    "ground_rb": 11.7
  },
  "f_15j": {
    "ground_rb": 11.7
  },
  "mig_29_9_13": {
    "ground_rb": 11.7
  },
  "mig_29_9_12g": {
    "ground_rb": 11.7
  },
  "f-4f_late": {
    "ab": 11.0
  },
  "mirage_2000c_s4": {
    "ground_rb": 11.3
  },
  "mirage_2000c_s5": {
    "ground_rb": 11.3
  },
  "mig_21_2000_iaf": {
    "rb": 11.7
  },
  "su_25tm": {
    "ground_rb": 11.7
  },
  "saab_ja37d": {
    "ground_rb": 11.0
  },
  "f-4ej_kai": {
    "ground_rb": 11.0
  },
  "tornado_f3": {
    "ground_rb": 11.0
  },
  "tornado_adv": {
    "ground_rb": 11.0
  },
  "kfir_c2": {
    "ground_rb": 10.7
  },
  "hawk_200_rda": {
    "ground_rb": 10.7
  },
  "a_7k": {
    "rb": 10.7
  },
  "mig_25pd": {
    "ab": 11.0
  },
  "f-4c": {
    "ab": 10.3
  },
  "j_22m1a": {
    "ground_rb": 11.0,
    "naval_rb": 9.3
  },
  "a_6e_tram": {
    "ground_rb": 11.0
  },
  "f-4jk": {
    "ground_rb": 10.3
  },
  "f-4k": {
    "ground_rb": 10.3
  },
  "f-4m_fgr2": {
    "ground_rb": 10.3
  },
  "f-4ej_adtw": {
    "ground_rb": 10.3
  },
  "f-4ej": {
    "ground_rb": 10.3
  },
  "saab_j35xs": {
    "ground_rb": 10.0
  },
  "saab_f35_wdns": {
    "ground_rb": 10.3
  },
  "f-104s": {
    "ground_rb": 10.0
  },
  "f-104s_cb": {
    "ground_rb": 10.0
  },
  "j_8b": {
    "ground_rb": 10.3
  },
  "av_8s_late_thailand": {
    "ground_rb": 10.3
  },
  "harrier_frs1": {
    "ground_rb": 10.3
  },
  "buccaneer_s2b": {
    "ground_rb": 10.7
  },
  "mig-21_mf_hungary": {
    "ground_rb": 9.7
  },
  "mig-21_mf": {
    "ground_rb": 9.7
  },
  "mig-21_smt": {
    "ground_rb": 9.7
  },
  "j_7d": {
    "ground_rb": 9.7
  },
  "f-8e": {
    "ground_rb": 9.7
  },
  "harrier_frs1_early": {
    "ground_rb": 10.0
  },
  "saab_j35d": {
    "ground_rb": 10.0
  },
  "mirage_3e": {
    "ground_rb": 9.7
  },
  "mirage_3s_c70_switzerland": {
    "ground_rb": 9.7
  },
  "nf_5a_netherlands": {
    "ground_rb": 9.7
  },
  "f-5a_thailand": {
    "ground_rb": 9.7
  },
  "f-5a_china": {
    "ground_rb": 9.7
  },
  "mirage_5ba": {
    "ground_rb": 9.7
  },
  "mirage_5f": {
    "ground_rb": 9.7
  },
  "mirage_3c": {
    "ground_rb": 9.7
  },
  "mirage_3cj": {
    "ground_rb": 9.7
  },
  "f-8e_fn": {
    "ground_rb": 9.7
  },
  "f8u-2": {
    "ground_rb": 9.7
  },
  "nesher": {
    "ground_rb": 9.7
  },
  "a_5c": {
    "ground_rb": 9.7
  },
  "su_25k": {
    "ground_rb": 9.7
  },
  "su_25": {
    "ground_rb": 9.7
  },
  "tornado_ids_de_wtd61": {
    "ground_rb": 9.7
  },
  "tornado_ids_it": {
    "ground_rb": 9.7
  },
  "tornado_ids_de_mfg": {
    "ground_rb": 9.7
  },
  "su_22m3": {
    "ground_rb": 9.7
  },
  "su_22m3_hungary": {
    "ground_rb": 9.7
  },
  "su_22um3k": {
    "ground_rb": 9.7
  },
  "f_111a": {
    "ground_rb": 9.7
  },
  "f-105d": {
    "ground_rb": 9.7
  },
  "f-104g": {
    "ground_rb": 9.7
  },
  "f-104g_italy": {
    "ground_rb": 9.7
  },
  "f-104j": {
    "ground_rb": 9.7
  },
  "f-104g_belgium": {
    "ground_rb": 9.7
  },
  "f-104g_china": {
    "ground_rb": 9.7
  },
  "f1": {
    "ground_rb": 9.7
  },
  "f-5a": {
    "ground_rb": 9.7
  },
  "f-5c": {
    "ground_rb": 9.7
  },
  "su_17m2": {
    "ground_rb": 9.3
  },
  "mig_23bn": {
    "ground_rb": 9.3
  },
  "a_4n": {
    "rb": 10.0
  },
  "hunter_f6": {
    "rb": 10.0,
    "ground_rb": 9.3
  },
  "f_6c_pakistan": {
    "ground_rb": 9.3
  },
  "saab_j35a": {
    "ground_rb": 9.3
  },
  "jaguar_gr1": {
    "ground_rb": 9.3
  },
  "t2_early": {
    "ab": 10.3,
    "ground_rb": 9.3
  },
  "t2": {
    "ab": 10.3,
    "ground_rb": 9.3
  },
  "av_8s_thailand": {
    "ground_rb": 9.3
  },
  "av_8a": {
    "ab": 9.7,
    "ground_rb": 9.3
  },
  "av_8c": {
    "ground_rb": 9.3
  },
  "harrier_gr1": {
    "ground_rb": 9.3
  },
  "harrier_gr3": {
    "ground_rb": 9.3
  },
  "f-104c": {
    "ground_rb": 9.3
  },
  "mig-21_s": {
    "ab": 9.3
  },
  "yak_130_early": {
    "ab": 9.3,
    "ground_rb": 9.7,
    "naval_rb": 9.3
  },
  "mig-21_f13": {
    "ground_rb": 9.0
  },
  "mig-21_pfm": {
    "ground_rb": 9.0
  },
  "mig-21_sps_k": {
    "ground_rb": 9.0
  },
  "j_7_mk2": {
    "ground_rb": 9.0
  },
  "mig-19j_6a": {
    "ground_rb": 9.0
  },
  "mig-19pt": {
    "ground_rb": 9.0
  },
  "mig-19s": {
    "ground_rb": 9.0
  },
  "saab_j32b": {
    "ground_rb": 9.0
  },
  "f_101c": {
    "ground_rb": 9.0
  },
  "f_106a_1972": {
    "ground_rb": 9.0
  },
  "lightning_f53": {
    "ground_rb": 9.0
  },
  "lightning_f6": {
    "ground_rb": 9.0
  },
  "f_100f_china": {
    "ground_rb": 9.0
  },
  "f-100a_china": {
    "ground_rb": 9.0
  },
  "f-100d": {
    "ground_rb": 9.0
  },
  "f-100d_france": {
    "ground_rb": 9.0
  },
  "yak-38": {
    "ground_rb": 9.0
  },
  "yak-38m": {
    "ground_rb": 9.0
  },
  "su-7b": {
    "ground_rb": 9.0
  },
  "su-7bkl": {
    "ground_rb": 9.0
  },
  "su-7bmk": {
    "ground_rb": 9.0
  },
  "q_5a": {
    "ground_rb": 9.0
  },
  "hunter_f58a_1971_switzerland": {
    "ground_rb": 9.0
  },
  "buccaneer_s2": {
    "ground_rb": 9.0
  },
  "f-104a": {
    "ground_rb": 9.0
  },
  "f-104a_china": {
    "ground_rb": 9.0
  },
  "fiat_g91_y": {
    "rb": 9.3
  },
  "fj_4b_agm_12b": {
    "ground_rb": 9.3
  },
  "f11f_1_late": {
    "ground_rb": 8.7
  },
  "hunter_f6_holland": {
    "ground_rb": 8.7
  },
  "hunter_f50_sweden": {
    "ground_rb": 8.7
  },
  "hunter_f1": {
    "ground_rb": 8.7
  },
  "f-86_cl_13b_mk6": {
    "ground_rb": 8.7
  },
  "f-86k_late_german": {
    "ground_rb": 8.7
  },
  "f-86k_late_italy": {
    "ground_rb": 8.7
  },
  "f-86k_late": {
    "ground_rb": 8.7
  },
  "saab_j29f": {
    "ground_rb": 8.7
  },
  "ffa_p16": {
    "ground_rb": 8.7
  },
  "q_5_early": {
    "ground_rb": 8.7
  },
  "md_460_sambad": {
    "ground_rb": 8.7
  },
  "md_460": {
    "ground_rb": 8.7
  },
  "md_460_saar": {
    "ground_rb": 8.7
  },
  "marut_mk1": {
    "rb": 8.7,
    "ground_rb": 8.7
  },
  "f3h-2": {
    "ground_rb": 8.7,
    "naval_rb": 9.3
  },
  "b_66b": {
    "ground_rb": 8.3,
    "naval_rb": 9.3
  },
  "b_52h": {
    "ground_rb": 8.3
  },
  "buccaneer_s1": {
    "rb": 8.3
  },
  "fiat_g91_r1": {
    "ground_rb": 8.3
  },
  "tu_95m": {
    "ground_rb": 8.0
  },
  "yak-30d": {
    "ground_rb": 8.0
  },
  "me-163b-0": {
    "ground_rb": 8.0
  },
  "so_4050_vautour_2n": {
    "rb": 8.7,
    "ground_rb": 8.0
  },
  "so_4050_vautour_2b": {
    "rb": 8.7,
    "ground_rb": 8.0
  },
  "so_4050_vautour_2n_iaf": {
    "rb": 8.7,
    "ground_rb": 8.0
  },
  "so_4050_vautour_2a_israel_iaf": {
    "rb": 8.7,
    "ground_rb": 8.0
  },
  "saab_j29d": {
    "ground_rb": 8.0
  },
  "a_4h": {
    "ground_rb": 8.0
  },
  "f-84f": {
    "ground_rb": 8.0
  },
  "f-84f_germany": {
    "ground_rb": 8.0
  },
  "f-84f_france": {
    "ground_rb": 8.0
  },
  "f-84f_italy": {
    "ground_rb": 8.0
  },
  "f-84f_israel_iaf": {
    "ground_rb": 8.0
  },
  "f-84f_iaf": {
    "ground_rb": 8.0
  },
  "so_4050_vautour_2a_iaf": {
    "rb": 8.7
  },
  "so_4050_vautour_2a": {
    "rb": 8.7
  },
  "yak-28b": {
    "ground_rb": 7.7,
    "naval_rb": 9.3
  },
  "f-86f-25": {
    "ab": 8.0
  },
  "b-57": {
    "ground_rb": 7.3,
    "naval_rb": 9.3
  },
  "b-57b": {
    "ground_rb": 7.3,
    "naval_rb": 9.3
  },
  "canberra_bmk2": {
    "ground_rb": 7.3,
    "naval_rb": 9.3
  },
  "canberra_bimk6": {
    "ground_rb": 7.3,
    "naval_rb": 9.3
  },
  "f-86f-35": {
    "ab": 7.7
  },
  "mb_326k": {
    "rb": 8.0,
    "ground_rb": 7.7
  },
  "l_39za_art_thailand": {
    "ground_rb": 7.7
  },
  "tu_4": {
    "ground_rb": 7.0
  },
  "tu_4_china": {
    "ground_rb": 7.0
  },
  "il_28_german": {
    "ground_rb": 7.0,
    "naval_rb": 9.3
  },
  "il_28_hungary": {
    "ground_rb": 7.0,
    "naval_rb": 9.3
  },
  "il_28": {
    "ground_rb": 7.0,
    "naval_rb": 9.3
  },
  "il_28_china": {
    "ground_rb": 7.0,
    "naval_rb": 9.3
  },
  "tu_14t": {
    "ground_rb": 7.0,
    "naval_rb": 9.3
  },
  "f3d_1": {
    "ground_rb": 6.7
  },
  "me-262a-1a_u4": {
    "ab": 5.3,
    "rb": 6.0,
    "ground_rb": 6.3
  },
  "yak-9ut": {
    "ground_rb": 6.7
  },
  "spitfire_ix": {
    "ground_rb": 6.3
  },
  "spitfire_ix_usa": {
    "ground_rb": 6.3
  },
  "spitfire_ix_ussr": {
    "ground_rb": 6.3
  },
  "spitfire_ix_plagis": {
    "ground_rb": 6.3
  },
  "spitfire_lf_mk9e_weisman": {
    "ground_rb": 6.3
  },
  "ju-288c": {
    "ab": 6.3,
    "rb": 6.3
  },
  "spitfire_mk18e": {
    "ground_rb": 6.0
  },
  "spitfire_lf_mk9c_cw_greece": {
    "ground_rb": 6.0
  },
  "seafire_fr47": {
    "ground_rb": 6.0
  },
  "yak-3_vk107": {
    "ground_rb": 6.0
  },
  "yak-3u": {
    "ground_rb": 6.0
  },
  "a7m2": {
    "ground_rb": 6.0
  },
  "p-59a": {
    "ground_rb": 6.0
  },
  "ki_84_otsu": {
    "ground_rb": 6.0
  },
  "a6m5ko": {
    "rb": 6.0,
    "ground_rb": 6.0
  },
  "a6m5otsu": {
    "rb": 6.0,
    "ground_rb": 6.0
  },
  "ki_87": {
    "rb": 5.3,
    "ground_rb": 5.3
  },
  "so_8000_narval": {
    "rb": 5.3,
    "ground_rb": 5.3
  },
  "douglas_ad_2": {
    "ground_rb": 5.3
  },
  "seafire_mk17": {
    "ground_rb": 5.7
  },
  "a6m6c": {
    "rb": 5.7,
    "ground_rb": 5.7
  },
  "a6m5hei": {
    "ground_rb": 5.7
  },
  "a7m1": {
    "rb": 5.7,
    "ground_rb": 5.7
  },
  "tu-1": {
    "rb": 5.0,
    "ground_rb": 5.0
  },
  "la-11_china": {
    "rb": 5.0
  },
  "la-11": {
    "rb": 5.0
  },
  "a-26b": {
    "ground_rb": 5.0
  },
  "f-82e": {
    "ground_rb": 5.0
  },
  "spitfire_mk5c_notrop": {
    "ground_rb": 5.3
  },
  "a6m3_mod22ko_zero": {
    "ground_rb": 5.3
  },
  "a6m5_zero": {
    "ground_rb": 5.3
  },
  "seafire_mk3_france": {
    "ground_rb": 5.3
  },
  "seafire_mk3": {
    "ground_rb": 5.3
  },
  "yak-3t": {
    "ground_rb": 5.3
  },
  "yak-3p": {
    "ground_rb": 5.3
  },
  "j2m5_30mm": {
    "ground_rb": 5.3
  },
  "j2m4_kai": {
    "ground_rb": 5.3
  },
  "j2m2": {
    "ground_rb": 5.3
  },
  "yak-9p_hungary": {
    "ab": 5.3,
    "ground_rb": 5.3
  },
  "yak-9p": {
    "ab": 5.3,
    "ground_rb": 5.3
  },
  "su-6_am42": {
    "ab": 4.7
  },
  "a-26b_10": {
    "ground_rb": 4.7
  },
  "wyvern_s4": {
    "rb": 5.3
  },
  "spitfire_mk9c_4cannons": {
    "ground_rb": 5.0
  },
  "yak-9u": {
    "ab": 5.0,
    "ground_rb": 5.0
  },
  "ki_61_1a_hei": {
    "ground_rb": 5.0
  },
  "ki_61_1a_hei_ep": {
    "ground_rb": 5.0
  },
  "p-38l_1_china_rocaf": {
    "ground_rb": 4.3
  },
  "p-38l": {
    "ground_rb": 4.3
  },
  "do_335a_0": {
    "ground_rb": 4.3
  },
  "su-8": {
    "rb": 4.3,
    "ground_rb": 4.3
  },
  "pyorremyrsky": {
    "ab": 5.0,
    "rb": 4.7
  },
  "ia_58a_pucara": {
    "ab": 4.7,
    "rb": 4.7
  },
  "spitfire_mk9c_iaf": {
    "rb": 4.7,
    "ground_rb": 4.7
  },
  "spitfire_ix_early": {
    "rb": 4.7,
    "ground_rb": 4.7
  },
  "a6m3_mod22_zero": {
    "rb": 4.7,
    "ground_rb": 4.7
  },
  "yak-3_eremin": {
    "ab": 4.7,
    "ground_rb": 4.7
  },
  "xp-55": {
    "ground_rb": 4.7
  },
  "bf-109g-2": {
    "ground_rb": 4.7
  },
  "bf-109g-2_romania": {
    "ground_rb": 4.7
  },
  "bf-109g-2_hungary": {
    "ground_rb": 4.7
  },
  "bf-109g-2_finland": {
    "ground_rb": 4.7
  },
  "me-410b-1_u2": {
    "ground_rb": 4.0
  },
  "do_335a_1": {
    "ground_rb": 4.0
  },
  "saab_t18b_2": {
    "ground_rb": 4.0
  },
  "saab_t18b_1": {
    "ab": 4.0,
    "ground_rb": 4.0
  },
  "su-6_m71": {
    "ab": 4.3,
    "rb": 4.0,
    "ground_rb": 4.0
  },
  "ju-388j": {
    "rb": 4.0,
    "ground_rb": 4.0
  },
  "he_219a_7": {
    "rb": 4.0,
    "ground_rb": 4.0
  },
  "tis_ma": {
    "rb": 4.0
  },
  "a6m3_zero": {
    "rb": 4.3,
    "ground_rb": 4.3
  },
  "ki_100_early": {
    "rb": 4.3,
    "ground_rb": 4.3
  },
  "itp_m1": {
    "ab": 5.0,
    "rb": 4.3,
    "ground_rb": 4.3
  },
  "su_6_single": {
    "rb": 3.7,
    "ground_rb": 3.7
  },
  "spitfire_mk5b_italy": {
    "ground_rb": 4.0
  },
  "spitfire_mk5b": {
    "ground_rb": 4.0
  },
  "b7a2_homare_23": {
    "ab": 4.0,
    "rb": 4.0
  },
  "b7a2": {
    "ab": 4.0,
    "rb": 4.0
  },
  "pe-2-110": {
    "ground_rb": 3.3
  },
  "firebrand_tf4": {
    "ground_rb": 3.3
  },
  "me-410a-1_u2": {
    "ab": 3.7,
    "ground_rb": 3.3
  },
  "mosquito_b_mk16": {
    "ground_rb": 3.3
  },
  "mosquito_fb_mk26_china": {
    "ab": 3.7,
    "ground_rb": 3.3
  },
  "mosquito_fb_mk6_ash_norway": {
    "ground_rb": 3.3
  },
  "mosquito_fb_mk6": {
    "ground_rb": 3.3
  },
  "mosquito_tr_mk33": {
    "ground_rb": 3.3
  },
  "tempest_mkv_vikkers": {
    "ab": 3.0,
    "ground_rb": 3.3
  },
  "sm_91": {
    "ground_rb": 3.3
  },
  "xa_38": {
    "ground_rb": 3.3
  },
  "bf-110g-2": {
    "ground_rb": 3.3
  },
  "ki_61_1a_ko": {
    "ground_rb": 3.7
  },
  "mb_157": {
    "ab": 3.7,
    "rb": 3.7,
    "ground_rb": 3.7
  },
  "ju-87d-5": {
    "ground_rb": 3.0
  },
  "il_2m_1943": {
    "ground_rb": 3.0
  },
  "pe-2-83": {
    "ground_rb": 3.0
  },
  "la-5_type39": {
    "ab": 3.7
  },
  "s_199": {
    "ab": 3.7
  },
  "fw-190a-1": {
    "ground_rb": 3.3
  },
  "ki_102_otsu": {
    "ab": 2.7
  },
  "sb2c_1c": {
    "ab": 2.7,
    "ground_rb": 2.7
  },
  "do_217n_1": {
    "ab": 2.7
  },
  "ju-88a-4": {
    "ab": 3.3
  },
  "ju-88a-4_finland": {
    "ab": 3.3
  },
  "bf-110f-2": {
    "ab": 3.3
  },
  "hurricanemkii_ussr": {
    "ab": 3.3
  },
  "yak-9b": {
    "ab": 3.3
  },
  "spitfiremkiia": {
    "ab": 3.0,
    "rb": 3.3,
    "ground_rb": 3.3
  },
  "spitfiremkiia_ep": {
    "ab": 3.0,
    "rb": 3.3,
    "ground_rb": 3.3
  },
  "a-35b": {
    "ab": 2.7,
    "ground_rb": 2.3
  },
  "i-153p": {
    "ground_rb": 3.0
  },
  "ki_44_1": {
    "ground_rb": 3.0
  },
  "ki_44_1_ep": {
    "ground_rb": 3.0
  },
  "iar_81c": {
    "ground_rb": 3.0
  },
  "fw_189c_0": {
    "ab": 2.7,
    "rb": 3.0,
    "ground_rb": 3.0
  },
  "b_239_finland": {
    "rb": 3.0,
    "ground_rb": 3.0
  },
  "mig_3_series_34": {
    "ab": 3.0,
    "rb": 3.0
  },
  "saab_b18a": {
    "ab": 2.3,
    "ground_rb": 2.3
  },
  "su-2_m82": {
    "ab": 2.3,
    "ground_rb": 2.3
  },
  "a-20g": {
    "ground_rb": 2.3
  },
  "p-40f-5_france_ep": {
    "ab": 2.7
  },
  "p-40f_10": {
    "ab": 2.7
  },
  "lagg-3-11": {
    "ab": 2.7
  },
  "yak_2_kabb": {
    "ab": 2.7
  },
  "i-16_type18": {
    "ground_rb": 2.7
  },
  "fokker_d23": {
    "ab": 2.7,
    "rb": 2.7,
    "ground_rb": 2.7
  },
  "wm_23": {
    "ab": 2.7,
    "rb": 2.7
  },
  "b6n2a": {
    "ground_rb": 2.0
  },
  "b6n2": {
    "ground_rb": 2.0
  },
  "mig_3_series_1_15": {
    "ab": 2.0,
    "rb": 2.0,
    "ground_rb": 2.0
  },
  "f2a-1": {
    "ground_rb": 2.3
  },
  "f2a-1_thach": {
    "ground_rb": 2.3
  },
  "j1n1_mod11_early": {
    "ab": 2.3,
    "ground_rb": 2.3
  },
  "i-153_m62_china": {
    "ab": 2.3
  },
  "i-153_m62": {
    "ab": 2.3
  },
  "i-153_m62_zhukovskiy": {
    "ab": 2.3
  },
  "sb_2m_103c": {
    "ab": 2.3
  },
  "re_2000_heja_2": {
    "ab": 2.3
  },
  "re_2000_ga": {
    "ab": 2.3
  },
  "re_2000_int": {
    "ab": 2.3
  },
  "hurricane_mk1b": {
    "ab": 2.3
  },
  "ms_410c1": {
    "ab": 2.3
  },
  "ms_406c1": {
    "ab": 2.3
  },
  "ms_405c1": {
    "ab": 2.3
  },
  "br_693_ab2": {
    "ab": 2.3,
    "rb": 2.0,
    "ground_rb": 2.0
  },
  "fc_20_bis": {
    "ab": 2.3
  },
  "ki_21_1ko": {
    "ab": 2.3
  },
  "ki_43_3_ko": {
    "ab": 2.3
  },
  "pbm_1": {
    "ab": 2.3
  },
  "bf-109c_1": {
    "ab": 2.3,
    "rb": 2.0,
    "ground_rb": 2.0
  },
  "bf-109c_1_promo": {
    "ab": 2.3,
    "rb": 2.0,
    "ground_rb": 2.0
  },
  "he_112a_0": {
    "ab": 2.0,
    "rb": 2.0,
    "ground_rb": 2.0
  },
  "su-2_tss1": {
    "ab": 2.0,
    "rb": 1.7
  },
  "ah_64d": {
    "ground_rb": 11.7
  },
  "ah_mk1": {
    "ground_rb": 11.7
  },
  "ah_64d_i_saraph": {
    "ground_rb": 11.7
  },
  "ah_64a": {
    "ground_rb": 11.3
  },
  "ahs": {
    "ground_rb": 11.3
  },
  "ah_64a_peten_iaf": {
    "ground_rb": 11.3
  },
  "ah_64a_greece_usa": {
    "ground_rb": 11.3
  },
  "ah_64d_netherlands": {
    "ground_rb": 11.3
  },
  "ah_64d_lightweight_japan": {
    "ground_rb": 11.3
  },
  "ah_64a_peten": {
    "ground_rb": 11.3
  },
  "ah_64d_japan": {
    "ground_rb": 11.3
  },
  "z_19e": {
    "ground_rb": 11.7
  },
  "z_19": {
    "ground_rb": 11.7
  },
  "hkp9a_cb3_fc": {
    "ground_rb": 9.3
  },
  "sa_342l_china": {
    "ground_rb": 9.3
  },
  "ah_1g": {
    "ground_rb": 7.7
  },
  "ah_1g_iaf": {
    "ground_rb": 7.7
  },
  "ussr_t_72b3_arena_m": {
    "ab": 12.0,
    "rb": 12.0
  },
  "ussr_zprk_2s6": {
    "rb": 11.0
  },
  "us_m1296_dragoon": {
    "ab": 9.3,
    "rb": 9.3
  },
  "ussr_9p157": {
    "ab": 9.3,
    "rb": 9.3
  },
  "us_xm1_chrysler": {
    "ab": 9.3,
    "rb": 9.3
  },
  "germ_leopard_1a5": {
    "ab": 9.0,
    "rb": 9.0
  },
  "it_leopard_1a5": {
    "ab": 9.0,
    "rb": 9.0
  },
  "sw_leopard_1a5no": {
    "ab": 9.0,
    "rb": 9.0
  },
  "fr_leopard_1a5be": {
    "ab": 9.0,
    "rb": 9.0
  },
  "us_m247": {
    "ab": 9.3,
    "rb": 9.3
  },
  "cn_pgz_09": {
    "ab": 9.3,
    "rb": 9.3
  },
  "germ_leopard_I_a1": {
    "ab": 8.7,
    "rb": 8.7
  },
  "cn_m_41d": {
    "rb": 8.3
  },
  "cn_pgz_88": {
    "ab": 8.0,
    "rb": 8.0
  },
  "ussr_2s19_m2": {
    "ab": 7.0,
    "rb": 7.0
  },
  "us_t26e5": {
    "rb": 7.0
  },
  "fr_amx_30_auf_1": {
    "ab": 6.7,
    "rb": 6.7
  },
  "ussr_2s19_m1": {
    "ab": 6.7,
    "rb": 6.7
  },
  "us_m18_super_hellcat": {
    "ab": 6.7,
    "rb": 6.7
  },
  "ussr_is_2_1944": {
    "ab": 6.3,
    "rb": 6.3
  },
  "ussr_is_2_1944_revenge": {
    "ab": 6.3,
    "rb": 6.3
  },
  "ussr_is_2_1944_321": {
    "ab": 6.3,
    "rb": 6.3
  },
  "cn_is_2_1944": {
    "ab": 6.3,
    "rb": 6.3
  },
  "ussr_object_248": {
    "ab": 6.3,
    "rb": 6.3
  },
  "germ_pzkpfw_VI_ausf_b_tiger_IIp": {
    "ab": 6.3,
    "rb": 6.3
  },
  "sw_kungstiger": {
    "ab": 6.3,
    "rb": 6.3
  },
  "uk_g6_spg": {
    "ab": 6.3,
    "rb": 6.3
  },
  "ussr_is_2_1943": {
    "ab": 6.0,
    "rb": 6.0
  },
  "cn_is_2_1943": {
    "ab": 6.0,
    "rb": 6.0
  },
  "cn_is_2_1943_no402": {
    "ab": 6.0,
    "rb": 6.0
  },
  "us_m109a1": {
    "ab": 6.0,
    "rb": 6.0
  },
  "uk_m109a1": {
    "ab": 6.0,
    "rb": 6.0
  },
  "uk_fv4005": {
    "ab": 6.0,
    "rb": 6.0
  },
  "cn_plz_83": {
    "ab": 6.0,
    "rb": 6.0
  },
  "ussr_2s3m": {
    "ab": 6.0,
    "rb": 6.0
  },
  "il_m109a1": {
    "ab": 6.0,
    "rb": 6.0
  },
  "uk_charioteer_mk_7": {
    "rb": 6.0
  },
  "sw_charioteer_mk_7": {
    "rb": 6.0
  },
  "ussr_btr_zd": {
    "rb": 6.3
  },
  "germ_m109g": {
    "ab": 5.7,
    "rb": 5.7
  },
  "it_m109g": {
    "ab": 5.7,
    "rb": 5.7
  },
  "ussr_2s1": {
    "ab": 5.7,
    "rb": 5.7
  },
  "it_2s1": {
    "ab": 5.7,
    "rb": 5.7
  },
  "il_m109": {
    "ab": 5.7,
    "rb": 5.7
  },
  "us_t1e1": {
    "ab": 5.3
  },
  "us_m6a1": {
    "ab": 5.0
  },
  "uk_a_22f_mk_7_churchill_1944": {
    "ab": 5.0,
    "rb": 5.0
  },
  "uk_a_22f_mk_7_churchill_crocodile": {
    "ab": 5.0,
    "rb": 5.0
  },
  "uk_a_33_excelsior": {
    "ab": 4.3,
    "rb": 4.3
  },
  "germ_stug_III_ausf_G": {
    "ab": 4.0
  },
  "uk_a27m_cromwell_5": {
    "ab": 3.7,
    "rb": 4.0
  },
  "uk_a27m_cromwell_5_rp3": {
    "ab": 3.7,
    "rb": 4.0
  },
  "us_m44": {
    "ab": 3.7,
    "rb": 3.7
  },
  "germ_m44": {
    "ab": 3.7,
    "rb": 3.7
  },
  "uk_m44": {
    "ab": 3.7,
    "rb": 3.7
  },
  "jp_m44": {
    "ab": 3.7,
    "rb": 3.7
  },
  "it_m44": {
    "ab": 3.7,
    "rb": 3.7
  },
  "fr_m44": {
    "ab": 3.7,
    "rb": 3.7
  },
  "us_m55": {
    "ab": 3.7,
    "rb": 3.7
  },
  "germ_m55": {
    "ab": 3.7,
    "rb": 3.7
  },
  "cn_m55": {
    "ab": 3.7,
    "rb": 3.7
  },
  "it_m55": {
    "ab": 3.7,
    "rb": 3.7
  },
  "fr_m55": {
    "ab": 3.7,
    "rb": 3.7
  },
  "ussr_su_152": {
    "rb": 3.7
  },
  "germ_hummel": {
    "ab": 3.3,
    "rb": 3.3
  },
  "us_m8a1": {
    "ab": 3.0,
    "rb": 3.0
  },
  "uk_valentine_mk_11": {
    "rb": 3.0
  },
  "germ_pzkpfw_38t_Marder_III_ausf_H": {
    "ab": 2.7,
    "rb": 2.7
  },
  "us_elco_80ft_pt_boat_mod02": {
    "ab": 2.7,
    "rb": 2.7
  },
  "us_elco_80ft_pt_boat_thunderbolt": {
    "ab": 2.7,
    "rb": 2.7
  },
  "us_flagstaff_pgh1": {
    "ab": 2.7,
    "rb": 2.7
  },
  "germ_vs8_hydrofoil": {
    "ab": 2.3,
    "rb": 2.3
  },
  "germ_vs10_hydrofoil": {
    "ab": 2.7,
    "rb": 2.7
  },
  "ussr_pr_123k_hydrofoils": {
    "ab": 2.0,
    "rb": 2.0
  },
  "ussr_pr_123k": {
    "ab": 2.0,
    "rb": 2.0
  },
  "ussr_pr_123bis": {
    "ab": 2.0,
    "rb": 2.0
  },
  "uk_destroyer_st_class_saumarez": {
    "ab": 4.0,
    "rb": 4.0
  },
  "uk_destroyer_tribal_mohawk": {
    "ab": 4.3,
    "rb": 4.3
  },
  "uk_destroyer_tribal": {
    "ab": 4.3,
    "rb": 4.3
  },
  "uk_destroyer_haida": {
    "ab": 4.3,
    "rb": 4.3
  },
  "uk_destroyer_k_class": {
    "ab": 4.3,
    "rb": 4.3
  },
  "uk_destroyer_n_class": {
    "ab": 4.3,
    "rb": 4.3
  },
  "uk_destroyer_j_class": {
    "ab": 4.3,
    "rb": 4.3
  },
  "jp_destroyer_akizuki": {
    "ab": 4.7,
    "rb": 4.7
  },
  "jp_destroyer_hatsuzuki": {
    "ab": 4.7,
    "rb": 4.7
  },
  "jp_destroyer_suzutsuki": {
    "ab": 4.7,
    "rb": 4.7
  },
  "fr_lcg_m_l9059": {
    "ab": 2.3,
    "rb": 2.3
  },
  "fr_marne_class_aisne": {
    "ab": 3.0,
    "rb": 3.0
  },
  "fr_marne_class_marne": {
    "ab": 3.0,
    "rb": 3.0
  },
  "fr_frigate_corse_class_brestois": {
    "ab": 4.0,
    "rb": 4.0
  }
}
RANK_OVERRIDES_V384 = {
  "fw_189c_0": "II",
  "germ_vs10_hydrofoil": "III",
  "ussr_pr_123k": "II"
}

def _apply_v384_august_br_item(item: dict[str, Any]) -> bool:
    vid = str(item.get("id") or item.get("identifier") or "")
    changed = False
    br = BR_OVERRIDES_V384.get(vid)
    if br:
        old = dict(item.get("br") or {})
        new = dict(old)
        new.update(br)
        if new != old:
            item["br"] = new
            changed = True
    nr = RANK_OVERRIDES_V384.get(vid)
    if nr:
        if item.get("rank") != nr:
            item["rank"] = nr
            changed = True
        if item.get("treeRank") != nr:
            item["treeRank"] = nr
            changed = True
    return changed

_old_write_lightweight_vehicles_v384 = write_lightweight_vehicles
def write_lightweight_vehicles(rows: list[dict[str, Any]], image_map: dict[str, str]) -> None:  # v3.84 override
    _old_write_lightweight_vehicles_v384(rows, image_map)
    try:
        p = DATA_DIR / "vehicles.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        changed = 0
        for item in data:
            if isinstance(item, dict) and _apply_v384_august_br_item(item):
                changed += 1
        if changed:
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"v3.84 August 2026 BR/rank overrides applied: {changed} vehicles.")
    except Exception as e:
        print("WARNING: v3.84 August 2026 BR/rank overrides failed:", e)

# --- end v3.84 layer ---


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Stopped by user. Restart update_from_api.bat to continue from cached images/manifest.")
        raise SystemExit(130)
    except Exception as e:
        print("ERROR:", e)
        raise

# --- v3.64: curated owner-only supplements should not disappear as API removals ---
# Some event/hidden vehicles are intentionally maintained by our curated availability layer even
# when they are absent from the public Vehicles API. Keep them visible when unavailable vehicles are shown.
CURATED_SUPPLEMENTAL_UNITS_V364 = [
    {
        "identifier": "fr_trident_class_glaive_p671",
        "country": "france",
        "vehicle_type": "ship",
        "vehicle_sub_types": ["coastal"],
        "era": 5,
        "arcade_br": 4.3,
        "realistic_br": 4.3,
        "realistic_ground_br": 4.3,
        "simulator_br": 4.3,
        "simulator_ground_br": 4.3,
        "event": "maritime_gendarme_2026",
        "is_premium": 0,
        "is_pack": 0,
        "on_marketplace": 0,
        "squadron_vehicle": 0,
        "visibility": "curated_owner_only_supplement",
    },
]
try:
    SUPPLEMENTAL_WIKI_UNITS_V263.extend([x for x in CURATED_SUPPLEMENTAL_UNITS_V364 if x.get("identifier") not in {r.get("identifier") for r in SUPPLEMENTAL_WIKI_UNITS_V263}])
except Exception:
    pass
_old_add_supplemental_wiki_units_v364 = _add_supplemental_wiki_units_v300_network
def _add_supplemental_wiki_units_v300_network(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:  # v3.64 override
    rows = _old_add_supplemental_wiki_units_v364(rows)
    seen = {row_identifier(r) for r in rows if row_identifier(r)}
    added = 0
    for r in CURATED_SUPPLEMENTAL_UNITS_V364:
        vid = r.get("identifier")
        if vid and vid not in seen:
            rows.append(dict(r)); seen.add(vid); added += 1
    if added:
        print(f"v3.64 curated owner-only supplements added: {added} vehicles not present in WT Vehicles API.")
    return rows
_old_diff_snapshots_v364 = _diff_snapshots_v300
def _diff_snapshots_v300(old_vehicles: Any, new_vehicles: Any, old_layout: Any, new_layout: Any) -> list[dict[str, Any]]:  # v3.64 override
    out = _old_diff_snapshots_v364(old_vehicles, new_vehicles, old_layout, new_layout)
    curated_ids = {str(x.get("identifier") or "").lower() for x in CURATED_SUPPLEMENTAL_UNITS_V364}
    # If an old dev snapshot lacked the v3.64 supplement, do not show a scary present→missing row
    # for a vehicle that is intentionally curated as owner-only.
    return [ch for ch in out if not (str(ch.get("unit_id") or "").lower() in curated_ids and ch.get("change_type") == "removed")]
