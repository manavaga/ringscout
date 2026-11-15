"""Thin Gemini Live Socket + Out-of-Band temp=0.0 Side-Brain for RingScout India POC."""

from __future__ import annotations

from array import array
import asyncio
import datetime
import io
import logging
import math
import os
import re
import struct
import subprocess
import wave
from typing import AsyncIterator, Sequence

import google.auth
from google.auth.credentials import Credentials
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request as GoogleAuthRequest
from google import genai
from google.genai import types

from ringscout.models import (
    ConversationalCallPlan,
    InrQuote,
    PlaceCategory,
    SideBrainDecision,
    TranscriptTurn,
    VocalTone,
)

logger = logging.getLogger(__name__)

GCP_PROJECT_ID = "manav-chub"
GCP_LOCATION = "us-central1"
LIVE_AUDIO_MODEL = "gemini-3.8-live"
DEFAULT_LIVE_VOICE = "Puck"
SUPPORTED_LIVE_VOICES: tuple[str, ...] = ("Puck", "Orus", "Kore", "Aoede")
FEMALE_GEMINI_VOICES: frozenset[str] = frozenset({"aoede", "kore", "leda", "zephyr"})
LIVE_FALLBACK_TARGETS: tuple[tuple[str, str], ...] = (
    ("us-central1", "gemini-3.8-live"),
    ("us-east1", "gemini-live-2.5-flash-native-audio"),
    ("us-central1", "gemini-live-2.5-flash-native-audio"),
    ("europe-west4", "gemini-live-2.5-flash-native-audio"),
)
SIDE_BRAIN_MODEL = "gemini-2.5-flash"
TTS_DIALOGUE_MODEL = "gemini-2.5-flash-preview-tts"

MAX_CALL_DURATION_SEC = 90.0
VOICEMAIL_BAILOUT_MAX_SEC = 5.0


class GcloudCliCredentials(Credentials):
    """ADC fallback using `gcloud auth print-access-token` when Cloudtop RAPT blocks Python refresh."""

    def __init__(self, quota_project_id: str = GCP_PROJECT_ID) -> None:
        super().__init__()
        self._quota_project_id = quota_project_id
        self.refresh(None)

    @property
    def quota_project_id(self) -> str:
        return self._quota_project_id

    def refresh(self, request: object) -> None:
        last_err = ""
        for cmd in (
            ["gcloud", "auth", "print-access-token", "--quiet"],
            ["gcloud", "auth", "application-default", "print-access-token", "--quiet"],
        ):
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=8, check=False)
            token = res.stdout.strip()
            if res.returncode == 0 and token:
                self.token = token
                self.expiry = datetime.datetime.now(datetime.timezone.utc).replace(
                    tzinfo=None
                ) + datetime.timedelta(minutes=45)
                return
            last_err = res.stderr.strip()
        raise RuntimeError(f"Unable to obtain GCP access token via gcloud CLI: {last_err}")


_cached_credentials: Credentials | None = None


def get_vertex_credentials() -> Credentials:
    """Resolve ADC credentials pinned to `manav-chub` without hardcoding tokens."""
    global _cached_credentials
    if _cached_credentials is not None and _cached_credentials.valid:
        return _cached_credentials

    try:
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
            quota_project_id=GCP_PROJECT_ID,
        )
        creds.refresh(GoogleAuthRequest())
        _cached_credentials = creds
        return creds
    except (RefreshError, Exception) as exc:
        logger.info("ADC direct refresh unavailable (%s); falling back to gcloud CLI token.", exc)
        _cached_credentials = GcloudCliCredentials(quota_project_id=GCP_PROJECT_ID)
        return _cached_credentials


def get_vertex_client(location: str = GCP_LOCATION) -> genai.Client:
    """Create a Vertex AI `genai.Client` strictly targeting `manav-chub`."""
    if os.environ.get("K_SERVICE"):
        return genai.Client(
            vertexai=True,
            project=GCP_PROJECT_ID,
            location=location,
        )
    return genai.Client(
        vertexai=True,
        project=GCP_PROJECT_ID,
        location=location,
        credentials=get_vertex_credentials(),
    )


_LIVE_META_TOKEN_RE = re.compile(
    r"(?:🧠\s*\[[^\]]*\]?|\[awaiting(?:\s+response)?[^\]]*\]?)",
    re.IGNORECASE,
)


def strip_live_meta_tokens(text: str) -> str:
    """Strip Gemini 3.8 Live Beyond internal turn-holding markers (`🧠[awaiting response]`) from transcripts."""
    if not text:
        return ""
    return _LIVE_META_TOKEN_RE.sub("", text)


def redact_phone_digits(text: str) -> str:
    """Strip any phone-like digit sequence (7+ digits) so voice prompts never carry phone numbers."""
    return re.sub(r"(?:\+?\d[\d\s\-().]{6,}\d)", "[redacted-number]", text or "").strip()


_PROMPT_PREFIX_RE = re.compile(
    r"^(?:please\s+)?(?:check\s+(?:which\s+open\s+[\w\s]+has|if|whether)|find\s+out\s+if|ask\s+if)\s+",
    re.IGNORECASE,
)
_PROMPT_SUFFIX_RE = re.compile(
    r"\s*(?:(?:is|are)\s+(?:in\s+stock|available)(?:\s+right\s+now|\s+today)?|"
    r"(?:in\s+stock|available)(?:\s+right\s+now|\s+today)?)\s*"
    r"(?:(?:and|,)\s*(?:confirm|get|check|ask|tell)[\s\S]*$)?$",
    re.IGNORECASE,
)


def extract_spoken_item_phrase(user_goal: str) -> str:
    """Convert a written UI search prompt into a crisp, natural spoken item name."""
    clean = redact_phone_digits(user_goal)
    item = _PROMPT_PREFIX_RE.sub("", clean).strip()
    item = _PROMPT_SUFFIX_RE.sub("", item).strip(" ?.!,;-")
    # Also strip trailing 'and get/confirm ... price ...' clauses if still present
    item = re.sub(
        r"\s+(?:and|&|aur)\s+(?:confirm|get|check|ask|kitne|what\s+is)\b[\s\S]*$",
        "",
        item,
        flags=re.IGNORECASE,
    ).strip(" ?.!,;-")
    if not item or len(item) > 95:
        item = clean[:75].strip(" ?.!,;-")
    return item


def spoken_shop_brand(shop_name: str) -> str:
    """Extract a crisp 2-3 word human-spoken shop name (e.g. 'Star chemist medical store bkc' -> 'Star Chemist')."""
    raw = re.sub(r"\s*[-–|]\s*google\s+maps.*$", "", (shop_name or "").strip(), flags=re.IGNORECASE)
    if not raw or any(
        tok in raw.lower()
        for tok in ("self-test", "my own", "dry-run", "my +91", "test call", "local shop")
    ):
        return ""
    clean = re.sub(
        r"\b(?:pvt\.?\s*ltd\.?|private\s+limited|llp|limited|and\s+general\s+store|general\s+store|"
        r"chemist\s*(?:&|and)\s*druggists?|druggists?|bkc|bandra\s+west|bandra\s+east|indiranagar|"
        r"hsr\s+layout|koramangala|mumbai|bengaluru|bangalore)\b",
        " ",
        raw,
        flags=re.IGNORECASE,
    )
    # Collapse redundant 'chemist medical store' -> 'Chemist'
    clean = re.sub(
        r"\b(chemist|pharmacy)\s+medical\s+store\b",
        r"\1",
        clean,
        flags=re.IGNORECASE,
    )
    clean = re.sub(r"[^\w\s&'.-]", " ", clean)
    words = [
        (w.capitalize() if (w.islower() or (w.isupper() and len(w) > 3)) else w)
        for w in clean.split()
        if w
    ]
    if not words:
        return ""
    return " ".join(words[:3])


