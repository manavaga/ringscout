"""Domain models for RingScout India-only Phase 1 / 1.5 Live PSTN POC."""

from __future__ import annotations

from enum import Enum
import time
from typing import Literal
from pydantic import BaseModel, Field


class PlaceCategory(str, Enum):
    """Supported Google Maps business & point-of-interest verticals."""

    HOTEL = "hotel"
    RESTAURANT = "restaurant"
    SPA = "spa"
    SALON = "salon"
    CLINIC = "clinic"
    DIAGNOSTICS = "diagnostics"
    PETROL = "petrol"
    AUTO = "auto"
    PHARMACY = "pharmacy"
    CUSTOM = "custom"


class VocalTone(str, Enum):
    """Explicit voice persona & accent lock (`en-IN` / `hi-IN`) to prevent mid-call US accent drift."""

    POLITE_INDIAN_ENGLISH = "polite_indian_english"
    CONVERSATIONAL_HINGLISH = "conversational_hinglish"
    NATURAL_HINDI = "natural_hindi"


class CarrierProvider(str, Enum):
    """Supported bidirectional WebSocket PSTN carriers for India (+91) calling."""

    VOBIZ = "vobiz"
    PLIVO = "plivo"
    TWILIO = "twilio"


class CallMode(str, Enum):
    """Supported call execution modes."""

    HINGLISH_SWARM = "hinglish_swarm"
    BROWSER_MIC = "browser_mic"
    LIVE_PSTN = "live_pstn"
    TWILIO_PSTN = "twilio_pstn"  # Backwards-compatible alias routed to LiveIndiaPstnAdapter


class CallStatus(str, Enum):
    """Live lifecycle state of a single shop call."""

    QUEUED = "queued"
    RINGING = "ringing"
    IVR_MENU = "ivr_menu"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    STOPPED = "stopped"
    FAILED = "failed"


class CallOutcome(str, Enum):
    """Clean call settlement outcome (ready for Phase 2 billing wrapper)."""

    PENDING = "pending"
    CONNECTED_QUOTE = "connected_quote"
    CONNECTED_NO_STOCK = "connected_no_stock"
    CONNECTED_INCONCLUSIVE = "connected_inconclusive"
    IVR_NAVIGATED = "ivr_navigated"
    VOICEMAIL_SKIPPED = "voicemail_skipped"
    BUSY_NO_ANSWER = "busy_no_answer"
    TIMED_OUT_90S = "timed_out_90s"
    STOPPED_BY_USER = "stopped_by_user"
    ERROR = "error"


class LiveCarrierConfig(BaseModel):
    """Runtime telephony credentials kept strictly in server RAM (never persisted to disk)."""

    provider: CarrierProvider = CarrierProvider.VOBIZ
    auth_id: str = Field(..., min_length=3, max_length=128)
    auth_token: str = Field(..., min_length=6, max_length=256)
    from_number: str = Field(..., min_length=8, max_length=24)
    from_numbers: list[str] = Field(
        default_factory=list,
        description="Optional pool of clean dedicated DIDs rotated across calls to avoid carrier spam flags.",
    )
    public_base_url: str | None = Field(default=None, max_length=300)


class IndianShopTarget(BaseModel):
    """Verified public Indian business listing from Google Maps (or explicit self-test phone)."""

    shop_id: str
    name: str
    phone_e164: str
    phone_display: str
    address: str
    locality: str = ""
    city: str = ""
    open_status: str = "OPEN NOW"
    is_open_now: bool = True
    rating: float | None = None
    maps_uri: str | None = None
    category: str = "Medical Store"
    locality_match_tier: int = 0
    locality_badge: str = ""


class TranscriptTurn(BaseModel):
    """Single timestamped utterance in a call."""

    speaker: Literal["agent", "shopkeeper", "system"]
    text: str
    timestamp_sec: float = 0.0


class InrQuote(BaseModel):
    """Structured call insight & INR quote extracted from a live business conversation."""

    in_stock: bool | None = None
    availability_label: str = ""
    price_inr: float | None = None
    price_display: str | None = None
    unit_or_pack: str | None = None
    eta_or_slot: str | None = None
    notes: str = ""
    key_insights: list[str] = Field(default_factory=list)
    vocal_tone: VocalTone = VocalTone.CONVERSATIONAL_HINGLISH


