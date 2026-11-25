"""Carrier adapters for RingScout India Phase 1 / 1.5 Live PSTN POC:
1. `HinglishSwarmAdapter` — Parallel Hinglish/Indian-English swarm + real multi-speaker WAV clips
2. `BrowserMicAdapter` (`BrowserMicLiveAdapter`) — Real-time WebSocket duplex bridge to Vertex Gemini Live (`manav-chub`)
3. `LiveIndiaPstnAdapter` (`TwilioToIndiaAdapter`) — Live India `+91` PSTN calling via `Vobiz.ai` (24kHz/16kHz L16 HD), `Plivo`, or `Twilio`
"""

from __future__ import annotations

from array import array
import asyncio
import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import struct
import time
from typing import Any, Callable
from urllib.parse import urlencode
from xml.sax.saxutils import escape as xml_escape

from fastapi import WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types
import httpx
from pydantic import BaseModel, Field

from ringscout.models import (
    CallMode,
    CallOutcome,
    CallStatus,
    CarrierProvider,
    IndianShopTarget,
    LiveCarrierConfig,
    PlaceCategory,
    QuestSession,
    ShopCallState,
    TranscriptTurn,
    VocalTone,
)
from ringscout.secrets_manager import get_secret
from ringscout.voice_engine import (
    GCP_LOCATION,
    LIVE_AUDIO_MODEL,
    LIVE_FALLBACK_TARGETS,
    MAX_CALL_DURATION_SEC,
    SIDE_BRAIN_MODEL,
    VOICEMAIL_BAILOUT_MAX_SEC,
    add_pstn_comfort_noise_pcm24,
    build_agent_disclosure_opening,
    build_thin_live_config,
    decision_to_inr_quote,
    evaluate_transcript_side_brain,
    extract_ivr_dtmf_digit,
    gate_pstn_background_chatter,
    generate_busy_tone_wav,
    generate_comfort_bed_pcm24,
    get_vertex_client,
    heuristic_side_brain_fallback,
    is_agent_farewell_turn,
    is_human_hold_phrase,
    is_ivr_or_hold_announcement,
    is_live_human_pickup_after_ivr,
    pcm16_to_wav_bytes,
    sanitize_option_a_line,
    strip_live_meta_tokens,
    synthesize_dialogue_wav,
)

logger = logging.getLogger(__name__)

EventPublishFn = Callable[[str, dict[str, Any]], None]

_SHOPKEEPER_VOICES = [
    "Charon",
    "Puck",
    "Kore",
    "Fenrir",
    "Orus",
    "Zephyr",
    "Leda",
    "Charon",
]


class _GeneratedTurn(BaseModel):
    speaker: str = Field(description="'agent' or 'shopkeeper'")
    text: str


class _GeneratedShopScenario(BaseModel):
    archetype: str = Field(
        description="One of: best_quote, ivr_then_quote, hinglish_idiom_quote, out_of_stock, voicemail, premium_quote, busy_no_answer, discount_quote"
    )
    turns: list[_GeneratedTurn] = Field(default_factory=list)


def _fallback_swarm_scenario(
    idx: int,
    shop: IndianShopTarget,
    user_goal: str,
    vocal_tone: VocalTone,
    caller_name: str = "",
) -> _GeneratedShopScenario:
    """Deterministic realistic Indian shopkeeper dialogue if LLM script generation is skipped/offline."""
    opening = build_agent_disclosure_opening(
        shop.name,
        user_goal,
        vocal_tone,
        shop_address=shop.address,
        locality=shop.locality,
        caller_name=caller_name,
    )
    slot = idx % 8

    goal_low = f"{user_goal} {shop.category}".lower()
    if any(k in goal_low for k in ("hotel", "room", "check-in", "checkin", "stay", "suite", "tariff")):
        base_price, alt_price, high_price = 6400, 5800, 8200
        idiom_phrase = "chhah hazaar chaar sau rupaye"
        unit = "per night + breakfast (10 AM early check-in ok)"
        eta = "Deluxe room ready now"
    elif any(k in goal_low for k in ("restaurant", "table for", "cafe", "bistro", "dinner", "rooftop", "buffet")):
        base_price, alt_price, high_price = 1500, 1200, 2200
        idiom_phrase = "dedh hazaar rupaye"
        unit = "approx for 2 (zero cover charge)"
        eta = "Table open at 8:30 PM (outdoor available)"
    elif any(k in goal_low for k in ("petrol", "xp95", "fuel", "cng", "ev charg", "nitrogen")):
        base_price, alt_price, high_price = 109, 104, 112
        idiom_phrase = "ek sau nau rupaye"
        unit = "per litre (XP95 & Nitrogen air active)"
        eta = "Pump open 24x7 · zero queue"
    elif any(k in goal_low for k in ("clinic", "opd", "doctor", "consultation", "polyclinic")):
        base_price, alt_price, high_price = 800, 700, 1200
        idiom_phrase = "aath sau rupaye"
        unit = "OPD consultation fee"
        eta = "Doctor available 6:00 PM today"
    elif any(k in goal_low for k in ("salon", "spa", "haircut", "grooming")):
        base_price, alt_price, high_price = 950, 850, 1400
        idiom_phrase = "nau sau pachaas rupaye"
        unit = "stylist appointment"
        eta = "5:30 PM slot open today"
    elif any(k in goal_low for k in ("tyre", "bridgestone", "car", "alignment")):
        base_price, alt_price, high_price = 6450, 6200, 6850
        idiom_phrase = "chhah hazaar chaar sau rupaye"
        unit = "per tyre incl. alignment"
        eta = "Bay open right now (30 mins)"
    elif any(k in goal_low for k in ("mri", "scan", "diagnostic", "x-ray", "blood")):
        base_price, alt_price, high_price = 5500, 4800, 6500
        idiom_phrase = "saadhe paanch hazaar rupaye"
        unit = "1.5T scan cash rate"
        eta = "4:30 PM slot open today"
    else:
        base_price, alt_price, high_price = 420, 395, 465
        idiom_phrase = "saadhe chaar sau rupaye"
        unit = "per strip / pack"
        eta = "In stock at counter now"

    is_en = vocal_tone == VocalTone.POLITE_INDIAN_ENGLISH
    closing_ok = (
        "Okay got it, thank you so much!"
        if is_en
        else "Achha theek hai ji, bahut dhanyavaad!"
    )

    if slot == 0:
        return _GeneratedShopScenario(
            archetype="best_quote",
            turns=[
                _GeneratedTurn(speaker="agent", text=opening),
                _GeneratedTurn(
                    speaker="shopkeeper",
                    text=(
                        f"Yes, we have it available right now. Cash price is ₹{alt_price} {unit}."
                        if is_en
                        else f"Namaste ji, haan stock mein available hai. Cash price ₹{alt_price} padega {unit}, abhi aake le jaiye."
                    ),
                ),
                _GeneratedTurn(speaker="agent", text=closing_ok),
            ],
        )
    if slot == 1:
        return _GeneratedShopScenario(
            archetype="ivr_then_quote",
            turns=[
                _GeneratedTurn(
                    speaker="shopkeeper",
                    text=f"Welcome to {shop.name}. For English press 1, Hindi aur counter enquiry ke liye 2 dabayein. Press 2 for counter desk.",
                ),
                _GeneratedTurn(speaker="agent", text=opening),
                _GeneratedTurn(
                    speaker="shopkeeper",
                    text=(
                        f"Hello, reception here — yes it is available, ₹{base_price} {unit}. {eta}."
                        if is_en
                        else f"Haan ji counter se bol raha hoon — bilkul mil jayega, ₹{base_price} lagega {unit}. {eta}."
                    ),
                ),
                _GeneratedTurn(speaker="agent", text=closing_ok),
            ],
        )
    if slot == 2:
        return _GeneratedShopScenario(
            archetype="hinglish_idiom_quote",
            turns=[
                _GeneratedTurn(speaker="agent", text=opening),
                _GeneratedTurn(
                    speaker="shopkeeper",
                    text=f"Haan ji bilkul stock mein hai! Total {idiom_phrase} padega ({unit}), 15 mins mein ready rakhta hoon.",
                ),
                _GeneratedTurn(
                    speaker="agent",
                    text="Got it, thank you so much!" if is_en else "Achha theek hai ji, bahut shukriya!",
                ),
            ],
        )
    if slot == 3:
        return _GeneratedShopScenario(
            archetype="out_of_stock",
            turns=[
                _GeneratedTurn(speaker="agent", text=opening),
                _GeneratedTurn(
                    speaker="shopkeeper",
                    text=(
                        "Sorry, no slot available today — next slot is tomorrow morning at 11 AM."
                        if is_en
                        else "Sorry ji, abhi out of stock ho gaya hai — kal subah 11 baje fresh stock aayega."
                    ),
                ),
                _GeneratedTurn(
                    speaker="agent",
                    text="Okay got it, thank you so much!" if is_en else "Koi baat nahi ji, update dene ke liye dhanyavaad!",
                ),
            ],
        )
    if slot == 4:
        return _GeneratedShopScenario(
            archetype="voicemail",
            turns=[
                _GeneratedTurn(
                    speaker="shopkeeper",
                    text="Aapke dwara dial kiya gaya number abhi switched off hai. Please leave a message after the beep.",
                ),
            ],
        )
    if slot == 5:
        return _GeneratedShopScenario(
            archetype="premium_quote",
            turns=[
                _GeneratedTurn(speaker="agent", text=opening),
                _GeneratedTurn(
                    speaker="shopkeeper",
                    text=f"Yes, available right now at our {shop.locality or 'main'} branch. It is ₹{high_price} {unit}.",
                ),
                _GeneratedTurn(speaker="agent", text=closing_ok),
            ],
        )
    if slot == 6:
        return _GeneratedShopScenario(archetype="busy_no_answer", turns=[])

    disc_price = max(int(base_price * 0.96), 50)
    return _GeneratedShopScenario(
        archetype="discount_quote",
        turns=[
            _GeneratedTurn(speaker="agent", text=opening),
            _GeneratedTurn(
                speaker="shopkeeper",
                text=(
                    f"Let me check on the system... yes available, ₹{disc_price} walk-in cash price ({unit})."
                    if is_en
                    else f"Ek second computer mein check karta hoon... haan ji 2 piece pade hain, ₹{disc_price} final rate padega ({unit})."
                ),
            ),
            _GeneratedTurn(speaker="agent", text=closing_ok),
        ],
    )


async def _generate_dynamic_swarm_scenario(
    idx: int,
    shop: IndianShopTarget,
    user_goal: str,
    vocal_tone: VocalTone,
    client: genai.Client | None = None,
    caller_name: str = "",
) -> _GeneratedShopScenario:
    """Generate a tailored Hinglish/en-IN dialogue for arbitrary user quests in India."""
    fallback = _fallback_swarm_scenario(idx, shop, user_goal, vocal_tone, caller_name=caller_name)
    slot = idx % 8
    if slot in (4, 6):
        return fallback

    archetype_instructions = {
        0: "Shopkeeper has the exact item/service available right now at a very competitive realistic Indian Rupee (₹) market price.",
        1: "Turn 1 is an automated Indian shop IVR ('Welcome to <shop>, For English press 1, Hindi aur counter ke liye 2 dabayein / Press 2 for counter'). Then Agent asks directly, and human clerk confirms stock + realistic ₹ price.",
        2: "Shopkeeper confirms availability and states the realistic ₹ price using natural Hinglish number words AND digits (e.g. 'saadhe chaar sau rupaye, ₹450' or 'pandra sau rupaye, ₹1500').",
        3: "Shopkeeper politely says it is currently out of stock / fully booked today, available tomorrow morning.",
        5: "Polite Indian English receptionist confirms immediate stock/slot at a slightly premium realistic ₹ market rate.",
        7: "Shopkeeper checks inventory ('Ek second check karta hoon...') and confirms availability with a slight cash discount in ₹.",
    }[slot]

    opening = build_agent_disclosure_opening(
        shop.name,
        user_goal,
        vocal_tone,
        shop_address=shop.address,
        locality=shop.locality,
        caller_name=caller_name,
    )
    prompt = (
        f"Create a realistic 3-turn phone call in {shop.locality}, {shop.city}, India.\n"
        f"Shop: {shop.name} ({shop.category})\n"
        f"Question: {user_goal}\n"
        f"Agent Vocal Tone: {vocal_tone.value} (if polite_indian_english, agent speaks 100% natural Indian English).\n"
        f"Scenario Role: {archetype_instructions}\n"
        f"Mandatory Rules:\n"
        f'1. The first \'agent\' turn MUST be: "{opening}"\n'
        f"2. Agent must NEVER say 'bhaiya' and NEVER say 'for a customer' / 'customer ko update'.\n"
        f"3. Never include any phone number. Use realistic Indian market prices in ₹."
    )

    try:
        genai_client = client or get_vertex_client(GCP_LOCATION)
        resp = await genai_client.aio.models.generate_content(
            model=SIDE_BRAIN_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                response_mime_type="application/json",
                response_schema=_GeneratedShopScenario,
            ),
        )
        parsed = _GeneratedShopScenario.model_validate_json(resp.text)
        if parsed.turns:
            for t in parsed.turns:
                if t.speaker == "agent":
                    t.text = sanitize_option_a_line(t.text, vocal_tone)
            return parsed
    except Exception as exc:
        logger.warning("Dynamic scenario generation fell back for %s: %s", shop.name, exc)

    return fallback