def sanitize_option_a_line(line: str, vocal_tone: VocalTone) -> str:
    """Enforce real local buyer tone: strip 'bhaiya', 'for a customer', and telemarketer 'Hello ji, namaste'."""
    cleaned = redact_phone_digits(line)
    # Strip Indian telemarketer/robocall opening trigger ('Hello ji, namaste' / 'Namaste ji')
    cleaned = re.sub(
        r"^(?:hello\s+ji\s*,?\s*namaste|namaste\s+ji|hello\s*,?\s*namaste|namaste)\b\s*[,—\-:.!]*\s*",
        "Hi hello — " if vocal_tone == VocalTone.POLITE_INDIAN_ENGLISH else "Haan hello — ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\bbhaiya\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bji\s*,?\s*ji\b", "ji", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"(?:ek\s+customer\s+ke\s+liye(?:\s+poochh\s+rah[ia]\s+th[ia])?\s*,?\s*|"
        r"(?:calling|checking|asking|inquiring|enquiring)\s+for\s+a\s+customer(?:\s+from\s+[\w\s]+)?\s*,?\s*|"
        r"\bfor\s+a\s+customer\b\s*,?\s*)",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    if vocal_tone == VocalTone.POLITE_INDIAN_ENGLISH:
        cleaned = re.sub(r"\b(?:ji|namaste)\b\s*,?", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,-")
    return cleaned


_sanitize_option_a_line = sanitize_option_a_line


_SPOKEN_STREET_RE = re.compile(
    r"\b([A-Z0-9][A-Za-z0-9'.&\-]*(?:\s+[A-Za-z0-9'.&\-]+){0,2}\s+"
    r"(?:Road|Rd|Street|St|Marg|Lane|Link\s+Road|Main\s+Road|Avenue|Circle|Chowk|Square|Bandstand|Pali\s+Hill|Carter\s+Road|Hill\s+Road|Linking\s+Road))\b",
    re.IGNORECASE,
)


def spoken_street_landmark(address: str, locality: str = "") -> str:
    """Extract a short spoken street/landmark confidence marker (e.g. 'Linking Road', 'Bandstand') from Maps address."""
    if not address:
        return ""
    clean = re.sub(r"\b\d{6}\b", "", address)
    m = _SPOKEN_STREET_RE.search(clean)
    if m:
        phrase = re.sub(r"\s{2,}", " ", m.group(1)).strip(" ,.-")
        phrase = re.sub(r"\bRd\.?$", "Road", phrase, flags=re.IGNORECASE)
        phrase = re.sub(r"\bSt\.?$", "Street", phrase, flags=re.IGNORECASE)
        words = phrase.split()
        return " ".join(words[-3:])
    parts = [p.strip() for p in clean.split(",") if p.strip()]
    loc_low = (locality or "").strip().lower()
    for part in parts[:2]:
        low = part.lower()
        if (
            2 < len(part) <= 24
            and not any(k in low for k in ("india", "maharashtra", "karnataka", "delhi", "shop ", "plot ", "floor"))
            and low != loc_low
            and not part.isdigit()
        ):
            return part
    return ""


def _infer_category_from_goal(
    user_goal: str, explicit: PlaceCategory = PlaceCategory.CUSTOM
) -> PlaceCategory:
    if explicit != PlaceCategory.CUSTOM:
        return explicit
    low = (user_goal or "").lower()
    if any(k in low for k in ("hotel", "room", "check-in", "checkin", "suite", "tariff", "stay")):
        return PlaceCategory.HOTEL
    if any(k in low for k in ("table for", "restaurant", "dinner", "lunch", "buffet", "rooftop", "reservation", "menu", "jain")):
        return PlaceCategory.RESTAURANT
    if any(k in low for k in ("petrol", "xp95", "diesel", "cng", "ev charg", "fuel", "nitrogen")):
        return PlaceCategory.PETROL
    if any(k in low for k in ("mri", "ct ", "ultrasound", "x-ray", "radiology", "scan")):
        return PlaceCategory.DIAGNOSTICS
    if any(k in low for k in ("doctor", "opd", "clinic", "consultation", "dentist")):
        return PlaceCategory.CLINIC
    if any(k in low for k in ("tyre", "tire", "alignment", "battery", "car service", "garage")):
        return PlaceCategory.AUTO
    if any(k in low for k in ("salon", "spa", "haircut", "grooming", "massage")):
        return PlaceCategory.SALON
    if any(k in low for k in ("strip", "tablet", "medicine", "capsule", "pharmacy", "chemist", "augmentin", "istamet")):
        return PlaceCategory.PHARMACY
    return PlaceCategory.CUSTOM


def heuristic_conversational_call_plan(
    user_goal: str,
    vocal_tone: VocalTone,
    place_category: PlaceCategory = PlaceCategory.CUSTOM,
) -> ConversationalCallPlan:
    """Deterministic 0ms category-adaptive local customer call plan (zero telemarketer 'Hello ji namaste')."""
    cat = _infer_category_from_goal(user_goal, place_category)
    item = extract_spoken_item_phrase(user_goal)
    low_goal = user_goal.lower()

    if cat == PlaceCategory.HOTEL:
        t1_hi = f"Haan hello — aaj check-in ke liye room available hai kya ({item})?"
        t2_hi = "Achha — direct phone booking par per-night tariff kitna padega?"
        t1_en = f"Hi hello — checking if you have rooms available for {item}?"
        t2_en = "Got it — what's your direct phone tariff per night, and is early check-in possible?"
        t1_nat = f"Haan hello — kya aaj rukne ke liye kamra khali hai ({item})?"
        t2_nat = "Achha — ek raat ka kiraya kitna lagega?"
    elif cat == PlaceCategory.RESTAURANT:
        t1_hi = f"Haan hello — aaj ke liye table mil jayega kya ({item})?"
        t2_hi = "Achha — waiting kitni hai aur koi cover charge hai kya?"
        t1_en = f"Hi hello — checking if we can get a table ({item})?"
        t2_en = "Got it — is there any wait time or cover charge?"
        t1_nat = f"Haan hello — kya aaj baithne ke liye table mil jayega ({item})?"
        t2_nat = "Achha — kitni der rukna padega?"
    elif cat == PlaceCategory.PETROL:
        t1_hi = f"Haan hello — hum 5 minute door hain, aapke pump par {item} abhi chalu hai kya?"
        t2_hi = "Achha — aur card/UPI aur hawa wala counter bhi chalu hai na?"
        t1_en = f"Hi hello — we're 5 minutes away, is {item} currently active at your station?"
        t2_en = "Got it — and is there any queue right now?"
        t1_nat = f"Haan hello — hum paas mein hi hain, kya abhi {item} chalu hai?"
        t2_nat = "Achha — bheed toh zyada nahi hai na?"
    elif cat == PlaceCategory.CLINIC:
        t1_hi = f"Haan hello — aaj OPD mein {item} ka appointment mil jayega kya?"
        t2_hi = "Achha — consultation fee kitni hai aur kis time aa sakte hain?"
        t1_en = f"Hi hello — checking if an OPD slot is open today for {item}?"
        t2_en = "Got it — what's the consultation fee and available timing?"
        t1_nat = f"Haan hello — kya aaj doctor sahab se milne ka samay mil jayega ({item})?"
        t2_nat = "Achha — parchi ki fees kitni hai?"
    elif cat == PlaceCategory.DIAGNOSTICS or any(
        k in low_goal for k in ("mri", "scan", "ct ", "ultrasound", "x-ray", "slot", "appointment")
    ):
        t1_hi = f"Haan hello — aaj {item} ka slot mil jayega kya?"
        t2_hi = "Achha — iska walk-in cash price kitna hai?"
        t1_en = f"Hi hello — do you have a slot today for {item}?"
        t2_en = "Got it — what's the walk-in cash price for that?"
        t1_nat = f"Haan hello — aaj {item} ka slot khali hai kya?"
        t2_nat = "Achha — iska walk-in charge kitna lagega?"
    elif cat in (PlaceCategory.SPA, PlaceCategory.SALON):
        t1_hi = f"Haan hello — aaj shaam ko {item} ke liye slot khali hai kya?"
        t2_hi = "Achha — iska charge kitna padega?"
        t1_en = f"Hi hello — do you have an appointment slot open today for {item}?"
        t2_en = "Got it — how much do you charge for that?"
        t1_nat = f"Haan hello — aaj {item} ke liye samay mil jayega kya?"
        t2_nat = "Achha — iska charge kitna hai?"
    elif cat == PlaceCategory.AUTO or "tyre" in low_goal or "tire" in low_goal:
        t1_hi = f"Haan hello — hum paas se bol rahe hain, aapke paas {item} abhi mil jayega kya?"
        t2_hi = (
            "Achha — alignment ke saath ek tyre kitne ka padega?"
            if ("tyre" in low_goal or "tire" in low_goal)
            else "Achha — fitting samet kitna kharcha aayega?"
        )
        t1_en = f"Hi hello — we're nearby, do you have {item} available right now?"
        t2_en = (
            "Got it — what's the per-tyre price with alignment?"
            if ("tyre" in low_goal or "tire" in low_goal)
            else "Got it — how much would that cost including service?"
        )
        t1_nat = f"Haan hello — kya aapke yahan abhi {item} ho jayega?"
        t2_nat = "Achha — kul kitna kharcha aayega?"
    else:
        t1_hi = f"Haan hello — aapke paas {item} mil jayega kya?"
        t2_hi = "Achha — ek strip kitne ka padega?" if "strip" in low_goal else "Achha — yeh kitne ka aata hai?"
        t1_en = f"Hi hello — do you have {item} in stock right now?"
        t2_en = "Got it — how much is one strip?" if "strip" in low_goal else "Got it — how much does that cost?"
        t1_nat = f"Haan hello — aapke paas abhi {item} hai kya?"
        t2_nat = "Achha — ek patti kitne rupaye ki hai?" if "strip" in low_goal else "Achha — iska daam kitna hai?"

    if vocal_tone == VocalTone.NATURAL_HINDI:
        return ConversationalCallPlan(
            vocal_tone=vocal_tone,
            spoken_item_short=item,
            turn1_availability_question=t1_nat,
            turn2_price_question=t2_nat,
            turn3_closing="Theek hai, shukriya!",
        )
    if vocal_tone == VocalTone.CONVERSATIONAL_HINGLISH:
        return ConversationalCallPlan(
            vocal_tone=vocal_tone,
            spoken_item_short=item,
            turn1_availability_question=t1_hi,
            turn2_price_question=t2_hi,
            turn3_closing="Achha theek hai, thank you!",
        )
    return ConversationalCallPlan(
        vocal_tone=vocal_tone,
        spoken_item_short=item,
        turn1_availability_question=t1_en,
        turn2_price_question=t2_en,
        turn3_closing="Okay got it, thanks so much!",
    )


async def compile_conversational_call_plan(
    user_goal: str,
    vocal_tone: VocalTone = VocalTone.CONVERSATIONAL_HINGLISH,
    place_category: PlaceCategory = PlaceCategory.CUSTOM,
    client: genai.Client | None = None,
) -> ConversationalCallPlan:
    """Rewrite a written user prompt into a casual, category-authentic Indian phone plan."""
    cat = _infer_category_from_goal(user_goal, place_category)
    fallback = heuristic_conversational_call_plan(user_goal, vocal_tone, place_category=cat)
    clean_goal = redact_phone_digits(user_goal)

    if vocal_tone == VocalTone.NATURAL_HINDI:
        tone_instruction = (
            "everyday spoken Hindi (`hi-IN`, e.g. 'Haan hello — aapke paas abhi ... hai kya?', "
            "'Achha, ek patti kitne rupaye ki hai?', 'Theek hai, shukriya!'). "
            "NEVER say 'Hello ji, namaste' or 'bhaiya' or 'ek customer ke liye'."
        )
    elif vocal_tone == VocalTone.CONVERSATIONAL_HINGLISH:
        tone_instruction = (
            "casual urban Mumbai/Bengaluru Hinglish (`hi-IN`, e.g. 'Haan hello — aapke paas ... mil jayega kya?', "
            "'Achha — ek strip kitne ka padega?', 'Achha theek hai, thank you!'). "
            "NEVER say 'Hello ji, namaste' (sounds like a spam sales call), NEVER say 'bhaiya', and NEVER say 'ek customer ke liye'."
        )
    else:
        tone_instruction = (
            "100% natural, casual Indian English (`en-IN`, e.g. 'Hi hello — do you have ... in stock right now?', "
            "'Got it — how much is one strip?', 'Okay got it, thanks so much!'). "
            "Do NOT mix Hindi words ('bhaiya', 'ji', 'namaste') into English mode, and NEVER say 'calling for a customer'."
        )

    prompt = (
        f"Convert this written user request into a natural, casual local buyer 2-turn phone call plan in {tone_instruction}\n"
        f"Written request: {clean_goal!r}\n\n"
        f"CRITICAL RULES:\n"
        f"1. NEVER copy UI prompt phrasing like 'Check which open medical store has...' or 'confirm cash price in ₹'.\n"
        f"2. NEVER open with 'Hello ji, namaste' or 'Good afternoon sir/ma'am' — shopkeepers hang up thinking it's a telemarketing bot. Start casually with 'Haan hello —' (Hindi/Hinglish) or 'Hi hello —' (Indian English).\n"
        f"3. NEVER say 'bhaiya' and NEVER say 'for a customer / ek customer ke liye'.\n"
        f"4. `turn1_availability_question` (<13 words): Casual hello + ONLY ask if the item or today's slot is available. Do NOT ask price in Turn 1.\n"
        f"5. `turn2_price_question` (<10 words): Brief natural acknowledgment ('Achha' / 'Got it') without 'thank you' + ask exact ₹ price / unit.\n"
        f"6. `turn3_closing` (<7 words): Casual quick thank-you ('Achha theek hai, thank you!' / 'Okay got it, thanks so much!').\n"
        f"7. Never include any phone numbers."
    )
    if os.environ.get("PYTEST_CURRENT_TEST") and client is None:
        return fallback
    try:
        genai_client = client or get_vertex_client(GCP_LOCATION)
        resp = await genai_client.aio.models.generate_content(
            model=SIDE_BRAIN_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.15,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                response_mime_type="application/json",
                response_schema=ConversationalCallPlan,
            ),
        )
        if resp.text:
            plan = ConversationalCallPlan.model_validate_json(resp.text)
            t1 = _sanitize_option_a_line(plan.turn1_availability_question, vocal_tone)
            t2 = _sanitize_option_a_line(plan.turn2_price_question, vocal_tone)
            t3 = _sanitize_option_a_line(plan.turn3_closing, vocal_tone)
            return ConversationalCallPlan(
                vocal_tone=vocal_tone,
                spoken_item_short=redact_phone_digits(plan.spoken_item_short) or fallback.spoken_item_short,
                turn1_availability_question=t1 or fallback.turn1_availability_question,
                turn2_price_question=t2 or fallback.turn2_price_question,
                turn3_closing=t3 or fallback.turn3_closing,
            )
    except Exception as exc:
        logger.info("Using deterministic conversational call plan (%s)", exc)
    return fallback


def _inject_confidence_markers(
    opening_line: str,
    shop_name: str,
    shop_address: str,
    locality: str,
    caller_name: str,
    vocal_tone: VocalTone,
    is_female_voice: bool = False,
) -> str:
    """Inject Street/Landmark + Caller First Name confidence markers into Turn 1 greeting."""
    brand = spoken_shop_brand(shop_name)
    if not brand:
        return opening_line
    street = spoken_street_landmark(shop_address, locality)
    safe_caller = re.sub(r"[^A-Za-z ]", "", (caller_name or "").strip())[:24].strip()

    if vocal_tone == VocalTone.POLITE_INDIAN_ENGLISH:
        loc_tag = f" on {street}" if street and street.lower() not in brand.lower() else ""
        name_tag = f"{safe_caller} here — " if safe_caller else ""
        prefix = f"Hi hello, {brand}{loc_tag}? {name_tag}"
    else:
        loc_tag = f" {street} wala" if street and street.lower() not in brand.lower() else ""
        verb_bol = "bol rahi hoon" if is_female_voice else "bol raha hoon"
        name_tag = f"{safe_caller} {verb_bol} — " if safe_caller else ""
        prefix = f"Haan hello, {brand}{loc_tag}? {name_tag}"

    greeting_re = re.compile(r"^(?:haan\s+hello|hi\s*,?\s*hello|hello)\b\s*[—\-:,]*\s*", re.IGNORECASE)
    if greeting_re.search(opening_line):
        return greeting_re.sub(prefix, opening_line, count=1)
    return f"{prefix}{opening_line}"


def build_agent_disclosure_opening(
    shop_name: str,
    user_goal: str,
    vocal_tone: VocalTone,
    is_live_pstn: bool = False,
    call_plan: ConversationalCallPlan | None = None,
    shop_address: str = "",
    locality: str = "",
    caller_name: str = "",
) -> str:
    """Return natural, human-paced Turn 1 opening with street-landmark & caller-name confidence markers."""
    plan = call_plan or heuristic_conversational_call_plan(user_goal, vocal_tone)
    return _inject_confidence_markers(
        opening_line=plan.turn1_availability_question,
        shop_name=shop_name,
        shop_address=shop_address,
        locality=locality,
        caller_name=caller_name,
        vocal_tone=vocal_tone,
        is_female_voice=False,
    )


def vocal_tone_to_language_code(vocal_tone: VocalTone) -> str:
    """Map RingScout `vocal_tone` to native Vertex Gemini SpeechConfig BCP-47 Indian locale (`hi-IN` or `en-IN`)."""
    if vocal_tone in (VocalTone.CONVERSATIONAL_HINGLISH, VocalTone.NATURAL_HINDI):
        return "hi-IN"
    return "en-IN"


def normalize_live_voice_name(voice_name: str | None) -> str:
    """Return a validated Gemini Live prebuilt voice name (defaulting to casual Indian customer 'Puck')."""
    cand = (voice_name or "").strip().title()
    return cand if cand in SUPPORTED_LIVE_VOICES else DEFAULT_LIVE_VOICE


def build_thin_live_config(
    shop_name: str,
    locality: str,
    user_goal: str,
    vocal_tone: VocalTone = VocalTone.CONVERSATIONAL_HINGLISH,
    is_live_pstn: bool = False,
    call_plan: ConversationalCallPlan | None = None,
    voice_name: str = DEFAULT_LIVE_VOICE,
    shop_address: str = "",
    place_category: PlaceCategory = PlaceCategory.CUSTOM,
    caller_name: str = "",
) -> types.LiveConnectConfig:
    """Build Thin Live Socket config — ZERO tools attached, `gemini-3.8-live` `en-IN`/`hi-IN` locale, confidence markers."""
    clean_goal = redact_phone_digits(user_goal)
    cat = _infer_category_from_goal(clean_goal, place_category)
    plan = call_plan or heuristic_conversational_call_plan(
        clean_goal, vocal_tone, place_category=cat
    )
    lang_code = vocal_tone_to_language_code(vocal_tone)
    resolved_voice = normalize_live_voice_name(voice_name)
    is_female_voice = resolved_voice.lower() in FEMALE_GEMINI_VOICES
    verb_gender_rule = (
        "Use feminine first-person Hindi verbs ('poochh rahi thi', 'bol rahi hoon')."
        if is_female_voice
        else "Use masculine first-person Hindi verbs ('poochh raha tha', 'bol raha hoon')."
    )

    opening_line = _inject_confidence_markers(
        opening_line=plan.turn1_availability_question,
        shop_name=shop_name,
        shop_address=shop_address,
        locality=locality,
        caller_name=caller_name,
        vocal_tone=vocal_tone,
        is_female_voice=is_female_voice,
    )

    if vocal_tone == VocalTone.NATURAL_HINDI:
        tone_guidance = (
            f"Speak natural, everyday spoken Hindi (`hi-IN`) like a local resident calling on mobile — casual, relaxed, slightly brisk, ZERO call-center cheerfulness. "
            f"{verb_gender_rule} NEVER say 'Hello ji, namaste' and NEVER call anyone 'bhaiya'."
        )
    elif vocal_tone == VocalTone.CONVERSATIONAL_HINGLISH:
        tone_guidance = (
            f"Speak casual, everyday Mumbai/Bengaluru Hinglish (`hi-IN`) like a real local customer on mobile ('Haan hello', 'achha', 'ek strip chahiye tha', 'kitne ka padega?', 'theek hai, thank you'). "
            f"ZERO telemarketing/IVR cheerfulness and NEVER say 'Hello ji, namaste' or 'bhaiya'. {verb_gender_rule}"
        )
    else:
        tone_guidance = (
            "Speak casual, natural Indian English (`en-IN` cadence: 'Hi hello', 'just checking', 'got it', 'how much is one strip?', 'okay got it, thanks!'). "
            "Sound like a real local resident calling on mobile — ZERO call-center script tone and NEVER use an American accent. "
            "If the shopkeeper replies in Hindi, smoothly switch to casual Hinglish so the conversation succeeds."
        )

    pstn_extra = (
        "\n6. LIVE PSTN & IVR HANDLING:\n"
        "   - AUTOMATED IVR ONLY: Only if an automated recorded IVR menu is speaking ('Press 1 for...', '2 dabayein'), stay quiet while our keypad handler presses the digit.\n"
        "   - LIVE HUMAN CLARIFICATIONS (ALWAYS RESPOND IMMEDIATELY!): Real Indian chemists & receptionists speak fast over shop noise and ask short questions — ALWAYS answer them right away in 1 crisp natural sentence (<10 words):\n"
        f"     * Repeating/confirming item name ('{plan.spoken_item_short} hai na?', 'Kaunsa bola?'): Confirm clearly ('Haan, {plan.spoken_item_short} — ek strip hai kya?' / 'Yes, {plan.spoken_item_short} — do you have it in stock?').\n"
        "     * Strength / mg / variant question ('50/500 ya 50/1000?', '25mg nahi aata', 'Plain ya contrast?'): Answer directly from the goal context; if the exact mg isn't made and they name an available strength (e.g. 50/500), ask if that one is in stock and its ₹ price!\n"
        "     * Quantity question ('Kitna goli / kitna strip chahiye?'): Say 'Bas ek strip chahiye' / 'Just one strip' (or 'Just one slot today' for scans).\n"
        "     * Substitute brand offered ('Woh nahi hai, doosra brand hai'): Ask 'Achha kaunsa brand hai, aur kitne ka aata hai?' / 'Which one do you have, and how much is it?'.\n"
        "     * Shelf-check hold ('ek minute / hold karo / dekhta hoon'): Say 'Haan sure, dekh lijiye' / 'Sure, please check' once, then wait.\n"
        f"     * Line/audibility check ('Hello? Awaaz aa rahi hai?'): Never restart with a formal greeting — say 'Haan hello, awaaz aa rahi hai — {plan.spoken_item_short} mil jayega kya?'."
        if is_live_pstn
        else ""
    )

    system_prompt = (
        f"You are calling '{shop_name}' in {locality} to check live availability and walk-in ₹ price for: {clean_goal}\n"
        f"CONVERSATION PLAYBOOK (SOUND 100% LIKE A REAL LOCAL BUYER, NEVER A SALES BOT):\n"
        f"1. ANTI-SPAM OPENING (<2.5s): NEVER say 'Hello ji, namaste', 'Good afternoon sir', or 'for a customer'. Open casually:\n"
        f'   - Turn 1 (Availability first): "{opening_line}"\n'
        f"2. PRICE FOLLOW-UP (<2s): Once they confirm it's available without mentioning price, ask:\n"
        f'   - Turn 2: "{plan.turn2_price_question}" (Skip Turn 2 if they already gave the ₹ price or said it is out of stock!).\n'
        f"3. QUICK NATURAL CLOSE (<1.5s): Once you have availability + ₹ price (or confirmed out-of-stock / next slot), close warmly and stop:\n"
        f'   - Turn 3: "{plan.turn3_closing}"\n'
        f"4. NATIVE INDIAN VOICE & LOCALE (`language_code={lang_code}`, `vocal_tone={vocal_tone.value}`, `voice={resolved_voice}`): {tone_guidance}\n"
        f"5. HONESTY IF ASKED WHO IS CALLING: If they explicitly ask who is speaking, say 'Checking from RingScout AI assistant for walk-in availability and rate.' Never invent fake names or phone numbers.{pstn_extra}"
    )

    # Thin Live Socket invariant: `tools` MUST remain unset / None on LiveConnectConfig.
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            language_code=lang_code,
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=resolved_voice)
            ),
        ),
        system_instruction=types.Content(parts=[types.Part(text=system_prompt)]),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                silence_duration_ms=320,
            )
        ),
    )


