"""Seamless Local Cloudtop <-> Cloud Run (`manav-chub`) Vertex & PSTN Bridge.

Why this exists:
1. On Cloudtop (`*.c.googlers.com`), personal `gcloud` OAuth / RAPT tokens expire periodically,
   AND external telecom carriers (`Vobiz.ai`, `Plivo`, `Twilio`) cannot reach corp-gated `*.c.googlers.com` WebSockets.
2. Meanwhile, RingScout's deployed Cloud Run service (`https://ringscout-hom7ntreuq-uc.a.run.app`) runs inside
   `manav-chub` with a non-expiring service account (`vertex_google_maps_grounding` + `gemini-2.5-flash` +
   `gemini-2.5-flash-preview-tts` 24kHz audio) AND a public `wss://` endpoint for real `+91` PSTN phone calls.
3. This bridge ensures local preview & local MCP/A2A testing ALWAYS get:
   - Real, live Google Maps listings per neighbourhood (never stale static lists)
   - Real 24kHz multi-speaker spoken Indian English / Hinglish `.wav` audio
   - Persistent `~/.config/ringscout/pstn_carrier.json` (`0600`) storage + live PSTN relay via Cloud Run
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any, Callable

import httpx

from ringscout.models import (
    CallOutcome,
    CallStatus,
    DiscoverRequest,
    IndianShopTarget,
    InrQuote,
    LiveCarrierConfig,
    PlaceCategory,
    QuestSession,
    TranscriptTurn,
)
from ringscout.secrets_manager import get_secret

if TYPE_CHECKING:
    from ringscout.carriers import LiveIndiaPstnAdapter

logger = logging.getLogger(__name__)

DEFAULT_CLOUD_RUN_URL = "https://ringscout-hom7ntreuq-uc.a.run.app"
CARRIER_CONFIG_FILE = Path.home() / ".config" / "ringscout" / "pstn_carrier.json"


def is_bridge_enabled() -> bool:
    """Enable Cloud Run bridge only on local Cloudtop/dev (never inside Cloud Run itself or hermetic pytest)."""
    if os.environ.get("K_SERVICE") or os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    if os.environ.get("RINGSCOUT_DISABLE_BRIDGE", "").strip() == "1":
        return False
    return True


def cloud_run_base_url() -> str:
    return (
        os.environ.get("RINGSCOUT_CLOUD_RUN_URL", "").strip() or DEFAULT_CLOUD_RUN_URL
    ).rstrip("/")


def _bridge_headers() -> dict[str, str]:
    hdrs = {"Content-Type": "application/json"}
    passcode = (
        get_secret("RINGSCOUT_ADMIN_PASSCODE")
        or os.environ.get("RINGSCOUT_PASSCODE", "").strip()
    )
    if passcode:
        hdrs["X-RingScout-Passcode"] = passcode
    return hdrs


def save_local_carrier_config(cfg: LiveCarrierConfig) -> None:
    """Persist carrier credentials (`0600`) locally so server restarts never lose PSTN setup."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        CARRIER_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CARRIER_CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(CARRIER_CONFIG_FILE)
        os.chmod(CARRIER_CONFIG_FILE, 0o600)
    except Exception as exc:
        logger.warning("Could not persist local PSTN config: %s", exc)


def load_local_carrier_config() -> LiveCarrierConfig | None:
    """Load persisted `0600` carrier config if present on disk."""
    if os.environ.get("PYTEST_CURRENT_TEST") or not CARRIER_CONFIG_FILE.exists():
        return None
    try:
        raw = json.loads(CARRIER_CONFIG_FILE.read_text(encoding="utf-8"))
        return LiveCarrierConfig.model_validate(raw)
    except Exception as exc:
        logger.warning("Could not load persisted PSTN config: %s", exc)
        return None


