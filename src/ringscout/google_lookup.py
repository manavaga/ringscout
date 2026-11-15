"""Live India shop lookup via multi-leg Google Maps + Search grounding with +91 hygiene & locality ranking."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

import httpx
from google import genai
from google.genai import types

from ringscout.cloud_run_bridge import fetch_places_via_cloud_run_bridge
from ringscout.india_locations import lookup_area_metadata
from ringscout.models import (
    DiscoverRequest,
    DiscoverResponse,
    IndianShopTarget,
    PlaceCategory,
)
from ringscout.voice_engine import GCP_LOCATION, SIDE_BRAIN_MODEL, get_vertex_client

logger = logging.getLogger(__name__)

_METRO_2DIGIT_STD = {"11", "20", "22", "33", "40", "44", "79", "80"}
_PIN_RE = re.compile(r"\b([1-9]\d{5})\b")
_DIRECTION_SUFFIX_RE = re.compile(
    r"\s+(?:west|east|north|south|\(w\)|\(e\)|\(n\)|\(s\)|w|e|n|s)$",
    re.IGNORECASE,
)

CATEGORY_DEFAULT_QUERIES: dict[PlaceCategory, str] = {
    PlaceCategory.HOTEL: "hotel boutique hotel luxury stay guest house inn open now",
    PlaceCategory.RESTAURANT: "restaurant cafe dining bistro open now",
    PlaceCategory.SPA: "day spa body massage thai spa wellness spa foot reflexology open now",
    PlaceCategory.SALON: "hair salon unisex salon beauty studio hair stylist open now",
    PlaceCategory.CLINIC: "medical clinic doctor OPD polyclinic open now",
    PlaceCategory.DIAGNOSTICS: "MRI CT scan diagnostic radiology center open now",
    PlaceCategory.PETROL: "petrol pump fuel station XP95 EV charging open now",
    PlaceCategory.AUTO: "car tyre shop wheel alignment battery auto garage open now",
    PlaceCategory.PHARMACY: "pharmacy medical store chemist open now",
    PlaceCategory.CUSTOM: "open local businesses",
}

_SPA_INTENT_KEYWORDS = (
    "spa",
    "massage",
    "deep tissue",
    "thai",
    "foot reflexology",
    "reflexology",
    "aromatherapy",
    "ayurvedic",
    "balinese",
    "swedish",
    "couple spa",
    "body scrub",
    "wellness center",
)
_HAIR_INTENT_KEYWORDS = (
    "haircut",
    "blow-dry",
    "blow dry",
    "keratin",
    "hair color",
    "highlights",
    "beard",
    "shave",
    "manicure",
    "pedicure",
    "waxing",
    "facial",
)


def infer_place_category(
    query: str = "",
    purpose: str = "",
    explicit: PlaceCategory | None = None,
) -> PlaceCategory:
    """Infer Google Maps `PlaceCategory` from explicit selection + natural-language user intent."""
    blob = f"{query} {purpose}".lower()
    # Even if user had 'salon' or 'custom' selected, explicit spa/massage words without hair words mean SPA!
    if explicit in (None, PlaceCategory.CUSTOM, PlaceCategory.SALON):
        if any(k in blob for k in _SPA_INTENT_KEYWORDS) and not any(h in blob for h in _HAIR_INTENT_KEYWORDS):
            return PlaceCategory.SPA
    if explicit and explicit != PlaceCategory.CUSTOM:
        return explicit
    if any(k in blob for k in ("hotel", "room", "check-in", "checkin", "resort", "stay", "suite", "tariff")):
        return PlaceCategory.HOTEL
    if any(k in blob for k in ("restaurant", "cafe", "table for", "dinner", "lunch", "buffet", "rooftop", "bistro", "bar")):
        return PlaceCategory.RESTAURANT
    if any(k in blob for k in ("petrol", "xp95", "diesel", "cng", "fuel", "ev charg", "indian oil", "hpcl", "bpcl", "shell")):
        return PlaceCategory.PETROL
    if any(k in blob for k in ("mri", "ct scan", "x-ray", "ultrasound", "sonography", "radiology", "blood test", "diagnostic")):
        return PlaceCategory.DIAGNOSTICS
    if any(k in blob for k in ("doctor", "clinic", "opd", "dentist", "dental", "pediatric", "dermatolog", "ortho", "consultation")):
        return PlaceCategory.CLINIC
    if any(k in blob for k in ("tyre", "tire", "wheel alignment", "car service", "mechanic", "battery", "puncture", "garage")):
        return PlaceCategory.AUTO
    if any(k in blob for k in _SPA_INTENT_KEYWORDS):
        return PlaceCategory.SPA
    if any(k in blob for k in ("salon", "haircut", "grooming", "parlour", "parlor", "barber", "nail")):
        return PlaceCategory.SALON
    if any(k in blob for k in ("pharmacy", "chemist", "medical store", "tablet", "strip", "medicine", "capsule", "insulin", "syrup")):
        return PlaceCategory.PHARMACY
    return PlaceCategory.CUSTOM


def resolve_maps_search_query(req: DiscoverRequest) -> tuple[str, PlaceCategory]:
    """Build an authoritative Google Maps search string from category + specific_place + purpose."""
    cat = infer_place_category(req.query, req.purpose, req.place_category)
    spec = req.specific_place.strip()
    raw_q = req.query.strip()
    if spec:
        return spec, cat
    if raw_q:
        return raw_q, cat
    base_q = CATEGORY_DEFAULT_QUERIES.get(cat, "open local businesses")
    if cat == PlaceCategory.CUSTOM and req.purpose.strip():
        return req.purpose.strip()[:120], cat
    return base_q, cat


_STREET_TOKEN_RE = re.compile(
    r"\b([A-Z0-9][A-Za-z0-9'.&\-]*(?:\s+[A-Za-z0-9'.&\-]+){0,3}\s+"
    r"(?:Road|Rd|Street|St|Marg|Lane|Link\s+Road|Cross\s+Road|Main\s+Road|Avenue|Ave|"
    r"Circle|Chowk|Naka|Square|Bandstand|Pali\s+Hill|Carter\s+Road|Hill\s+Road|Linking\s+Road))\b",
    re.IGNORECASE,
)


def extract_street_landmark(address: str, locality: str = "") -> str:
    """Extract a short, human-spoken street or landmark confidence marker from a Google Maps address."""
    if not address:
        return ""
    clean = re.sub(r"\b\d{6}\b", "", address)
    m = _STREET_TOKEN_RE.search(clean)
    if m:
        phrase = re.sub(r"\s{2,}", " ", m.group(1)).strip(" ,.-")
        # Normalize 'Rd' -> 'Road' so Gemini speaks 'Linking Road' instead of 'Linking R-D'
        phrase = re.sub(r"\bRd\.?$", "Road", phrase, flags=re.IGNORECASE)
        phrase = re.sub(r"\bSt\.?$", "Street", phrase, flags=re.IGNORECASE)
        words = phrase.split()
        if len(words) > 4:
            phrase = " ".join(words[-4:])
        return phrase
    parts = [p.strip() for p in clean.split(",") if p.strip()]
    loc_low = (locality or "").strip().lower()
    for part in parts[:2]:
        low = part.lower()
        if (
            len(part) <= 28
            and not any(k in low for k in ("india", "maharashtra", "karnataka", "delhi", "shop no", "plot no", "floor", "flr"))
            and low != loc_low
            and not part.isdigit()
        ):
            return part
    return ""


def normalize_indian_phone(raw_phone: str | None) -> tuple[str | None, str | None, str]:
    """Deterministically normalize Indian +91 numbers and block 1800/1860 toll-free call centers.

    Returns:
        `(phone_e164, phone_display, status_reason)` where `status_reason` is:
        - `"ok"` when valid local Indian mobile or STD landline
        - `"toll_free"` when `1800` / `1860` / `000800` national call center
        - `"invalid"` otherwise
    """
    if not raw_phone:
        return None, None, "invalid"

    cleaned = raw_phone.strip()
    # Reject explicit non-Indian international prefixes (+1, +44, etc.)
    if cleaned.startswith("+") and not cleaned.startswith("+91"):
        return None, None, "invalid"

    digits = re.sub(r"\D", "", cleaned)
    if not digits:
        return None, None, "invalid"

    # Strip leading 0091 / 91 / 0 prefixes carefully while checking toll-free first
    if digits.startswith(
        ("1800", "1860", "01800", "01860", "911800", "911860", "00911800", "00911860", "000800")
    ):
        return None, None, "toll_free"

    if len(digits) == 14 and digits.startswith("0091"):
        national = digits[4:]
    elif len(digits) == 12 and digits.startswith("91"):
        national = digits[2:]
    elif len(digits) == 13 and digits.startswith("091"):
        national = digits[3:]
    elif len(digits) == 11 and digits.startswith("0"):
        national = digits[1:]
    elif len(digits) == 10:
        national = digits
    else:
        return None, None, "invalid"

    if national.startswith(("1800", "1860")):
        return None, None, "toll_free"

    if len(national) != 10:
        return None, None, "invalid"

    first = national[0]
    if first == "0":
        return None, None, "invalid"
    # Reject synthetic / placeholder dummy numbers (e.g. mobile 98200 00000) while allowing real vanity STD landlines (e.g. 022 6170 0000)
    is_mobile = first in ("6", "7", "8", "9") and national[:2] not in ("79", "80")
    if (
        national.endswith("000000")
        or (is_mobile and national.endswith("00000"))
        or national[2:] == "12345678"
        or len(set(national[2:])) == 1
    ):
        return None, None, "invalid"

    e164 = f"+91{national}"

    if national[:2] in _METRO_2DIGIT_STD and national[2] in ("2", "3", "4", "6") and first not in ("9", "6"):
        display = f"+91 {national[:2]} {national[2:6]} {national[6:]}"
    elif first in ("6", "7", "8", "9"):
        display = f"+91 {national[:5]} {national[5:]}"
    elif first in ("1", "2", "3", "4", "5"):
        if national[:2] in _METRO_2DIGIT_STD:
            display = f"+91 {national[:2]} {national[2:6]} {national[6:]}"
        else:
            display = f"+91 {national[:3]} {national[3:6]} {national[6:]}"
    else:
        return None, None, "invalid"

    return e164, display, "ok"


def is_open_status_acceptable(open_status: str | None, is_open_flag: bool | None = None) -> bool:
    """Return False if a listing is explicitly closed, temporarily closed, or permanently closed."""
    if is_open_flag is False:
        return False
    if not open_status:
        return True
    low = open_status.strip().lower()
    if any(
        bad in low
        for bad in ("permanently closed", "temporarily closed", "closed now", "currently closed")
    ):
        return False
    if low == "closed" or low.startswith("closed "):
        return False
    return True


def is_relevant_for_query(item: dict[str, Any], query: str, specific_place: str = "") -> bool:
    """Filter out modality/category-mismatched listings (e.g. hair salons on Spa searches, clinics on Hotel searches)."""
    q_low = (query or "").strip().lower()
    if not q_low:
        return True

    name_low = str(item.get("name") or "").lower()
    name_cat = f"{name_low} {item.get('category') or ''}".lower()

    # 0. Explicit specific_place pass-through: never filter out a place the user asked for by name!
    if specific_place and specific_place.strip():
        sp_tokens = [t for t in re.split(r"[^a-z0-9]+", specific_place.lower()) if len(t) >= 3]
        if sp_tokens and any(tok in name_low for tok in sp_tokens):
            return True

    # 1. Pure Spa / Massage guard (strictly separate Spas from pure Hair/Unisex Salons!)
    is_pure_spa_query = (
        any(k in q_low for k in ("day spa", "body massage", "thai spa", "wellness spa", "reflexology"))
        or ("spa" in q_low and "salon" not in q_low and "hair" not in q_low)
    )
    if is_pure_spa_query:
        spa_non_business = (
            "physiotherap",
            "hospital",
            "pathology",
            "diagnostic",
            "pharmacy",
            "chemist",
            "medical store",
            "service center",
            "service centre",
            "bosch",
            "siemens",
            "idevice",
            "repair",
            "tyre",
            "petrol",
        )
        if any(b in name_cat for b in spa_non_business):
            return False
        has_spa_signal = any(
            ok in name_cat
            for ok in ("spa", "massage", "wellness", "thai", "ayurved", "reflexology", "therapy", "retreat", "hammam")
        )
        if not has_spa_signal:
            return False
        # Exclude hair/makeup/barber academies & pure unisex hair salons that aren't true spas
        if any(h in name_low for h in ("hair salon", "barber", "makeup studio", "cut da crap", "hair studio")) and "spa" not in name_low:
            return False
        return True

    # 2. MRI / Diagnostic modality guard
    if "mri" in q_low and "dental" not in q_low and "cbct" not in q_low:
        if "mri" in name_cat or "magnetic resonance" in name_cat:
            return True
        non_mri_specialties = (
            "cbct",
            "dental",
            "orthodont",
            "breast biopsy",
            "sonography centre",
            "sonography center",
            "ultrasound only",
            "x-ray clinic",
            "eye clinic",
            "optical",
            "skin clinic",
            "hair clinic",
            "physiotherap",
            "blood collection",
            "collection centre",
            "collection center",
            "blood testing",
        )
        if any(bad in name_cat for bad in non_mri_specialties):
            return False
        if (
            any(p in name_cat for p in ("pathology lab", "pathlab", "path lab"))
            and not any(ok in name_cat for ok in ("radiology", "imaging", "scan", "hospital"))
        ):
            return False
        return True

    # 2. Salon / Spa guard (exclude physiotherapy, medical clinics, appliance/phone service centers)
    if any(k in q_low for k in ("salon", "parlour", "parlor", "haircut", "grooming")):
        salon_bad = (
            "physiotherap",
            "hospital",
            "pathology",
            "diagnostic",
            "pharmacy",
            "chemist",
            "medical store",
            "service center",
            "service centre",
            "bosch",
            "siemens",
            "idevice",
            "mobile repair",
            "electronics",
            "tyre",
            "petrol",
        )
        if any(b in name_cat for b in salon_bad):
            return False
        if "clinic" in name_cat and not any(
            ok in name_cat for ok in ("salon", "hair", "beauty", "parlour", "parlor", "spa", "groom", "makeover", "aesthetic")
        ):
            return False
        return True

    # 3. Hotel / Stay guard (exclude medical clinics, appliance/mobile service centers, salons, chemists)
    if any(k in q_low for k in ("hotel", "boutique hotel", "luxury stay", "guest house", "resort")):
        hotel_bad = (
            "clinic",
            "doctor",
            "dr ",
            "dr.",
            "physiotherap",
            "hospital",
            "diagnostic",
            "pathology",
            "pharmacy",
            "chemist",
            "service center",
            "service centre",
            "bosch",
            "siemens",
            "idevice",
            "repair",
            "salon",
            "parlour",
            "tyre",
            "petrol pump",
        )
        if any(b in name_cat for b in hotel_bad):
            return False
        return True

    # 4. Restaurant / Cafe guard
    if any(k in q_low for k in ("restaurant", "cafe", "bistro", "dining")):
        rest_bad = (
            "clinic",
            "hospital",
            "physiotherap",
            "diagnostic",
            "pharmacy",
            "chemist",
            "service center",
            "service centre",
            "repair",
            "salon",
            "tyre",
        )
        if any(b in name_cat for b in rest_bad):
            return False
        return True

    return True


def _extract_pin_code(text: str) -> int | None:
    m = _PIN_RE.search(text or "")
    return int(m.group(1)) if m else None


def _matches_exact_locality(blob_low: str, loc_low: str) -> bool:
    """Match 'Bandra West' against 'Bandra West', 'Bandra (W)', 'Bandra W,', etc. with word boundaries."""
    if not loc_low:
        return True
    if re.search(rf"\b{re.escape(loc_low)}\b", blob_low):
        return True
    dir_initials = {
        " west": "w",
        " east": "e",
        " north": "n",
        " south": "s",
    }
    for suffix, init_ch in dir_initials.items():
        if loc_low.endswith(suffix):
            stem = loc_low[: -len(suffix)].strip()
            if stem:
                pat = rf"\b{re.escape(stem)}\s*(?:\(\s*{init_ch}\s*\)|-\s*{init_ch}\b|\b{init_ch}\b)"
                if re.search(pat, blob_low):
                    return True
    return False


def _canonical_dedupe_key(name: str, address: str) -> str:
    """Collapse identical brand+PIN listings returned across multiple Maps discovery legs."""
    tokens = re.findall(r"[a-z0-9]+", (name or "").lower())
    stop = {
        "the",
        "and",
        "of",
        "in",
        "dr",
        "drs",
        "centre",
        "center",
        "clinic",
        "hospital",
        "hospitals",
        "diagnostics",
        "diagnostic",
        "research",
        "multispeciality",
        "multispecialty",
        "pvt",
        "ltd",
    }
    sig_tokens = [t for t in tokens if t not in stop][:2]
    if not sig_tokens:
        sig_tokens = tokens[:2]
    pin = _extract_pin_code(address)
    if not sig_tokens or pin is None:
        return ""
    return f"{'_'.join(sig_tokens)}::{pin}"


def _compute_locality_tier_and_badge(
    name: str,
    address: str,
    target_locality: str,
    city: str,
    anchor_pins: set[int],
    known_pins: set[int] | None = None,
    area_aliases: list[str] | None = None,
) -> tuple[int, str]:
    loc_clean = (target_locality or "").strip()
    loc_low = loc_clean.lower()
    if not loc_low:
        return 0, f"In {city}"

    pin = _extract_pin_code(address)
    addr_name_low = f"{name} {address}".lower()

    # If authoritative PINs exist for target_locality (e.g. Bandra East = 400051 vs Bandra West = 400050),
    # match exact PINs directly as Tier 0 and prevent wrong-PIN neighbours from claiming Tier 0!
    if known_pins and pin is not None:
        if pin in known_pins:
            return 0, f"In {loc_clean}"
    else:
        if _matches_exact_locality(addr_name_low, loc_low):
            return 0, f"In {loc_clean}"
        if area_aliases and any(re.search(rf"\b{re.escape(a)}\b", addr_name_low) for a in area_aliases):
            return 0, f"In {loc_clean}"

    if not known_pins or pin is None:
        if _matches_exact_locality(addr_name_low, loc_low):
            return 0, f"In {loc_clean}"
        if area_aliases and any(re.search(rf"\b{re.escape(a)}\b", addr_name_low) for a in area_aliases):
            return 0, f"In {loc_clean}"

    eff_pins = known_pins or anchor_pins
    if pin is not None and eff_pins:
        min_delta = min(abs(pin - p) for p in eff_pins)
        if min_delta <= 5:
            return 1, "Adjacent Neighbourhood"
        if min_delta <= 14:
            return 2, f"Nearby · {city}"

    base_loc = _DIRECTION_SUFFIX_RE.sub("", loc_low).strip()
    if len(base_loc) >= 3 and base_loc in addr_name_low:
        return 1, "Adjacent Neighbourhood"

    return 3, f"Wider {city}"


def filter_and_dedupe_raw_shops(
    raw_items: list[dict[str, Any]],
    city: str,
    locality: str,
    max_shops: int = 8,
    query: str = "",
    specific_place: str = "",
) -> tuple[list[IndianShopTarget], int, int]:
    """Apply deterministic +91 normalization, 1800 blocking, modality filtering, and locality-tier sorting."""
    candidates: list[dict[str, Any]] = []
    by_phone_idx: dict[str, int] = {}
    seen_name_pin: set[str] = set()
    toll_free_dropped = 0
    closed_or_invalid_dropped = 0
    sp_tokens = [t for t in re.split(r"[^a-z0-9]+", (specific_place or "").lower()) if len(t) >= 3]

    for item in raw_items:
        open_status = str(item.get("open_status") or "OPEN NOW").strip()
        is_open_flag = item.get("is_open_now")
        if not is_open_status_acceptable(open_status, is_open_flag):
            closed_or_invalid_dropped += 1
            continue

        if query and not is_relevant_for_query(item, query, specific_place=specific_place):
            closed_or_invalid_dropped += 1
            continue

        e164, display, reason = normalize_indian_phone(str(item.get("phone") or ""))
        if reason == "toll_free":
            toll_free_dropped += 1
            continue
        if reason != "ok" or not e164 or not display:
            closed_or_invalid_dropped += 1
            continue

        rating_raw = item.get("rating")
        rating: float | None = None
        if rating_raw is not None:
            try:
                r_val = float(rating_raw)
                if 1.0 <= r_val <= 5.0:
                    rating = round(r_val, 1)
            except (TypeError, ValueError):
                rating = None

        raw_uri = str(item.get("maps_uri") or "").strip()
        safe_maps_uri = raw_uri if raw_uri.startswith(("https://", "http://")) else None
        raw_name = str(item.get("name") or "").strip()
        name_str = re.sub(r"\s*[-–|]\s*google\s+maps\s*$", "", raw_name, flags=re.IGNORECASE).strip()
        raw_addr_str = str(item.get("address") or "").strip()
        addr_str = raw_addr_str or f"{city}".strip()

        if e164 in by_phone_idx:
            existing = candidates[by_phone_idx[e164]]
            if existing["rating"] is None and rating is not None:
                existing["rating"] = rating
            if not existing["maps_uri"] and safe_maps_uri:
                existing["maps_uri"] = safe_maps_uri
            if not existing["raw_address"] and raw_addr_str:
                existing["raw_address"] = raw_addr_str
                existing["address"] = raw_addr_str
            continue

        dedupe_key = _canonical_dedupe_key(name_str, raw_addr_str)
        if dedupe_key and dedupe_key in seen_name_pin:
            continue

        by_phone_idx[e164] = len(candidates)
        if dedupe_key:
            seen_name_pin.add(dedupe_key)

        candidates.append(
            {
                "orig_idx": len(candidates),
                "name": name_str or f"Local Shop {len(candidates) + 1}",
                "phone_e164": e164,
                "phone_display": display,
                "raw_address": raw_addr_str,
                "address": addr_str,
                "locality": str(item.get("locality") or locality).strip(),
                "city": str(item.get("city") or city).strip(),
                "open_status": open_status,
                "rating": rating,
                "maps_uri": safe_maps_uri,
                "category": str(item.get("category") or "Local Business").strip(),
            }
        )

    loc_low = (locality or "").strip().lower()
    area_meta = lookup_area_metadata(city, locality)
    known_pins: set[int] = set(area_meta["pins"]) if area_meta and area_meta.get("pins") else set()
    area_aliases: list[str] = list(area_meta.get("aliases", [])) if area_meta else []

    anchor_pins: set[int] = set(known_pins)
    if loc_low:
        for cand in candidates:
            if _matches_exact_locality(f"{cand['name']} {cand['raw_address']}".lower(), loc_low):
                pin = _extract_pin_code(cand["raw_address"])
                if pin is not None:
                    anchor_pins.add(pin)

    for cand in candidates:
        tier, badge = _compute_locality_tier_and_badge(
            name=cand["name"],
            address=cand["raw_address"],
            target_locality=locality,
            city=city,
            anchor_pins=anchor_pins,
            known_pins=known_pins or None,
            area_aliases=area_aliases or None,
        )
        cand["tier"] = tier
        cand["badge"] = badge
        name_l = cand["name"].lower()
        cand["spec_rank"] = 0 if (sp_tokens and any(t in name_l for t in sp_tokens)) else 1

    candidates.sort(
        key=lambda c: (
            c["spec_rank"],
            c["tier"],
            -(c["rating"] or 0.0),
            c["orig_idx"],
        )
    )

    accepted: list[IndianShopTarget] = []
    for shop_idx, cand in enumerate(candidates[:max_shops], start=1):
        accepted.append(
            IndianShopTarget(
                shop_id=f"shop_{shop_idx}",
                name=cand["name"],
                phone_e164=cand["phone_e164"],
                phone_display=cand["phone_display"],
                address=cand["address"],
                locality=cand["locality"],
                city=cand["city"],
                open_status=cand["open_status"],
                is_open_now=True,
                rating=cand["rating"],
                maps_uri=cand["maps_uri"],
                category=cand["category"],
                locality_match_tier=cand["tier"],
                locality_badge=cand["badge"],
            )
        )

    return accepted, toll_free_dropped, closed_or_invalid_dropped


def _extract_json_array(text: str) -> list[dict[str, Any]]:
    """Extract a JSON array of shop dicts from grounded model text."""
    if not text:
        return []
    fenced = re.search(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", text)
    candidate = fenced.group(1) if fenced else None
    if not candidate:
        bracket = re.search(r"(\[[\s\S]*\])", text)
        candidate = bracket.group(1) if bracket else None
    if not candidate:
        return []
    try:
        parsed = json.loads(candidate)
        return [x for x in parsed if isinstance(x, dict)] if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def _build_discovery_prompts(req: DiscoverRequest) -> tuple[str, str, str]:
    """Build 3 complementary Google Maps grounded prompts across Hotels, Restaurants, Clinics, Petrol, MRI, & Retail."""
    json_spec = (
        "CRITICAL: Copy the exact phone number and street address strictly from Google Maps place data — "
        'if a place has no phone number on Google Maps, set `"phone": ""` (NEVER guess or invent digits).\n'
        "Return ONLY a valid JSON array where each element has these exact keys:\n"
        '{"name": "...", "phone": "+91...", "address": "...", '
        '"open_status": "OPEN NOW · ...", "is_open_now": true, "rating": 4.5, "category": "..."}'
    )
    eff_query, cat = resolve_maps_search_query(req)
    target_n = max(req.max_shops + 5, 12)
    q_low = f"{eff_query} {req.purpose}".lower()

    if req.specific_place.strip():
        sp = req.specific_place.strip()
        leg1 = (
            f"Use Google Maps to find the exact listing and direct local +91 phone number for '{sp}' "
            f"in or near {req.locality}, {req.city}, India, plus up to {max(req.max_shops, 5)} closest matching branches/alternatives.\n{json_spec}"
        )
        leg2 = (
            f"Use Google Maps to find '{sp}' and similar top-rated {cat.value if cat != PlaceCategory.CUSTOM else 'places'} "
            f"in {req.locality}, {req.city}, India with direct +91 front-desk or reception numbers.\n{json_spec}"
        )
        return leg1, leg2, leg2

    if "mri" in q_low or cat == PlaceCategory.DIAGNOSTICS:
        maps_leg1 = (
            f"Use Google Maps to find up to {target_n} real, currently OPEN diagnostic imaging centers, "
            f"1.5T/3T MRI/CT scan centers, and radiology labs in {req.locality}, {req.city}, India for '{eff_query}'.\n"
            f"Exclude dental CBCT-only clinics unless explicitly requested.\n{json_spec}"
        )
        maps_leg2 = (
            f"Use Google Maps to find up to 10 real, currently OPEN multispeciality hospitals and medical centers "
            f"with an in-house Radiology / Diagnostic department in {req.locality}, {req.city}, India.\n{json_spec}"
        )
        maps_leg3 = (
            f"Use Google Maps to find up to 10 open diagnostic & pathology/imaging labs in and immediately adjacent "
            f"to {req.locality}, {req.city}, India with direct local +91 numbers (avoid 1800 numbers).\n{json_spec}"
        )
        return maps_leg1, maps_leg2, maps_leg3

    if cat == PlaceCategory.HOTEL:
        maps_leg1 = (
            f"Use Google Maps to find up to {target_n} real, currently OPEN hotels, boutique stays, and business hotels "
            f"strictly in {req.locality}, {req.city}, India with a direct +91 front-desk or reservations phone number (avoid 1800 central numbers).\n{json_spec}"
        )
        maps_leg2 = (
            f"Use Google Maps to find up to 10 real 4-star, 5-star, and well-rated boutique hotels in and immediately around "
            f"{req.locality}, {req.city}, India with direct +91 hotel reception numbers.\n{json_spec}"
        )
        maps_leg3 = (
            f"Use Google Maps to find up to 10 open serviced apartments, guest houses, and executive stays in "
            f"{req.locality}, {req.city}, India.\n{json_spec}"
        )
        return maps_leg1, maps_leg2, maps_leg3

    if cat == PlaceCategory.RESTAURANT:
        maps_leg1 = (
            f"Use Google Maps to find up to {target_n} real, currently OPEN restaurants, cafes, and dining venues "
            f"strictly in {req.locality}, {req.city}, India matching '{eff_query}' with direct +91 host/reservation phone numbers.\n{json_spec}"
        )
        maps_leg2 = (
            f"Use Google Maps to find up to 10 popular top-rated restaurants, rooftop lounges, and family dining spots "
            f"in {req.locality}, {req.city}, India with local +91 numbers.\n{json_spec}"
        )
        return maps_leg1, maps_leg2, maps_leg2

    if cat == PlaceCategory.CLINIC:
        maps_leg1 = (
            f"Use Google Maps to find up to {target_n} real, currently OPEN doctor clinics, polyclinics, and specialist OPD centers "
            f"in {req.locality}, {req.city}, India matching '{eff_query}' with direct +91 reception numbers.\n{json_spec}"
        )
        maps_leg2 = (
            f"Use Google Maps to find up to 10 open multispeciality clinics, family physicians, and hospital OPD desks "
            f"in and immediately around {req.locality}, {req.city}, India.\n{json_spec}"
        )
        return maps_leg1, maps_leg2, maps_leg2

    if cat == PlaceCategory.PETROL:
        maps_leg1 = (
            f"Use Google Maps to find up to {target_n} real, currently OPEN petrol pumps, IndianOil / HP / Bharat Petroleum / Shell "
            f"fuel stations, CNG stations, and EV fast-charging hubs in and around {req.locality}, {req.city}, India with +91 phone numbers.\n{json_spec}"
        )
        maps_leg2 = (
            f"Use Google Maps to find up to 10 open 24x7 fuel stations, highway/arterial petrol pumps, and EV charging stations "
            f"near {req.locality}, {req.city}, India with local +91 contact numbers.\n{json_spec}"
        )
        return maps_leg1, maps_leg2, maps_leg2

    if cat == PlaceCategory.SPA:
        area_meta_spa = lookup_area_metadata(req.city, req.locality)
        pin_hint_spa = (
            f" (PIN {'/'.join(str(p) for p in area_meta_spa['pins'])}, {', '.join(area_meta_spa['aliases'][:3])})"
            if area_meta_spa
            else ""
        )
        maps_leg1 = (
            f"Use Google Maps to find up to {target_n} real, currently OPEN day spas, body massage centers, "
            f"and Thai / deep-tissue wellness spas strictly inside {req.locality}{pin_hint_spa}, {req.city}, India "
            f"with direct +91 appointment numbers (exclude hair-only salons and barber shops).\n{json_spec}"
        )
        maps_leg2 = (
            f"Use Google Maps to find up to 10 top-rated luxury spas, aromatherapy wellness centers, sauna spas, "
            f"and foot reflexology centers in {req.locality}{pin_hint_spa}, {req.city}, India with local +91 reception numbers.\n{json_spec}"
        )
        maps_leg3 = (
            f"Use Google Maps to find up to 10 popular Thai spas and Ayurvedic massage spas "
            f"in and immediately adjacent to {req.locality}{pin_hint_spa}, {req.city}, India.\n{json_spec}"
        )
        return maps_leg1, maps_leg2, maps_leg3

    if cat == PlaceCategory.SALON:
        area_meta_s = lookup_area_metadata(req.city, req.locality)
        pin_hint = f" (PIN {'/'.join(str(p) for p in area_meta_s['pins'])}, {', '.join(area_meta_s['aliases'][:3])})" if area_meta_s else ""
        maps_leg1 = (
            f"Use Google Maps to find up to {target_n} real, currently OPEN unisex hair salons, beauty parlours, "
            f"and styling studios strictly inside {req.locality}{pin_hint}, {req.city}, India with direct +91 appointment numbers.\n{json_spec}"
        )
        maps_leg2 = (
            f"Use Google Maps to find up to 10 top-rated hair couture salons, grooming lounges, and blow-dry studios "
            f"strictly in {req.locality}{pin_hint}, {req.city}, India with local +91 reception numbers.\n{json_spec}"
        )
        maps_leg3 = (
            f"Use Google Maps to find up to 10 popular neighbourhood beauty salons and barber/grooming studios "
            f"in {req.locality}{pin_hint}, {req.city}, India.\n{json_spec}"
        )
        return maps_leg1, maps_leg2, maps_leg3

    if cat == PlaceCategory.AUTO:
        area_meta_a = lookup_area_metadata(req.city, req.locality)
        pin_hint_a = f" (PIN {'/'.join(str(p) for p in area_meta_a['pins'])})" if area_meta_a else ""
        maps_leg1 = (
            f"Use Google Maps to find up to {target_n} real, currently OPEN car tyre dealers, wheel alignment shops, "
            f"and car battery / auto service garages in {req.locality}{pin_hint_a}, {req.city}, India with direct +91 phone numbers.\n{json_spec}"
        )
        maps_leg2 = (
            f"Use Google Maps to find up to 10 authorised tyre shops, multi-brand car workshops, and battery dealers "
            f"in and immediately around {req.locality}{pin_hint_a}, {req.city}, India.\n{json_spec}"
        )
        return maps_leg1, maps_leg2, maps_leg2

    if cat == PlaceCategory.PHARMACY or any(k in q_low for k in ("pharmacy", "chemist", "medical store", "medicine", "druggist")):
        maps_leg1 = (
            f"Use Google Maps to find up to {target_n} real, currently OPEN independent medical stores, chemists, "
            f"and pharmacies strictly inside {req.locality}, {req.city}, India with a direct local +91 mobile or "
            f"STD landline phone number (avoid 1800 toll-free numbers).\n{json_spec}"
        )
        maps_leg2 = (
            f"Use Google Maps to find up to 10 real, currently OPEN 24x7 pharmacies, hospital pharmacies, and "
            f"surgical druggists in or immediately adjacent to {req.locality}, {req.city}, India.\n{json_spec}"
        )
        maps_leg3 = (
            f"Use Google Maps to find up to 10 additional open neighbourhood chemists and Wellness Forever / "
            f"Apollo / Noble local branch pharmacies in {req.locality}, {req.city}, India with local +91 branch numbers.\n{json_spec}"
        )
        return maps_leg1, maps_leg2, maps_leg3

    maps_leg1 = (
        f"Use Google Maps to find up to {target_n} real, currently OPEN local businesses strictly in "
        f"{req.locality}, {req.city}, India for:\n"
        f'"{eff_query}"\n'
        f"Prefer businesses with a direct local Indian +91 mobile or STD landline phone number (avoid 1800 numbers).\n"
        f"{json_spec}"
    )
    maps_leg2 = (
        f"Use Google Maps to find up to 10 additional real, currently OPEN top-rated venues, authorised centers, "
        f"or popular local branches in and immediately around {req.locality}, {req.city}, India for:\n"
        f'"{eff_query}"\n{json_spec}'
    )
    maps_leg3 = (
        f"Use Google Maps to find up to 10 open local providers in "
        f"{req.locality}, {req.city}, India for '{eff_query}' with local +91 phone numbers.\n{json_spec}"
    )
    return maps_leg1, maps_leg2, maps_leg3


async def _discover_via_places_rest_api(
    req: DiscoverRequest, api_key: str
) -> tuple[list[dict[str, Any]], str]:
    """Optional direct Google Places `places:searchText` lookup when `GOOGLE_PLACES_API_KEY` is set."""
    text_query = f"{req.query} in {req.locality}, {req.city}, India"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "places.displayName,places.formattedAddress,places.internationalPhoneNumber,"
            "places.nationalPhoneNumber,places.rating,places.googleMapsUri,"
            "places.currentOpeningHours,places.primaryTypeDisplayName"
        ),
    }
    payload = {
        "textQuery": text_query,
        "regionCode": "IN",
        "openNow": True,
        "maxResultCount": min(req.max_shops + 8, 20),
    }
    async with httpx.AsyncClient(timeout=15.0) as http:
        resp = await http.post(
            "https://places.googleapis.com/v1/places:searchText",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    raw_items: list[dict[str, Any]] = []
    for p in data.get("places", []):
        hours = p.get("currentOpeningHours") or {}
        is_open = hours.get("openNow", True)
        raw_items.append(
            {
                "name": (p.get("displayName") or {}).get("text", ""),
                "phone": p.get("internationalPhoneNumber") or p.get("nationalPhoneNumber") or "",
                "address": p.get("formattedAddress") or "",
                "open_status": "OPEN NOW" if is_open else "Closed",
                "is_open_now": is_open,
                "rating": p.get("rating"),
                "maps_uri": p.get("googleMapsUri"),
                "category": (p.get("primaryTypeDisplayName") or {}).get("text", "Local Business"),
            }
        )
    return raw_items, "google_places_v1"


async def _run_grounded_leg(
    genai_client: genai.Client,
    prompt: str,
    tool: types.Tool,
    timeout_sec: float = 30.0,
) -> Any:
    async with asyncio.timeout(timeout_sec):
        return await genai_client.aio.models.generate_content(
            model=SIDE_BRAIN_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                tools=[tool],
            ),
        )


async def _discover_via_vertex_google_maps(
    req: DiscoverRequest,
    client: genai.Client | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Exhaustive parallel multi-leg lookup (`googleMaps` x2 + `googleSearch` local branch enrichment)."""
    genai_client = client or get_vertex_client(GCP_LOCATION)
    maps_leg1, maps_leg2, search_leg3 = _build_discovery_prompts(req)

    # For quick <=5 lookups (e.g. A2A burst or fast tests) that aren't specialized MRI/hospital queries,
    # run Leg 1; for exhaustive (>5) or MRI/diagnostic lookups, run all 3 legs concurrently!
    run_three_legs = req.max_shops > 5 or any(
        k in req.query.lower() for k in ("mri", "ct ", "scan", "radiology", "diagnostic", "hospital")
    )
    coros = [
        _run_grounded_leg(genai_client, maps_leg1, types.Tool(google_maps=types.GoogleMaps()), 30.0),
    ]
    if run_three_legs:
        coros.append(
            _run_grounded_leg(genai_client, maps_leg2, types.Tool(google_maps=types.GoogleMaps()), 28.0)
        )
        coros.append(
            _run_grounded_leg(genai_client, search_leg3, types.Tool(google_maps=types.GoogleMaps()), 28.0)
        )

    results = await asyncio.gather(*coros, return_exceptions=True)

    # If the primary leg failed with an auth/API error and every leg failed, surface the primary exception
    valid_resps = [r for r in results if not isinstance(r, BaseException)]
    if not valid_resps:
        first_err = results[0]
        if isinstance(first_err, Exception):
            raise first_err
        raise RuntimeError("Discovery legs returned no valid responses")

    raw_items: list[dict[str, Any]] = []
    maps_links: list[tuple[str, str]] = []

    for resp in valid_resps:
        raw_items.extend(_extract_json_array(getattr(resp, "text", "") or ""))
        if getattr(resp, "candidates", None):
            gm = getattr(resp.candidates[0], "grounding_metadata", None)
            for chunk in getattr(gm, "grounding_chunks", None) or []:
                maps_obj = getattr(chunk, "maps", None)
                uri = getattr(maps_obj, "uri", None)
                title = getattr(maps_obj, "title", None)
                if uri and title:
                    maps_links.append((title.lower().strip(), uri))

    for item in raw_items:
        name_low = str(item.get("name") or "").lower().strip()
        if not name_low:
            continue
        for title_low, uri in maps_links:
            if name_low in title_low or title_low in name_low:
                item["maps_uri"] = uri
                break
        if not item.get("maps_uri"):
            q_enc = httpx.URL("https://www.google.com/maps/search/").copy_with(
                params={"api": "1", "query": f"{item.get('name')} {req.locality} {req.city}"}
            )
            item["maps_uri"] = str(q_enc)

    return raw_items, "vertex_google_maps_grounding"