# --- Deterministic Hinglish / Devanagari / ₹ Price & Signal Fallbacks ---

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

_HINGLISH_BASE_NUMBERS: dict[str, int] = {
    "ek": 1,
    "एक": 1,
    "do": 2,
    "दो": 2,
    "teen": 3,
    "तीन": 3,
    "chaar": 4,
    "char": 4,
    "चार": 4,
    "paanch": 5,
    "panch": 5,
    "पांच": 5,
    "पाँच": 5,
    "chhah": 6,
    "chhe": 6,
    "che": 6,
    "छह": 6,
    "saat": 7,
    "सात": 7,
    "aath": 8,
    "आठ": 8,
    "nau": 9,
    "नौ": 9,
    "das": 10,
    "दस": 10,
    "gyarah": 11,
    "ग्यारह": 11,
    "barah": 12,
    "बारह": 12,
    "pandra": 15,
    "pandrah": 15,
    "पंद्रह": 15,
    "bees": 20,
    "बीस": 20,
    "pachees": 25,
    "pachis": 25,
    "पच्चीस": 25,
    "tees": 30,
    "तीस": 30,
    "paintees": 35,
    "chalees": 40,
    "chalis": 40,
    "pachaas": 50,
    "pachas": 50,
    "पचास": 50,
    "saath": 60,
    "sattar": 70,
    "assi": 80,
    "nabbe": 90,
}