class SideBrainDecision(BaseModel):
    """Schema enforced at temperature=0.0 on the Out-of-Band Flash Side-Brain."""

    vocal_tone: VocalTone = Field(
        description="Required Indian accent/language lock to prevent mid-call drift."
    )
    in_stock: bool | None = Field(
        default=None,
        description="True if item/room/table/slot/service is available, False if explicitly unavailable, None if unknown.",
    )
    availability_label: str = Field(
        default="",
        description="Short category-aware status badge, e.g. 'Room Available', 'Table Available', 'OPD Slot Open', 'Fuel Active', 'In Stock', 'Waitlist / Next Slot', or 'Unavailable'.",
    )
    price_inr: float | None = Field(
        default=None,
        description="Numeric INR price/tariff/fee (e.g., 4500.0 for '₹4,500 room tariff' or 450.0 for 'saadhe chaar sau').",
    )
    unit_or_pack: str | None = Field(
        default=None,
        description="Unit described by staff, e.g. 'per night + GST', 'cover per person', 'consultation fee', 'per strip'.",
    )
    eta_or_slot: str | None = Field(
        default=None,
        description="Check-in time, table slot, wait time, OPD timing, or pickup readiness.",
    )
    dtmf_digit: str | None = Field(
        default=None,
        description="Single keypad digit ('0'-'9', '*', '#') if an IVR menu asks to press a key.",
    )
    is_voicemail: bool = Field(
        default=False,
        description="True if answered by carrier voicemail or automated 'switched off / busy' recording.",
    )
    should_hangup: bool = Field(
        default=False,
        description="True once availability + rate/details or explicit unavailability is confirmed and polite thank-you is ready.",
    )
    summary_notes: str = Field(
        default="",
        description="Crisp 1-line note summarizing what the staff confirmed.",
    )
    key_insights: list[str] = Field(
        default_factory=list,
        description="Up to 3 crisp verified factual bullets answered on the call (e.g. 'Early check-in at 10 AM free', 'XP95 available').",
    )
    suggested_agent_reply: str | None = Field(
        default=None,
        description="Next natural Hinglish or polite Indian English line for the voice agent.",
    )


class ShopCallState(BaseModel):
    """Full live state of one parallel shop call."""

    shop: IndianShopTarget
    mode: CallMode = CallMode.HINGLISH_SWARM
    status: CallStatus = CallStatus.QUEUED
    outcome: CallOutcome = CallOutcome.PENDING
    vocal_tone: VocalTone = VocalTone.CONVERSATIONAL_HINGLISH
    duration_sec: float = 0.0
    dtmf_sent: list[str] = Field(default_factory=list)
    transcript: list[TranscriptTurn] = Field(default_factory=list)
    quote: InrQuote | None = None
    audio_url: str | None = None
    disclosure_delivered: bool = False
    twilio_call_sid: str | None = None
    carrier_provider: str | None = None


class DiscoverRequest(BaseModel):
    """Request to discover real open places on Google Maps from category + user purpose."""

    query: str = Field(default="", max_length=400)
    purpose: str = Field(default="", max_length=500)
    place_category: PlaceCategory = PlaceCategory.CUSTOM
    specific_place: str = Field(default="", max_length=160)
    city: str = Field(default="Mumbai", min_length=2, max_length=80)
    locality: str = Field(default="Bandra West", max_length=100)
    max_shops: int = Field(default=5, ge=1, le=15)


class DiscoverResponse(BaseModel):
    """Discovered Google Maps places + filtered counts for Approve-Before-Dial UI."""

    query: str
    place_category: PlaceCategory = PlaceCategory.CUSTOM
    city: str
    locality: str
    source: str
    filtered_toll_free_count: int = 0
    filtered_closed_or_invalid_count: int = 0
    shops: list[IndianShopTarget]


class QuestCreateRequest(BaseModel):
    """Approved list of places and user purpose/question to dial in parallel."""

    user_goal: str = Field(..., min_length=3, max_length=500)
    place_category: PlaceCategory = PlaceCategory.CUSTOM
    caller_name: str = Field(default="", max_length=60)
    city: str = Field(default="Mumbai")
    locality: str = Field(default="Bandra West")
    preferred_tone: VocalTone = VocalTone.CONVERSATIONAL_HINGLISH
    voice_name: str = Field(default="Puck", max_length=32)
    mode: CallMode = CallMode.HINGLISH_SWARM
    origin_channel: str = Field(default="web_consumer", max_length=40)
    caller_agent: str = Field(default="Consumer Web UI", max_length=80)
    shops: list[IndianShopTarget] = Field(..., min_length=1, max_length=15)


class CustomPlaceAddRequest(BaseModel):
    """Add a specific named Google Maps place OR user-known business +91 phone to the call list."""

    place_name: str = Field(..., min_length=2, max_length=160)
    phone: str = Field(default="", max_length=32)
    city: str = Field(default="Mumbai", min_length=2, max_length=80)
    locality: str = Field(default="Bandra West", max_length=100)
    place_category: PlaceCategory = PlaceCategory.CUSTOM


class SelfTestCallRequest(BaseModel):
    """1-click dry-run request to call the operator's own +91 mobile phone."""

    my_phone: str = Field(..., min_length=10, max_length=20)
    user_goal: str = Field(
        default="Check if a Deluxe room is available for check-in tonight with 10 AM early check-in and confirm direct phone tariff in ₹",
        min_length=3,
        max_length=500,
    )
    place_category: PlaceCategory = PlaceCategory.HOTEL
    caller_name: str = Field(default="", max_length=60)
    preferred_tone: VocalTone = VocalTone.CONVERSATIONAL_HINGLISH
    voice_name: str = Field(default="Puck", max_length=32)