async def sync_carrier_config_to_cloud_run(cfg: LiveCarrierConfig) -> bool:
    """Mirror carrier credentials to Cloud Run so live PSTN WebSockets (`wss://...run.app`) can dial out."""
    if not is_bridge_enabled():
        return False
    base = cloud_run_base_url()
    payload = {
        "provider": cfg.provider.value,
        "auth_id": cfg.auth_id,
        "auth_token": cfg.auth_token,
        "from_number": cfg.from_number,
        "public_base_url": cfg.public_base_url or base,
    }
    try:
        async with httpx.AsyncClient(timeout=12.0) as http:
            resp = await http.post(f"{base}/api/pstn/config", headers=_bridge_headers(), json=payload)
            return resp.status_code == 200
    except Exception as exc:
        logger.warning("Cloud Run PSTN config sync skipped: %s", exc)
        return False


async def fetch_places_via_cloud_run_bridge(
    enriched_query: str,
    city: str,
    locality: str,
    max_shops: int,
) -> tuple[list[dict[str, Any]], str] | None:
    """Call `manav-chub` Cloud Run `/api/discover` to get real live Vertex Google Maps places."""
    if not is_bridge_enabled():
        return None
    base = cloud_run_base_url()
    try:
        async with httpx.AsyncClient(timeout=28.0) as http:
            resp = await http.post(
                f"{base}/api/discover",
                headers=_bridge_headers(),
                json={
                    "query": enriched_query[:380],
                    "city": city,
                    "locality": locality,
                    "max_shops": min(max(max_shops + 4, 8), 15),
                },
            )
            if resp.status_code != 200:
                logger.warning("Cloud Run discover bridge returned HTTP %s", resp.status_code)
                return None
            data = resp.json()
            shops = data.get("shops") or []
            raw_items: list[dict[str, Any]] = []
            for s in shops:
                raw_items.append(
                    {
                        "name": s.get("name") or "",
                        "phone": s.get("phone_e164") or s.get("phone_display") or "",
                        "address": s.get("address") or "",
                        "locality": s.get("locality") or locality,
                        "city": s.get("city") or city,
                        "open_status": s.get("open_status") or "OPEN NOW",
                        "is_open_now": s.get("is_open_now", True),
                        "rating": s.get("rating"),
                        "maps_uri": s.get("maps_uri"),
                        "category": s.get("category") or "Google Maps Listing",
                    }
                )
            return raw_items, str(data.get("source") or "vertex_google_maps_grounding")
    except Exception as exc:
        logger.warning("Cloud Run discover bridge error: %s", exc)
        return None


def _default_availability_label(cat: PlaceCategory, in_stock: bool | None) -> str:
    if in_stock is True:
        return {
            PlaceCategory.HOTEL: "ROOM AVAILABLE",
            PlaceCategory.RESTAURANT: "TABLE AVAILABLE",
            PlaceCategory.CLINIC: "SLOT OPEN TODAY",
            PlaceCategory.DIAGNOSTICS: "SLOT OPEN TODAY",
            PlaceCategory.PETROL: "PUMP / CHARGER ACTIVE",
            PlaceCategory.SPA: "SPA SLOT OPEN",
            PlaceCategory.SALON: "APPOINTMENT OPEN",
            PlaceCategory.AUTO: "IN STOCK · READY",
            PlaceCategory.PHARMACY: "IN STOCK",
        }.get(cat, "AVAILABLE NOW")
    if in_stock is False:
        return "UNAVAILABLE TODAY"
    return "CHECKED"