def extract_hinglish_inr_amount(text: str) -> float | None:
    """Deterministically parse ₹ INR amounts from English, Hinglish, or Devanagari speech."""
    if not text:
        return None
    norm = text.translate(_DEVANAGARI_DIGITS).lower().strip()
    norm_clean = re.sub(r"[,]", "", norm)

    # 1. Explicit currency symbol / word patterns: ₹420, rs. 1,450, 450 rupees/rupaye/bucks/inr
    patterns = [
        r"(?:₹|rs\.?|inr)\s*(\d+(?:\.\d{1,2})?)",
        r"(\d+(?:\.\d{1,2})?)\s*(?:₹|rs\.?|inr|rupees?|rupaye|rupaya|rupiya|bucks|रुपये|रुपए|का\s+है|लगेगा|पड़ेगा)",
    ]
    for pat in patterns:
        m = re.search(pat, norm_clean, flags=re.IGNORECASE)
        if m:
            val = float(m.group(1))
            if 5.0 <= val <= 500_000.0:
                return val

    # 2. Compound & fractional Hinglish / Hindi idioms — evaluate hazaar (1000) BEFORE sau (100)
    def _tok_val(tok: str) -> float:
        return float(tok) if tok.isdigit() else float(_HINGLISH_BASE_NUMBERS.get(tok, 0))

    # Compound '<X> hazaar <Y> sau' (e.g. 'chhah hazaar chaar sau' -> 6400)
    m_comp = re.search(
        r"(?<!\S)([^\s,.;!?]+)\s+(?:hazaar|hazar|हज़ार|हजार)\s+([^\s,.;!?]+)\s+(?:sau|सौ)(?:\s+(\d+))?(?!\w)",
        norm_clean,
    )
    if m_comp:
        v_thou = _tok_val(m_comp.group(1))
        v_hund = _tok_val(m_comp.group(2))
        extra = float(m_comp.group(3)) if m_comp.group(3) else 0.0
        if v_thou > 0 and v_hund > 0:
            return v_thou * 1000.0 + v_hund * 100.0 + extra

    mult_list = [
        ("hazaar", 1000.0),
        ("hazar", 1000.0),
        ("हज़ार", 1000.0),
        ("हजार", 1000.0),
        ("sau", 100.0),
        ("सौ", 100.0),
    ]
    for unit_word, mult in mult_list:
        if re.search(rf"(?<!\S)(?:dedh|डेढ़)\s+{unit_word}(?!\w)", norm_clean):
            return 1.5 * mult
        if re.search(rf"(?<!\S)(?:dhai|dhaai|ढाई)\s+{unit_word}(?!\w)", norm_clean):
            return 2.5 * mult
        if re.search(rf"(?<!\S)(?:sawa|सवा)\s+{unit_word}(?!\w)", norm_clean):
            return 1.25 * mult

        m_frac = re.search(
            rf"(?<!\S)(saadhe|sadhe|साढ़े|साढे|sawa|सवा|paune|पौने)\s+([^\s,.;!?]+)\s+{unit_word}(?!\w)",
            norm_clean,
        )
        if m_frac:
            mod, base_tok = m_frac.group(1), m_frac.group(2)
            base_val = _tok_val(base_tok)
            if base_val > 0:
                if mod in ("saadhe", "sadhe", "साढ़े", "साढे"):
                    return (base_val + 0.5) * mult
                if mod in ("sawa", "सवा"):
                    return (base_val + 0.25) * mult
                if mod in ("paune", "पौने"):
                    return (base_val - 0.25) * mult

        m_whole = re.search(rf"(?<!\S)([^\s,.;!?]+)\s+{unit_word}(?:\s+(\d+))?(?!\w)", norm_clean)
        if m_whole:
            base_val = _tok_val(m_whole.group(1))
            extra = float(m_whole.group(2)) if m_whole.group(2) else 0.0
            if base_val > 0:
                return base_val * mult + extra

    return None