def _local_preview_fallback_places(
    cat: PlaceCategory, city: str, locality: str
) -> list[dict[str, Any]]:
    """Curated Google Maps India fallback listings used ONLY on local Cloudtop preview when ADC is expired."""
    loc = locality or "Bandra West"
    cty = city or "Mumbai"
    catalog: dict[PlaceCategory, list[tuple[str, str, str, float, str]]] = {
        PlaceCategory.HOTEL: [
            ("Taj Lands End", "+91 22 6668 1234", f"Bandstand, {loc}, {cty} 400050", 4.7, "5-Star Hotel"),
            ("Executive Enclave Hotel", "+91 22 6696 9000", f"Pali Hill Road, {loc}, {cty} 400050", 4.2, "Boutique Hotel"),
            ("Theory9 Premium Service Apartments", "+91 98207 49999", f"16th Road, {loc}, {cty} 400050", 4.5, "Serviced Stay"),
            ("Hotel Metro Palace", "+91 22 2642 7311", f"Hill Road, {loc}, {cty} 400050", 4.0, "Business Hotel"),
            ("Le Sutra Art Hotel", "+91 22 2649 2995", f"Carter Road, {loc}, {cty} 400052", 4.3, "Boutique Hotel"),
        ],
        PlaceCategory.RESTAURANT: [
            ("Bastian At The Top", "+91 22 5033 3555", f"Kohinoor Square, Shivaji Park / {loc}, {cty}", 4.6, "Fine Dining Restaurant"),
            ("Pali Village Cafe", "+91 98333 58490", f"Adarsh Nagar, Pali Hill, {loc}, {cty} 400050", 4.4, "European Bistro"),
            ("Candies Pali Hill", "+91 22 2642 4124", f"Mac Ronells, St Andrews Road, {loc}, {cty} 400050", 4.5, "Cafe & Bakery"),
            ("Izumi Bandra", "+91 86574 55585", f"Linking Road, {loc}, {cty} 400050", 4.7, "Japanese Restaurant"),
            ("Salt Water Cafe", "+91 98202 88949", f"Chapel Road, {loc}, {cty} 400050", 4.4, "All-Day Cafe"),
        ],
        PlaceCategory.PETROL: [
            ("IndianOil - Bandra Auto Service", "+91 22 2642 3841", f"SV Road, {loc}, {cty} 400050", 4.2, "Petrol Pump & XP95"),
            ("HP Petrol Pump - Linking Road", "+91 22 2648 2190", f"Linking Road, {loc}, {cty} 400052", 4.1, "Fuel & Nitrogen Station"),
            ("Bharat Petroleum - Turner Road", "+91 22 2640 5512", f"Turner Road, {loc}, {cty} 400050", 4.3, "24x7 Fuel & EV Hub"),
            ("Tata Power EZ Charge - Bandstand", "+91 92235 81212", f"Bandstand Road, {loc}, {cty} 400050", 4.4, "EV Fast Charging"),
        ],
        PlaceCategory.CLINIC: [
            ("Holy Family OPD & Polyclinic", "+91 22 6267 0555", f"St Andrews Road, {loc}, {cty} 400050", 4.3, "Multispeciality Clinic"),
            ("Lilavati Outpatient Desk", "+91 22 6931 8000", f"Bandra Reclamation, {loc}, {cty} 400050", 4.4, "Specialist OPD"),
            ("Bandra Family Care Clinic", "+91 98201 44290", f"Hill Road, {loc}, {cty} 400050", 4.6, "General Physician"),
            ("Apollo Sugar & Dental Clinic", "+91 22 2640 8822", f"Linking Road, {loc}, {cty} 400050", 4.5, "Polyclinic"),
        ],
        PlaceCategory.DIAGNOSTICS: [
            ("ILLUME Imaging and Diagnostics", "+91 93240 38669", f"St Martins Road, {loc}, {cty} 400050", 4.8, "MRI & Radiology"),
            ("Bandra Diagnostic Centre", "+91 22 2642 3566", f"Hill Road, {loc}, {cty} 400050", 4.5, "Diagnostic Center"),
            ("NM Medical Radiology", "+91 22 4342 8888", f"14th Road, Khar / {loc}, {cty} 400052", 4.6, "MRI & CT Lab"),
            ("Agilus Diagnostics Bandra", "+91 98203 67776", f"SV Road, {loc}, {cty} 400050", 4.4, "Pathology & Scans"),
        ],
        PlaceCategory.AUTO: [
            ("Bombay Tyres & Wheel Care", "+91 98200 74321", f"SV Road, {loc}, {cty} 400050", 4.5, "Tyre & Alignment Shop"),
            ("Saifee Auto & Battery Point", "+91 22 2640 1988", f"Linking Road, {loc}, {cty} 400050", 4.4, "Auto Service & Battery"),
            ("Pal Tyre Service", "+91 98211 33490", f"Turner Road, {loc}, {cty} 400050", 4.6, "Authorised Tyre Dealer"),
        ],
        PlaceCategory.SPA: [
            ("Dream Spa Bandra", "+91 98920 25216", f"Waterfield Road, {loc}, {cty} 400050", 4.9, "Day Spa & Massage"),
            ("Suzumi Wellness - Sauna & Spa", "+91 75060 09926", f"Waterfield Road, {loc}, {cty} 400050", 4.9, "Wellness & Sauna Spa"),
            ("Sally's Day Spa", "+91 89764 90701", f"Pali Hill Road, {loc}, {cty} 400050", 4.8, "Day Spa"),
            ("Four Fountains De-Stress Spa", "+91 22 6522 4488", f"Linking Road, {loc}, {cty} 400050", 4.6, "Body Massage Spa"),
        ],
        PlaceCategory.SALON: [
            ("BBLUNT Salon Bandra", "+91 98204 82110", f"Pali Mala Road, {loc}, {cty} 400050", 4.6, "Hair & Beauty Salon"),
            ("Jean-Claude Biguine Salon", "+91 22 6522 2211", f"Waterfield Road, {loc}, {cty} 400050", 4.5, "Hair Styling Salon"),
            ("Truefitt & Hill Bandra", "+91 98192 44010", f"Linking Road, {loc}, {cty} 400050", 4.7, "Grooming Lounge"),
        ],
        PlaceCategory.PHARMACY: [
            ("Shanti Chemist", "+91 91674 63311", f"Hill Road, {loc}, {cty} 400050", 4.5, "24x7 Chemist"),
            ("Star Chemist", "+91 96198 66626", f"Linking Road, {loc}, {cty} 400050", 4.4, "Medical Store"),
            ("Wellness Forever Bandra", "+91 22 2640 7711", f"Carter Road, {loc}, {cty} 400050", 4.6, "24x7 Pharmacy"),
            ("Noble Plus Pharmacy", "+91 22 2643 1188", f"Turner Road, {loc}, {cty} 400050", 4.3, "Pharmacy"),
        ],
    }
    rows = catalog.get(cat) or catalog[PlaceCategory.HOTEL]
    out: list[dict[str, Any]] = []
    for name, phone, addr, rating, label in rows:
        q_enc = httpx.URL("https://www.google.com/maps/search/").copy_with(
            params={"api": "1", "query": f"{name} {loc} {cty}"}
        )
        out.append(
            {
                "name": name,
                "phone": phone,
                "address": addr,
                "open_status": "OPEN NOW",
                "is_open_now": True,
                "rating": rating,
                "maps_uri": str(q_enc),
                "category": label,
            }
        )
    return out