async def relay_quest_via_cloud_run(
    *,
    quest: QuestSession,
    mode_str: str,
    audio_store: dict[tuple[str, str], bytes],
    publish: Callable[[str, dict[str, Any]], None],
    stop_event: asyncio.Event,
    pstn_adapter: "LiveIndiaPstnAdapter | None" = None,
) -> bool:
    """Execute `hinglish_swarm` (real 24kHz Vertex TTS) or `live_pstn` (real +91 calls) via Cloud Run."""
    if not is_bridge_enabled():
        return False
    base = cloud_run_base_url()
    hdrs = _bridge_headers()

    try:
        async with httpx.AsyncClient(timeout=45.0) as http:
            if mode_str == "live_pstn" and pstn_adapter is not None:
                cfg = pstn_adapter.resolve_config()
                if cfg is not None:
                    await sync_carrier_config_to_cloud_run(cfg)

            # Fast-path: try starting the quest immediately (if /api/discover just ran on this Cloud Run instance)
            quest_body = {
                "user_goal": (
                    f"{quest.user_goal} (Caller first name: {quest.caller_name})"
                    if quest.caller_name and quest.caller_name.lower() not in quest.user_goal.lower()
                    else quest.user_goal
                ),
                "place_category": quest.place_category.value,
                "caller_name": quest.caller_name,
                "city": quest.city,
                "locality": quest.locality,
                "preferred_tone": quest.preferred_tone.value,
                "voice_name": quest.voice_name,
                "mode": mode_str,
                "shops": [s.model_dump() for s in quest.shops],
            }
            start_resp = await http.post(f"{base}/api/quests", headers=hdrs, json=quest_body)

            # If Cloud Run autoscaled or cold-started, re-verify every selected Google Maps / custom shop in parallel and retry
            if start_resp.status_code == 400 and "unverified" in start_resp.text.lower():
                reverify_tasks = []
                for s in quest.shops:
                    if s.shop_id.startswith("custom_"):
                        reverify_tasks.append(
                            http.post(
                                f"{base}/api/places/add",
                                headers=hdrs,
                                json={
                                    "place_name": s.name,
                                    "phone": s.phone_e164,
                                    "city": s.city or quest.city,
                                    "locality": s.locality or quest.locality,
                                    "place_category": quest.place_category.value,
                                },
                            )
                        )
                    else:
                        reverify_tasks.append(
                            http.post(
                                f"{base}/api/discover",
                                headers=hdrs,
                                json={
                                    "query": f"{s.name} {s.locality or quest.locality}"[:180],
                                    "city": quest.city,
                                    "locality": quest.locality,
                                    "max_shops": 8,
                                },
                            )
                        )
                await asyncio.gather(*reverify_tasks, return_exceptions=True)
                start_resp = await http.post(f"{base}/api/quests", headers=hdrs, json=quest_body)

            if start_resp.status_code != 200:
                err_detail = start_resp.text[:220]
                logger.warning("Cloud Run quest relay returned HTTP %s: %s", start_resp.status_code, err_detail)
                if mode_str == "live_pstn":
                    for sid, local_call in quest.calls.items():
                        local_call.status = CallStatus.FAILED
                        local_call.outcome = CallOutcome.ERROR
                        local_call.transcript.append(
                            TranscriptTurn(
                                speaker="system",
                                text=f"PSTN relay error (HTTP {start_resp.status_code}): {err_detail}",
                                timestamp_sec=0.0,
                            )
                        )
                        publish(
                            "call_update",
                            {"quest_id": quest.quest_id, "shop_id": sid, "call": local_call.model_dump()},
                        )
                    quest.status = "completed"
                    publish("quest_complete", {"quest_id": quest.quest_id, "status": "completed"})
                    return True
                return False

            start_json = start_resp.json()
            remote_qid = start_json["quest"]["quest_id"]
            poll_hdrs = dict(hdrs)
            if start_json.get("quest_token"):
                poll_hdrs["X-Quest-Token"] = str(start_json["quest_token"])
            fetched_audio: set[str] = set()
            cdr_terminal_sids: set[str] = set()
            ring_started_at: dict[str, float] = {}
            last_vobiz_cdr_poll = 0.0
            vobiz_cfg = pstn_adapter.resolve_config() if (mode_str == "live_pstn" and pstn_adapter is not None) else None
            terminal_states = {CallStatus.COMPLETED, CallStatus.SKIPPED, CallStatus.STOPPED, CallStatus.FAILED}
            deadline = time.monotonic() + (150.0 if mode_str == "live_pstn" else 38.0)

            while time.monotonic() < deadline:
                if stop_event.is_set():
                    await http.post(f"{base}/api/quests/{remote_qid}/stop", headers=poll_hdrs)
                    return True

                await asyncio.sleep(0.85)
                poll = await http.get(f"{base}/api/quests/{remote_qid}", headers=poll_hdrs)
                if poll.status_code != 200:
                    continue
                r_quest = poll.json().get("quest") or {}
                r_calls = r_quest.get("calls") or {}
                now_mono = time.monotonic()

                # Live Vobiz CDR sync: Vobiz does not fire answer_url/hangup_url on unanswered Ring Timeout / Busy Line!
                has_ringing = any(
                    sid not in cdr_terminal_sids
                    and (rc.get("status") == "ringing" or quest.calls.get(sid, None) and quest.calls[sid].status == CallStatus.RINGING)
                    for sid, rc in r_calls.items()
                )
                vobiz_cdrs_by_uuid: dict[str, dict[str, Any]] = {}
                if (
                    mode_str == "live_pstn"
                    and has_ringing
                    and vobiz_cfg is not None
                    and vobiz_cfg.provider.value == "vobiz"
                    and (now_mono - last_vobiz_cdr_poll) >= 2.5
                ):
                    last_vobiz_cdr_poll = now_mono
                    try:
                        cdr_resp = await http.get(
                            f"https://api.vobiz.ai/api/v1/Account/{vobiz_cfg.auth_id}/Call/?limit=20",
                            headers={
                                "X-Auth-ID": vobiz_cfg.auth_id,
                                "X-Auth-Token": vobiz_cfg.auth_token,
                            },
                            timeout=6.0,
                        )
                        if cdr_resp.status_code == 200:
                            for obj in cdr_resp.json().get("objects") or []:
                                cuuid = str(obj.get("call_uuid") or "").strip()
                                if cuuid:
                                    vobiz_cdrs_by_uuid[cuuid] = obj
                    except Exception as cdr_exc:
                        logger.debug("Vobiz CDR poll skipped: %s", cdr_exc)

                for sid, rc in r_calls.items():
                    if sid not in quest.calls:
                        continue
                    local_call = quest.calls[sid]
                    if rc.get("twilio_call_sid"):
                        local_call.twilio_call_sid = str(rc["twilio_call_sid"])
                    if rc.get("carrier_provider"):
                        local_call.carrier_provider = str(rc["carrier_provider"])

                    remote_status_str = str(rc.get("status") or "")
                    remote_turns = rc.get("transcript") or []
                    remote_connected = remote_status_str in ("in_progress", "ivr_menu", "completed") or any(
                        t.get("speaker") in ("shopkeeper", "agent") for t in remote_turns
                    )
                    if remote_connected:
                        cdr_terminal_sids.discard(sid)

                    if sid not in cdr_terminal_sids:
                        local_call.status = CallStatus(rc.get("status", local_call.status.value))
                        local_call.outcome = CallOutcome(rc.get("outcome", local_call.outcome.value))
                        local_call.duration_sec = float(rc.get("duration_sec") or 0.0)
                        local_call.dtmf_sent = list(rc.get("dtmf_sent") or [])
                        local_call.transcript = [
                            TranscriptTurn.model_validate(t) for t in remote_turns
                        ]

                        if local_call.status == CallStatus.RINGING and local_call.twilio_call_sid:
                            ring_started_at.setdefault(sid, now_mono)
                            cuuid = local_call.twilio_call_sid or ""
                            cdr_item = vobiz_cdrs_by_uuid.get(cuuid)
                            if cdr_item and cdr_item.get("end_time") and not cdr_item.get("answer_time"):
                                cause = str(cdr_item.get("hangup_cause_name") or "No Answer / Busy")
                                cdr_terminal_sids.add(sid)
                                local_call.status = CallStatus.SKIPPED
                                local_call.outcome = CallOutcome.BUSY_NO_ANSWER
                                local_call.quote = None
                                local_call.transcript = [
                                    TranscriptTurn(
                                        speaker="system",
                                        text=f"Vobiz Telecom: {cause} (line did not answer within 25s).",
                                        timestamp_sec=round(now_mono - ring_started_at[sid], 1),
                                    )
                                ]
                            elif (now_mono - ring_started_at[sid]) >= 45.0 and not local_call.transcript:
                                cdr_terminal_sids.add(sid)
                                local_call.status = CallStatus.SKIPPED
                                local_call.outcome = CallOutcome.BUSY_NO_ANSWER
                                local_call.quote = None
                                local_call.transcript = [
                                    TranscriptTurn(
                                        speaker="system",
                                        text="Line rang for 25s with no pickup (Ring Timeout).",
                                        timestamp_sec=25.0,
                                    )
                                ]

                    rq = rc.get("quote")
                    call_answered_ok = local_call.status not in (
                        CallStatus.SKIPPED,
                        CallStatus.FAILED,
                    ) and local_call.outcome not in (
                        CallOutcome.BUSY_NO_ANSWER,
                        CallOutcome.VOICEMAIL_SKIPPED,
                        CallOutcome.ERROR,
                    )
                    if isinstance(rq, dict) and call_answered_ok:
                        in_st = rq.get("in_stock")
                        avail_lbl = rq.get("availability_label") or _default_availability_label(
                            quest.place_category, in_st
                        )
                        notes_str = str(rq.get("notes") or "").replace("’1,", "₹1,").replace("’2,", "₹2,").replace("’3,", "₹3,")
                        import re as _re_q
                        notes_str = _re_q.sub(r"[’'](?=\d{1,2},\d{3}\b|\d{3,5}\b)", "₹", notes_str)
                        insights = [
                            _re_q.sub(r"[’'](?=\d{1,2},\d{3}\b|\d{3,5}\b)", "₹", str(x))
                            for x in (rq.get("key_insights") or [])
                        ]
                        if not insights and notes_str:
                            insights = [notes_str]
                        if rq.get("eta_or_slot") and rq["eta_or_slot"] not in " ".join(insights):
                            insights.append(f"Timing / Slot: {rq['eta_or_slot']}")
                        p_inr = rq.get("price_inr")
                        p_disp = rq.get("price_display")
                        if p_inr is None and in_st is True and notes_str:
                            from ringscout.voice_engine import extract_hinglish_inr_amount

                            p_inr = extract_hinglish_inr_amount(notes_str)
                            if p_inr is not None and not p_disp:
                                p_disp = f"₹{int(p_inr):,}"
                        local_call.quote = InrQuote(
                            in_stock=in_st,
                            availability_label=avail_lbl,
                            price_inr=p_inr,
                            price_display=p_disp,
                            unit_or_pack=rq.get("unit_or_pack"),
                            eta_or_slot=rq.get("eta_or_slot"),
                            notes=notes_str,
                            key_insights=insights[:3],
                        )
                    elif not call_answered_ok:
                        local_call.quote = None

                    if rc.get("audio_url") and sid not in fetched_audio and local_call.status in terminal_states:
                        wav_r = await http.get(f"{base}{rc['audio_url']}", headers=hdrs)
                        if wav_r.status_code == 200 and len(wav_r.content) > 200:
                            audio_store[(quest.quest_id, sid)] = wav_r.content
                            local_call.audio_url = f"/api/quests/{quest.quest_id}/calls/{sid}/audio.wav"
                            fetched_audio.add(sid)

                    publish(
                        "call_update",
                        {
                            "quest_id": quest.quest_id,
                            "shop_id": sid,
                            "call": local_call.model_dump(),
                        },
                    )

                if r_quest.get("status") in ("completed", "stopped"):
                    quest.status = r_quest["status"]
                    publish("quest_complete", {"quest_id": quest.quest_id, "status": quest.status})
                    return True

                if quest.calls and all(c.status in terminal_states for c in quest.calls.values()):
                    await http.post(f"{base}/api/quests/{remote_qid}/stop", headers=hdrs)
                    quest.status = "completed"
                    publish("quest_complete", {"quest_id": quest.quest_id, "status": "completed"})
                    return True

            quest.status = "completed"
            publish("quest_complete", {"quest_id": quest.quest_id, "status": "completed"})
            return True
    except Exception as exc:
        logger.warning("Cloud Run quest relay error (%s)", exc)
        return False