_SPOKEN_KEYPAD_WORDS: tuple[tuple[str, str], ...] = (
    (r"\b(?:zero|shunya|शून्य)\b", "0"),
    (r"\b(?:one|ek|एक)\b", "1"),
    (r"\b(?:two|दो|दोन)\b", "2"),
    # Only treat Latin 'do'/'don' as '2' when adjacent to keypad verbs (never English verb 'do')
    (r"\b(?:do|don)\b(?=\s*(?:dabay|dabaye|dial|डायल|दबा))", "2"),
    (r"\b(?:three|teen|तीन)\b", "3"),
    (r"\b(?:four|chaar|char|चार)\b", "4"),
    (r"\b(?:five|paanch|panch|पांच|पाँच)\b", "5"),
    (r"\b(?:six|chhah|chhe|che|छह)\b", "6"),
    (r"\b(?:seven|saat|सात)\b", "7"),
    (r"\b(?:eight|aath|आठ)\b", "8"),
    (r"\b(?:nine|nau|नौ)\b", "9"),
    (r"\b(?:star|asterisk)\b", "*"),
    (r"\b(?:hash|pound)\b", "#"),
)


def normalize_ivr_spoken_digits(text: str) -> str:
    """Convert spoken English/Hindi/Marathi keypad words ('press one', 'press zero', 'teen dial karein') to digits."""
    norm = (text or "").translate(_DEVANAGARI_DIGITS).lower()
    for pat, digit in _SPOKEN_KEYPAD_WORDS:
        norm = re.sub(pat, digit, norm)
    return norm