_LOCAL_ADC_FORBIDDEN_UNTIL: float = 0.0


async def discover_indian_shops(
    req: DiscoverRequest,
    client: genai.Client | None = None,
) -> DiscoverResponse:
    """Discover real open Indian places from Google Maps + Search with +91 hygiene & locality ranking."""
    global _LOCAL_ADC_FORBIDDEN_UNTIL
    eff_query, cat = resolve_maps_search_query(req)
    enriched_req = req.model_copy(update={"query": eff_query, "place_category": cat})
    places_key = os.environ.get("GOOGLE_PLACES_API_KEY", "").strip()

    skip_dead_local_adc = (
        client is None
        and not places_key
        and not os.environ.get("K_SERVICE")
        and not os.environ.get("PYTEST_CURRENT_TEST")
        and time.monotonic() < _LOCAL_ADC_FORBIDDEN_UNTIL
    )
    try:
        if skip_dead_local_adc:
            raise RuntimeError("cached_local_adc_403_reauthentication")
        if places_key:
            try:
                raw_items, source = await _discover_via_places_rest_api(enriched_req, places_key)
            except Exception as exc:
                logger.warning("Places REST API failed (%s); using Vertex googleMaps grounding.", exc)
                raw_items, source = await _discover_via_vertex_google_maps(enriched_req, client=client)
        else:
            raw_items, source = await _discover_via_vertex_google_maps(enriched_req, client=client)
    except Exception as exc:
        err_low = str(exc).lower()
        is_local_reauth = (
            (not os.environ.get("K_SERVICE"))
            and (not os.environ.get("PYTEST_CURRENT_TEST"))
            and any(
                tok in err_low
                for tok in (
                    "reauthentication",
                    "unable to obtain gcp access token",
                    "refresh",
                    "credentials",
                    "permission_denied",
                    "403",
                )
            )
        )
        if is_local_reauth:
            _LOCAL_ADC_FORBIDDEN_UNTIL = time.monotonic() + 300.0
            area_meta = lookup_area_metadata(req.city, req.locality)
            pin_clause = (
                f" in {req.locality} PIN {area_meta['pins'][0]} ({', '.join(area_meta['aliases'][:2])})"
                if area_meta and area_meta.get("pins")
                else f" in {req.locality}"
            )
            if req.specific_place.strip():
                bridged = await fetch_places_via_cloud_run_bridge(
                    enriched_query=f'"{req.specific_place.strip()}"{pin_clause}',
                    city=req.city,
                    locality=req.locality,
                    max_shops=max(req.max_shops, 5),
                )
            elif cat == PlaceCategory.SPA:
                leg_a, leg_b = await asyncio.gather(
                    fetch_places_via_cloud_run_bridge(
                        enriched_query=f"day spa body massage center{pin_clause}",
                        city=req.city,
                        locality=req.locality,
                        max_shops=12,
                    ),
                    fetch_places_via_cloud_run_bridge(
                        enriched_query=f"thai spa wellness spa aromatherapy{pin_clause}",
                        city=req.city,
                        locality=req.locality,
                        max_shops=12,
                    ),
                )
                merged_spa = (leg_a[0] if leg_a else []) + (leg_b[0] if leg_b else [])
                bridged = (merged_spa, "vertex_google_maps_grounding") if merged_spa else None
            else:
                bridged = await fetch_places_via_cloud_run_bridge(
                    enriched_query=f"{eff_query}{pin_clause}",
                    city=req.city,
                    locality=req.locality,
                    max_shops=req.max_shops,
                )
            if bridged is not None and bridged[0]:
                raw_items, source = bridged
            else:
                logger.warning("Local Cloudtop ADC expired (%s); serving curated Maps preview places.", exc)
                raw_items = _local_preview_fallback_places(cat, req.city, req.locality)
                source = "curated_maps_preview"
        else:
            raise

    shops, toll_free_cnt, invalid_cnt = filter_and_dedupe_raw_shops(
        raw_items,
        city=req.city,
        locality=req.locality,
        max_shops=req.max_shops,
        query=eff_query,
        specific_place=req.specific_place,
    )

    return DiscoverResponse(
        query=eff_query,
        place_category=cat,
        city=req.city,
        locality=req.locality,
        source=source,
        filtered_toll_free_count=toll_free_cnt,
        filtered_closed_or_invalid_count=invalid_cnt,
        shops=shops,
    )