async def relay_self_test_via_cloud_run(
    *,
    my_phone: str,
    quest: QuestSession,
    audio_store: dict[tuple[str, str], bytes],
    publish: Callable[[str, dict[str, Any]], None],
    stop_event: asyncio.Event,
    pstn_adapter: "LiveIndiaPstnAdapter | None" = None,
) -> bool:
    """Relay a 1-click `+91` Self-Test PSTN Call through Cloud Run (`wss://...run.app`)."""
    if not is_bridge_enabled():
        return False
    base = cloud_run_base_url()
    hdrs = _bridge_headers()
    try:
        async with httpx.AsyncClient(timeout=45.0) as http:
            if pstn_adapter is not None:
                cfg = pstn_adapter.resolve_config()
                if cfg is not None:
                    await sync_carrier_config_to_cloud_run(cfg)

            start_resp = await http.post(
                f"{base}/api/pstn/test-call",
                headers=hdrs,
                json={
                    "my_phone": my_phone,
                    "user_goal": quest.user_goal,
                    "preferred_tone": quest.preferred_tone.value,
                    "voice_name": quest.voice_name,
                },
            )
            if start_resp.status_code != 200:
                logger.warning("Cloud Run self-test relay returned HTTP %s: %s", start_resp.status_code, start_resp.text[:180])
                return False

            remote_qid = start_resp.json()["quest"]["quest_id"]
            deadline = time.monotonic() + 92.0
            fetched_audio = False
            terminal_states = {CallStatus.COMPLETED, CallStatus.SKIPPED, CallStatus.STOPPED, CallStatus.FAILED}

            while time.monotonic() < deadline:
                if stop_event.is_set():
                    await http.post(f"{base}/api/quests/{remote_qid}/stop", headers=hdrs)
                    return True
                await asyncio.sleep(0.9)
                poll = await http.get(f"{base}/api/quests/{remote_qid}", headers=hdrs)
                if poll.status_code != 200:
                    continue
                r_quest = poll.json().get("quest") or {}
                rc = (r_quest.get("calls") or {}).get("self_test_1")
                if rc and "self_test_1" in quest.calls:
                    local_call = quest.calls["self_test_1"]
                    local_call.status = CallStatus(rc.get("status", local_call.status.value))
                    local_call.outcome = CallOutcome(rc.get("outcome", local_call.outcome.value))
                    local_call.duration_sec = float(rc.get("duration_sec") or 0.0)
                    local_call.dtmf_sent = list(rc.get("dtmf_sent") or [])
                    local_call.transcript = [
                        TranscriptTurn.model_validate(t) for t in (rc.get("transcript") or [])
                    ]
                    if rc.get("audio_url") and not fetched_audio:
                        wav_r = await http.get(f"{base}{rc['audio_url']}", headers=hdrs)
                        if wav_r.status_code == 200 and len(wav_r.content) > 200:
                            audio_store[(quest.quest_id, "self_test_1")] = wav_r.content
                            local_call.audio_url = f"/api/quests/{quest.quest_id}/calls/self_test_1/audio.wav"
                            fetched_audio = True
                    publish(
                        "call_update",
                        {
                            "quest_id": quest.quest_id,
                            "shop_id": "self_test_1",
                            "call": local_call.model_dump(),
                        },
                    )
                if r_quest.get("status") in ("completed", "stopped"):
                    quest.status = r_quest["status"]
                    publish("quest_complete", {"quest_id": quest.quest_id, "status": quest.status})
                    return True

                if quest.calls and all(c.status in terminal_states for c in quest.calls.values()):
                    await http.post(f"{base}/api/quests/{remote_qid}/stop", headers=hdrs)
                    quest.status = "completed"
                    publish("quest_complete", {"quest_id": quest.quest_id, "status": "completed"})
                    return True
            return True
    except Exception as exc:
        logger.warning("Cloud Run self-test relay error: %s", exc)
        return False