def is_ivr_or_hold_announcement(text: str) -> bool:
    """Deterministically classify automated hospital/clinic IVR prompts & PBX queue recordings (never human greetings)."""
    if not text:
        return False
    norm = normalize_ivr_spoken_digits(text)
    has_keypad = bool(
        re.search(r"\b(?:press|dial)\s*[0-9#*]\b", norm)
        or re.search(
            r"\b[0-9#*]\s+for\s+(?:opd|admission|casualty|radiology|appointment|english|hindi|operator)\b",
            norm,
        )
        or any(
            k in norm
            for k in (
                "dabayein",
                "dabaye",
                "दबाएं",
                "दबायें",
                "डायल करें",
                "डायल करा",
                "dial kara",
                "dial kare",
            )
        )
    )
    if has_keypad:
        return True

    # If a live receptionist says 'Welcome to X, how may I help you?', that is a HUMAN, not an IVR!
    if any(h in norm for h in ("how may i help", "how can i help", "speaking")):
        return False

    automated_pbx_markers = (
        "more than healthcare",
        "extension number",
        "desired extension",
        "operator assistance",
        "operator will assist",
        "assist you shortly",
        "your call is important",
        "all our executives",
        "for information in english",
        "jankari ke liye",
        "जानकारी के लिए",
        "माहितीसाठी",
    )
    return any(m in norm for m in automated_pbx_markers)


def is_live_human_pickup_after_ivr(
    text: str, allow_short_utterance_fallback: bool = False
) -> bool:
    """Return True when a live receptionist picks up after an IVR transfer (never on Stage-2 IVR prefixes)."""
    if not text or is_ivr_or_hold_announcement(text):
        return False
    clean = text.strip().lower()
    words = clean.split()
    if not words or len(words) > 16:
        return False
    ivr_sentence_starters = (
        "please",
        "kindly",
        "for ",
        "to ",
        "or ",
        "press",
        "dial",
        "thank you for",
        "welcome",
        "your ",
        "all ",
        "if ",
        "kripya",
        "कृपया",
    )
    if clean.startswith(ivr_sentence_starters):
        return False
    explicit_human_greetings = (
        "hello",
        "hi ",
        "hi,",
        "hi.",
        "good morning",
        "good afternoon",
        "good evening",
        "namaste",
        "haan",
        "speaking",
        "how may i help",
        "how can i help",
        "boliye",
        "जी",
        "हेलो",
        "हैलो",
        "हाँ",
        "हां",
        "बोलिए",
        "नमस्ते",
    )
    if any(cue in clean for cue in explicit_human_greetings):
        return True
    if allow_short_utterance_fallback and len(words) <= 6:
        dept_pickups = ("hospital", "radiology", "reception", "diagnostics", "clinic", "pharmacy", "ji")
        return any(d in clean for d in dept_pickups)
    return False


def extract_ivr_dtmf_digit(
    text: str,
    preferred_tone: VocalTone = VocalTone.CONVERSATIONAL_HINGLISH,
    user_goal: str = "",
    already_sent: Sequence[str] = (),
) -> str | None:
    """Deterministically extract IVR keypad digit from English/Hindi/Marathi prompts (digits OR spoken words)."""
    if not text:
        return None
    lower = normalize_ivr_spoken_digits(text)
    goal_low = (user_goal or "").lower()
    sent_set = set(already_sent)

    dept_priority: list[str] = []
    if any(k in goal_low for k in ("mri", "scan", "ct ", "radiology", "x-ray", "ultrasound")):
        dept_priority.extend(["radiology", "mri", "imaging", "scan", "appointment", "booking"])
    elif any(k in goal_low for k in ("hotel", "room", "check-in", "tariff", "stay")):
        dept_priority.extend(["reservations", "reservation", "room", "mumbai", "bengaluru", "delhi", "hyderabad", "pune", "goa", "front desk"])
    elif any(k in goal_low for k in ("medicine", "strip", "tablet", "pharmacy", "chemist", "augmentin")):
        dept_priority.extend(["pharmacy", "chemist", "medicine", "counter"])

    if preferred_tone in (VocalTone.CONVERSATIONAL_HINGLISH, VocalTone.NATURAL_HINDI):
        dept_priority.extend(
            ["hindi", "operator", "assistance", "advisor", "reception", "counter", "enquiry", "inquiry", "opd", "sales", "english"]
        )
    else:
        dept_priority.extend(
            ["english", "operator", "assistance", "advisor", "appointment", "reception", "counter", "enquiry", "inquiry", "opd", "sales"]
        )

    candidates: list[str] = []
    for kw in dept_priority:
        for pat in (
            rf"\b{kw}\b[^.?!,\d]{{0,42}}?(?:press|dial)\s*([0-9#*])",
            rf"(?:press|dial)\s*([0-9#*])\s*\.?\s*(?:for|to)\b[^?!\d]{{0,42}}?\b{kw}\b",
            rf"\b([0-9#*])\s+for\b[^.?!,\d]{{0,30}}?\b{kw}\b",
            rf"\b{kw}\b[^.?!,\d]{{0,42}}?([0-9#*])\s*(?:dabay|दबाएं|दबायें|dial\s*kar|डायल)",
        ):
            m = re.search(pat, lower)
            if m:
                candidates.append(m.group(1))

    for generic_pat in (
        r"(?:to\s+speak\s+with|for\s+appointments?|for\s+operator|for\s+assistance)[^.?!\d]{0,42}?(?:press|dial)\s*([0-9#*])",
        r"(?:press|dial)\s*([0-9#*])\s*(?:for|to\s+speak)",
        r"ke\s+liye\s*([0-9#*])\s*(?:dabay|दबाएं|दबायें|dial\s*kar|डायल)",
    ):
        m = re.search(generic_pat, lower)
        if m:
            candidates.append(m.group(1))

    for dig in candidates:
        if dig not in sent_set:
            return dig
    return None


def is_agent_farewell_turn(text: str) -> bool:
    """Return True when the voice agent spoke its final closing gratitude turn (never on Beat 2 price questions)."""
    if not text:
        return False
    clean = text.strip().lower()
    if "?" in clean:
        return False
    # Guard against ASR omitting '?' on Beat 2 price / follow-up questions using interrogative/request markers
    followup_question_tokens = (
        "what is",
        "what's",
        "whats",
        "what ",
        "how much",
        "how ",
        "which ",
        "when ",
        "where ",
        "could you",
        "can you",
        "would you",
        "please tell",
        "please share",
        "let me know",
        "kitna",
        "kitne",
        "kitni",
        "kya ",
        "kab ",
        "kaunsa",
        "kaunsi",
        "bata ",
        "batay",
        "do you have",
        "is there",
        "is a slot",
        "mil jayega",
        "mil jayegi",
        "hai kya",
        "uplabdh hai",
        "in stock",
    )
    if any(tok in clean for tok in followup_question_tokens):
        return False
    farewell_markers = (
        "dhanyavaad",
        "dhanyawad",
        "धन्यवाद",
        "shukriya",
        "शुक्रिया",
        "thank you so much",
        "thanks so much",
        "thanks a lot",
        "thank you very much",
        "have a great day",
        "have a good day",
        "thank you",
        "thanks",
    )
    return any(marker in clean for marker in farewell_markers)


def is_human_hold_phrase(text: str) -> bool:
    """Return True when a live shopkeeper/receptionist asks the caller to wait while checking stock/slots."""
    if not text:
        return False
    low = text.strip().lower()
    hold_cues = (
        "ek minute",
        "1 minute",
        "one minute",
        "ruko",
        "rukiye",
        "hold karo",
        "hold kijiye",
        "hold on",
        "just a moment",
        "just a second",
        "let me check",
        "checking",
        "dekhta hoon",
        "dekhti hoon",
        "dekh ke batata",
        "check karke",
        "एक मिनट",
        "रुकिए",
        "देखता हूँ",
        "चेक कर",
    )
    return any(cue in low for cue in hold_cues)