class ConversationalCallPlan(BaseModel):
    """Pre-compiled 2-beat spoken Indian call plan so the voice agent never reads raw UI prompts aloud."""

    vocal_tone: VocalTone = VocalTone.CONVERSATIONAL_HINGLISH
    spoken_item_short: str = Field(
        description="Short natural spoken name of the medicine, scan, or item (e.g. 'Augmentin 625 Duo ki ek strip')."
    )
    turn1_availability_question: str = Field(
        description="Warm <2.5s opening greeting + availability check ONLY (never bundle price + stock in sentence 1)."
    )
    turn2_price_question: str = Field(
        description="Natural <2s follow-up asking exact ₹ price/unit ONLY after shopkeeper says yes."
    )
    turn3_closing: str = Field(
        default="Achha theek hai ji, bahut dhanyavaad!",
        description="Warm 1-second polite gender-neutral closing once quote or out-of-stock is confirmed.",
    )


class AP2SpendMandate(BaseModel):
    """Google AP2 (Agent Payments Protocol) spending authorization mandate."""

    mandate_id: str = Field(..., min_length=3, max_length=128)
    principal_id: str = Field(default="authorized_user", max_length=128)
    agent_id: str = Field(default="external_mcp_agent", max_length=128)
    max_amount_usd: float = Field(..., ge=0.01, le=100.0)
    currency: str = Field(default="USD", max_length=12)
    purpose: str = Field(default="local_business_voice_scout", max_length=240)
    expires_at_epoch: float | None = None
    signature: str = Field(default="", max_length=512)


class A2AScoutRequest(BaseModel):
    """Agent-to-Agent (A2A / MCP / ChatGPT Actions / Stripe MPP / x402) voice insights request."""

    query: str = Field(
        default="",
        max_length=300,
        description="Optional Google Maps search query (auto-derived from place_category + item_question if omitted).",
    )
    place_category: PlaceCategory = Field(
        default=PlaceCategory.CUSTOM,
        description="Google Maps place category: hotel, restaurant, clinic, diagnostics, petrol, auto, salon, pharmacy, or custom.",
    )
    specific_place: str = Field(
        default="",
        max_length=160,
        description="Optional exact place name on Google Maps (e.g. 'Taj Lands End', 'Bastian Bandra').",
    )
    caller_name: str = Field(
        default="",
        max_length=60,
        description="Optional first name used as a natural caller confidence marker.",
    )
    item_question: str = Field(
        ...,
        min_length=3,
        max_length=500,
        description="Purpose & exact question to ask the place on the phone.",
    )
    city: str = Field(default="Mumbai", min_length=2, max_length=80)
    locality: str = Field(default="Bandra West", max_length=100)
    max_shops: int = Field(default=3, ge=1, le=10)
    preferred_tone: VocalTone = VocalTone.CONVERSATIONAL_HINGLISH
    execution_mode: Literal["auto", "live_pstn", "swarm"] = "auto"
    ap2_mandate: AP2SpendMandate | None = None


class QuestSession(BaseModel):
    """Aggregate state of a parallel scouting quest."""

    quest_id: str
    user_goal: str
    place_category: PlaceCategory = PlaceCategory.CUSTOM
    caller_name: str = ""
    city: str
    locality: str
    preferred_tone: VocalTone
    voice_name: str = "Puck"
    mode: CallMode
    shops: list[IndianShopTarget]
    calls: dict[str, ShopCallState]
    call_plan: ConversationalCallPlan | None = None
    origin_channel: str = "web_consumer"
    caller_agent: str = "Consumer Web UI"
    status: Literal["running", "completed", "stopped"] = "running"
    created_at_epoch: float = Field(default_factory=time.time)

    def ranked_quotes(self) -> list[ShopCallState]:
        """Return calls sorted: connected in-stock lowest ₹ first, then connected in-stock, connected out-of-stock, then unanswered/skipped."""

        def sort_key(call: ShopCallState) -> tuple[int, float, float]:
            q = call.quote
            connected_ok = call.status not in (
                CallStatus.SKIPPED,
                CallStatus.FAILED,
                CallStatus.STOPPED,
            ) and call.outcome not in (
                CallOutcome.BUSY_NO_ANSWER,
                CallOutcome.VOICEMAIL_SKIPPED,
                CallOutcome.ERROR,
            )
            if connected_ok and q and q.in_stock is True and q.price_inr is not None:
                return (0, q.price_inr, -(call.shop.rating or 0.0))
            if connected_ok and q and q.in_stock is True:
                return (1, float("inf"), -(call.shop.rating or 0.0))
            if connected_ok and q and q.in_stock is False:
                return (2, float("inf"), -(call.shop.rating or 0.0))
            if connected_ok:
                return (3, float("inf"), -(call.shop.rating or 0.0))
            return (4, float("inf"), -(call.shop.rating or 0.0))

        return sorted(self.calls.values(), key=sort_key)