class HinglishSwarmAdapter:
    """Executes up to 8 concurrent Indian shop calls with temp=0.0 Side-Brain extraction & WAV proof."""

    def __init__(
        self,
        audio_store: dict[tuple[str, str], bytes],
        tts_concurrency: int = 4,
        synthesize_audio: bool = True,
        max_call_duration_sec: float = MAX_CALL_DURATION_SEC,
    ) -> None:
        self._audio_store = audio_store
        self._tts_sem = asyncio.Semaphore(tts_concurrency)
        self._synthesize_audio = synthesize_audio
        self._max_call_duration_sec = max_call_duration_sec

    async def run_quest(
        self,
        quest: QuestSession,
        stop_event: asyncio.Event,
        publish: EventPublishFn,
        client: genai.Client | None = None,
    ) -> None:
        tasks = [
            asyncio.create_task(
                self._run_single_shop_call(
                    idx=idx,
                    quest=quest,
                    shop=shop,
                    stop_event=stop_event,
                    publish=publish,
                    client=client,
                )
            )
            for idx, shop in enumerate(quest.shops)
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        quest.status = "stopped" if stop_event.is_set() else "completed"
        publish("quest_complete", {"quest_id": quest.quest_id, "status": quest.status})

    async def _sleep_cancellable(self, delay: float, stop_event: asyncio.Event) -> bool:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
            return True
        except asyncio.TimeoutError:
            return False

    async def _run_single_shop_call(
        self,
        idx: int,
        quest: QuestSession,
        shop: IndianShopTarget,
        stop_event: asyncio.Event,
        publish: EventPublishFn,
        client: genai.Client | None = None,
    ) -> None:
        call = quest.calls[shop.shop_id]
        start_ts = time.monotonic()
        frozen_duration: float | None = None

        def emit_update() -> None:
            if frozen_duration is not None:
                call.duration_sec = frozen_duration
            else:
                call.duration_sec = round(
                    min(time.monotonic() - start_ts, self._max_call_duration_sec), 1
                )
            publish(
                "call_update",
                {"quest_id": quest.quest_id, "shop_id": shop.shop_id, "call": call.model_dump()},
            )

        async def _conversation_body() -> None:
            nonlocal frozen_duration
            call.status = CallStatus.RINGING
            emit_update()

            if await self._sleep_cancellable(0.3 + (idx % 4) * 0.15, stop_event):
                self._mark_stopped(call, emit_update)
                return

            scenario = await _generate_dynamic_swarm_scenario(
                idx=idx,
                shop=shop,
                user_goal=quest.user_goal,
                vocal_tone=quest.preferred_tone,
                client=client,
                caller_name=quest.caller_name,
            )
            # Reset active call timer to actual pick-up / ring completion so LLM script setup isn't counted as talk time
            call_connect_ts = time.monotonic()

            if stop_event.is_set():
                self._mark_stopped(call, emit_update)
                return

            if scenario.archetype == "busy_no_answer" or not scenario.turns:
                if await self._sleep_cancellable(1.1, stop_event):
                    self._mark_stopped(call, emit_update)
                    return
                frozen_duration = round(time.monotonic() - call_connect_ts, 1)
                self._audio_store[(quest.quest_id, shop.shop_id)] = generate_busy_tone_wav(2.2)
                call.audio_url = f"/api/quests/{quest.quest_id}/calls/{shop.shop_id}/audio.wav"
                call.status = CallStatus.SKIPPED
                call.outcome = CallOutcome.BUSY_NO_ANSWER
                call.transcript.append(
                    TranscriptTurn(
                        speaker="system",
                        text="Line busy (400Hz Indian PSTN busy tone) — auto-ended cleanly.",
                        timestamp_sec=frozen_duration,
                    )
                )
                emit_update()
                return

            call.status = CallStatus.IN_PROGRESS
            emit_update()

            for turn_idx, turn in enumerate(scenario.turns):
                if await self._sleep_cancellable(0.4, stop_event):
                    self._mark_stopped(call, emit_update)
                    return

                speaker = "agent" if turn.speaker.lower() == "agent" else "shopkeeper"
                now_rel = round(time.monotonic() - call_connect_ts, 1)
                if speaker == "agent" and not call.disclosure_delivered:
                    call.disclosure_delivered = True

                call.transcript.append(
                    TranscriptTurn(speaker=speaker, text=turn.text, timestamp_sec=now_rel)
                )

                # Out-of-Band deterministic/temp=0.0 Side-Brain check on Turn 1 IVR or Voicemail
                if speaker == "shopkeeper" and turn_idx == 0:
                    quick_dec = heuristic_side_brain_fallback(
                        call.transcript, preferred_tone=quest.preferred_tone
                    )
                    if quick_dec.is_voicemail:
                        try:
                            vm_dec = await asyncio.wait_for(
                                evaluate_transcript_side_brain(
                                    user_goal=quest.user_goal,
                                    shop_name=shop.name,
                                    transcript=call.transcript,
                                    preferred_tone=quest.preferred_tone,
                                    client=client,
                                ),
                                timeout=VOICEMAIL_BAILOUT_MAX_SEC - 1.0,
                            )
                        except asyncio.TimeoutError:
                            vm_dec = quick_dec
                        if vm_dec.is_voicemail:
                            frozen_duration = min(
                                round(time.monotonic() - call_connect_ts, 1),
                                VOICEMAIL_BAILOUT_MAX_SEC - 0.2,
                            )
                            call.status = CallStatus.SKIPPED
                            call.outcome = CallOutcome.VOICEMAIL_SKIPPED
                            call.transcript.append(
                                TranscriptTurn(
                                    speaker="system",
                                    text=f"Voicemail/switched-off detected -> bailed out in {frozen_duration:.1f}s (<5s guard).",
                                    timestamp_sec=frozen_duration,
                                )
                            )
                            emit_update()
                            if self._synthesize_audio and not stop_event.is_set():
                                async with self._tts_sem:
                                    wav = await synthesize_dialogue_wav(
                                        call.transcript,
                                        shopkeeper_voice=_SHOPKEEPER_VOICES[
                                            idx % len(_SHOPKEEPER_VOICES)
                                        ],
                                        vocal_tone=quest.preferred_tone,
                                        client=client,
                                    )
                                self._audio_store[(quest.quest_id, shop.shop_id)] = wav
                                call.audio_url = (
                                    f"/api/quests/{quest.quest_id}/calls/{shop.shop_id}/audio.wav"
                                )
                                emit_update()
                            return

                    if quick_dec.dtmf_digit and quick_dec.dtmf_digit not in call.dtmf_sent:
                        call.status = CallStatus.IVR_MENU
                        call.dtmf_sent.append(quick_dec.dtmf_digit)
                        call.transcript.append(
                            TranscriptTurn(
                                speaker="system",
                                text=f"temp=0.0 Side-Brain detected IVR -> sent DTMF '{quick_dec.dtmf_digit}'",
                                timestamp_sec=round(time.monotonic() - call_connect_ts, 1),
                            )
                        )
                        emit_update()
                        if await self._sleep_cancellable(0.3, stop_event):
                            self._mark_stopped(call, emit_update)
                            return
                        call.status = CallStatus.IN_PROGRESS

                emit_update()

            # Freeze actual phone call duration BEFORE post-call Side-Brain & WAV rendering
            frozen_duration = round(
                min(time.monotonic() - call_connect_ts, self._max_call_duration_sec), 1
            )

            if stop_event.is_set():
                self._mark_stopped(call, emit_update)
                return

            decision = await evaluate_transcript_side_brain(
                user_goal=quest.user_goal,
                shop_name=shop.name,
                transcript=call.transcript,
                preferred_tone=quest.preferred_tone,
                place_category=quest.place_category,
                client=client,
            )
            call.quote = decision_to_inr_quote(decision)
            call.vocal_tone = decision.vocal_tone

            if self._synthesize_audio and not stop_event.is_set():
                async with self._tts_sem:
                    wav_bytes = await synthesize_dialogue_wav(
                        call.transcript,
                        shopkeeper_voice=_SHOPKEEPER_VOICES[idx % len(_SHOPKEEPER_VOICES)],
                        vocal_tone=quest.preferred_tone,
                        agent_voice=quest.voice_name,
                        client=client,
                    )
                self._audio_store[(quest.quest_id, shop.shop_id)] = wav_bytes
                call.audio_url = f"/api/quests/{quest.quest_id}/calls/{shop.shop_id}/audio.wav"

            if stop_event.is_set():
                self._mark_stopped(call, emit_update)
                return

            call.status = CallStatus.COMPLETED
            if call.dtmf_sent and decision.in_stock is True:
                call.outcome = CallOutcome.IVR_NAVIGATED
            elif decision.in_stock is True:
                call.outcome = CallOutcome.CONNECTED_QUOTE
            elif decision.in_stock is False:
                call.outcome = CallOutcome.CONNECTED_NO_STOCK
            else:
                call.outcome = CallOutcome.CONNECTED_INCONCLUSIVE
            emit_update()

        try:
            await asyncio.wait_for(_conversation_body(), timeout=self._max_call_duration_sec)
        except asyncio.TimeoutError:
            frozen_duration = self._max_call_duration_sec
            call.status = CallStatus.COMPLETED
            call.outcome = CallOutcome.TIMED_OUT_90S
            call.transcript.append(
                TranscriptTurn(
                    speaker="system",
                    text=f"{int(self._max_call_duration_sec)}s hard call duration cap reached — hung up cleanly.",
                    timestamp_sec=self._max_call_duration_sec,
                )
            )
            emit_update()

    @staticmethod
    def _mark_stopped(call: ShopCallState, emit_update: Callable[[], None]) -> None:
        call.status = CallStatus.STOPPED
        call.outcome = CallOutcome.STOPPED_BY_USER
        call.transcript.append(
            TranscriptTurn(
                speaker="system",
                text="Stopped immediately by user Stop All panic switch.",
                timestamp_sec=call.duration_sec,
            )
        )
        emit_update()


def _upsample_pcm16_16k_to_24k(pcm16_16k: bytes) -> bytes:
    """Pure-Python 16kHz -> 24kHz (2:3) linear interpolator for 16-bit signed LE PCM."""
    even_len = len(pcm16_16k) - (len(pcm16_16k) % 2)
    if even_len == 0:
        return b""
    samples = array("h")
    samples.frombytes(pcm16_16k[:even_len])
    out = array("h")
    n = len(samples)
    for i in range(0, n - 1, 2):
        s0 = samples[i]
        s1 = samples[i + 1]
        s2 = samples[i + 2] if (i + 2) < n else s1
        out.append(s0)
        out.append((s0 + s1) // 2)
        out.append((s1 + s2) // 2)
    if n % 2 == 1:
        out.append(samples[-1])
    return out.tobytes()


def _ulaw_byte_to_pcm16(u_val: int) -> int:
    """Decode a single ITU-T G.711 mu-law byte into signed 16-bit PCM."""
    u = (~u_val) & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    sample = ((mantissa << 3) + 0x84) << exponent
    sample -= 0x84
    return -sample if sign else sample


def _ulaw_8k_to_pcm16_16k(ulaw_bytes: bytes) -> bytes:
    """Convert 8kHz G.711 mu-law payload from Twilio Media Streams to 16kHz PCM16 for Gemini Live."""
    out = array("h")
    prev = 0
    for b in ulaw_bytes:
        cur = max(-32768, min(32767, _ulaw_byte_to_pcm16(b)))
        out.append((prev + cur) // 2)
        out.append(cur)
        prev = cur
    return out.tobytes()


def _upsample_pcm16_8k_to_16k(pcm8: bytes) -> bytes:
    """Linear 1:2 upsampler for 8kHz signed 16-bit PCM -> 16kHz signed 16-bit PCM."""
    even_len = len(pcm8) - (len(pcm8) % 2)
    if even_len == 0:
        return b""
    samples = array("h")
    samples.frombytes(pcm8[:even_len])
    out = array("h")
    prev = 0
    for cur in samples:
        out.append((prev + cur) // 2)
        out.append(cur)
        prev = cur
    return out.tobytes()


def _pcm16_24k_to_ulaw_8k(pcm24: bytes) -> bytes:
    """Anti-aliased 5-tap low-pass filter + 3:1 downsample (24kHz -> 8kHz) + G.711 mu-law encode."""
    even_len = len(pcm24) - (len(pcm24) % 2)
    if even_len == 0:
        return b""
    samples = array("h")
    samples.frombytes(pcm24[:even_len])
    n = len(samples)
    out = bytearray()
    for i in range(0, n, 3):
        sm2 = samples[i - 2] if i >= 2 else samples[i]
        sm1 = samples[i - 1] if i >= 1 else samples[i]
        s0 = samples[i]
        sp1 = samples[i + 1] if (i + 1) < n else s0
        sp2 = samples[i + 2] if (i + 2) < n else s0
        s = (sm2 + 2 * sm1 + 3 * s0 + 2 * sp1 + sp2) // 9
        sign = 0x80 if s < 0 else 0
        mag = min(32635, abs(s)) + 0x84
        exp = 7
        mask = 0x4000
        while exp > 0 and not (mag & mask):
            exp -= 1
            mask >>= 1
        mant = (mag >> (exp + 3)) & 0x0F
        out.append((~(sign | (exp << 4) | mant)) & 0xFF)
    return bytes(out)


class BrowserMicLiveAdapter:
    """Real-time duplex WebSocket relay between laptop mic/speaker and Vertex Gemini Live (`manav-chub`)."""

    def __init__(
        self,
        audio_store: dict[tuple[str, str], bytes],
        max_call_duration_sec: float = MAX_CALL_DURATION_SEC,
    ) -> None:
        self._audio_store = audio_store
        self._max_call_duration_sec = max_call_duration_sec

    async def handle_websocket(
        self,
        websocket: WebSocket,
        quest: QuestSession,
        call: ShopCallState,
        stop_event: asyncio.Event,
        publish: EventPublishFn,
    ) -> None:
        await websocket.accept()
        start_ts = time.monotonic()
        recorded_24k_pcm = bytearray()
        side_brain_lock = asyncio.Lock()
        hangup_event = asyncio.Event()

        call.mode = CallMode.BROWSER_MIC
        call.status = CallStatus.IN_PROGRESS
        call.disclosure_delivered = False

        def emit_state() -> None:
            call.duration_sec = round(
                min(time.monotonic() - start_ts, self._max_call_duration_sec), 1
            )
            publish(
                "call_update",
                {
                    "quest_id": quest.quest_id,
                    "shop_id": call.shop.shop_id,
                    "call": call.model_dump(),
                },
            )

        emit_state()

        client = get_vertex_client(GCP_LOCATION)
        live_cfg = build_thin_live_config(
            shop_name=call.shop.name,
            locality=call.shop.locality or quest.locality,
            user_goal=quest.user_goal,
            vocal_tone=quest.preferred_tone,
            call_plan=quest.call_plan,
            voice_name=quest.voice_name,
            shop_address=call.shop.address,
            place_category=quest.place_category,
            caller_name=quest.caller_name,
        )

        agent_text_buf: list[str] = []
        shop_text_buf: list[str] = []

        async def flush_side_brain() -> None:
            async with side_brain_lock:
                decision = await evaluate_transcript_side_brain(
                    user_goal=quest.user_goal,
                    shop_name=call.shop.name,
                    transcript=call.transcript,
                    preferred_tone=quest.preferred_tone,
                    place_category=quest.place_category,
                    client=client,
                )
                call.quote = decision_to_inr_quote(decision)
                if decision.is_voicemail:
                    call.outcome = CallOutcome.VOICEMAIL_SKIPPED
                    hangup_event.set()
                emit_state()
                try:
                    await websocket.send_json(
                        {
                            "type": "quote_update",
                            "quote": call.quote.model_dump(),
                            "should_hangup": decision.should_hangup,
                            "dtmf_digit": decision.dtmf_digit,
                        }
                    )
                except Exception:
                    pass

        seed_pickup_line = (
            "Hello? Haan boliye, dukaan se bol raha hoon."
            if quest.preferred_tone in (VocalTone.CONVERSATIONAL_HINGLISH, VocalTone.NATURAL_HINDI)
            else f"Hello, {call.shop.name} counter speaking, how may I help you?"
        )

        timed_out_90s = False
        try:
            async with asyncio.timeout(self._max_call_duration_sec):
                async with client.aio.live.connect(
                    model=LIVE_AUDIO_MODEL, config=live_cfg
                ) as session:
                    await session.send_client_content(
                        turns=types.Content(
                            role="user",
                            parts=[types.Part(text=seed_pickup_line)],
                        ),
                        turn_complete=True,
                    )
                    await websocket.send_json(
                        {
                            "type": "session_ready",
                            "model": LIVE_AUDIO_MODEL,
                            "shop_name": call.shop.name,
                            "vocal_tone": quest.preferred_tone.value,
                        }
                    )

                    async def browser_to_gemini() -> None:
                        while not stop_event.is_set() and not hangup_event.is_set():
                            raw = await websocket.receive_text()
                            msg = json.loads(raw)
                            mtype = msg.get("type")
                            if mtype == "audio_pcm16":
                                pcm16_16k = base64.b64decode(msg["data"])
                                recorded_24k_pcm.extend(_upsample_pcm16_16k_to_24k(pcm16_16k))
                                await session.send_realtime_input(
                                    audio=types.Blob(
                                        data=pcm16_16k,
                                        mime_type="audio/pcm;rate=16000",
                                    )
                                )
                            elif mtype == "shopkeeper_text":
                                txt = str(msg.get("text") or "").strip()
                                if txt:
                                    call.transcript.append(
                                        TranscriptTurn(
                                            speaker="shopkeeper",
                                            text=txt,
                                            timestamp_sec=round(time.monotonic() - start_ts, 1),
                                        )
                                    )
                                    emit_state()
                                    await websocket.send_json(
                                        {
                                            "type": "transcript",
                                            "speaker": "shopkeeper",
                                            "text": txt,
                                        }
                                    )
                                    asyncio.create_task(flush_side_brain())
                                    await session.send_client_content(
                                        turns=types.Content(
                                            role="user", parts=[types.Part(text=txt)]
                                        ),
                                        turn_complete=True,
                                    )
                            elif mtype == "hangup":
                                hangup_event.set()
                                break

                    async def gemini_to_browser() -> None:
                        # Outer loop keeps multi-turn Gemini Live conversation open after each turn_complete
                        while not stop_event.is_set() and not hangup_event.is_set():
                            async for srv_msg in session.receive():
                                if stop_event.is_set() or hangup_event.is_set():
                                    return
                                sc = getattr(srv_msg, "server_content", None)
                                if not sc:
                                    continue

                                if sc.model_turn and sc.model_turn.parts:
                                    call.disclosure_delivered = True
                                    for part in sc.model_turn.parts:
                                        if part.inline_data and part.inline_data.data:
                                            audio_bytes = part.inline_data.data
                                            recorded_24k_pcm.extend(audio_bytes)
                                            await websocket.send_json(
                                                {
                                                    "type": "audio_pcm24",
                                                    "data": base64.b64encode(audio_bytes).decode(
                                                        "ascii"
                                                    ),
                                                }
                                            )

                                in_tr = getattr(sc, "input_transcription", None)
                                if in_tr and getattr(in_tr, "text", None):
                                    shop_text_buf.append(in_tr.text)
                                    await websocket.send_json(
                                        {
                                            "type": "partial_transcript",
                                            "speaker": "shopkeeper",
                                            "text": "".join(shop_text_buf).strip(),
                                        }
                                    )

                                out_tr = getattr(sc, "output_transcription", None)
                                if out_tr and getattr(out_tr, "text", None):
                                    agent_text_buf.append(out_tr.text)
                                    joined_clean = strip_live_meta_tokens(
                                        "".join(agent_text_buf)
                                    ).strip()
                                    if joined_clean and shop_text_buf:
                                        full_shop = "".join(shop_text_buf).strip()
                                        shop_text_buf.clear()
                                        if full_shop:
                                            call.transcript.append(
                                                TranscriptTurn(
                                                    speaker="shopkeeper",
                                                    text=full_shop,
                                                    timestamp_sec=round(
                                                        time.monotonic() - start_ts, 1
                                                    ),
                                                )
                                            )
                                            await websocket.send_json(
                                                {
                                                    "type": "transcript",
                                                    "speaker": "shopkeeper",
                                                    "text": full_shop,
                                                }
                                            )
                                            asyncio.create_task(flush_side_brain())

                                    if joined_clean:
                                        call.disclosure_delivered = True
                                        await websocket.send_json(
                                            {
                                                "type": "partial_transcript",
                                                "speaker": "agent",
                                                "text": joined_clean,
                                            }
                                        )

                                if getattr(sc, "turn_complete", False):
                                    if agent_text_buf:
                                        full_agent = strip_live_meta_tokens(
                                            "".join(agent_text_buf)
                                        ).strip()
                                        agent_text_buf.clear()
                                        if full_agent:
                                            call.transcript.append(
                                                TranscriptTurn(
                                                    speaker="agent",
                                                    text=full_agent,
                                                    timestamp_sec=round(
                                                        time.monotonic() - start_ts, 1
                                                    ),
                                                )
                                            )
                                            emit_state()
                                            await websocket.send_json(
                                                {
                                                    "type": "transcript",
                                                    "speaker": "agent",
                                                    "text": full_agent,
                                                }
                                            )

                    done, pending = await asyncio.wait(
                        [
                            asyncio.create_task(browser_to_gemini()),
                            asyncio.create_task(gemini_to_browser()),
                            asyncio.create_task(stop_event.wait()),
                            asyncio.create_task(hangup_event.wait()),
                        ],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    for task in done:
                        if not task.cancelled():
                            exc = task.exception()
                            if exc and not isinstance(exc, WebSocketDisconnect):
                                raise exc
        except TimeoutError:
            timed_out_90s = True
        except WebSocketDisconnect:
            logger.info("Browser mic WebSocket closed for %s", call.shop.name)
        except Exception as exc:
            logger.warning("Browser mic session error for %s: %s", call.shop.name, exc)
            try:
                await websocket.send_json({"type": "error", "message": str(exc)})
            except Exception:
                pass
        finally:
            for buf, spk in ((shop_text_buf, "shopkeeper"), (agent_text_buf, "agent")):
                if buf:
                    raw_rem = "".join(buf)
                    rem = (
                        strip_live_meta_tokens(raw_rem).strip()
                        if spk == "agent"
                        else raw_rem.strip()
                    )
                    if rem:
                        call.transcript.append(
                            TranscriptTurn(
                                speaker=spk,
                                text=rem,
                                timestamp_sec=round(time.monotonic() - start_ts, 1),
                            )
                        )

            if call.transcript:
                decision = await evaluate_transcript_side_brain(
                    user_goal=quest.user_goal,
                    shop_name=call.shop.name,
                    transcript=call.transcript,
                    preferred_tone=quest.preferred_tone,
                    place_category=quest.place_category,
                    client=client,
                )
                call.quote = decision_to_inr_quote(decision)

            if recorded_24k_pcm:
                self._audio_store[(quest.quest_id, call.shop.shop_id)] = pcm16_to_wav_bytes(
                    bytes(recorded_24k_pcm), sample_rate=24000
                )
                call.audio_url = (
                    f"/api/quests/{quest.quest_id}/calls/{call.shop.shop_id}/audio.wav"
                )

            if stop_event.is_set():
                call.status = CallStatus.STOPPED
                call.outcome = CallOutcome.STOPPED_BY_USER
            elif timed_out_90s:
                call.status = CallStatus.COMPLETED
                call.outcome = CallOutcome.TIMED_OUT_90S
            elif call.outcome == CallOutcome.VOICEMAIL_SKIPPED:
                call.status = CallStatus.SKIPPED
            else:
                call.status = CallStatus.COMPLETED
                if call.quote and call.quote.in_stock is True:
                    call.outcome = CallOutcome.CONNECTED_QUOTE
                elif call.quote and call.quote.in_stock is False:
                    call.outcome = CallOutcome.CONNECTED_NO_STOCK
                else:
                    call.outcome = CallOutcome.CONNECTED_INCONCLUSIVE
            emit_state()


# Standard alias matching AGENTS.md
BrowserMicAdapter = BrowserMicLiveAdapter


_PROCESS_HMAC_FALLBACK = secrets.token_hex(32)

_DTMF_FREQS: dict[str, tuple[float, float]] = {
    "1": (697.0, 1209.0),
    "2": (697.0, 1336.0),
    "3": (697.0, 1477.0),
    "4": (770.0, 1209.0),
    "5": (770.0, 1336.0),
    "6": (770.0, 1477.0),
    "7": (852.0, 1209.0),
    "8": (852.0, 1336.0),
    "9": (852.0, 1477.0),
    "*": (941.0, 1209.0),
    "0": (941.0, 1336.0),
    "#": (941.0, 1477.0),
}


def generate_dtmf_pcm24(digit: str, duration_sec: float = 0.18) -> bytes:
    """Synthesize standard ITU-T Q.23 dual-tone audio (24kHz signed 16-bit LE PCM)."""
    freqs = _DTMF_FREQS.get(digit.strip())
    if not freqs:
        return b""
    f_low, f_high = freqs
    sample_rate = 24000
    n_samples = int(sample_rate * duration_sec)
    out = bytearray()
    for i in range(n_samples):
        t = i / sample_rate
        val = int(7500 * (math.sin(2.0 * math.pi * f_low * t) + math.sin(2.0 * math.pi * f_high * t)))
        out.extend(struct.pack("<h", val))
    return bytes(out)


def _mask_identifier(value: str) -> str:
    clean = value.strip()
    if len(clean) <= 4:
        return "****"
    return f"****{clean[-4:]}"


def _normalize_public_urls(raw_base: str) -> tuple[str, str]:
    """Return `(http_base, ws_base)` without trailing slashes from an https/http/wss/ws base URL."""
    base = raw_base.strip().rstrip("/")
    if base.startswith("wss://"):
        return "https://" + base[len("wss://") :], base
    if base.startswith("ws://"):
        return "http://" + base[len("ws://") :], base
    if base.startswith("https://"):
        return base, "wss://" + base[len("https://") :]
    if base.startswith("http://"):
        return base, "ws://" + base[len("http://") :]
    return f"https://{base}", f"wss://{base}"


def _pcm16_rms(pcm_bytes: bytes) -> float:
    """Compute RMS amplitude of 16-bit signed LE PCM bytes for PSTN voice/IVR energy detection."""
    even = len(pcm_bytes) - (len(pcm_bytes) % 2)
    if even <= 0:
        return 0.0
    samples = array("h")
    samples.frombytes(pcm_bytes[:even])
    if not samples:
        return 0.0
    acc = sum(s * s for s in samples)
    return math.sqrt(acc / len(samples))


class TimelineCallRecorder:
    """True-timeline 2-track (inbound caller + outbound agent playhead) 24kHz PCM16 call mixer.

    Prevents full-duplex WebSocket interleaving stutter where continuous 20ms carrier frames
    would otherwise slice bursty Gemini speech chunks and stretch call duration.
    """

    def __init__(self, sample_rate: int = 24000, start_ts: float | None = None) -> None:
        self.sample_rate = sample_rate
        self.start_ts = time.monotonic() if start_ts is None else start_ts
        self._inbound = array("h")
        self._outbound = array("h")
        self._inbound_cursor = 0
        self._outbound_playhead = 0
        self._outbound_spans: list[tuple[int, int]] = []

    def append_inbound(self, pcm24: bytes, now_ts: float | None = None) -> None:
        even = len(pcm24) - (len(pcm24) % 2)
        if even <= 0:
            return
        chunk = array("h")
        chunk.frombytes(pcm24[:even])
        now = time.monotonic() if now_ts is None else now_ts
        wall_sample = max(0, int((now - self.start_ts) * self.sample_rate))
        # Resync cursor only if RTP stream paused by >120ms; otherwise preserve sample-exact continuity
        if wall_sample > self._inbound_cursor + int(0.12 * self.sample_rate):
            pad = wall_sample - len(self._inbound)
            if pad > 0:
                self._inbound.extend([0] * pad)
            self._inbound_cursor = len(self._inbound)
        self._inbound.extend(chunk)
        self._inbound_cursor = len(self._inbound)

    def append_outbound(self, pcm24: bytes, now_ts: float | None = None) -> None:
        even = len(pcm24) - (len(pcm24) % 2)
        if even <= 0:
            return
        chunk = array("h")
        chunk.frombytes(pcm24[:even])
        now = time.monotonic() if now_ts is None else now_ts
        wall_sample = max(0, int((now - self.start_ts) * self.sample_rate))
        start_sample = max(wall_sample, self._outbound_playhead)
        if len(self._outbound) < start_sample:
            self._outbound.extend([0] * (start_sample - len(self._outbound)))
        end_sample = start_sample + len(chunk)
        if start_sample == len(self._outbound):
            self._outbound.extend(chunk)
        else:
            if len(self._outbound) < end_sample:
                self._outbound.extend([0] * (end_sample - len(self._outbound)))
            self._outbound[start_sample:end_sample] = chunk
        self._outbound_playhead = end_sample
        if self._outbound_spans and self._outbound_spans[-1][1] >= start_sample:
            prev_s, prev_e = self._outbound_spans[-1]
            self._outbound_spans[-1] = (prev_s, max(prev_e, end_sample))
        else:
            self._outbound_spans.append((start_sample, end_sample))

    def clear_unplayed_outbound(self, now_ts: float | None = None) -> None:
        """Truncate unplayed queued outbound audio on shopkeeper barge-in (`clearAudio`)."""
        now = time.monotonic() if now_ts is None else now_ts
        wall_sample = max(0, int((now - self.start_ts) * self.sample_rate))
        if len(self._outbound) > wall_sample:
            del self._outbound[wall_sample:]
        self._outbound_playhead = min(self._outbound_playhead, wall_sample)
        clipped: list[tuple[int, int]] = []
        for s_idx, e_idx in self._outbound_spans:
            if s_idx < wall_sample:
                clipped.append((s_idx, min(e_idx, wall_sample)))
        self._outbound_spans = clipped

    def remaining_playout_sec(self, now_ts: float | None = None) -> float:
        now = time.monotonic() if now_ts is None else now_ts
        wall_sample = max(0, int((now - self.start_ts) * self.sample_rate))
        return max(0.0, (self._outbound_playhead - wall_sample) / float(self.sample_rate))

    def has_audio(self) -> bool:
        return bool(len(self._inbound) or len(self._outbound))

    def render_pcm24(self) -> bytes:
        n_in = len(self._inbound)
        n_out = len(self._outbound)
        total = max(n_in, n_out)
        if total == 0:
            return b""
        mixed = array("h", self._inbound)
        if total > n_in:
            mixed.extend([0] * (total - n_in))
        out_buf = self._outbound
        for s_idx, e_idx in self._outbound_spans:
            upper = min(e_idx, n_out)
            for i in range(s_idx, upper):
                val = mixed[i] + out_buf[i]
                mixed[i] = -32768 if val < -32768 else (32767 if val > 32767 else val)
        return mixed.tobytes()


def _maybe_mark_quest_complete(quest: QuestSession, publish: EventPublishFn) -> None:
    """Mark a live quest completed once every call reaches a terminal state."""
    if quest.status != "running":
        return
    terminal = {
        CallStatus.COMPLETED,
        CallStatus.SKIPPED,
        CallStatus.STOPPED,
        CallStatus.FAILED,
    }
    if quest.calls and all(c.status in terminal for c in quest.calls.values()):
        quest.status = "completed"
        publish("quest_complete", {"quest_id": quest.quest_id, "status": "completed"})


@dataclass
class _PrewarmedLiveSlot:
    """Task-safe pre-warmed `gemini-3.8-live` WebSocket slot opened while PSTN is ringing."""

    ready_event: asyncio.Event
    release_event: asyncio.Event
    claimed: bool = False
    session: Any = None
    model_name: str = LIVE_AUDIO_MODEL
    region: str = GCP_LOCATION
    owner_task: asyncio.Task[None] | None = None


class LiveIndiaPstnAdapter:
    """Unified India `+91` live PSTN voice adapter supporting `Vobiz.ai`, `Plivo`, and `Twilio`.

    - `Vobiz.ai` (`CarrierProvider.VOBIZ`): Indian `+91` local DID, `₹0.45/min`, native `L16` 16kHz/24kHz HD audio.
    - `Plivo` (`CarrierProvider.PLIVO`): Bidirectional 8kHz G.711 mu-law WebSocket streaming.
    - `Twilio` (`CarrierProvider.TWILIO`): TwiML `<Connect><Stream>` 8kHz G.711 mu-law WebSocket streaming.
    """

    def __init__(self, audio_store: dict[tuple[str, str], bytes]) -> None:
        self._audio_store = audio_store
        self._runtime_config: LiveCarrierConfig | None = None
        self._prewarmed_slots: dict[tuple[str, str], _PrewarmedLiveSlot] = {}
        self._cli_rr_index: int = 0

    def _pick_outbound_cli(self, cfg: LiveCarrierConfig) -> str:
        """Rotate across configured clean DIDs (`from_numbers`) to avoid single-CLI carrier spam triggers."""
        pool = [n.strip() for n in (cfg.from_numbers or []) if n and n.strip()]
        if cfg.from_number and cfg.from_number.strip() not in pool:
            pool.insert(0, cfg.from_number.strip())
        if not pool:
            return cfg.from_number
        chosen = pool[self._cli_rr_index % len(pool)]
        self._cli_rr_index += 1
        return chosen

    def _release_prewarm_slot(self, quest_id: str, shop_id: str) -> None:
        slot = self._prewarmed_slots.pop((quest_id, shop_id), None)
        if slot is not None:
            slot.release_event.set()

    def _start_prewarm_slot(
        self, quest: QuestSession, shop: IndianShopTarget, http_base: str
    ) -> None:
        """Open `gemini-3.8-live` while Vobiz/Plivo/Twilio is ringing so pickup latency is 0ms."""
        if "testserver" in http_base or (
            ("127.0.0.1" in http_base or "localhost" in http_base)
            and not os.environ.get("K_SERVICE")
        ):
            return
        key = (quest.quest_id, shop.shop_id)
        self._release_prewarm_slot(quest.quest_id, shop.shop_id)
        slot = _PrewarmedLiveSlot(
            ready_event=asyncio.Event(),
            release_event=asyncio.Event(),
        )
        self._prewarmed_slots[key] = slot

        live_cfg = build_thin_live_config(
            shop_name=shop.name,
            locality=shop.locality or quest.locality,
            user_goal=quest.user_goal,
            vocal_tone=quest.preferred_tone,
            is_live_pstn=True,
            call_plan=quest.call_plan,
            voice_name=quest.voice_name,
            shop_address=shop.address,
            place_category=quest.place_category,
            caller_name=quest.caller_name,
        )

        async def _owner() -> None:
            try:
                live_client = get_vertex_client(GCP_LOCATION)
                async with live_client.aio.live.connect(
                    model=LIVE_AUDIO_MODEL, config=live_cfg
                ) as sess:
                    slot.session = sess
                    slot.ready_event.set()
                    try:
                        await asyncio.wait_for(slot.release_event.wait(), timeout=46.0)
                    except TimeoutError:
                        if slot.claimed:
                            await asyncio.wait_for(
                                slot.release_event.wait(),
                                timeout=MAX_CALL_DURATION_SEC + 5.0,
                            )
            except Exception as exc:
                logger.info("Prewarmed Gemini Live slot %s closed: %s", key, exc)
            finally:
                slot.session = None
                slot.ready_event.set()
                if self._prewarmed_slots.get(key) is slot:
                    self._prewarmed_slots.pop(key, None)

        slot.owner_task = asyncio.create_task(_owner())

    def set_runtime_config(self, cfg: LiveCarrierConfig | None) -> None:
        """Store runtime carrier credentials in RAM (never written to disk)."""
        self._runtime_config = cfg

    @staticmethod
    def _env_config() -> LiveCarrierConfig | None:
        v_id = get_secret("VOBIZ_AUTH_ID")
        v_tok = get_secret("VOBIZ_AUTH_TOKEN")
        v_from = get_secret("VOBIZ_FROM_NUMBER")
        if v_id and v_tok and v_from:
            return LiveCarrierConfig(
                provider=CarrierProvider.VOBIZ,
                auth_id=v_id,
                auth_token=v_tok,
                from_number=v_from,
                public_base_url=os.environ.get("PUBLIC_BASE_URL")
                or os.environ.get("PUBLIC_WSS_BASE_URL"),
            )
        if all(
            os.environ.get(k, "").strip()
            for k in ("PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN", "PLIVO_FROM_NUMBER")
        ):
            return LiveCarrierConfig(
                provider=CarrierProvider.PLIVO,
                auth_id=os.environ["PLIVO_AUTH_ID"].strip(),
                auth_token=os.environ["PLIVO_AUTH_TOKEN"].strip(),
                from_number=os.environ["PLIVO_FROM_NUMBER"].strip(),
                public_base_url=os.environ.get("PUBLIC_BASE_URL")
                or os.environ.get("PUBLIC_WSS_BASE_URL"),
            )
        if all(
            os.environ.get(k, "").strip()
            for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER")
        ):
            return LiveCarrierConfig(
                provider=CarrierProvider.TWILIO,
                auth_id=os.environ["TWILIO_ACCOUNT_SID"].strip(),
                auth_token=os.environ["TWILIO_AUTH_TOKEN"].strip(),
                from_number=os.environ["TWILIO_FROM_NUMBER"].strip(),
                public_base_url=os.environ.get("PUBLIC_BASE_URL")
                or os.environ.get("PUBLIC_WSS_BASE_URL"),
            )
        return None

    def resolve_config(self) -> LiveCarrierConfig | None:
        return self._runtime_config or self._env_config()

    def is_configured(self) -> bool:
        return self.resolve_config() is not None

    def masked_status(self) -> dict[str, Any]:
        cfg = self.resolve_config()
        if not cfg:
            return {
                "configured": False,
                "provider": None,
                "auth_id_masked": None,
                "from_number": None,
                "public_base_url": os.environ.get("PUBLIC_BASE_URL"),
            }
        return {
            "configured": True,
            "provider": cfg.provider.value,
            "auth_id_masked": _mask_identifier(cfg.auth_id),
            "from_number": cfg.from_number,
            "from_numbers": cfg.from_numbers or [cfg.from_number],
            "pool_size": len(set([cfg.from_number] + list(cfg.from_numbers or []))),
            "public_base_url": cfg.public_base_url or os.environ.get("PUBLIC_BASE_URL"),
        }

    @staticmethod
    def _hmac_secret() -> bytes:
        raw = get_secret("RINGSCOUT_HMAC_SECRET") or _PROCESS_HMAC_FALLBACK
        return raw.encode("utf-8")

    @classmethod
    def sign_pstn_token(cls, provider: str, quest_id: str, shop_id: str) -> str:
        msg = f"{provider}:{quest_id}:{shop_id}".encode("utf-8")
        return hmac.new(cls._hmac_secret(), msg, hashlib.sha256).hexdigest()

    @classmethod
    def verify_pstn_token(
        cls, provider: str, quest_id: str, shop_id: str, sig: str | None
    ) -> bool:
        if not sig:
            return False
        expected = cls.sign_pstn_token(provider, quest_id, shop_id)
        return hmac.compare_digest(expected, sig.strip())

    @staticmethod
    def build_answer_xml(provider: CarrierProvider, ws_stream_url: str) -> str:
        """Build carrier-specific bidirectional WebSocket stream XML."""
        escaped_url = xml_escape(
            ws_stream_url, entities={'"': "&quot;", "'": "&apos;"}
        )
        if provider == CarrierProvider.VOBIZ:
            return (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Response><Stream bidirectional="true" keepCallAlive="true" '
                f'contentType="audio/x-l16;rate=16000">{escaped_url}</Stream></Response>'
            )
        if provider == CarrierProvider.PLIVO:
            return (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Response><Stream bidirectional="true" keepCallAlive="true" '
                f'contentType="audio/x-mulaw;rate=8000">{escaped_url}</Stream></Response>'
            )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<Response><Connect><Stream url="{escaped_url}" /></Connect></Response>'
        )

    async def place_india_call(
        self,
        quest: QuestSession,
        shop: IndianShopTarget,
        public_base_url: str,
        publish: EventPublishFn,
    ) -> str:
        """Initiate an outbound PSTN call to a `+91` shop via Vobiz.ai, Plivo, or Twilio."""
        call = quest.calls[shop.shop_id]
        cfg = self.resolve_config()
        if not cfg:
            call.status = CallStatus.FAILED
            call.outcome = CallOutcome.ERROR
            call.transcript.append(
                TranscriptTurn(
                    speaker="system",
                    text="No PSTN carrier configured. Add Vobiz.ai, Plivo, or Twilio credentials first.",
                    timestamp_sec=0.0,
                )
            )
            publish(
                "call_update",
                {"quest_id": quest.quest_id, "shop_id": shop.shop_id, "call": call.model_dump()},
            )
            _maybe_mark_quest_complete(quest, publish)
            return ""

        effective_base = (cfg.public_base_url or public_base_url).strip()
        http_base, ws_base = _normalize_public_urls(effective_base)
        prov = cfg.provider.value
        sig = self.sign_pstn_token(prov, quest.quest_id, shop.shop_id)
        qs = urlencode({"sig": sig})

        answer_url = f"{http_base}/api/pstn/answer/{prov}/{quest.quest_id}/{shop.shop_id}?{qs}"
        status_url = f"{http_base}/api/pstn/status/{prov}/{quest.quest_id}/{shop.shop_id}?{qs}"
        ws_stream_url = f"{ws_base}/ws/pstn/stream/{prov}/{quest.quest_id}/{shop.shop_id}?{qs}"

        call.status = CallStatus.RINGING
        call.carrier_provider = prov
        publish(
            "call_update",
            {"quest_id": quest.quest_id, "shop_id": shop.shop_id, "call": call.model_dump()},
        )

        outbound_cli = self._pick_outbound_cli(cfg)

        try:
            if cfg.provider == CarrierProvider.VOBIZ:
                async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http:
                    resp = await http.post(
                        f"https://api.vobiz.ai/api/v1/Account/{cfg.auth_id}/Call/",
                        headers={
                            "X-Auth-ID": cfg.auth_id,
                            "X-Auth-Token": cfg.auth_token,
                            "Content-Type": "application/json",
                        },
                        json={
                            "from": outbound_cli,
                            "to": shop.phone_e164,
                            "answer_url": answer_url,
                            "answer_method": "POST",
                            "hangup_url": status_url,
                            "hangup_method": "POST",
                            "ring_timeout": 42,
                            "time_limit": int(MAX_CALL_DURATION_SEC),
                        },
                    )
                    resp.raise_for_status()
                    body = resp.json()
                    call_sid = str(
                        body.get("request_uuid")
                        or body.get("call_uuid")
                        or body.get("api_id")
                        or ""
                    )
            elif cfg.provider == CarrierProvider.PLIVO:
                async with httpx.AsyncClient(
                    timeout=15.0, auth=(cfg.auth_id, cfg.auth_token)
                ) as http:
                    resp = await http.post(
                        f"https://api.plivo.com/v1/Account/{cfg.auth_id}/Call/",
                        json={
                            "from": outbound_cli,
                            "to": shop.phone_e164,
                            "answer_url": answer_url,
                            "answer_method": "POST",
                            "hangup_url": status_url,
                            "hangup_method": "POST",
                            "ring_timeout": 42,
                            "time_limit": int(MAX_CALL_DURATION_SEC),
                        },
                    )
                    resp.raise_for_status()
                    body = resp.json()
                    call_sid = str(body.get("request_uuid") or body.get("call_uuid") or "")
            else:
                twiml = self.build_answer_xml(CarrierProvider.TWILIO, ws_stream_url)
                async with httpx.AsyncClient(
                    timeout=15.0, auth=(cfg.auth_id, cfg.auth_token)
                ) as http:
                    resp = await http.post(
                        f"https://api.twilio.com/2010-04-01/Accounts/{cfg.auth_id}/Calls.json",
                        data={
                            "To": shop.phone_e164,
                            "From": outbound_cli,
                            "Twiml": twiml,
                            "StatusCallback": status_url,
                            "StatusCallbackMethod": "POST",
                            "Timeout": "42",
                            "TimeLimit": str(int(MAX_CALL_DURATION_SEC)),
                        },
                    )
                    resp.raise_for_status()
                    call_sid = str(resp.json().get("sid") or "")

            call.twilio_call_sid = call_sid
            if quest.status == "stopped":
                await self.terminate_india_call(call_sid, prov)
                return call_sid
            self._start_prewarm_slot(quest, shop, http_base)
            if not os.environ.get("PYTEST_CURRENT_TEST"):
                asyncio.create_task(
                    self._ring_timeout_watchdog(quest, shop.shop_id, publish, timeout_sec=47.0)
                )
            publish(
                "call_update",
                {"quest_id": quest.quest_id, "shop_id": shop.shop_id, "call": call.model_dump()},
            )
            return call_sid
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:180] if exc.response is not None else str(exc)
            carrier_msg = "check caller ID / wallet balance / webhook URL."
            if exc.response is not None:
                try:
                    err_j = exc.response.json()
                    raw_e = str(err_j.get("error") or err_j.get("message") or "").strip()
                    if raw_e:
                        carrier_msg = raw_e[:140]
                except Exception:
                    pass
            logger.warning(
                "PSTN call via %s failed for %s (%s): %s",
                prov,
                shop.name,
                exc.response.status_code,
                detail,
            )
            call.status = CallStatus.FAILED
            call.outcome = CallOutcome.ERROR
            call.transcript.append(
                TranscriptTurn(
                    speaker="system",
                    text=f"{prov.upper()} HTTP {exc.response.status_code}: {carrier_msg}",
                    timestamp_sec=0.0,
                )
            )
            publish(
                "call_update",
                {"quest_id": quest.quest_id, "shop_id": shop.shop_id, "call": call.model_dump()},
            )
            _maybe_mark_quest_complete(quest, publish)
            return ""
        except Exception as exc:
            logger.warning("PSTN call via %s failed for %s: %s", prov, shop.name, exc)
            call.status = CallStatus.FAILED
            call.outcome = CallOutcome.ERROR
            call.transcript.append(
                TranscriptTurn(
                    speaker="system",
                    text=f"{prov.upper()} dial error: {type(exc).__name__}",
                    timestamp_sec=0.0,
                )
            )
            publish(
                "call_update",
                {"quest_id": quest.quest_id, "shop_id": shop.shop_id, "call": call.model_dump()},
            )
            _maybe_mark_quest_complete(quest, publish)
            return ""

    async def terminate_india_call(
        self, call_sid: str, provider_override: str | None = None
    ) -> None:
        cfg = self.resolve_config()
        if not call_sid or not cfg:
            return
        provider = (
            CarrierProvider(provider_override)
            if provider_override in {p.value for p in CarrierProvider}
            else cfg.provider
        )
        try:
            if provider == CarrierProvider.VOBIZ:
                async with httpx.AsyncClient(timeout=10.0) as http:
                    await http.delete(
                        f"https://api.vobiz.ai/api/v1/Account/{cfg.auth_id}/Call/{call_sid}/",
                        headers={
                            "X-Auth-ID": cfg.auth_id,
                            "X-Auth-Token": cfg.auth_token,
                        },
                    )
            elif provider == CarrierProvider.PLIVO:
                async with httpx.AsyncClient(
                    timeout=10.0, auth=(cfg.auth_id, cfg.auth_token)
                ) as http:
                    await http.delete(
                        f"https://api.plivo.com/v1/Account/{cfg.auth_id}/Call/{call_sid}/"
                    )
            else:
                async with httpx.AsyncClient(
                    timeout=10.0, auth=(cfg.auth_id, cfg.auth_token)
                ) as http:
                    await http.post(
                        f"https://api.twilio.com/2010-04-01/Accounts/{cfg.auth_id}/Calls/{call_sid}.json",
                        data={"Status": "completed"},
                    )
        except Exception as exc:
            logger.warning("Failed to hang up %s call %s: %s", provider.value, call_sid, exc)

    async def send_india_dtmf(
        self, call_sid: str, digit: str, provider_override: str | None = None
    ) -> None:
        """Send out-of-band RFC2833 DTMF digit on Vobiz/Plivo active call legs."""
        cfg = self.resolve_config()
        clean_digit = digit.strip()[:1]
        if not call_sid or not cfg or clean_digit not in _DTMF_FREQS:
            return
        provider = (
            CarrierProvider(provider_override)
            if provider_override in {p.value for p in CarrierProvider}
            else cfg.provider
        )
        try:
            if provider == CarrierProvider.VOBIZ:
                async with httpx.AsyncClient(timeout=8.0) as http:
                    await http.post(
                        f"https://api.vobiz.ai/api/v1/Account/{cfg.auth_id}/Call/{call_sid}/DTMF/",
                        headers={
                            "X-Auth-ID": cfg.auth_id,
                            "X-Auth-Token": cfg.auth_token,
                            "Content-Type": "application/json",
                        },
                        json={"digits": clean_digit},
                    )
            elif provider == CarrierProvider.PLIVO:
                async with httpx.AsyncClient(
                    timeout=8.0, auth=(cfg.auth_id, cfg.auth_token)
                ) as http:
                    await http.post(
                        f"https://api.plivo.com/v1/Account/{cfg.auth_id}/Call/{call_sid}/DTMF/",
                        json={"digits": clean_digit},
                    )
        except Exception as exc:
            logger.warning("Out-of-band DTMF %s via %s failed: %s", clean_digit, provider.value, exc)

    async def _ring_timeout_watchdog(
        self,
        quest: QuestSession,
        shop_id: str,
        publish: EventPublishFn,
        timeout_sec: float = 47.0,
    ) -> None:
        """Auto-close RINGING legs if the carrier never fires hangup_url on an unanswered ring timeout."""
        await asyncio.sleep(timeout_sec)
        call = quest.calls.get(shop_id)
        if not call or call.status != CallStatus.RINGING or quest.status != "running":
            return
        self._release_prewarm_slot(quest.quest_id, shop_id)
        call.status = CallStatus.SKIPPED
        call.outcome = CallOutcome.BUSY_NO_ANSWER
        call.transcript.append(
            TranscriptTurn(
                speaker="system",
                text="Line rang for 42s with no pickup (Ring Timeout).",
                timestamp_sec=42.0,
            )
        )
        publish(
            "call_update",
            {"quest_id": quest.quest_id, "shop_id": shop_id, "call": call.model_dump()},
        )
        _maybe_mark_quest_complete(quest, publish)

    def handle_status_webhook(
        self,
        quest: QuestSession,
        call: ShopCallState,
        payload: dict[str, Any],
        publish: EventPublishFn,
    ) -> None:
        """Process carrier hangup / unanswered / busy callbacks cleanly."""
        call_uuid = str(
            payload.get("CallUUID")
            or payload.get("CallSid")
            or payload.get("call_uuid")
            or payload.get("RequestUUID")
            or ""
        ).strip()
        if call_uuid and not call.twilio_call_sid:
            call.twilio_call_sid = call_uuid

        raw_status = str(
            payload.get("CallStatus") or payload.get("call_status") or payload.get("Event") or ""
        ).lower()
        hangup_cause = str(
            payload.get("HangupCause")
            or payload.get("HangupCauseName")
            or payload.get("hangup_cause")
            or ""
        ).lower()

        unanswered_statuses = {
            "busy",
            "no-answer",
            "no_answer",
            "timeout",
            "failed",
            "canceled",
            "cancelled",
            "rejected",
            "hangup",
            "completed",
        }
        unanswered_causes = (
            "busy",
            "busy line",
            "no_answer",
            "no answer",
            "ring timeout",
            "no_user_response",
            "subscriber_absent",
            "no_route",
            "temporary_failure",
            "recovery_on_timer_expire",
            "call_rejected",
            "rejected",
            "unallocated",
            "congestion",
            "originator_cancel",
            "normal_clearing",
        )
        if call.status in (CallStatus.QUEUED, CallStatus.RINGING):
            if raw_status in unanswered_statuses or any(
                tok in hangup_cause for tok in unanswered_causes
            ):
                self._release_prewarm_slot(quest.quest_id, call.shop.shop_id)
                call.status = CallStatus.SKIPPED
                call.outcome = CallOutcome.BUSY_NO_ANSWER
                reason_lbl = hangup_cause or raw_status or "unanswered"
                call.transcript.append(
                    TranscriptTurn(
                        speaker="system",
                        text=f"Shop line ended before stream ({reason_lbl}).",
                        timestamp_sec=0.0,
                    )
                )
                publish(
                    "call_update",
                    {
                        "quest_id": quest.quest_id,
                        "shop_id": call.shop.shop_id,
                        "call": call.model_dump(),
                    },
                )
                _maybe_mark_quest_complete(quest, publish)

    async def handle_media_websocket(
        self,
        websocket: WebSocket,
        quest: QuestSession,
        call: ShopCallState,
        stop_event: asyncio.Event,
        publish: EventPublishFn,
        provider: CarrierProvider = CarrierProvider.TWILIO,
    ) -> None:
        """Bidirectional PSTN WebSocket (`Vobiz` L16 HD / `Plivo` & `Twilio` G.711) <-> Vertex Gemini Live."""
        await websocket.accept()
        start_ts = time.monotonic()
        stream_id: str | None = None
        inbound_encoding = (
            "audio/x-l16" if provider == CarrierProvider.VOBIZ else "audio/x-mulaw"
        )
        inbound_rate = 16000 if provider == CarrierProvider.VOBIZ else 8000
        recorder = TimelineCallRecorder(sample_rate=24000, start_ts=start_ts)
        side_brain_lock = asyncio.Lock()
        hangup_event = asyncio.Event()
        polite_close_ready = False
        hangup_scheduled = False
        pending_side_brain_task: asyncio.Task[None] | None = None
        inbound_voice_frames = 0
        last_inbound_voice_ts = 0.0
        fg_voice_frames = 0
        last_fg_voice_ts = 0.0
        last_model_audio_ts = 0.0
        last_silent_recovery_ts = 0.0
        noise_gate_state: dict[str, float] = {}
        ivr_mode_active = False
        last_dtmf_ts = 0.0
        last_shop_chunk_ts = 0.0
        human_beat1_delivered = False
        human_beat1_ts = 0.0
        human_shop_turns = 0
        silent_trunk_bailout = False

        call.status = CallStatus.IN_PROGRESS
        call.carrier_provider = provider.value

        def emit_state() -> None:
            call.duration_sec = round(
                min(time.monotonic() - start_ts, MAX_CALL_DURATION_SEC), 1
            )
            publish(
                "call_update",
                {
                    "quest_id": quest.quest_id,
                    "shop_id": call.shop.shop_id,
                    "call": call.model_dump(),
                },
            )

        emit_state()
        client = get_vertex_client(GCP_LOCATION)
        live_cfg = build_thin_live_config(
            shop_name=call.shop.name,
            locality=call.shop.locality or quest.locality,
            user_goal=quest.user_goal,
            vocal_tone=quest.preferred_tone,
            is_live_pstn=True,
            call_plan=quest.call_plan,
            voice_name=quest.voice_name,
            shop_address=call.shop.address,
            place_category=quest.place_category,
            caller_name=quest.caller_name,
        )

        agent_text_buf: list[str] = []
        shop_text_buf: list[str] = []

        async def schedule_polite_hangup(delay_sec: float = 2.2) -> None:
            await asyncio.sleep(delay_sec)
            hangup_event.set()
            if call.twilio_call_sid:
                await self.terminate_india_call(call.twilio_call_sid, provider.value)

        async def send_pcm24_to_carrier(
            audio_24k: bytes, mix_comfort_bed: bool = False
        ) -> None:
            if not audio_24k:
                return
            shaped_24k = (
                add_pstn_comfort_noise_pcm24(audio_24k)
                if mix_comfort_bed
                else audio_24k
            )
            recorder.append_outbound(shaped_24k)
            if provider == CarrierProvider.TWILIO:
                if stream_id:
                    ulaw_out = _pcm16_24k_to_ulaw_8k(shaped_24k)
                    await websocket.send_json(
                        {
                            "event": "media",
                            "streamSid": stream_id,
                            "media": {
                                "payload": base64.b64encode(ulaw_out).decode("ascii")
                            },
                        }
                    )
            elif provider == CarrierProvider.VOBIZ:
                play_msg: dict[str, Any] = {
                    "event": "playAudio",
                    "media": {
                        "contentType": "audio/x-l16",
                        "sampleRate": 24000,
                        "payload": base64.b64encode(shaped_24k).decode("ascii"),
                    },
                }
                if stream_id:
                    play_msg["streamId"] = stream_id
                await websocket.send_json(play_msg)
            else:
                ulaw_out = _pcm16_24k_to_ulaw_8k(shaped_24k)
                play_msg = {
                    "event": "playAudio",
                    "media": {
                        "contentType": "audio/x-mulaw",
                        "sampleRate": 8000,
                        "payload": base64.b64encode(ulaw_out).decode("ascii"),
                    },
                }
                if stream_id:
                    play_msg["streamId"] = stream_id
                await websocket.send_json(play_msg)

        async def clear_carrier_outbound() -> None:
            recorder.clear_unplayed_outbound()
            try:
                if provider == CarrierProvider.TWILIO and stream_id:
                    await websocket.send_json({"event": "clear", "streamSid": stream_id})
                elif provider in (CarrierProvider.VOBIZ, CarrierProvider.PLIVO):
                    clear_evt: dict[str, Any] = {"event": "clearAudio"}
                    if stream_id:
                        clear_evt["streamId"] = stream_id
                    await websocket.send_json(clear_evt)
            except Exception:
                pass

        async def dispatch_ivr_dtmf(digit: str) -> None:
            nonlocal ivr_mode_active, last_dtmf_ts, human_beat1_delivered, human_shop_turns
            clean_digit = digit.strip()[:1]
            now_d = time.monotonic()
            allow_repeat_stage = last_dtmf_ts > 0.0 and (now_d - last_dtmf_ts) >= 4.5
            if (
                not clean_digit
                or len(call.dtmf_sent) >= 4
                or (clean_digit in call.dtmf_sent and not allow_repeat_stage)
                or (last_dtmf_ts > 0.0 and (now_d - last_dtmf_ts) < 2.6)
            ):
                return
            ivr_mode_active = True
            # Reset pre-transfer turn flags so when a real human answers after the keypad transfer,
            # `post_ivr_human_nudge()` always speaks Turn 1 cleanly!
            human_beat1_delivered = False
            human_shop_turns = 0
            call.disclosure_delivered = False
            last_dtmf_ts = now_d
            noise_gate_state.clear()
            agent_text_buf.clear()
            await clear_carrier_outbound()
            if shop_text_buf:
                ivr_snip = "".join(shop_text_buf).strip()
                shop_text_buf.clear()
                if ivr_snip:
                    call.transcript.append(
                        TranscriptTurn(
                            speaker="shopkeeper",
                            text=f"[IVR] {ivr_snip}",
                            timestamp_sec=round(now_d - start_ts, 1),
                        )
                    )
            call.dtmf_sent.append(clean_digit)
            call.status = CallStatus.IVR_MENU
            call.transcript.append(
                TranscriptTurn(
                    speaker="system",
                    text=f"IVR menu detected — pressed keypad '{clean_digit}' (agent mic muted until human answers).",
                    timestamp_sec=round(now_d - start_ts, 1),
                )
            )
            dtmf_pcm = generate_dtmf_pcm24(clean_digit)
            try:
                await send_pcm24_to_carrier(dtmf_pcm, mix_comfort_bed=False)
            except Exception:
                pass
            if call.twilio_call_sid:
                asyncio.create_task(
                    self.send_india_dtmf(
                        call.twilio_call_sid, clean_digit, provider.value
                    )
                )
            emit_state()

        async def flush_side_brain() -> None:
            nonlocal polite_close_ready
            async with side_brain_lock:
                decision = await evaluate_transcript_side_brain(
                    user_goal=quest.user_goal,
                    shop_name=call.shop.name,
                    transcript=call.transcript,
                    preferred_tone=quest.preferred_tone,
                    place_category=quest.place_category,
                    client=client,
                )
                call.quote = decision_to_inr_quote(decision)
                if decision.dtmf_digit:
                    await dispatch_ivr_dtmf(decision.dtmf_digit)
                if decision.is_voicemail:
                    call.outcome = CallOutcome.VOICEMAIL_SKIPPED
                    hangup_event.set()
                    if call.twilio_call_sid:
                        asyncio.create_task(
                            self.terminate_india_call(call.twilio_call_sid, provider.value)
                        )
                elif decision.should_hangup:
                    polite_close_ready = True
                emit_state()

        inbound_pcm_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=400)
        stream_started = asyncio.Event()

        async def carrier_ws_reader() -> None:
            nonlocal stream_id, inbound_encoding, inbound_rate, inbound_voice_frames, last_inbound_voice_ts
            nonlocal fg_voice_frames, last_fg_voice_ts
            try:
                while not stop_event.is_set() and not hangup_event.is_set():
                    raw = await websocket.receive_text()
                    evt = json.loads(raw)
                    ename = str(evt.get("event") or "").lower()
                    if ename == "start":
                        start_obj = (
                            evt.get("start") if isinstance(evt.get("start"), dict) else {}
                        )
                        stream_id = (
                            str(
                                evt.get("streamId")
                                or start_obj.get("streamSid")
                                or start_obj.get("streamId")
                                or ""
                            )
                            or None
                        )
                        call_id = str(
                            evt.get("callId")
                            or start_obj.get("callSid")
                            or start_obj.get("callId")
                            or ""
                        )
                        if call_id:
                            call.twilio_call_sid = call_id
                        mf = evt.get("mediaFormat") or start_obj.get("mediaFormat") or {}
                        if isinstance(mf, dict):
                            if mf.get("encoding"):
                                inbound_encoding = str(mf["encoding"]).lower()
                            if mf.get("sampleRate"):
                                inbound_rate = int(mf["sampleRate"])
                        logger.info(
                            "PSTN stream started: provider=%s call_id=%s stream_id=%s fmt=%s@%s",
                            provider.value,
                            call_id,
                            stream_id,
                            inbound_encoding,
                            inbound_rate,
                        )
                        stream_started.set()
                    elif ename == "media":
                        if not stream_started.is_set():
                            stream_started.set()
                        media_obj = evt.get("media") or {}
                        b64_payload = media_obj.get("payload")
                        if not b64_payload:
                            continue
                        raw_bytes = base64.b64decode(b64_payload)
                        if "l16" in inbound_encoding or "pcm" in inbound_encoding:
                            pcm16_16k = (
                                raw_bytes
                                if inbound_rate == 16000
                                else _upsample_pcm16_8k_to_16k(raw_bytes)
                            )
                        else:
                            pcm16_16k = _ulaw_8k_to_pcm16_16k(raw_bytes)
                        now_m = time.monotonic()
                        raw_rms = _pcm16_rms(pcm16_16k)
                        if (now_m - start_ts) > 0.20 and raw_rms >= 200.0:
                            inbound_voice_frames += 1
                            last_inbound_voice_ts = now_m
                        # Preserve 100% raw audio in .wav recording, while gating far-field shop chatter for Gemini VAD
                        recorder.append_inbound(
                            _upsample_pcm16_16k_to_24k(pcm16_16k), now_ts=now_m
                        )
                        gated_pcm16, eff_rms = gate_pstn_background_chatter(
                            pcm16_16k, noise_gate_state
                        )
                        if (now_m - start_ts) > 0.20 and eff_rms >= 550.0:
                            fg_voice_frames += 1
                            last_fg_voice_ts = now_m
                        if inbound_pcm_queue.full():
                            try:
                                inbound_pcm_queue.get_nowait()
                            except asyncio.QueueEmpty:
                                pass
                        inbound_pcm_queue.put_nowait(gated_pcm16)
                    elif ename == "stop":
                        hangup_event.set()
                        inbound_pcm_queue.put_nowait(None)
                        break
            except WebSocketDisconnect:
                hangup_event.set()
                inbound_pcm_queue.put_nowait(None)
            except Exception as exc:
                logger.info("Carrier WS reader ended for %s: %s", call.shop.name, exc)
                hangup_event.set()
                inbound_pcm_queue.put_nowait(None)

        reader_task = asyncio.create_task(carrier_ws_reader())
        timed_out_90s = False
        last_live_err: Exception | None = None
        try:
            async with asyncio.timeout(MAX_CALL_DURATION_SEC):
                try:
                    await asyncio.wait_for(stream_started.wait(), timeout=1.5)
                except TimeoutError:
                    pass

                # Prime handset with 140ms warm comfort room tone so line never feels dead on pickup
                try:
                    await send_pcm24_to_carrier(
                        generate_comfort_bed_pcm24(0.14), mix_comfort_bed=False
                    )
                except Exception:
                    pass

                async def _run_active_live_session(
                    session: Any, pool_model: str, pool_region: str
                ) -> None:
                    nonlocal hangup_scheduled, pending_side_brain_task, ivr_mode_active
                    nonlocal human_beat1_delivered, human_beat1_ts, human_shop_turns
                    nonlocal last_shop_chunk_ts, silent_trunk_bailout
                    nonlocal last_model_audio_ts, last_silent_recovery_ts

                    logger.info(
                        "Connected Gemini Live %s (%s) for %s",
                        pool_model,
                        pool_region,
                        call.shop.name,
                    )

                    async def fast_pickup_greeting() -> None:
                        """Start Turn 1 in ~1.25s on silent pickups, or let native VAD answer 'Hello?' without double-turns."""
                        nonlocal last_silent_recovery_ts, human_shop_turns
                        await asyncio.sleep(1.25)
                        if (
                            not stop_event.is_set()
                            and not hangup_event.is_set()
                            and not ivr_mode_active
                            and not human_beat1_delivered
                            and not call.disclosure_delivered
                            and not shop_text_buf
                            and not call.dtmf_sent
                            and fg_voice_frames == 0
                            and last_silent_recovery_ts == 0.0
                        ):
                            last_silent_recovery_ts = time.monotonic()
                            await session.send_client_content(
                                turns=types.Content(
                                    role="user",
                                    parts=[
                                        types.Part(
                                            text=(
                                                "Shopkeeper picked up silently. "
                                                "Speak your casual Turn 1 availability question now."
                                            )
                                        )
                                    ],
                                ),
                                turn_complete=True,
                            )
                            return

                        # Foreground speech was heard (e.g. 'Hello?' or IVR): let native 320ms VAD respond first,
                        # and only fire fallback nudge if speech paused >=1.05s with no model audio & no prior recovery.
                        deadline = time.monotonic() + 3.8
                        while (
                            not stop_event.is_set()
                            and not hangup_event.is_set()
                            and not ivr_mode_active
                            and not human_beat1_delivered
                            and last_silent_recovery_ts == 0.0
                            and time.monotonic() < deadline
                        ):
                            await asyncio.sleep(0.20)
                            if (
                                last_fg_voice_ts > 0.0
                                and (time.monotonic() - last_fg_voice_ts) >= 1.05
                                and not ivr_mode_active
                                and not human_beat1_delivered
                                and not call.disclosure_delivered
                                and not call.dtmf_sent
                                and last_silent_recovery_ts == 0.0
                            ):
                                pending_hello = "".join(shop_text_buf).strip()
                                if pending_hello and is_ivr_or_hold_announcement(pending_hello):
                                    return
                                if pending_hello:
                                    shop_text_buf.clear()
                                    human_shop_turns += 1
                                    call.transcript.append(
                                        TranscriptTurn(
                                            speaker="shopkeeper",
                                            text=pending_hello,
                                            timestamp_sec=round(time.monotonic() - start_ts, 1),
                                        )
                                    )
                                    emit_state()
                                last_silent_recovery_ts = time.monotonic()
                                prompt_txt = (
                                    f"Shopkeeper answered ({pending_hello!r}). Speak your casual Turn 1 availability question now."
                                    if pending_hello
                                    else "Shopkeeper answered 'Hello?'. Speak your casual Turn 1 availability question now."
                                )
                                await session.send_client_content(
                                    turns=types.Content(
                                        role="user",
                                        parts=[types.Part(text=prompt_txt)],
                                    ),
                                    turn_complete=True,
                                )
                                return

                    async def silent_trunk_watchdog() -> None:
                        """Recover stuck VAD turns in <1.6s over shop noise, and bail out cleanly on dead PBX trunks."""
                        nonlocal silent_trunk_bailout, human_shop_turns, last_silent_recovery_ts
                        recheck_sent = False
                        while not stop_event.is_set() and not hangup_event.is_set():
                            await asyncio.sleep(0.40)
                            now_w = time.monotonic()

                            # 1. Stuck-VAD Recovery: if Gemini transcribed shopkeeper words (`shop_text_buf`)
                            #    and >=1.55s passed with no foreground speech and no model response, flush & answer!
                            if (
                                not ivr_mode_active
                                and shop_text_buf
                                and last_shop_chunk_ts > 0.0
                                and (now_w - last_shop_chunk_ts) >= 1.55
                                and (last_fg_voice_ts == 0.0 or (now_w - last_fg_voice_ts) >= 0.70)
                                and not strip_live_meta_tokens("".join(agent_text_buf)).strip()
                                and (last_model_audio_ts == 0.0 or (now_w - last_model_audio_ts) >= 1.20)
                            ):
                                stuck_shop = "".join(shop_text_buf).strip()
                                shop_text_buf.clear()
                                if stuck_shop:
                                    can_ivr_w = (human_shop_turns == 0) and (not human_beat1_delivered)
                                    if can_ivr_w and is_ivr_or_hold_announcement(stuck_shop):
                                        ivr_mode_active = True
                                        call.status = CallStatus.IVR_MENU
                                        emit_state()
                                    else:
                                        human_shop_turns += 1
                                        call.transcript.append(
                                            TranscriptTurn(
                                                speaker="shopkeeper",
                                                text=stuck_shop,
                                                timestamp_sec=round(now_w - start_ts, 1),
                                            )
                                        )
                                        emit_state()
                                        await flush_side_brain()
                                        if (
                                            not hangup_event.is_set()
                                            and not hangup_scheduled
                                            and (now_w - last_silent_recovery_ts) >= 1.8
                                        ):
                                            if polite_close_ready:
                                                last_silent_recovery_ts = now_w
                                                try:
                                                    await session.send_client_content(
                                                        turns=types.Content(
                                                            role="user",
                                                            parts=[
                                                                types.Part(
                                                                    text=f"Shopkeeper confirmed: '{stuck_shop}'. Say your quick natural thank-you closing now."
                                                                )
                                                            ],
                                                        ),
                                                        turn_complete=True,
                                                    )
                                                except Exception:
                                                    pass
                                            elif not is_human_hold_phrase(stuck_shop):
                                                last_silent_recovery_ts = now_w
                                                try:
                                                    await session.send_client_content(
                                                        turns=types.Content(
                                                            role="user",
                                                            parts=[
                                                                types.Part(
                                                                    text=f"Shopkeeper on the phone just said: '{stuck_shop}'. Answer them directly and naturally in 1 short sentence right now."
                                                                )
                                                            ],
                                                        ),
                                                        turn_complete=True,
                                                    )
                                                except Exception:
                                                    pass

                            # 2. Unanswered IVR extension bailout (35s)
                            if (
                                ivr_mode_active
                                and last_dtmf_ts > 0.0
                                and (now_w - last_dtmf_ts) >= 35.0
                                and (now_w - last_inbound_voice_ts) >= 8.0
                            ):
                                silent_trunk_bailout = True
                                call.transcript.append(
                                    TranscriptTurn(
                                        speaker="system",
                                        text="IVR operator extension rang unanswered for 35s; ended call cleanly.",
                                        timestamp_sec=round(now_w - start_ts, 1),
                                    )
                                )
                                hangup_event.set()
                                if call.twilio_call_sid:
                                    await self.terminate_india_call(
                                        call.twilio_call_sid, provider.value
                                    )
                                return

                            # 3. Silent dead PBX trunk bailout after Beat 1 (11s recheck -> 21s clean hangup)
                            if (
                                ivr_mode_active
                                or not human_beat1_delivered
                                or human_beat1_ts <= 0.0
                                or human_shop_turns > 0
                                or shop_text_buf
                            ):
                                continue
                            quiet_for = (
                                (now_w - last_inbound_voice_ts)
                                if last_inbound_voice_ts > 0.0
                                else (now_w - human_beat1_ts)
                            )
                            since_beat1 = now_w - human_beat1_ts
                            if not recheck_sent and since_beat1 >= 11.0 and quiet_for >= 5.5:
                                recheck_sent = True
                                chk = (
                                    "Haan hello, awaaz aa rahi hai?"
                                    if quest.preferred_tone
                                    in (VocalTone.CONVERSATIONAL_HINGLISH, VocalTone.NATURAL_HINDI)
                                    else "Hello, am I audible?"
                                )
                                try:
                                    await session.send_client_content(
                                        turns=types.Content(
                                            role="user",
                                            parts=[
                                                types.Part(
                                                    text=f"No reply heard yet — say '{chk}' briefly once."
                                                )
                                            ],
                                        ),
                                        turn_complete=True,
                                    )
                                except Exception:
                                    pass
                            elif since_beat1 >= 21.0 and quiet_for >= 7.5:
                                silent_trunk_bailout = True
                                call.transcript.append(
                                    TranscriptTurn(
                                        speaker="system",
                                        text="No human voice on PBX trunk after 20s; ended call cleanly.",
                                        timestamp_sec=round(now_w - start_ts, 1),
                                    )
                                )
                                hangup_event.set()
                                if call.twilio_call_sid:
                                    await self.terminate_india_call(
                                        call.twilio_call_sid, provider.value
                                    )
                                return

                    async def post_ivr_human_nudge() -> None:
                        """Ensure Turn 1 fires promptly after human pickup post-IVR while never talking over active speech."""
                        nonlocal last_silent_recovery_ts
                        await asyncio.sleep(0.95)
                        deadline = time.monotonic() + 2.8
                        while (
                            last_fg_voice_ts > 0.0
                            and (time.monotonic() - last_fg_voice_ts) < 0.65
                            and time.monotonic() < deadline
                            and not stop_event.is_set()
                            and not hangup_event.is_set()
                        ):
                            await asyncio.sleep(0.20)
                        now_p = time.monotonic()
                        if (
                            not stop_event.is_set()
                            and not hangup_event.is_set()
                            and not ivr_mode_active
                            and not human_beat1_delivered
                            and (last_silent_recovery_ts == 0.0 or (now_p - last_silent_recovery_ts) >= 1.8)
                            and not is_ivr_or_hold_announcement("".join(shop_text_buf))
                        ):
                            last_silent_recovery_ts = now_p
                            try:
                                await session.send_client_content(
                                    turns=types.Content(
                                        role="user",
                                        parts=[
                                            types.Part(
                                                text=(
                                                    "Live receptionist just answered after IVR transfer. "
                                                    "Speak your casual Turn 1 availability question now."
                                                )
                                            )
                                        ],
                                    ),
                                    turn_complete=True,
                                )
                            except Exception:
                                pass

                    nudge_task = asyncio.create_task(fast_pickup_greeting())
                    watchdog_task = asyncio.create_task(silent_trunk_watchdog())

                    async def queue_to_gemini() -> None:
                        while not stop_event.is_set() and not hangup_event.is_set():
                            chunk = await inbound_pcm_queue.get()
                            if chunk is None:
                                return
                            buf = bytearray(chunk)
                            # Coalesce up to 4 queued 20ms frames (~100ms max) to avoid WS rate limits
                            for _ in range(4):
                                if inbound_pcm_queue.empty():
                                    break
                                nxt = inbound_pcm_queue.get_nowait()
                                if nxt is None:
                                    await session.send_realtime_input(
                                        audio=types.Blob(
                                            data=bytes(buf),
                                            mime_type="audio/pcm;rate=16000",
                                        )
                                    )
                                    return
                                buf.extend(nxt)
                            await session.send_realtime_input(
                                audio=types.Blob(
                                    data=bytes(buf),
                                    mime_type="audio/pcm;rate=16000",
                                )
                            )

                    async def gemini_to_carrier() -> None:
                        nonlocal hangup_scheduled, pending_side_brain_task, ivr_mode_active
                        nonlocal human_beat1_delivered, human_beat1_ts, human_shop_turns, last_shop_chunk_ts
                        nonlocal last_model_audio_ts, last_silent_recovery_ts
                        turn_had_model_audio = False
                        turn_human_shop_text = ""
                        while not stop_event.is_set() and not hangup_event.is_set():
                            async for srv_msg in session.receive():
                                if stop_event.is_set() or hangup_event.is_set():
                                    return
                                sc = getattr(srv_msg, "server_content", None)
                                if not sc:
                                    continue

                                if getattr(sc, "interrupted", False):
                                    await clear_carrier_outbound()

                                in_tr = getattr(sc, "input_transcription", None)
                                if in_tr and getattr(in_tr, "text", None):
                                    now_in = time.monotonic()
                                    gap_sec = (now_in - last_shop_chunk_ts) if last_shop_chunk_ts > 0.0 else 0.0
                                    if ivr_mode_active and shop_text_buf and gap_sec >= 1.3:
                                        prev_ivr = "".join(shop_text_buf).strip()
                                        shop_text_buf.clear()
                                        if prev_ivr:
                                            call.transcript.append(
                                                TranscriptTurn(
                                                    speaker="shopkeeper",
                                                    text=f"[IVR] {prev_ivr}",
                                                    timestamp_sec=round(now_in - start_ts, 1),
                                                )
                                            )
                                    last_shop_chunk_ts = now_in
                                    shop_text_buf.append(in_tr.text)
                                    buf_joined = "".join(shop_text_buf)

                                    can_enter_ivr = (human_shop_turns == 0) and (not human_beat1_delivered)
                                    has_explicit_keypad = bool(
                                        call.quote is None
                                        and is_ivr_or_hold_announcement(buf_joined)
                                        and any(k in buf_joined.lower() for k in ("dial ", "press ", "dabay", "दबा"))
                                    )
                                    if (can_enter_ivr or has_explicit_keypad) and is_ivr_or_hold_announcement(buf_joined):
                                        if not ivr_mode_active:
                                            ivr_mode_active = True
                                            call.status = CallStatus.IVR_MENU
                                            agent_text_buf.clear()
                                            await clear_carrier_outbound()
                                            emit_state()
                                    elif ivr_mode_active and is_live_human_pickup_after_ivr(
                                        buf_joined, allow_short_utterance_fallback=False
                                    ):
                                        ivr_mode_active = False
                                        call.status = CallStatus.IN_PROGRESS
                                        emit_state()
                                        asyncio.create_task(post_ivr_human_nudge())

                                    if can_enter_ivr or ivr_mode_active or has_explicit_keypad:
                                        allow_repeat = last_dtmf_ts > 0.0 and (now_in - last_dtmf_ts) >= 4.5
                                        sent_block = () if allow_repeat else call.dtmf_sent
                                        fast_digit = extract_ivr_dtmf_digit(
                                            buf_joined,
                                            preferred_tone=quest.preferred_tone,
                                            user_goal=quest.user_goal,
                                            already_sent=sent_block,
                                        )
                                        if fast_digit:
                                            await dispatch_ivr_dtmf(fast_digit)

                                if sc.model_turn and sc.model_turn.parts:
                                    if ivr_mode_active:
                                        agent_text_buf.clear()
                                        continue
                                    now_out = time.monotonic()
                                    last_model_audio_ts = now_out
                                    call.disclosure_delivered = True
                                    if not human_beat1_delivered:
                                        human_beat1_delivered = True
                                        human_beat1_ts = now_out
                                    if call.status == CallStatus.IVR_MENU:
                                        call.status = CallStatus.IN_PROGRESS
                                    for part in sc.model_turn.parts:
                                        if part.inline_data and part.inline_data.data:
                                            turn_had_model_audio = True
                                            await send_pcm24_to_carrier(
                                                part.inline_data.data
                                            )

                                out_tr = getattr(sc, "output_transcription", None)
                                if out_tr and getattr(out_tr, "text", None):
                                    if not ivr_mode_active:
                                        agent_text_buf.append(out_tr.text)
                                    joined_clean_out = (
                                        ""
                                        if ivr_mode_active
                                        else strip_live_meta_tokens(
                                            "".join(agent_text_buf)
                                        ).strip()
                                    )
                                    if joined_clean_out and shop_text_buf:
                                        full_shop = "".join(shop_text_buf).strip()
                                        shop_text_buf.clear()
                                        if full_shop:
                                            can_ivr = (human_shop_turns == 0) and (not human_beat1_delivered)
                                            if can_ivr and is_ivr_or_hold_announcement(full_shop):
                                                ivr_mode_active = True
                                                call.status = CallStatus.IVR_MENU
                                                agent_text_buf.clear()
                                                await clear_carrier_outbound()
                                            else:
                                                if ivr_mode_active and is_live_human_pickup_after_ivr(
                                                    full_shop, allow_short_utterance_fallback=True
                                                ):
                                                    ivr_mode_active = False
                                                    call.status = CallStatus.IN_PROGRESS
                                                    if not human_beat1_delivered:
                                                        asyncio.create_task(post_ivr_human_nudge())
                                                if not ivr_mode_active:
                                                    human_shop_turns += 1
                                                    turn_human_shop_text = full_shop
                                            prefix = "[IVR] " if ivr_mode_active and not full_shop.startswith("[IVR]") else ""
                                            call.transcript.append(
                                                TranscriptTurn(
                                                    speaker="shopkeeper",
                                                    text=f"{prefix}{full_shop}",
                                                    timestamp_sec=round(
                                                        time.monotonic() - start_ts, 1
                                                    ),
                                                )
                                            )
                                            emit_state()
                                            pending_side_brain_task = asyncio.create_task(
                                                flush_side_brain()
                                            )
                                    if not ivr_mode_active and joined_clean_out:
                                        call.disclosure_delivered = True

                                if getattr(sc, "turn_complete", False):
                                    if shop_text_buf:
                                        full_shop = "".join(shop_text_buf).strip()
                                        shop_text_buf.clear()
                                        if full_shop:
                                            can_ivr = (human_shop_turns == 0) and (not human_beat1_delivered)
                                            if can_ivr and is_ivr_or_hold_announcement(full_shop):
                                                ivr_mode_active = True
                                                call.status = CallStatus.IVR_MENU
                                            elif ivr_mode_active and is_live_human_pickup_after_ivr(
                                                full_shop, allow_short_utterance_fallback=True
                                            ):
                                                ivr_mode_active = False
                                                call.status = CallStatus.IN_PROGRESS
                                                asyncio.create_task(post_ivr_human_nudge())
                                            if not ivr_mode_active:
                                                human_shop_turns += 1
                                                turn_human_shop_text = full_shop
                                            prefix = "[IVR] " if ivr_mode_active and not full_shop.startswith("[IVR]") else ""
                                            call.transcript.append(
                                                TranscriptTurn(
                                                    speaker="shopkeeper",
                                                    text=f"{prefix}{full_shop}",
                                                    timestamp_sec=round(
                                                        time.monotonic() - start_ts, 1
                                                    ),
                                                )
                                            )
                                            emit_state()
                                            await flush_side_brain()
                                    if pending_side_brain_task and not pending_side_brain_task.done():
                                        await asyncio.gather(
                                            pending_side_brain_task, return_exceptions=True
                                        )
                                    full_agent = ""
                                    if agent_text_buf:
                                        if ivr_mode_active:
                                            agent_text_buf.clear()
                                        else:
                                            full_agent = strip_live_meta_tokens(
                                                "".join(agent_text_buf)
                                            ).strip()
                                            agent_text_buf.clear()
                                            if full_agent:
                                                call.transcript.append(
                                                    TranscriptTurn(
                                                        speaker="agent",
                                                        text=full_agent,
                                                        timestamp_sec=round(
                                                            time.monotonic() - start_ts, 1
                                                        ),
                                                    )
                                                )
                                                emit_state()

                                    had_audio_this_turn = turn_had_model_audio
                                    turn_had_model_audio = False
                                    turn_shop_this_turn = turn_human_shop_text
                                    turn_human_shop_text = ""

                                    if not ivr_mode_active:
                                        # Silent-Turn Recovery: if Gemini Beyond emitted `turn_complete` with 0 outbound audio
                                        # (e.g. `🧠[awaiting response]`) after a live shopkeeper spoke, prompt an immediate answer!
                                        now_tc = time.monotonic()
                                        if (
                                            turn_shop_this_turn
                                            and not full_agent
                                            and not had_audio_this_turn
                                            and not hangup_scheduled
                                            and not hangup_event.is_set()
                                            and (now_tc - last_silent_recovery_ts) >= 1.8
                                        ):
                                            if polite_close_ready:
                                                last_silent_recovery_ts = now_tc
                                                try:
                                                    await session.send_client_content(
                                                        turns=types.Content(
                                                            role="user",
                                                            parts=[
                                                                types.Part(
                                                                    text=f"Shopkeeper confirmed: '{turn_shop_this_turn}'. Say your quick natural thank-you closing now."
                                                                )
                                                            ],
                                                        ),
                                                        turn_complete=True,
                                                    )
                                                except Exception:
                                                    pass
                                                continue
                                            elif not is_human_hold_phrase(turn_shop_this_turn):
                                                last_silent_recovery_ts = now_tc
                                                try:
                                                    await session.send_client_content(
                                                        turns=types.Content(
                                                            role="user",
                                                            parts=[
                                                                types.Part(
                                                                    text=f"Shopkeeper on the phone just said: '{turn_shop_this_turn}'. Answer them directly and naturally in 1 short sentence right now."
                                                                )
                                                            ],
                                                        ),
                                                        turn_complete=True,
                                                    )
                                                except Exception:
                                                    pass
                                                continue

                                        farewell_done = (
                                            len(call.transcript) >= 2
                                            and is_agent_farewell_turn(full_agent)
                                        )
                                        async with side_brain_lock:
                                            should_close_now = (
                                                (bool(full_agent) or had_audio_this_turn)
                                                and polite_close_ready
                                                and "?" not in full_agent
                                            ) or farewell_done
                                        if (
                                            should_close_now
                                            and not hangup_scheduled
                                            and not hangup_event.is_set()
                                        ):
                                            hangup_scheduled = True
                                            playout_wait = min(
                                                6.0,
                                                max(
                                                    1.8,
                                                    recorder.remaining_playout_sec() + 0.65,
                                                ),
                                            )
                                            asyncio.create_task(
                                                schedule_polite_hangup(playout_wait)
                                            )

                    done, pending = await asyncio.wait(
                        [
                            asyncio.create_task(queue_to_gemini()),
                            asyncio.create_task(gemini_to_carrier()),
                            asyncio.create_task(stop_event.wait()),
                            asyncio.create_task(hangup_event.wait()),
                        ],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    nudge_task.cancel()
                    watchdog_task.cancel()
                    await asyncio.gather(
                        nudge_task, watchdog_task, return_exceptions=True
                    )
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    for task in done:
                        if not task.cancelled():
                            exc = task.exception()
                            if exc and not isinstance(exc, WebSocketDisconnect):
                                raise exc

                # 1. Claim pre-warmed `gemini-3.8-live` session (0ms pickup!) if opened while ringing
                used_prewarm = False
                slot = self._prewarmed_slots.get((quest.quest_id, call.shop.shop_id))
                if slot is not None and not slot.claimed:
                    slot.claimed = True
                    try:
                        await asyncio.wait_for(slot.ready_event.wait(), timeout=2.5)
                        if slot.session is not None:
                            used_prewarm = True
                            await _run_active_live_session(
                                slot.session, slot.model_name, slot.region
                            )
                            last_live_err = None
                    except TimeoutError:
                        used_prewarm = False
                    except Exception as pre_exc:
                        logger.warning(
                            "Prewarmed Gemini Live session ended early for %s (%s); reconnecting...",
                            call.shop.name,
                            pre_exc,
                        )
                        used_prewarm = call.disclosure_delivered
                    finally:
                        self._release_prewarm_slot(quest.quest_id, call.shop.shop_id)

                # 2. Fallback to on-demand `gemini-3.8-live` (`LIVE_FALLBACK_TARGETS`) if not prewarmed
                if not used_prewarm:
                    for pool_region, pool_model in LIVE_FALLBACK_TARGETS:
                        if stop_event.is_set() or hangup_event.is_set():
                            break
                        try:
                            live_client = get_vertex_client(pool_region)
                            async with live_client.aio.live.connect(
                                model=pool_model, config=live_cfg
                            ) as session:
                                await _run_active_live_session(
                                    session, pool_model, pool_region
                                )
                                last_live_err = None
                                break
                        except TimeoutError:
                            raise
                        except Exception as pool_exc:
                            last_live_err = pool_exc
                            logger.warning(
                                "Gemini Live pool %s/%s failed for %s (%s); failing over...",
                                pool_region,
                                pool_model,
                                call.shop.name,
                                pool_exc,
                            )
                            await asyncio.sleep(0.15)
        except TimeoutError:
            timed_out_90s = True
            if call.twilio_call_sid:
                await self.terminate_india_call(call.twilio_call_sid, provider.value)
        except WebSocketDisconnect:
            logger.info("PSTN media stream disconnected for %s", call.shop.name)
        except Exception as exc:
            last_live_err = exc
            logger.info("PSTN media stream ended for %s: %s", call.shop.name, exc)
        finally:
            self._release_prewarm_slot(quest.quest_id, call.shop.shop_id)
            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)
            if last_live_err and not call.disclosure_delivered and not stop_event.is_set():
                call.transcript.append(
                    TranscriptTurn(
                        speaker="system",
                        text=f"Voice model error: {str(last_live_err)[:140]}",
                        timestamp_sec=round(time.monotonic() - start_ts, 1),
                    )
                )
            for buf, spk in ((shop_text_buf, "shopkeeper"), (agent_text_buf, "agent")):
                if buf:
                    raw_rem = "".join(buf)
                    rem = (
                        strip_live_meta_tokens(raw_rem).strip()
                        if spk == "agent"
                        else raw_rem.strip()
                    )
                    if rem:
                        call.transcript.append(
                            TranscriptTurn(
                                speaker=spk,
                                text=rem,
                                timestamp_sec=round(time.monotonic() - start_ts, 1),
                            )
                        )

            if call.transcript:
                decision = await evaluate_transcript_side_brain(
                    user_goal=quest.user_goal,
                    shop_name=call.shop.name,
                    transcript=call.transcript,
                    preferred_tone=quest.preferred_tone,
                    place_category=quest.place_category,
                    client=client,
                )
                call.quote = decision_to_inr_quote(decision)

            if recorder.has_audio():
                self._audio_store[(quest.quest_id, call.shop.shop_id)] = pcm16_to_wav_bytes(
                    recorder.render_pcm24(), sample_rate=24000
                )
                call.audio_url = (
                    f"/api/quests/{quest.quest_id}/calls/{call.shop.shop_id}/audio.wav"
                )

            if stop_event.is_set():
                if call.twilio_call_sid:
                    await self.terminate_india_call(call.twilio_call_sid, provider.value)
                call.status = CallStatus.STOPPED
                call.outcome = CallOutcome.STOPPED_BY_USER
            elif timed_out_90s:
                call.status = CallStatus.COMPLETED
                call.outcome = CallOutcome.TIMED_OUT_90S
            elif silent_trunk_bailout and (not call.quote or call.quote.in_stock is None):
                call.status = CallStatus.SKIPPED
                call.outcome = CallOutcome.BUSY_NO_ANSWER
            elif call.outcome == CallOutcome.VOICEMAIL_SKIPPED:
                call.status = CallStatus.SKIPPED
            else:
                call.status = CallStatus.COMPLETED
                if call.quote and call.quote.in_stock is True:
                    call.outcome = (
                        CallOutcome.IVR_NAVIGATED
                        if call.dtmf_sent
                        else CallOutcome.CONNECTED_QUOTE
                    )
                elif call.quote and call.quote.in_stock is False:
                    call.outcome = CallOutcome.CONNECTED_NO_STOCK
                else:
                    call.outcome = CallOutcome.CONNECTED_INCONCLUSIVE
            emit_state()
            _maybe_mark_quest_complete(quest, publish)


# Backwards-compatible alias matching AGENTS.md
TwilioToIndiaAdapter = LiveIndiaPstnAdapter