def gate_pstn_background_chatter(
    pcm16_bytes: bytes, state: dict[str, float]
) -> tuple[bytes, float]:
    """Adaptive near-field handset noise gate for noisy Indian streets/chemists.

    - Leaves quiet callers / soft IVRs (`peak_ema < 2200`) 100% untouched so nothing soft is ever clipped.
    - Once loud near-field handset speech (`rms >= 2200`, e.g. 5000-10000 RMS) is established, attenuates
      continuous far-field shop/street background chatter (`< 26%` of speaker RMS after a 160ms hangover tail)
      by -28 dB so Gemini Live's server VAD cleanly closes turns instead of hanging for 20s.
    """
    even_len = len(pcm16_bytes) - (len(pcm16_bytes) % 2)
    if even_len == 0:
        return pcm16_bytes, 0.0
    samples = array("h")
    samples.frombytes(pcm16_bytes[:even_len])
    if not samples:
        return pcm16_bytes, 0.0
    rms = math.sqrt(sum(x * x for x in samples) / len(samples))
    peak = state.get("peak_ema", 0.0)

    # Cap open threshold at 1350 RMS so arm's-length human speech (1500-3000 RMS) is never gated
    # even after a loud 10,000+ RMS pickup or IVR, while 300-850 RMS shop chatter is suppressed.
    open_thresh = min(1350.0, max(900.0, peak * 0.24))
    if rms >= open_thresh:
        state["peak_ema"] = min(12000.0, (0.76 * peak + 0.24 * rms) if peak > 0.0 else rms)
        if rms >= 900.0:
            state["hangover"] = 8.0  # 8 * 20ms = 160ms natural consonant release tail
        return pcm16_bytes, rms

    # Never gate quiet lines where loud near-field speech hasn't been established yet
    if peak < 2200.0:
        return pcm16_bytes, rms

    hang = state.get("hangover", 0.0)
    if hang > 0.0:
        state["hangover"] = hang - 1.0
        return pcm16_bytes, rms

    state["peak_ema"] = max(0.0, peak * 0.992)
    attenuated = array("h", (int(x * 0.04) for x in samples))
    return attenuated.tobytes(), rms * 0.04


def _default_availability_label(
    in_stock: bool | None, cat: PlaceCategory, eta_or_slot: str | None = None
) -> str:
    if in_stock is True:
        labels = {
            PlaceCategory.HOTEL: "Room Available",
            PlaceCategory.RESTAURANT: "Table Available",
            PlaceCategory.CLINIC: "OPD Slot Open",
            PlaceCategory.DIAGNOSTICS: "Scan Slot Open",
            PlaceCategory.PETROL: "Available Now",
            PlaceCategory.AUTO: "Available Now",
            PlaceCategory.SALON: "Slot Available",
            PlaceCategory.PHARMACY: "In Stock",
        }
        return labels.get(cat, "Confirmed Available")
    if in_stock is False:
        return "Waitlist / Next Slot" if eta_or_slot else "Unavailable Today"
    return "Checking / Partial"


def heuristic_side_brain_fallback(
    transcript: list[TranscriptTurn],
    preferred_tone: VocalTone = VocalTone.CONVERSATIONAL_HINGLISH,
    user_goal: str = "",
    place_category: PlaceCategory = PlaceCategory.CUSTOM,
) -> SideBrainDecision:
    """Fast deterministic fallback for unit tests or transient API hiccups."""
    cat = _infer_category_from_goal(user_goal, place_category)
    shop_lines = " ".join(t.text for t in transcript if t.speaker == "shopkeeper")
    lower = shop_lines.lower()

    is_voicemail = any(
        kw in lower
        for kw in (
            "leave a message after the beep",
            "voicemail",
            "switched off",
            "abhi vyast hai",
            "sampark nahi ho pa raha",
            "currently unavailable",
        )
    )
    if is_voicemail:
        return SideBrainDecision(
            vocal_tone=preferred_tone,
            in_stock=None,
            is_voicemail=True,
            should_hangup=True,
            summary_notes="Carrier voicemail / unavailable line detected; bailed out cleanly.",
        )

    dtmf_digit = extract_ivr_dtmf_digit(shop_lines, preferred_tone=preferred_tone, user_goal=user_goal)
    price_inr = extract_hinglish_inr_amount(shop_lines)

    out_of_stock = any(
        kw in lower
        for kw in (
            "out of stock",
            "not in stock",
            "stock mein nahi",
            "khatam ho gaya",
            "available nahi",
            "no slot",
            "don't have slot",
            "do not have slot",
            "slot full",
            "nahi milega",
            "not available",
        )
    )
    in_stock_pos = any(
        kw in lower
        for kw in (
            "in stock",
            "stock mein hai",
            "mil jayega",
            "available hai",
            "haan ji hai",
            "bilkul hai",
            "slot available",
            "ready hai",
        )
    )

    in_stock: bool | None = None
    if out_of_stock:
        in_stock = False
    elif in_stock_pos or price_inr is not None:
        in_stock = True

    eta_or_slot: str | None = None
    for alt_pat in (
        r"(day after tomorrow)",
        r"(tomorrow\s*(?:morning|afternoon|evening)?)",
        r"(kal\s*(?:subah|dopahar|shaam)?)",
        r"(parson\s*(?:subah|dopahar|shaam)?)",
    ):
        m_alt = re.search(alt_pat, lower)
        if m_alt:
            eta_or_slot = f"Next slot: {m_alt.group(1).strip()}" if in_stock is False else m_alt.group(1).strip()
            break

    last_agent = next((t.text for t in reversed(transcript) if t.speaker == "agent"), "")
    should_hangup = (
        (in_stock is False)
        or (in_stock is True and price_inr is not None)
        or (len(transcript) >= 2 and is_agent_farewell_turn(last_agent))
    )
    notes = shop_lines.strip()[-140:] if shop_lines.strip() else "Awaiting shopkeeper response."

    avail_lbl = _default_availability_label(in_stock, cat, eta_or_slot)
    insights = [s.strip() for s in re.split(r"[.!?]+", shop_lines) if len(s.strip()) > 6][:3]

    return SideBrainDecision(
        vocal_tone=preferred_tone,
        in_stock=in_stock,
        availability_label=avail_lbl,
        price_inr=price_inr,
        eta_or_slot=eta_or_slot,
        dtmf_digit=dtmf_digit,
        is_voicemail=False,
        should_hangup=should_hangup,
        summary_notes=notes,
        key_insights=insights,
    )


async def evaluate_transcript_side_brain(
    user_goal: str,
    shop_name: str,
    transcript: list[TranscriptTurn],
    preferred_tone: VocalTone = VocalTone.CONVERSATIONAL_HINGLISH,
    place_category: PlaceCategory = PlaceCategory.CUSTOM,
    client: genai.Client | None = None,
) -> SideBrainDecision:
    """Out-of-Band Side-Brain running on `gemini-2.5-flash` at `temperature=0.0`."""
    cat = _infer_category_from_goal(user_goal, place_category)
    fallback = heuristic_side_brain_fallback(
        transcript,
        preferred_tone=preferred_tone,
        user_goal=user_goal,
        place_category=cat,
    )
    if not transcript:
        return fallback

    convo_text = "\n".join(
        f"[{t.timestamp_sec:04.1f}s] {t.speaker.upper()}: {t.text}" for t in transcript
    )
    prompt = (
        f"Analyze this live phone call in India between RingScout AI AGENT and SHOPKEEPER/RECEPTIONIST at '{shop_name}'.\n"
        f"Customer Quest: {user_goal}\n"
        f"Locked Vocal Tone: {preferred_tone.value} (MUST preserve this vocal_tone to prevent accent drift).\n\n"
        f"Transcript so far:\n{convo_text}\n\n"
        f"Instructions:\n"
        f"1. Convert any English, Hinglish, or Hindi spoken rupee price (e.g. 'saadhe chaar sau' -> 450, "
        f"'dedh hazaar' -> 1500, 'two thousand' / '20 hundred' -> 2000, '₹420' -> 420) into numeric `price_inr`. "
        f"Use item domain context (e.g., 1.5T MRI in India is typically ₹2,000–₹12,000; medicine strips ₹30–₹2,000), "
        f"and leave `price_inr=null` if no price was stated.\n"
        f"2. Set `in_stock=true` if available/slot open today, `false` if unavailable/today full, `null` if not yet answered.\n"
        f"3. Populate `eta_or_slot` with the slot timing — INCLUDING alternative availability when today is full "
        f"(e.g., if receptionist says 'No slot today, available day after tomorrow', set `in_stock=false` and "
        f"`eta_or_slot='Next available: day after tomorrow'`).\n"
        f"4. Set `dtmf_digit` ONLY if an automated IVR menu asks to press a keypad digit (match radiology/MRI, pharmacy, or reception/advisor).\n"
        f"5. Set `is_voicemail=true` and `should_hangup=true` if this is carrier voicemail or 'switched off'.\n"
        f"6. Set `should_hangup=true` once availability + price (or explicit no-slot/out-of-stock, or the AGENT's final thank-you closing) is complete."
    )

    if os.environ.get("PYTEST_CURRENT_TEST") and client is None:
        return fallback
    try:
        genai_client = client or get_vertex_client(GCP_LOCATION)
        resp = await genai_client.aio.models.generate_content(
            model=SIDE_BRAIN_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                response_mime_type="application/json",
                response_schema=SideBrainDecision,
            ),
        )
        decision = SideBrainDecision.model_validate_json(resp.text)
        if decision.price_inr is None and fallback.price_inr is not None:
            decision.price_inr = fallback.price_inr
        if not decision.eta_or_slot and fallback.eta_or_slot:
            decision.eta_or_slot = fallback.eta_or_slot
        if (decision.in_stock is True and decision.price_inr is not None) or decision.is_voicemail:
            decision.should_hangup = True
        if fallback.should_hangup and len(transcript) >= 2:
            last_ag = next((t.text for t in reversed(transcript) if t.speaker == "agent"), "")
            if is_agent_farewell_turn(last_ag):
                decision.should_hangup = True
        if not decision.availability_label:
            decision.availability_label = _default_availability_label(
                decision.in_stock, cat, decision.eta_or_slot
            )
        if not decision.key_insights and fallback.key_insights:
            decision.key_insights = fallback.key_insights
        decision.vocal_tone = preferred_tone
        return decision
    except Exception as exc:
        logger.warning("Side-Brain LLM call fell back to deterministic parser: %s", exc)
        return fallback


def decision_to_inr_quote(decision: SideBrainDecision) -> InrQuote:
    """Format a SideBrainDecision into a UI-ready InrQuote."""
    price_display: str | None = None
    if decision.price_inr is not None:
        if float(decision.price_inr).is_integer():
            price_display = f"₹{int(decision.price_inr):,}"
        else:
            price_display = f"₹{decision.price_inr:,.2f}"

    return InrQuote(
        in_stock=decision.in_stock,
        availability_label=decision.availability_label,
        price_inr=decision.price_inr,
        price_display=price_display,
        unit_or_pack=decision.unit_or_pack,
        eta_or_slot=decision.eta_or_slot,
        notes=decision.summary_notes,
        key_insights=decision.key_insights,
        vocal_tone=decision.vocal_tone,
    )


def pcm16_to_wav_bytes(pcm_bytes: bytes, sample_rate: int = 24000, channels: int = 1) -> bytes:
    """Wrap raw 16-bit signed little-endian PCM bytes into a standard RIFF `.wav` container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def generate_comfort_bed_pcm24(duration_sec: float = 0.14, level: int = 85) -> bytes:
    """Generate warm low-level (-48 dBFS) analog handset room tone so PSTN pauses never sound dead."""
    n_samples = max(1, int(24000 * duration_sec))
    out = array("h")
    state = 0x5A3C91
    prev = 0
    for _ in range(n_samples):
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        raw = ((state >> 16) % (2 * level + 1)) - level
        smoothed = (prev * 3 + raw) // 4
        prev = smoothed
        out.append(smoothed)
    return out.tobytes()


def add_pstn_comfort_noise_pcm24(pcm24: bytes, level: int = 70) -> bytes:
    """Mix subtle (-50 dBFS) warm telephone comfort tone into 24kHz PCM16 speech chunks."""
    even_len = len(pcm24) - (len(pcm24) % 2)
    if even_len == 0:
        return b""
    samples = array("h")
    samples.frombytes(pcm24[:even_len])
    state = 0x1F2E3D
    prev = 0
    for idx in range(len(samples)):
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        raw = ((state >> 16) % (2 * level + 1)) - level
        prev = (prev * 3 + raw) // 4
        mixed = samples[idx] + prev
        samples[idx] = -32768 if mixed < -32768 else (32767 if mixed > 32767 else mixed)
    return samples.tobytes()


def generate_busy_tone_wav(duration_sec: float = 2.4, sample_rate: int = 24000) -> bytes:
    """Synthesize an authentic Indian PSTN 400Hz busy cadence WAV clip for unanswered/busy lines."""
    total_samples = int(duration_sec * sample_rate)
    frames = bytearray()
    for i in range(total_samples):
        t = i / sample_rate
        # Standard 400Hz busy cadence: 0.375s ON, 0.375s OFF
        on_cycle = (t % 0.75) < 0.375
        sample = int(8500 * math.sin(2.0 * math.pi * 400.0 * t)) if on_cycle else 0
        frames.extend(struct.pack("<h", sample))
    return pcm16_to_wav_bytes(bytes(frames), sample_rate=sample_rate)


async def synthesize_dialogue_wav(
    transcript: list[TranscriptTurn],
    shopkeeper_voice: str = "Charon",
    vocal_tone: VocalTone = VocalTone.CONVERSATIONAL_HINGLISH,
    agent_voice: str = DEFAULT_LIVE_VOICE,
    client: genai.Client | None = None,
) -> bytes:
    """Synthesize a real multi-speaker Indian English / Hindi / Hinglish `.wav` recording via Vertex TTS."""
    spoken_turns = [t for t in transcript if t.speaker in ("agent", "shopkeeper") and t.text.strip()]
    if not spoken_turns:
        return generate_busy_tone_wav(1.8)

    lines = []
    for turn in spoken_turns:
        label = "Agent" if turn.speaker == "agent" else "Shopkeeper"
        lines.append(f"{label}: {turn.text.strip()}")
    if vocal_tone == VocalTone.NATURAL_HINDI:
        tone_desc = "casual everyday spoken Hindi (hi-IN)"
    elif vocal_tone == VocalTone.CONVERSATIONAL_HINGLISH:
        tone_desc = "casual everyday urban Indian Hinglish (hi-IN)"
    else:
        tone_desc = "casual natural Indian English (en-IN)"
    script_text = (
        f"TTS the following phone call in {tone_desc} (vocal_tone={vocal_tone.value}) "
        f"with authentic Indian local-buyer cadence (zero call-center polish, never drift into a US accent):\n"
        + "\n".join(lines)
    )

    if os.environ.get("PYTEST_CURRENT_TEST") and client is None:
        return generate_busy_tone_wav(1.5)
    try:
        genai_client = client or get_vertex_client(GCP_LOCATION)
        cfg = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                language_code=vocal_tone_to_language_code(vocal_tone),
                multi_speaker_voice_config=types.MultiSpeakerVoiceConfig(
                    speaker_voice_configs=[
                        types.SpeakerVoiceConfig(
                            speaker="Agent",
                            voice_config=types.VoiceConfig(
                                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                    voice_name=normalize_live_voice_name(agent_voice)
                                )
                            ),
                        ),
                        types.SpeakerVoiceConfig(
                            speaker="Shopkeeper",
                            voice_config=types.VoiceConfig(
                                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                    voice_name=shopkeeper_voice
                                )
                            ),
                        ),
                    ]
                )
            ),
        )
        resp = await genai_client.aio.models.generate_content(
            model=TTS_DIALOGUE_MODEL,
            contents=script_text,
            config=cfg,
        )
        for cand in resp.candidates or []:
            if cand.content and cand.content.parts:
                for part in cand.content.parts:
                    if part.inline_data and part.inline_data.data:
                        return pcm16_to_wav_bytes(part.inline_data.data, sample_rate=24000)
    except Exception as exc:
        logger.warning("Multi-speaker Vertex TTS fallback triggered: %s", exc)

    return generate_busy_tone_wav(1.5)
