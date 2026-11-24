"""Red-to-green verification suite for RingScout India Phase 1 / 1.5 Live PSTN."""

from __future__ import annotations

from array import array
import asyncio
import base64
import io
import json
from pathlib import Path
import tomllib
import wave
from typing import Any

from fastapi.testclient import TestClient
import httpx
import pytest

import ringscout.a2a_gateway as a2a_gateway
from ringscout.carriers import (
    HinglishSwarmAdapter,
    LiveIndiaPstnAdapter,
    TimelineCallRecorder,
    _pcm16_24k_to_ulaw_8k,
    _ulaw_8k_to_pcm16_16k,
    _upsample_pcm16_8k_to_16k,
    _upsample_pcm16_16k_to_24k,
    generate_dtmf_pcm24,
)
from ringscout.google_lookup import (
    discover_indian_shops,
    filter_and_dedupe_raw_shops,
    normalize_indian_phone,
)
from ringscout.models import (
    CallMode,
    CallOutcome,
    CallStatus,
    CarrierProvider,
    DiscoverRequest,
    DiscoverResponse,
    IndianShopTarget,
    InrQuote,
    QuestSession,
    ShopCallState,
    TranscriptTurn,
    VocalTone,
)
from ringscout.server import create_app
from ringscout.voice_engine import (
    GCP_PROJECT_ID,
    MAX_CALL_DURATION_SEC,
    VOICEMAIL_BAILOUT_MAX_SEC,
    add_pstn_comfort_noise_pcm24,
    build_thin_live_config,
    evaluate_transcript_side_brain,
    extract_hinglish_inr_amount,
    extract_ivr_dtmf_digit,
    gate_pstn_background_chatter,
    generate_busy_tone_wav,
    generate_comfort_bed_pcm24,
    heuristic_conversational_call_plan,
    heuristic_side_brain_fallback,
    is_agent_farewell_turn,
    is_human_hold_phrase,
    is_ivr_or_hold_announcement,
    is_live_human_pickup_after_ivr,
    sanitize_option_a_line,
    spoken_shop_brand,
    strip_live_meta_tokens,
)
from ringscout.x402_crypto import (
    generate_evm_keypair,
    sign_x402_exact_evm_payload,
    verify_x402_eip3009_signature_locally,
)


def test_indian_phone_hygiene_and_1800_blocking() -> None:
    assert normalize_indian_phone("+91 98450 12345") == (
        "+919845012345",
        "+91 98450 12345",
        "ok",
    )
    assert normalize_indian_phone("0091 98450 12345") == (
        "+919845012345",
        "+91 98450 12345",
        "ok",
    )
    assert normalize_indian_phone("098450-12345") == (
        "+919845012345",
        "+91 98450 12345",
        "ok",
    )
    assert normalize_indian_phone("080 2521 3456") == (
        "+918025213456",
        "+91 80 2521 3456",
        "ok",
    )
    assert normalize_indian_phone("+91-22-2640-1234") == (
        "+912226401234",
        "+91 22 2640 1234",
        "ok",
    )

    for tf in (
        "1800-102-5555",
        "+91 1800 209 1111",
        "0091 1800 209 1111",
        "1860-500-1066",
        "000800-100-9999",
    ):
        e164, disp, reason = normalize_indian_phone(tf)
        assert e164 is None and disp is None
        assert reason == "toll_free"

    for foreign in ("+1 415 555 2671", "+44 20 7946 0958", "100"):
        e164, _, reason = normalize_indian_phone(foreign)
        assert e164 is None
        assert reason == "invalid"


def test_filter_and_dedupe_raw_shops_and_xss_maps_uri() -> None:
    raw = [
        {
            "name": "National Chain Helpline",
            "phone": "1800 209 8888",
            "open_status": "Open 24 hours",
        },
        {
            "name": "Closed Chemist",
            "phone": "+91 98450 11111",
            "open_status": "Temporarily closed",
        },
        {
            "name": "Dr. Reddy's & Sons Medicals",
            "phone": "080-2521-4455",
            "address": "100 Feet Rd, Indiranagar",
            "open_status": "OPEN NOW",
            "rating": 4.6,
            "maps_uri": "javascript:alert(1)",
        },
        {
            "name": "Sri Lakshmi Medicals Duplicate",
            "phone": "+91 80 2521 4455",
            "open_status": "OPEN NOW",
        },
    ]
    shops, tf_cnt, bad_cnt = filter_and_dedupe_raw_shops(
        raw, city="Bengaluru", locality="Indiranagar", max_shops=8
    )
    assert tf_cnt == 1
    assert bad_cnt == 1
    assert len(shops) == 1
    assert shops[0].phone_e164 == "+918025214455"
    assert shops[0].maps_uri is None


def test_thin_live_socket_zero_tools_vocal_tone_and_phone_redaction() -> None:
    assert GCP_PROJECT_ID == "manav-chub"
    assert MAX_CALL_DURATION_SEC == 150.0
    assert VOICEMAIL_BAILOUT_MAX_SEC == 5.0

    req = DiscoverRequest(query="pharmacy medical store chemist open now")
    assert req.max_shops == 5

    cfg = build_thin_live_config(
        shop_name="Indiranagar MedPlus",
        locality="Indiranagar",
        user_goal="Augmentin 625 strip price, call me back on +91 98450 99999",
        vocal_tone=VocalTone.CONVERSATIONAL_HINGLISH,
        is_live_pstn=True,
    )
    assert cfg.tools is None
    assert cfg.response_modalities == ["AUDIO"]
    sys_text = cfg.system_instruction.parts[0].text
    assert "AI assistant" in sys_text
    assert "conversational_hinglish" in sys_text
    assert "ek minute / hold karo" in sys_text
    assert "98450" not in sys_text
    assert "[redacted-number]" in sys_text


@pytest.mark.parametrize(
    ("utterance", "expected_inr"),
    [
        (" साढ़े चार सौ रुपये ", 450.0),
        ("Haan ji stock mein hai, saadhe chaar sau rupaye padega", 450.0),
        ("450 bucks per strip", 450.0),
        ("Cash price ₹6,450 lagega alignment samet", 6450.0),
        ("Total chhah hazaar chaar sau rupaye padega", 6400.0),
        ("Total dedh hazaar rupaye padega", 1500.0),
        ("Dhai sau rupaye ki strip hai", 250.0),
        ("Paune paanch sau lagega", 475.0),
        ("Sawa teen sau ka hai", 325.0),
        ("Sawa sau rupaye", 125.0),
        ("Sawa hazaar padega", 1250.0),
    ],
)
def test_hinglish_inr_quote_extraction(utterance: str, expected_inr: float) -> None:
    assert extract_hinglish_inr_amount(utterance) == expected_inr


def test_side_brain_ivr_voicemail_and_negative_slot_heuristics() -> None:
    ivr_turns = [
        TranscriptTurn(
            speaker="shopkeeper",
            text="Welcome to City Diagnostics. English ke liye 1 dabayein, Hindi ke liye 2 dabayein.",
            timestamp_sec=1.2,
        )
    ]
    ivr_dec = heuristic_side_brain_fallback(ivr_turns, VocalTone.CONVERSATIONAL_HINGLISH)
    assert ivr_dec.dtmf_digit == "2"

    vm_turns = [
        TranscriptTurn(
            speaker="shopkeeper",
            text="Aapke dwara dial kiya gaya number abhi switched off hai. Please leave a message after the beep.",
            timestamp_sec=2.0,
        )
    ]
    vm_dec = heuristic_side_brain_fallback(vm_turns, VocalTone.POLITE_INDIAN_ENGLISH)
    assert vm_dec.is_voicemail is True
    assert vm_dec.should_hangup is True

    neg_turns = [
        TranscriptTurn(
            speaker="shopkeeper",
            text="Sorry sir, there is no slot available today for Brain MRI.",
            timestamp_sec=3.0,
        )
    ]
    neg_dec = heuristic_side_brain_fallback(neg_turns, VocalTone.POLITE_INDIAN_ENGLISH)
    assert neg_dec.in_stock is False


@pytest.mark.asyncio
async def test_behavioral_hard_call_duration_cap() -> None:
    shop = IndianShopTarget(
        shop_id="s1",
        name="Slow Shop",
        phone_e164="+918025213456",
        phone_display="+91 80 2521 3456",
        address="Indiranagar",
    )
    quest = QuestSession(
        quest_id="q_cap",
        user_goal="Check medicine",
        city="Bengaluru",
        locality="Indiranagar",
        preferred_tone=VocalTone.CONVERSATIONAL_HINGLISH,
        mode=CallMode.HINGLISH_SWARM,
        shops=[shop],
        calls={"s1": ShopCallState(shop=shop)},
    )
    adapter = HinglishSwarmAdapter(
        audio_store={},
        synthesize_audio=False,
        max_call_duration_sec=0.15,
    )
    await adapter.run_quest(
        quest=quest,
        stop_event=asyncio.Event(),
        publish=lambda *_: None,
    )
    assert quest.calls["s1"].outcome == CallOutcome.TIMED_OUT_90S
    assert quest.calls["s1"].duration_sec == 0.15


def test_ranked_quotes_and_wav_generation() -> None:
    s1 = IndianShopTarget(
        shop_id="s1", name="Shop Expensive", phone_e164="+919845000001", phone_display="+91 98450 00001", address="A"
    )
    s2 = IndianShopTarget(
        shop_id="s2", name="Shop Best", phone_e164="+919845000002", phone_display="+91 98450 00002", address="B"
    )
    s3 = IndianShopTarget(
        shop_id="s3", name="Shop Out", phone_e164="+919845000003", phone_display="+91 98450 00003", address="C"
    )
    quest = QuestSession(
        quest_id="q_test",
        user_goal="Test item",
        city="Bengaluru",
        locality="Indiranagar",
        preferred_tone=VocalTone.CONVERSATIONAL_HINGLISH,
        mode=CallMode.HINGLISH_SWARM,
        shops=[s1, s2, s3],
        calls={
            "s1": ShopCallState(
                shop=s1, status=CallStatus.COMPLETED, quote=InrQuote(in_stock=True, price_inr=480.0)
            ),
            "s2": ShopCallState(
                shop=s2, status=CallStatus.COMPLETED, quote=InrQuote(in_stock=True, price_inr=395.0)
            ),
            "s3": ShopCallState(
                shop=s3, status=CallStatus.COMPLETED, quote=InrQuote(in_stock=False, price_inr=None)
            ),
        },
    )
    ranked = quest.ranked_quotes()
    assert [c.shop.shop_id for c in ranked] == ["s2", "s1", "s3"]

    wav_bytes = generate_busy_tone_wav(0.5)
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        assert wf.getframerate() == 24000
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2

    up = _upsample_pcm16_16k_to_24k(b"\x00\x01" * 160)
    assert len(up) == 240 * 2


def test_live_pstn_carrier_xml_hmac_and_codecs() -> None:
    sig = LiveIndiaPstnAdapter.sign_pstn_token("vobiz", "q_123", "shop_1")
    assert LiveIndiaPstnAdapter.verify_pstn_token("vobiz", "q_123", "shop_1", sig) is True
    assert LiveIndiaPstnAdapter.verify_pstn_token("vobiz", "q_123", "shop_2", sig) is False
    assert LiveIndiaPstnAdapter.verify_pstn_token("plivo", "q_123", "shop_1", sig) is False
    assert LiveIndiaPstnAdapter.verify_pstn_token("vobiz", "q_123", "shop_1", None) is False

    vobiz_xml = LiveIndiaPstnAdapter.build_answer_xml(
        CarrierProvider.VOBIZ, "wss://demo.run.app/ws/pstn/stream/vobiz/q_1/s_1?sig=abc&x=1"
    )
    assert 'contentType="audio/x-l16;rate=16000"' in vobiz_xml
    assert 'bidirectional="true"' in vobiz_xml
    assert "&amp;x=1" in vobiz_xml

    plivo_xml = LiveIndiaPstnAdapter.build_answer_xml(
        CarrierProvider.PLIVO, "wss://demo.run.app/ws/pstn/stream/plivo/q_1/s_1?sig=abc"
    )
    assert 'contentType="audio/x-mulaw;rate=8000"' in plivo_xml

    twilio_xml = LiveIndiaPstnAdapter.build_answer_xml(
        CarrierProvider.TWILIO, "wss://demo.run.app/ws/pstn/stream/twilio/q_1/s_1?sig=abc"
    )
    assert "<Connect><Stream url=" in twilio_xml

    # Anti-aliased 24kHz -> 8kHz mu-law -> 16kHz PCM16 round-trip sizing
    pcm24 = b"\x20\x08" * 240  # 240 samples @ 24kHz (10ms)
    ulaw8 = _pcm16_24k_to_ulaw_8k(pcm24)
    assert len(ulaw8) == 80  # 80 samples @ 8kHz (10ms)
    pcm16_from_ulaw = _ulaw_8k_to_pcm16_16k(ulaw8)
    assert len(pcm16_from_ulaw) == 160 * 2  # 160 samples @ 16kHz (10ms)

    pcm16_from_pcm8 = _upsample_pcm16_8k_to_16k(b"\x10\x04" * 80)
    assert len(pcm16_from_pcm8) == 160 * 2

    dtmf2 = generate_dtmf_pcm24("2", duration_sec=0.18)
    assert len(dtmf2) == int(24000 * 0.18) * 2
    assert generate_dtmf_pcm24("invalid") == b""


def test_wheel_package_data_and_host_header_hardening() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    pkg_data = pyproject["tool"]["setuptools"]["package-data"]["ringscout"]
    assert "static/*" in pkg_data
    assert any("requests" in dep for dep in pyproject["project"]["dependencies"])
    assert (repo_root / "src" / "ringscout" / "static" / "index.html").is_file()

    # Verify spoofed X-Forwarded-Host cannot hijack detected_public_base_url
    app = create_app(synthesize_audio=False, require_provenance=False)
    client = TestClient(app)
    cfg = client.get(
        "/api/pstn/config",
        headers={
            "Host": "ringscout-real.us-central1.run.app",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "evil.attacker.invalid",
        },
    ).json()
    assert cfg["detected_public_base_url"] == "https://ringscout-real.us-central1.run.app"


def test_pstn_runtime_config_masking_webhooks_and_self_test_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialed_calls: list[dict[str, Any]] = []

    async def fake_post(
        self: httpx.AsyncClient, url: str, **kwargs: Any
    ) -> httpx.Response:
        dialed_calls.append({"url": url, "kwargs": kwargs})
        return httpx.Response(
            status_code=201,
            json={"request_uuid": "vobiz_call_999"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    app = create_app(synthesize_audio=False, require_provenance=True)
    client = TestClient(app)

    # 1. Block 1800 toll-free From number
    tf_cfg = client.post(
        "/api/pstn/config",
        json={
            "provider": "vobiz",
            "auth_id": "MA_INDIA_123456",
            "auth_token": "super_secret_vobiz_token",
            "from_number": "1800-102-5555",
        },
    )
    assert tf_cfg.status_code == 400

    # 2. Save valid Vobiz +91 DID with public Cloud Run URL and verify token masking
    ok_cfg = client.post(
        "/api/pstn/config",
        json={
            "provider": "vobiz",
            "auth_id": "MA_INDIA_123456",
            "auth_token": "super_secret_vobiz_token",
            "from_number": "080 4567 8901",
            "public_base_url": "https://ringscout-1004579867040.us-central1.run.app",
        },
    )
    assert ok_cfg.status_code == 200
    cfg_body = ok_cfg.json()
    assert cfg_body["configured"] is True
    assert cfg_body["provider"] == "vobiz"
    assert cfg_body["from_number"] == "+918045678901"
    assert cfg_body["auth_id_masked"] == "****3456"
    assert "super_secret_vobiz_token" not in str(cfg_body)
    assert cfg_body["public_wss_ready"] is True

    # 3. 1-Click Self-Test blocks 1800 numbers, allows personal +91 mobile,
    #    and does NOT pollute verified_google_phones for multi-shop quests!
    bad_self = client.post(
        "/api/pstn/test-call",
        json={"my_phone": "1800-209-1111"},
    )
    assert bad_self.status_code == 400

    ok_self = client.post(
        "/api/pstn/test-call",
        json={
            "my_phone": "+91 98450 54321",
            "user_goal": "Check Augmentin 625 strip price in ₹",
        },
    )
    assert ok_self.status_code == 200
    quest_id = ok_self.json()["quest"]["quest_id"]
    assert len(dialed_calls) == 1
    assert "api.vobiz.ai/api/v1/Account/MA_INDIA_123456/Call/" in dialed_calls[0]["url"]
    assert dialed_calls[0]["kwargs"]["headers"]["X-Auth-ID"] == "MA_INDIA_123456"
    assert dialed_calls[0]["kwargs"]["json"]["to"] == "+919845054321"

    # Multi-shop /api/quests must STILL reject that personal number because it wasn't on Google Maps
    still_blocked = client.post(
        "/api/quests",
        json={
            "user_goal": "Check medicine",
            "mode": "live_pstn",
            "shops": [
                {
                    "shop_id": "shop_1",
                    "name": "Personal Phone",
                    "phone_e164": "+919845054321",
                    "phone_display": "+91 98450 54321",
                    "address": "Home",
                }
            ],
        },
    )
    assert still_blocked.status_code == 400

    # 4. Verify HMAC-protected Answer XML & Busy Status Webhook
    unauth_xml = client.post(f"/api/pstn/answer/vobiz/{quest_id}/self_test_1")
    assert unauth_xml.status_code == 403

    sig = LiveIndiaPstnAdapter.sign_pstn_token("vobiz", quest_id, "self_test_1")
    authed_xml = client.post(
        f"/api/pstn/answer/vobiz/{quest_id}/self_test_1?sig={sig}",
        data={"CallUUID": "vobiz_live_uuid_77"},
    )
    assert authed_xml.status_code == 200
    assert "audio/x-l16;rate=16000" in authed_xml.text
    assert f"wss://ringscout-1004579867040.us-central1.run.app/ws/pstn/stream/vobiz/{quest_id}/self_test_1" in authed_xml.text

    status_cb = client.post(
        f"/api/pstn/status/vobiz/{quest_id}/self_test_1?sig={sig}",
        data={"CallStatus": "completed", "HangupCauseName": "SUBSCRIBER_ABSENT"},
    )
    assert status_cb.status_code == 200
    q_after = client.get(f"/api/quests/{quest_id}").json()["quest"]
    assert q_after["calls"]["self_test_1"]["outcome"] == CallOutcome.BUSY_NO_ANSWER.value
    assert q_after["status"] == "completed"

    # Stopped quest returns immediate <Hangup/> XML if carrier answers late
    client.post(f"/api/quests/{quest_id}/stop")
    hangup_xml = client.post(f"/api/pstn/answer/vobiz/{quest_id}/self_test_1?sig={sig}")
    assert "<Hangup/>" in hangup_xml.text


def test_cloud_run_passcode_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RINGSCOUT_PASSCODE", "india-chemist-pass-99")
    app = create_app(synthesize_audio=False, require_provenance=False)
    client = TestClient(app)

    health = client.get("/api/health").json()
    assert health["passcode_required"] is True

    assert client.get("/api/pstn/config").status_code == 401
    assert (
        client.get(
            "/api/pstn/config",
            headers={"X-RingScout-Passcode": "india-chemist-pass-99"},
        ).status_code
        == 200
    )


def test_api_blocks_unverified_personal_numbers_1800_and_supports_stop_all() -> None:
    strict_app = create_app(synthesize_audio=False, require_provenance=True)
    strict_client = TestClient(strict_app)

    # Personal +91 number NOT discovered via /api/discover must be rejected
    unverified_resp = strict_client.post(
        "/api/quests",
        json={
            "user_goal": "Check medicine",
            "city": "Bengaluru",
            "locality": "Indiranagar",
            "mode": "browser_mic",
            "shops": [
                {
                    "shop_id": "shop_1",
                    "name": "Random Personal Phone",
                    "phone_e164": "+919845099887",
                    "phone_display": "+91 98450 99887",
                    "address": "Private Home",
                }
            ],
        },
    )
    assert unverified_resp.status_code == 400
    assert "Google Maps" in unverified_resp.json()["detail"]

    app = create_app(synthesize_audio=False, require_provenance=False)
    client = TestClient(app)

    bad_resp = client.post(
        "/api/quests",
        json={
            "user_goal": "Check medicine",
            "city": "Bengaluru",
            "locality": "Indiranagar",
            "mode": "browser_mic",
            "shops": [
                {
                    "shop_id": "shop_1",
                    "name": "Toll Free Center",
                    "phone_e164": "18001025555",
                    "phone_display": "1800 102 5555",
                    "address": "MG Road",
                }
            ],
        },
    )
    assert bad_resp.status_code == 400

    ok_resp = client.post(
        "/api/quests",
        json={
            "user_goal": "Check Augmentin 625 strip price",
            "city": "Bengaluru",
            "locality": "Indiranagar",
            "mode": "browser_mic",
            "shops": [
                {
                    "shop_id": "shop_1",
                    "name": "Indiranagar Chemist",
                    "phone_e164": "+918025213456",
                    "phone_display": "+91 80 2521 3456",
                    "address": "100ft Rd, Indiranagar",
                }
            ],
        },
    )
    assert ok_resp.status_code == 200
    quest_id = ok_resp.json()["quest"]["quest_id"]

    stop_resp = client.post(f"/api/quests/{quest_id}/stop")
    assert stop_resp.status_code == 200
    stopped_data = stop_resp.json()
    assert stopped_data["quest"]["status"] == "stopped"
    assert stopped_data["quest"]["calls"]["shop_1"]["outcome"] == CallOutcome.STOPPED_BY_USER.value


def test_humanized_2beat_call_plan_and_comfort_noise() -> None:
    raw_prompt = (
        "Check which open medical store has Augmentin 625 Duo (strip of 10) "
        "in stock right now and confirm the cash price in ₹"
    )
    plan = heuristic_conversational_call_plan(raw_prompt, VocalTone.CONVERSATIONAL_HINGLISH)
    assert plan.spoken_item_short == "Augmentin 625 Duo (strip of 10)"
    assert "Check which open" not in plan.turn1_availability_question
    assert "confirm the cash price" not in plan.turn1_availability_question
    assert "Augmentin 625 Duo" in plan.turn1_availability_question
    assert "namaste" not in plan.turn1_availability_question.lower()
    assert "kitne" in plan.turn2_price_question

    bed = generate_comfort_bed_pcm24(duration_sec=0.1)
    assert len(bed) == int(24000 * 0.1) * 2
    bed_samples = array("h")
    bed_samples.frombytes(bed)
    assert 0 < max(abs(x) for x in bed_samples) < 250

    silent_in = b"\x00\x00" * 240
    mixed = add_pstn_comfort_noise_pcm24(silent_in)
    mixed_samples = array("h")
    mixed_samples.frombytes(mixed)
    assert 0 < max(abs(x) for x in mixed_samples) < 250


def test_a2a_agent_card_x402_ap2_402_challenge_and_mcp_jsonrpc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RINGSCOUT_PASSCODE", "owner-pass-777")
    monkeypatch.setenv("RINGSCOUT_PAYMENT_GATE_MODE", "required")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", "0x122F8Fcaf2152420445Aa424E1D8C0306935B5c9")

    async def fake_discover(req: DiscoverRequest) -> DiscoverResponse:
        return DiscoverResponse(
            query=req.query,
            city=req.city,
            locality=req.locality,
            source="vertex_google_maps_grounding",
            shops=[
                IndianShopTarget(
                    shop_id="shop_1",
                    name="Prakash Medicals",
                    phone_e164="+919449159625",
                    phone_display="+91 94491 59625",
                    address="Indiranagar, Bengaluru",
                    locality="Indiranagar",
                    city="Bengaluru",
                    open_status="OPEN NOW",
                    rating=4.7,
                )
            ],
        )

    async def fake_compile(goal: str, tone: VocalTone) -> Any:
        return heuristic_conversational_call_plan(goal, tone)

    async def fake_facilitator_post(
        self: httpx.AsyncClient, url: str, **kwargs: Any
    ) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={"isValid": True, "payer": "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(a2a_gateway, "discover_indian_shops", fake_discover)
    monkeypatch.setattr(a2a_gateway, "compile_conversational_call_plan", fake_compile)
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_facilitator_post)

    app = create_app(synthesize_audio=False, require_provenance=True)
    client = TestClient(app)

    # 1. Public A2A Discovery Card
    card = client.get("/.well-known/agent.json")
    assert card.status_code == 200
    card_json = card.json()
    assert "x402-v2" in card_json["protocols"]
    assert "google-ap2-1.0" in card_json["protocols"]
    assert card_json["payment"]["x402_asset"] == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    # 2. Unpaid POST /api/v1/scout -> HTTP 402 with x402 v2 + MPP headers
    unpaid = client.post(
        "/api/v1/scout",
        json={
            "query": "pharmacy chemist open now",
            "item_question": "Augmentin 625 Duo strip price",
            "city": "Bengaluru",
            "locality": "Indiranagar",
            "max_shops": 1,
        },
    )
    assert unpaid.status_code == 402
    assert "Payment realm=" in unpaid.headers["WWW-Authenticate"]
    decoded_req = json.loads(base64.b64decode(unpaid.headers["PAYMENT-REQUIRED"]).decode("utf-8"))
    assert decoded_req["x402Version"] == 2
    assert decoded_req["accepts"][0]["amount"] == "750000"

    # 3. Unauthenticated attacker trying `sandbox: true` WITHOUT owner passcode -> Rejected 402!
    forged_sandbox = base64.b64encode(
        json.dumps({"x402Version": 2, "sandbox": True}).encode("utf-8")
    ).decode("ascii")
    atk = client.post(
        "/api/v1/scout",
        headers={"PAYMENT-SIGNATURE": forged_sandbox},
        json={"item_question": "Augmentin 625 Duo strip price", "max_shops": 1},
    )
    assert atk.status_code == 402

    # 4. Insufficient AP2 spend mandate ($0.20 < $0.75) -> Rejected 402!
    valid_x402 = base64.b64encode(
        json.dumps(
            {
                "x402Version": 2,
                "accepted": decoded_req["accepts"][0],
                "payload": {
                    "signature": "0xdeadbeef",
                    "authorization": {
                        "from": "0x742d35Cc6634C0532925a3b844Bc454e4438f44e",
                        "to": "0x122F8Fcaf2152420445Aa424E1D8C0306935B5c9",
                        "value": "750000",
                    },
                },
            }
        ).encode("utf-8")
    ).decode("ascii")
    low_mandate = client.post(
        "/api/v1/scout",
        headers={"PAYMENT-SIGNATURE": valid_x402},
        json={
            "item_question": "Augmentin 625 Duo strip price",
            "max_shops": 1,
            "ap2_mandate": {
                "mandate_id": "mnd_low",
                "max_amount_usd": 0.20,
            },
        },
    )
    assert low_mandate.status_code == 402
    assert "below required" in low_mandate.json()["error"]

    # 5. Valid AP2 Mandate ($2.00) + Verified x402 Base USDC signature -> 200 OK Oracle Receipt!
    paid = client.post(
        "/api/v1/scout",
        headers={"PAYMENT-SIGNATURE": valid_x402},
        json={
            "item_question": "Check which open medical store has Augmentin 625 Duo (strip of 10) in stock right now and confirm cash price in ₹",
            "max_shops": 1,
            "ap2_mandate": {
                "mandate_id": "mnd_ok_01",
                "principal_id": "nri_user_us",
                "agent_id": "claude_mcp",
                "max_amount_usd": 2.00,
            },
        },
    )
    assert paid.status_code == 200
    paid_json = paid.json()
    assert paid_json["settlement"]["rail"] == "x402-v2-base-usdc"
    assert paid_json["settlement"]["ap2_mandate_id"] == "mnd_ok_01"
    assert len(paid_json["settlement"]["ap2_mandate_sha256"]) == 16
    assert paid_json["call_plan"]["spoken_item_short"] == "Augmentin 625 Duo (strip of 10)"
    assert len(paid_json["ranked_quotes"]) == 1

    # 6. Streamable HTTP MCP JSON-RPC (`initialize` -> `tools/list` -> unpaid `tools/call`)
    mcp_init = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
    ).json()
    assert mcp_init["result"]["serverInfo"]["name"] == "ringscout-india-voice-oracle"

    mcp_tools = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ).json()
    tool_names = [t["name"] for t in mcp_tools["result"]["tools"]]
    assert "scout_india_local_businesses" in tool_names

    mcp_unpaid = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "scout_india_local_businesses",
                "arguments": {"item_question": "Augmentin 625"},
            },
        },
    ).json()
    assert mcp_unpaid["result"]["isError"] is True
    assert "x402Version" in mcp_unpaid["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_live_vertex_manav_chub_maps_and_side_brain() -> None:
    """Live integration test against `manav-chub` Vertex googleMaps grounding & temp=0.0 Side-Brain."""
    try:
        disc = await discover_indian_shops(
            DiscoverRequest(
                query="pharmacy medical store chemist open now",
                city="Bengaluru",
                locality="Indiranagar",
                max_shops=5,
            )
        )
    except Exception as exc:
        if any(k in str(exc) for k in ("403", "PERMISSION_DENIED", "Reauthentication", "RefreshError")):
            pytest.skip(
                "manav-chub OAuth session expired on Cloudtop; re-run after `gcloud auth login admin@manavagarwal.altostrat.com`."
            )
        raise

    assert disc.source == "vertex_google_maps_grounding"
    assert len(disc.shops) >= 1
    for shop in disc.shops:
        assert shop.phone_e164.startswith("+91")
        assert not shop.phone_e164.startswith("+911800")

    decision = await evaluate_transcript_side_brain(
        user_goal="Check Augmentin 625 strip of 10 price in ₹",
        shop_name=disc.shops[0].name,
        transcript=[
            TranscriptTurn(
                speaker="agent",
                text="Namaste ji! Main RingScout AI assistant bol rahi hoon — Augmentin 625 strip stock mein hai kya?",
                timestamp_sec=0.8,
            ),
            TranscriptTurn(
                speaker="shopkeeper",
                text="Haan ji bilkul stock mein hai, saadhe chaar sau rupaye padega ek strip ka, abhi le jaiye.",
                timestamp_sec=4.1,
            ),
        ],
        preferred_tone=VocalTone.CONVERSATIONAL_HINGLISH,
    )
    assert decision.in_stock is True
    assert decision.price_inr == 450.0
    assert decision.vocal_tone == VocalTone.CONVERSATIONAL_HINGLISH
    assert decision.should_hangup is True


def test_timeline_call_recorder_eliminates_interleaving_stutter_and_bargein() -> None:
    rec = TimelineCallRecorder(sample_rate=24000, start_ts=100.0)
    # 2.0s of continuous 20ms (480 samples) caller mic frames with constant value +1000
    mic_20ms = array("h", [1000] * 480).tobytes()
    for frame_idx in range(100):
        rec.append_inbound(mic_20ms, now_ts=100.0 + frame_idx * 0.02)

    # Faster-than-realtime burst of 3 x 0.5s (12,000 samples) AI speech frames (+4000) arriving at t=0.40..0.44s
    ai_500ms = array("h", [4000] * 12000).tobytes()
    rec.append_outbound(ai_500ms, now_ts=100.40)
    rec.append_outbound(ai_500ms, now_ts=100.42)
    rec.append_outbound(ai_500ms, now_ts=100.44)

    # At t=0.90s, 1.0s of queued AI speech remains in playout buffer (playhead is at 101.90s)
    assert abs(rec.remaining_playout_sec(now_ts=100.90) - 1.0) < 0.02

    rendered = rec.render_pcm24()
    samples = array("h")
    samples.frombytes(rendered)
    # Exact 2.0s (48,000 samples) — NOT stretched to 3.5s by sequential concatenation!
    assert len(samples) == 48000
    # Before AI starts (sample 1000 @ t=0.04s): caller mic only (1000)
    assert samples[1000] == 1000
    # During AI speech across all 3 burst chunks (0.40s..1.90s = samples 9600..45600): continuous 1000 + 4000 = 5000!
    assert samples[10000] == 5000
    assert samples[25000] == 5000
    assert samples[44000] == 5000
    # After AI finishes (sample 47000 @ t=1.958s): caller mic only (1000)
    assert samples[47000] == 1000

    # Verify barge-in truncation at t=1.0s clears unplayed outbound beyond sample 24000
    rec.clear_unplayed_outbound(now_ts=101.0)
    assert rec.remaining_playout_sec(now_ts=101.0) == 0.0
    barge_samples = array("h")
    barge_samples.frombytes(rec.render_pcm24())
    assert barge_samples[20000] == 5000
    assert barge_samples[30000] == 1000


def test_option_a_persona_gender_neutral_and_language_lock() -> None:
    mri_goal = "Check if a 1.5T Brain MRI (plain) slot is available today and confirm the walk-in cash price in ₹"
    en_plan = heuristic_conversational_call_plan(mri_goal, VocalTone.POLITE_INDIAN_ENGLISH)
    for turn in (
        en_plan.turn1_availability_question,
        en_plan.turn2_price_question,
        en_plan.turn3_closing,
    ):
        assert "bhaiya" not in turn.lower()
        assert "customer" not in turn.lower()
        assert "namaste" not in turn.lower()
    assert "Hi hello" in en_plan.turn1_availability_question
    assert "walk-in cash price" in en_plan.turn2_price_question

    hi_plan = heuristic_conversational_call_plan(mri_goal, VocalTone.CONVERSATIONAL_HINGLISH)
    for turn in (
        hi_plan.turn1_availability_question,
        hi_plan.turn2_price_question,
        hi_plan.turn3_closing,
    ):
        assert "bhaiya" not in turn.lower()
        assert "customer" not in turn.lower()
        assert "namaste" not in turn.lower()
    assert "Haan hello" in hi_plan.turn1_availability_question

    cfg = build_thin_live_config(
        shop_name="Holy Family Hospital",
        locality="Bandra West",
        user_goal=mri_goal,
        vocal_tone=VocalTone.POLITE_INDIAN_ENGLISH,
        is_live_pstn=True,
        call_plan=en_plan,
    )
    assert cfg.tools is None
    assert cfg.speech_config is not None
    assert cfg.speech_config.language_code == "en-IN"
    assert cfg.speech_config.voice_config.prebuilt_voice_config.voice_name == "Puck"
    assert cfg.realtime_input_config is not None
    assert (
        cfg.realtime_input_config.automatic_activity_detection.silence_duration_ms == 320
    )

    hi_cfg = build_thin_live_config(
        shop_name="Prakash Chemist",
        locality="Indiranagar",
        user_goal=mri_goal,
        vocal_tone=VocalTone.CONVERSATIONAL_HINGLISH,
        is_live_pstn=True,
        call_plan=hi_plan,
    )
    assert hi_cfg.speech_config is not None
    assert hi_cfg.speech_config.language_code == "hi-IN"


def test_anti_spam_shop_brand_nearfield_noise_gate_and_hindi_mode() -> None:
    """Regression suite for Round 3 live chemist calls (`q_a96ab82278` Shanti, Star BKC, Elite)."""
    # 1. Clean 2-3 word spoken shop brand extraction (never 'medical and general store bkc')
    assert spoken_shop_brand("Star chemist medical store bkc") == "Star Chemist"
    assert spoken_shop_brand("ELITE MEDICAL AND GENERAL STORE") == "Elite Medical"
    assert spoken_shop_brand("Shanti Chemist") == "Shanti Chemist"
    assert spoken_shop_brand("My +91 Phone (Dry-Run Chemist Test)") == ""

    # 2. Sanitizer strips telemarketer 'Hello ji, namaste' even if generated by an LLM
    cleaned = sanitize_option_a_line(
        "Hello ji, namaste — Istamet 25 mil jayega kya?",
        VocalTone.CONVERSATIONAL_HINGLISH,
    )
    assert "namaste" not in cleaned.lower()
    assert cleaned.startswith("Haan hello")

    # 3. Natural Hindi (`hi-IN`) mode + shop brand injection in system instruction
    hindi_plan = heuristic_conversational_call_plan(
        "Check if Istamet 25 is in stock and confirm cash price in ₹",
        VocalTone.NATURAL_HINDI,
    )
    assert "namaste" not in hindi_plan.turn1_availability_question.lower()
    assert "patti" in hindi_plan.turn2_price_question or "daam" in hindi_plan.turn2_price_question
    hindi_cfg = build_thin_live_config(
        shop_name="Star chemist medical store bkc",
        locality="Bandra West",
        user_goal="Check if Istamet 25 is in stock and confirm cash price in ₹",
        vocal_tone=VocalTone.NATURAL_HINDI,
        is_live_pstn=True,
        call_plan=hindi_plan,
        voice_name="Orus",
    )
    assert hindi_cfg.speech_config.language_code == "hi-IN"
    assert hindi_cfg.speech_config.voice_config.prebuilt_voice_config.voice_name == "Orus"
    sys_txt = hindi_cfg.system_instruction.parts[0].text
    assert "Haan hello, Star Chemist?" in sys_txt
    assert "poochh raha tha" in sys_txt

    # 4. Adaptive near-field handset noise gate:
    #    - Leaves quiet lines (`peak_ema < 2200`) 100% untouched
    #    - Suppresses 500 RMS shop background chatter by -28dB once 8000 RMS handset speech occurs
    gate_state: dict[str, float] = {}
    soft_frame = array("h", [500, -500] * 160).tobytes()
    out_soft, eff_soft = gate_pstn_background_chatter(soft_frame, gate_state)
    assert out_soft == soft_frame
    assert eff_soft == pytest.approx(500.0, abs=1.0)

    loud_handset = array("h", [8000, -8000] * 160).tobytes()
    for _ in range(8):
        gate_pstn_background_chatter(loud_handset, gate_state)
    assert gate_state["peak_ema"] >= 5000.0

    # Drain 8-frame hangover tail, then verify background shop chatter (500 RMS) is gated to ~20 RMS
    for _ in range(9):
        gated_bg, eff_bg = gate_pstn_background_chatter(soft_frame, gate_state)
    assert eff_bg < 30.0
    assert gated_bg != soft_frame

    # Judge regression: even after a 10,500 RMS loud pickup/IVR, arm's-length human speech (1,800 RMS) is NEVER clipped!
    very_loud = array("h", [10500, -10500] * 160).tobytes()
    for _ in range(6):
        gate_pstn_background_chatter(very_loud, gate_state)
    arms_length_speech = array("h", [1800, -1800] * 160).tobytes()
    out_arm, eff_arm = gate_pstn_background_chatter(arms_length_speech, gate_state)
    assert out_arm == arms_length_speech
    assert eff_arm == pytest.approx(1800.0, abs=1.0)

    # Judge + Beyond regression: `🧠[awaiting response]` meta-tokens are stripped cleanly
    assert strip_live_meta_tokens("🧠[awaiting response]").strip() == ""
    assert strip_live_meta_tokens("🧠[awaiting response] Haan, Istamet 25?").strip() == "Haan, Istamet 25?"

    # 5. Human shelf-check hold detector
    assert is_human_hold_phrase("Ek minute ruko dekhta hoon") is True
    assert is_human_hold_phrase("Istamet 25 hai na?") is False


def test_exhaustive_mri_filtering_and_locality_proximity_ranking() -> None:
    raw_items = [
        {
            "name": "Dadar Distant MRI Center",
            "phone": "+91 98204 51234",
            "address": "NC Kelkar Rd, Dadar West, Mumbai, Maharashtra 400028",
            "open_status": "OPEN NOW",
            "rating": 4.9,
            "category": "MRI Center",
        },
        {
            "name": "INsight CBCT Bandra",
            "phone": "+91 22 2643 4938",
            "address": "Santosh Nagar, Bandra West, Mumbai 400050",
            "open_status": "OPEN NOW",
            "rating": 5.0,
            "category": "Dental Clinic",
        },
        {
            "name": "AERA Diagnostics- Breast Biopsy & Sonography Centre",
            "phone": "+91 91371 99152",
            "address": "Linking Rd, Khar West, Mumbai 400052",
            "open_status": "OPEN NOW",
            "rating": 5.0,
            "category": "Diagnostic Center",
        },
        {
            "name": "NM Medical Khar Radiology & MRI",
            "phone": "+91 22 4342 8888",
            "address": "14th Rd, Khar West, Mumbai, Maharashtra 400052",
            "open_status": "OPEN NOW",
            "rating": 4.7,
            "category": "MRI & Radiology Lab",
        },
        {
            "name": "Holy Family Multispeciality Hospital",
            "phone": "+91 22 6267 0555",
            "address": "St Andrews Rd, Bandra West, Mumbai, Maharashtra 400050",
            "open_status": "OPEN NOW",
            "rating": 4.3,
            "category": "Hospital",
        },
        {
            "name": "ILLUME Imaging & MRI",
            "phone": "+91 93240 38669",
            "address": "St Martins Rd, Bandra (W), Mumbai, Maharashtra 400050",
            "open_status": "OPEN NOW",
            "rating": 4.8,
            "category": "MRI Center",
        },
    ]
    shops, tf_cnt, skipped_cnt = filter_and_dedupe_raw_shops(
        raw_items,
        city="Mumbai",
        locality="Bandra West",
        max_shops=12,
        query="MRI scan center radiology imaging hospital",
    )
    assert tf_cnt == 0
    # Dental CBCT and Breast Sonography-only clinics filtered out deterministically
    assert skipped_cnt == 2
    assert [s.name for s in shops] == [
        "ILLUME Imaging & MRI",
        "Holy Family Multispeciality Hospital",
        "NM Medical Khar Radiology & MRI",
        "Dadar Distant MRI Center",
    ]
    assert shops[0].locality_match_tier == 0
    assert shops[1].locality_match_tier == 0
    assert shops[2].locality_match_tier == 1
    assert shops[3].locality_match_tier >= 2


def test_ivr_both_word_orders_farewell_hangup_and_alt_slot_capture() -> None:
    assert (
        extract_ivr_dtmf_digit(
            "Welcome to NM Medical. For pathology press 1, for radiology press 2, to speak with our health advisor press 3.",
            preferred_tone=VocalTone.POLITE_INDIAN_ENGLISH,
            user_goal="1.5T Brain MRI slot today",
        )
        == "2"
    )
    assert (
        extract_ivr_dtmf_digit(
            "Thank you for calling. To speak with our health advisor, press 2.",
            preferred_tone=VocalTone.POLITE_INDIAN_ENGLISH,
        )
        == "2"
    )
    assert is_agent_farewell_turn("Got it, thank you — what is the walk-in cash price for that?") is False
    # Judge regression: ASR often omits '?' on Beat 2 contractions — must still return False!
    assert is_agent_farewell_turn("Got it, thank you. What's the walk-in cash price.") is False
    assert is_agent_farewell_turn("Got it, thank you, could you share the cash rate.") is False
    assert is_agent_farewell_turn("Achha dhanyavaad ji, iska rate kitna padega.") is False
    # Period-split Beat 3 farewells must return True!
    assert is_agent_farewell_turn("Okay, got it. Thank you.") is True
    assert is_agent_farewell_turn("Okay got it, thank you so much!") is True
    assert is_agent_farewell_turn("Thank you so much for confirming the cash rate!") is True
    assert is_agent_farewell_turn("Achha theek hai ji, bahut dhanyavaad!") is True

    # Judge regression: 'Near Bandra Worli Sea Link, Worli 400018' and missing address must NOT be Tier 0 'In Bandra West'!
    worli_and_unlisted = [
        {
            "name": "Worli Seaface Imaging",
            "phone": "+91 98204 71234",
            "address": "Near Bandra Worli Sea Link, Worli, Mumbai 400018",
            "open_status": "OPEN NOW",
            "rating": 5.0,
            "category": "MRI Center",
        },
        {
            "name": "Unlisted Address MRI",
            "phone": "+91 98204 81234",
            "address": "",
            "open_status": "OPEN NOW",
            "rating": 5.0,
            "category": "MRI Center",
        },
        {
            "name": "Placeholder Zero Phone MRI",
            "phone": "+91 98200 00000",
            "address": "Waterfield Rd, Bandra West, Mumbai 400050",
            "open_status": "OPEN NOW",
            "rating": 5.0,
            "category": "MRI Center",
        },
        {
            "name": "Dr Lal PathLabs",
            "phone": "+91 98204 61234",
            "address": "Hill Rd, Bandra West, Mumbai 400050",
            "open_status": "OPEN NOW",
            "rating": 4.9,
            "category": "Pathology lab",
        },
        {
            "name": "Lilavati Hospital & Research Centre",
            "phone": "+91 22 6930 1000",
            "address": "Bandra Reclamation, Bandra (W), Mumbai 400050",
            "open_status": "OPEN NOW",
            "rating": 4.9,
            "category": "Hospital",
        },
        {
            "name": "Lilavati Hospital And Research Centre",
            "phone": "+91 22 2675 1000",
            "address": "Bandra Reclamation, Bandra West, Mumbai 400050",
            "open_status": "OPEN NOW",
            "rating": 4.6,
            "category": "Hospital",
        },
    ]
    ranked_edge, _, dropped_edge = filter_and_dedupe_raw_shops(
        worli_and_unlisted,
        city="Mumbai",
        locality="Bandra West",
        max_shops=10,
        query="MRI scan center",
    )
    assert dropped_edge == 2  # Pure pathology lab + 98200 00000 placeholder dropped
    assert ranked_edge[0].name == "Lilavati Hospital & Research Centre"
    assert ranked_edge[0].locality_match_tier == 0
    # Duplicate Lilavati entry collapsed cleanly
    assert sum("Lilavati" in s.name for s in ranked_edge) == 1
    assert all(s.locality_match_tier > 0 for s in ranked_edge[1:])


def test_lilavati_multistage_spoken_word_ivr_and_human_pickup_detection() -> None:
    stage1 = (
        "Welcome to Leelavati Hospital. More than healthcare, human care. "
        "Press one for information in English. Marathi madhe don dial kara. Hindi ke liye teen dial karein."
    )
    assert is_ivr_or_hold_announcement(stage1) is True
    assert (
        extract_ivr_dtmf_digit(
            stage1,
            preferred_tone=VocalTone.POLITE_INDIAN_ENGLISH,
            user_goal="Full Body MRI scan slot today",
            already_sent=(),
        )
        == "1"
    )

    stage2 = (
        "Please dial the desired extension number. Or press zero for operator assistance. "
        "Four for OPD, five for admission, and six for casualty. Please hold on. Operator will assist you shortly."
    )
    assert is_ivr_or_hold_announcement(stage2) is True
    assert is_ivr_or_hold_announcement("Please hold on. Operator will assist you shortly.") is True
    assert (
        extract_ivr_dtmf_digit(
            stage2,
            preferred_tone=VocalTone.POLITE_INDIAN_ENGLISH,
            user_goal="Full Body MRI scan slot today",
            already_sent=["1"],
        )
        == "0"
    )
    assert is_live_human_pickup_after_ivr("Hello, Leelavati.") is True
    assert is_live_human_pickup_after_ivr("हैलो, लीलावती हॉस्पिटल।") is True
    assert is_live_human_pickup_after_ivr("Lilavati Hospital", allow_short_utterance_fallback=True) is True
    assert is_live_human_pickup_after_ivr("Please listen carefully", allow_short_utterance_fallback=False) is False
    assert is_live_human_pickup_after_ivr("For appointments", allow_short_utterance_fallback=True) is False
    assert is_live_human_pickup_after_ivr("Please hold on. Operator will assist you shortly.") is False
    # Judge regression: English 'do' must NEVER convert to '2' or trigger IVR/DTMF!
    human_question = "Hello ma'am, what can I do for your MRI appointment?"
    assert is_ivr_or_hold_announcement(human_question) is False
    assert extract_ivr_dtmf_digit(human_question, VocalTone.POLITE_INDIAN_ENGLISH, "Brain MRI") is None
    assert is_ivr_or_hold_announcement("Thank you for calling Lilavati, how may I help you?") is False


def test_google_a2a_v1_jsonrpc_and_x402_eip3009_cryptography(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RINGSCOUT_PASSCODE", "owner-pass-777")
    monkeypatch.setenv("RINGSCOUT_PAYMENT_GATE_MODE", "required")
    _, seller_wallet = generate_evm_keypair()
    buyer_priv, buyer_wallet = generate_evm_keypair()
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", seller_wallet)

    async def fake_discover(req: DiscoverRequest) -> DiscoverResponse:
        return DiscoverResponse(
            query=req.query,
            city=req.city,
            locality=req.locality,
            source="vertex_google_maps_grounding",
            shops=[
                IndianShopTarget(
                    shop_id="shop_1",
                    name="ILLUME Imaging and Diagnostics",
                    phone_e164="+919324038669",
                    phone_display="+91 93240 38669",
                    address="St Martins Rd, Bandra West, Mumbai 400050",
                    locality="Bandra West",
                    city="Mumbai",
                    open_status="OPEN NOW",
                    rating=4.9,
                    locality_badge="In Bandra West",
                )
            ],
        )

    async def fake_compile(goal: str, tone: VocalTone) -> Any:
        return heuristic_conversational_call_plan(goal, tone)

    async def fake_facilitator_insufficient_sepolia(
        self: httpx.AsyncClient, url: str, **kwargs: Any
    ) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={
                "isValid": False,
                "invalidReason": "invalid_exact_evm_insufficient_balance",
                "payer": buyer_wallet,
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(a2a_gateway, "discover_indian_shops", fake_discover)
    monkeypatch.setattr(a2a_gateway, "compile_conversational_call_plan", fake_compile)
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_facilitator_insufficient_sepolia)

    app = create_app(synthesize_audio=False, require_provenance=True)
    client = TestClient(app)

    # 1. Official Google A2A v1.0 Agent Card & /.well-known/x402 Bazaar catalog
    card = client.get("/.well-known/agent-card.json")
    assert card.status_code == 200
    assert card.json()["supportedInterfaces"][0]["protocolBinding"] == "JSON-RPC"
    assert card.json()["payment"]["pay_to"] == seller_wallet

    x402_cat = client.get("/.well-known/x402")
    assert x402_cat.status_code == 200
    assert len(x402_cat.json()["resources"]) >= 3

    # Crawler GET /api/v1/scout returns 402 with Bazaar schema
    probe402 = client.get("/api/v1/scout")
    assert probe402.status_code == 402
    assert probe402.json()["extensions"]["bazaar"]["discoverable"] is True

    # 2. Unpaid POST /a2a (`message/send`) -> HTTP 402 INPUT_REQUIRED
    rpc_msg = {
        "jsonrpc": "2.0",
        "id": "req_1",
        "method": "message/send",
        "params": {
            "message": {
                "messageId": "m_1",
                "role": "user",
                "parts": [
                    {
                        "kind": "data",
                        "data": {
                            "query": "MRI scan center",
                            "item_question": "Check 1.5T Brain MRI slot today and cash price in ₹",
                            "city": "Mumbai",
                            "locality": "Bandra West",
                            "max_shops": 1,
                            "preferred_tone": "polite_indian_english",
                            "execution_mode": "swarm",
                        },
                    }
                ],
            }
        },
    }
    unpaid_a2a = client.post("/a2a", json=rpc_msg)
    assert unpaid_a2a.status_code == 402
    challenge = json.loads(base64.b64decode(unpaid_a2a.headers["PAYMENT-REQUIRED"]).decode("utf-8"))
    sepolia_req = next(r for r in challenge["accepts"] if r["network"] == "eip155:84532")

    # 3. Sign real secp256k1 EIP-712 TransferWithAuthorization (EIP-3009) & verify ecrecover + tamper rejection
    signed_payload = sign_x402_exact_evm_payload(
        buyer_private_key_hex=buyer_priv,
        accepted_requirement=sepolia_req,
    )
    ok_sig, rec_addr, _ = verify_x402_eip3009_signature_locally(signed_payload)
    assert ok_sig is True and rec_addr == buyer_wallet

    tampered = json.loads(json.dumps(signed_payload))
    tampered["payload"]["authorization"]["value"] = "1"
    bad_sig, _, _ = verify_x402_eip3009_signature_locally(tampered)
    assert bad_sig is False

    b64_sig = base64.b64encode(json.dumps(signed_payload).encode("utf-8")).decode("ascii")

    # Unauthenticated external caller with unfunded Sepolia wallet is still rejected (402)!
    unfunded_ext = client.post(
        "/a2a",
        headers={"PAYMENT-SIGNATURE": b64_sig},
        json=rpc_msg,
    )
    assert unfunded_ext.status_code == 402

    # Authenticated owner testing EIP-3009 Sepolia handshake settles & returns completed A2A Task + PAYMENT-RESPONSE!
    paid_a2a = client.post(
        "/a2a",
        headers={
            "PAYMENT-SIGNATURE": b64_sig,
            "X-RingScout-Passcode": "owner-pass-777",
        },
        json=rpc_msg,
    )
    assert paid_a2a.status_code == 200
    assert "PAYMENT-RESPONSE" in paid_a2a.headers
    task_done = paid_a2a.json()["result"]["task"]
    assert task_done["status"]["state"] == "COMPLETED"
    assert task_done["metadata"]["settlement"]["rail"] == "x402-v2-eip3009-sepolia-verified"
    assert task_done["metadata"]["settlement"]["payer"] == buyer_wallet

    # Verify A2A `tasks/get` retrieves the completed task
    t_get = client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": "req_2", "method": "tasks/get", "params": {"taskId": task_done["taskId"]}},
    ).json()
    assert t_get["result"]["task"]["status"]["state"] == "COMPLETED"

    # Judge regression: replaying the exact same EIP-3009 nonce a second time must be rejected (402)!
    replay_a2a = client.post(
        "/a2a",
        headers={
            "PAYMENT-SIGNATURE": b64_sig,
            "X-RingScout-Passcode": "owner-pass-777",
        },
        json=rpc_msg,
    )
    assert replay_a2a.status_code == 402
    assert "replay blocked" in replay_a2a.json()["error"]["message"]

    # Browser UI Door 2 helper endpoint (`/api/v1/demo/buyer-sign-x402`) produces valid Base64 EIP-3009 signature
    demo_sign = client.post(
        "/api/v1/demo/buyer-sign-x402",
        headers={"X-RingScout-Passcode": "owner-pass-777"},
    )
    assert demo_sign.status_code == 200
    demo_sign_json = demo_sign.json()
    assert demo_sign_json["buyer_wallet"].startswith("0x")
    decoded_demo_sig = json.loads(
        base64.b64decode(demo_sign_json["payment_signature_b64"]).decode("utf-8")
    )
    ok_demo_sig, _, _ = verify_x402_eip3009_signature_locally(decoded_demo_sig)
    assert ok_demo_sig is True

    turns = [
        TranscriptTurn(speaker="agent", text="Hi, good afternoon — do you have a slot today for 1.5T Brain MRI?", timestamp_sec=1.5),
        TranscriptTurn(speaker="shopkeeper", text="Sorry ma'am, we don't have slot today, available day after tomorrow.", timestamp_sec=6.2),
    ]
    dec = heuristic_side_brain_fallback(turns, VocalTone.POLITE_INDIAN_ENGLISH, user_goal="1.5T Brain MRI")
    assert dec.in_stock is False
    assert dec.eta_or_slot == "Next slot: day after tomorrow"
    assert dec.should_hangup is True


def test_universal_9_verticals_street_confidence_markers_and_did_rotation() -> None:
    from ringscout.google_lookup import extract_street_landmark, infer_place_category, resolve_maps_search_query
    from ringscout.models import LiveCarrierConfig, PlaceCategory
    from ringscout.voice_engine import spoken_street_landmark

    assert infer_place_category("", "Deluxe room tonight with early check-in") == PlaceCategory.HOTEL
    assert infer_place_category("", "Table for 4 tonight at 8:30 PM") == PlaceCategory.RESTAURANT
    assert infer_place_category("", "XP95 petrol and nitrogen air working") == PlaceCategory.PETROL
    assert infer_place_category("", "Pediatrician OPD slot today") == PlaceCategory.CLINIC

    q_hotel, cat_hotel = resolve_maps_search_query(
        DiscoverRequest(place_category=PlaceCategory.HOTEL, purpose="Room tonight", city="Mumbai", locality="Bandra West")
    )
    assert cat_hotel == PlaceCategory.HOTEL
    assert "hotel" in q_hotel.lower()

    landmark = spoken_street_landmark("Mac Ronells, St Andrews Rd, Bandra West, Mumbai 400050", "Bandra West")
    assert "Andrews Road" in landmark
    assert extract_street_landmark("Plot 14, Linking Road, Bandra West 400050", "Bandra West") != ""

    hotel_plan = heuristic_conversational_call_plan(
        "Deluxe double room tonight and 10 AM early check-in",
        VocalTone.POLITE_INDIAN_ENGLISH,
        PlaceCategory.HOTEL,
    )
    cfg = build_thin_live_config(
        shop_name="Taj Lands End",
        locality="Bandra West",
        user_goal="Deluxe double room tonight and 10 AM early check-in",
        vocal_tone=VocalTone.POLITE_INDIAN_ENGLISH,
        is_live_pstn=True,
        call_plan=hotel_plan,
        shop_address="Bandstand Road, Bandra West, Mumbai 400050",
        place_category=PlaceCategory.HOTEL,
        caller_name="Rahul",
    )
    sys_prompt = cfg.system_instruction.parts[0].text
    assert "Bandstand Road" in sys_prompt
    assert "Rahul" in sys_prompt
    assert "Taj Lands End" in sys_prompt
    assert cfg.tools is None

    # Verify Multi-DID anti-spam caller-ID rotation across 3 outbound DIDs
    pstn = LiveIndiaPstnAdapter(audio_store={})
    pstn.set_runtime_config(
        LiveCarrierConfig(
            provider=CarrierProvider.VOBIZ,
            auth_id="MA123",
            auth_token="sec123",
            from_number="+912260001111",
            from_numbers=["+912260001111", "+918040002222", "+912260003333"],
        )
    )
    resolved = pstn.resolve_config()
    assert resolved is not None
    picked = [pstn._pick_outbound_cli(resolved) for _ in range(3)]
    assert picked == ["+912260001111", "+918040002222", "+912260003333"]


def test_phase_a_open_connectors_chatgpt_openapi_geo_and_traffic_analytics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RINGSCOUT_PASSCODE", "owner-pass-777")
    monkeypatch.setenv("RINGSCOUT_PAYMENT_GATE_MODE", "off")

    from ringscout.models import PlaceCategory

    async def fake_discover(req: DiscoverRequest) -> DiscoverResponse:
        return DiscoverResponse(
            query=req.query or "hotels open now",
            place_category=PlaceCategory.HOTEL,
            city=req.city,
            locality=req.locality,
            source="vertex_google_maps_grounding",
            shops=[
                IndianShopTarget(
                    shop_id="shop_1",
                    name="Taj Lands End",
                    phone_e164="+912266681234",
                    phone_display="+91 22 6668 1234",
                    address="Bandstand, Bandra West, Mumbai 400050",
                    locality="Bandra West",
                    city="Mumbai",
                    open_status="OPEN NOW",
                    rating=4.7,
                )
            ],
        )

    monkeypatch.setattr(a2a_gateway, "discover_indian_shops", fake_discover)

    app = create_app(synthesize_audio=False, require_provenance=True)
    client = TestClient(app)

    # 1. Public GEO & Discovery endpoints work with zero auth & record AI Bot crawls
    robots = client.get("/robots.txt", headers={"User-Agent": "Mozilla/5.0 OAI-SearchBot/1.0"})
    assert robots.status_code == 200
    assert "OAI-SearchBot" in robots.text
    assert "Claude-SearchBot" in robots.text

    llms = client.get("/llms.txt", headers={"User-Agent": "Claude-SearchBot/1.0"})
    assert llms.status_code == 200
    assert "scout_places_on_google_maps" in llms.text

    sitemap = client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    assert "/developers" in sitemap.text

    mcp_manifest = client.get("/.well-known/mcp.json")
    assert mcp_manifest.status_code == 200
    assert "scout_places_on_google_maps" in mcp_manifest.json()["tools"]

    openapi_spec = client.get("/openapi-actions.json")
    assert openapi_spec.status_code == 200
    assert openapi_spec.json()["paths"]["/api/v1/scout"]["post"]["operationId"] == "scoutPlacesOnGoogleMaps"

    # 2. Separated HTML surfaces render cleanly
    home_html = client.get("/", headers={"Referer": "https://chatgpt.com/"})
    assert home_html.status_code == 200
    assert "application/ld+json" in home_html.text
    assert client.get("/developers").status_code == 200
    assert client.get("/admin").status_code == 200

    # 3. Unauthenticated Claude / Gemini MCP connector calls `scout_places_on_google_maps` with ZERO payment wall in Phase A!
    mcp_call = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {
                "name": "scout_places_on_google_maps",
                "arguments": {
                    "place_category": "hotel",
                    "city": "Mumbai",
                    "locality": "Bandra West",
                    "item_question": "Deluxe room tonight and 10 AM early check-in tariff in ₹",
                    "max_shops": 1,
                    "execution_mode": "swarm",
                },
            },
        },
    )
    assert mcp_call.status_code == 200
    mcp_res = mcp_call.json()["result"]
    assert mcp_res["isError"] is False
    parsed_mcp = json.loads(mcp_res["content"][0]["text"])
    assert parsed_mcp["settlement"]["rail"] == "open-a2a-phase-a"
    assert parsed_mcp["settlement"]["charged_usd"] == 0.0
    assert parsed_mcp["place_category"] == "hotel"
    assert parsed_mcp["ranked_quotes"][0]["availability_label"] != ""

    # 4. Admin Analytics accurately tracked OAI-SearchBot, Claude-SearchBot, ChatGPT Referral, and MCP call
    analytics = client.get(
        "/api/admin/analytics",
        headers={"X-RingScout-Passcode": "owner-pass-777"},
    ).json()
    assert analytics["totals"]["ai_bot_crawls"] >= 2
    assert analytics["ai_bots_by_agent"]["ChatGPT SearchBot (OpenAI)"] >= 1
    assert analytics["ai_bots_by_agent"]["Claude SearchBot (Anthropic)"] >= 1
    assert analytics["referrals_by_source"]["ChatGPT Referral"] >= 1
    assert analytics["protocol_activity"]["MCP Connector Call (POST /mcp)"] >= 1


def test_stripe_mpp_spt_payment_intent_settlement_and_replay_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RINGSCOUT_PASSCODE", "owner-pass-777")
    monkeypatch.delenv("RINGSCOUT_PAYMENT_GATE_MODE", raising=False)

    from ringscout.models import PlaceCategory
    from ringscout.stripe_protocol import _b64url_decode, update_runtime_payment_config

    async def fake_discover(req: DiscoverRequest) -> DiscoverResponse:
        return DiscoverResponse(
            query="restaurants open now",
            place_category=PlaceCategory.RESTAURANT,
            city=req.city,
            locality=req.locality,
            source="vertex_google_maps_grounding",
            shops=[
                IndianShopTarget(
                    shop_id="shop_1",
                    name="Bastian At The Top",
                    phone_e164="+912250333555",
                    phone_display="+91 22 5033 3555",
                    address="Linking Road, Bandra West, Mumbai",
                    locality="Bandra West",
                    city="Mumbai",
                    open_status="OPEN NOW",
                    rating=4.6,
                )
            ],
        )

    async def fake_stripe_api_post(
        self: httpx.AsyncClient, url: str, **kwargs: Any
    ) -> httpx.Response:
        assert "api.stripe.com/v1/payment_intents" in url
        form = kwargs.get("data") or {}
        assert form["amount"] == "75"
        assert form["currency"] == "usd"
        assert form["confirm"] == "true"
        assert form["automatic_payment_methods[allow_redirects]"] == "never"
        return httpx.Response(
            status_code=200,
            json={"id": "pi_3StripeLiveMppVerified99", "status": "succeeded", "amount": 75},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(a2a_gateway, "discover_indian_shops", fake_discover)
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_stripe_api_post)

    app = create_app(synthesize_audio=False, require_provenance=True)
    client = TestClient(app)

    try:
        # 1. Flip runtime payment gate to Phase B ("required") + set Stripe test secret key via /api/admin/payment-config
        upd = client.post(
            "/api/admin/payment-config",
            headers={"X-RingScout-Passcode": "owner-pass-777"},
            json={
                "gate_mode": "required",
                "stripe_secret_key": "sk_test_ringscout_mpp_demo_key_123456",
                "stripe_checkout_url": "https://buy.stripe.com/test_ringscout",
            },
        )
        assert upd.status_code == 200
        assert upd.json()["gate_mode"] == "required"
        assert upd.json()["stripe_mode"] == "test"

        # 2. Unpaid POST /api/v1/scout returns HTTP 402 with spec-compliant Stripe MPP WWW-Authenticate
        unpaid = client.post(
            "/api/v1/scout",
            json={
                "place_category": "restaurant",
                "item_question": "Table for 4 tonight at 8:30 PM",
                "city": "Mumbai",
                "locality": "Bandra West",
                "max_shops": 1,
            },
        )
        assert unpaid.status_code == 402
        www_auth = unpaid.headers["WWW-Authenticate"]
        assert 'method="stripe"' in www_auth
        assert 'intent="charge"' in www_auth
        assert unpaid.json()["mpp"]["checkoutUrl"] == "https://buy.stripe.com/test_ringscout"

        # 3. Mint signed Stripe MPP SPT/PaymentMethod credential and settle via Stripe PaymentIntents API
        signed_mpp = client.post(
            "/api/v1/demo/buyer-sign-stripe",
            headers={"X-RingScout-Passcode": "owner-pass-777"},
        ).json()
        paid = client.post(
            "/api/v1/scout",
            headers={"Authorization": signed_mpp["authorization_header"]},
            json={
                "place_category": "restaurant",
                "item_question": "Table for 4 tonight at 8:30 PM",
                "city": "Mumbai",
                "locality": "Bandra West",
                "max_shops": 1,
            },
        )
        assert paid.status_code == 200
        assert "Payment-Receipt" in paid.headers
        receipt_decoded = json.loads(_b64url_decode(paid.headers["Payment-Receipt"]).decode("utf-8"))
        assert receipt_decoded["method"] == "stripe"
        assert receipt_decoded["paymentIntent"] == "pi_3StripeLiveMppVerified99"
        assert paid.json()["settlement"]["rail"] == "stripe-mpp-spt-live"

        # 4. Replaying the exact same Stripe MPP credential must be rejected (402)!
        replay = client.post(
            "/api/v1/scout",
            headers={"Authorization": signed_mpp["authorization_header"]},
            json={"place_category": "restaurant", "item_question": "Table for 4", "max_shops": 1},
        )
        assert replay.status_code == 402
        assert "replay blocked" in replay.json()["error"]
    finally:
        update_runtime_payment_config(gate_mode="off", stripe_secret_key="", stripe_checkout_url="")


def test_judge_p0_p1_regressions_quests_list_mpp_nonce_pstn_gate_and_pi_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RINGSCOUT_PASSCODE", "owner-pass-777")
    monkeypatch.setenv("RINGSCOUT_PAYMENT_GATE_MODE", "off")

    from ringscout.models import PlaceCategory
    from ringscout.stripe_protocol import (
        build_stripe_mpp_challenge,
        update_runtime_payment_config,
        verify_mpp_challenge_binding,
    )

    # 1. 250 consecutive Stripe MPP challenges self-verify 100% (zero '_' delimiter failures)
    for _ in range(250):
        ch = build_stripe_mpp_challenge("https://ringscout.example.com/api/v1/scout")
        ok, err = verify_mpp_challenge_binding(
            ch, expected_resource_url="https://ringscout.example.com/mcp"
        )
        assert ok is True, err

    # Cross-origin MPP challenge replay is rejected
    ch_evil = build_stripe_mpp_challenge("https://other-merchant.example.com/api")
    ok_cross, _ = verify_mpp_challenge_binding(
        ch_evil, expected_resource_url="https://ringscout.example.com/api/v1/scout"
    )
    assert ok_cross is False

    async def fake_discover(req: DiscoverRequest) -> DiscoverResponse:
        return DiscoverResponse(
            query="hotels open now",
            place_category=PlaceCategory.HOTEL,
            city="Mumbai",
            locality="Bandra West",
            source="vertex_google_maps_grounding",
            shops=[
                IndianShopTarget(
                    shop_id="shop_1",
                    name="Taj Lands End",
                    phone_e164="+912266681234",
                    phone_display="+91 22 6668 1234",
                    address="Bandstand Road, Bandra West, Mumbai 400050",
                    locality="Bandra West",
                    city="Mumbai",
                    open_status="OPEN NOW",
                    rating=4.7,
                )
            ],
        )

    monkeypatch.setattr(a2a_gateway, "discover_indian_shops", fake_discover)
    app = create_app(synthesize_audio=False, require_provenance=True)
    client = TestClient(app)

    try:
        # 2. Unauthenticated Phase A caller trying execution_mode='live_pstn' is blocked (no toll-fraud!)
        pstn_attempt = client.post(
            "/api/v1/scout",
            json={
                "place_category": "hotel",
                "item_question": "Room tonight",
                "execution_mode": "live_pstn",
                "max_shops": 1,
            },
        )
        assert pstn_attempt.status_code == 400
        assert "verified Stripe MPP / x402 payment or owner passcode" in pstn_attempt.json()["detail"]

        # 3. Swarm scout succeeds, cites street landmark + caller_name, AND GET /api/quests returns 200 OK (no 500!)
        ok_scout = client.post(
            "/api/v1/scout",
            headers={"X-Forwarded-For": "203.0.113.1, 198.51.100.42", "User-Agent": "BotA/1"},
            json={
                "place_category": "hotel",
                "caller_name": "Rahul",
                "item_question": "Deluxe room tonight and 10 AM early check-in",
                "execution_mode": "swarm",
                "max_shops": 1,
            },
        )
        assert ok_scout.status_code == 200
        t0 = ok_scout.json()["ranked_quotes"][0]["transcript"][0]["text"]
        assert "Bandstand Road" in t0 or "Taj Lands End" in t0

        q_list = client.get(
            "/api/quests",
            headers={"X-RingScout-Passcode": "owner-pass-777"},
        )
        assert q_list.status_code == 200
        quests_arr = q_list.json()["quests"]
        assert len(quests_arr) == 1
        assert quests_arr[0]["created_at"].endswith("Z")

        # 4. Spoofing leftmost XFF + rotating User-Agent cannot bypass daily free cap when rightmost IP matches!
        update_runtime_payment_config(free_daily_calls_per_client=2)
        second = client.post(
            "/api/v1/scout",
            headers={"X-Forwarded-For": "9.9.9.9, 198.51.100.42", "User-Agent": "RotatedUA/2"},
            json={"place_category": "hotel", "item_question": "Room tonight", "execution_mode": "swarm", "max_shops": 1},
        )
        assert second.status_code == 200
        third_spoofed = client.post(
            "/api/v1/scout",
            headers={"X-Forwarded-For": "8.8.8.8, 198.51.100.42", "User-Agent": "RotatedUA/3"},
            json={"place_category": "hotel", "item_question": "Room tonight", "execution_mode": "swarm", "max_shops": 1},
        )
        assert third_spoofed.status_code == 402

        # 5. Stripe path-traversal token `pi_../charges` is rejected before HTTP call
        monkeypatch.setenv("RINGSCOUT_PAYMENT_GATE_MODE", "required")
        update_runtime_payment_config(stripe_secret_key="sk_test_dummy_key_123456")
        bad_pi = client.post(
            "/api/v1/scout",
            headers={"X-Stripe-Payment-Token": "pi_../charges"},
            json={"place_category": "hotel", "item_question": "Room tonight", "max_shops": 1},
        )
        assert bad_pi.status_code == 402
        assert "Malformed Stripe token" in bad_pi.json()["error"]
    finally:
        update_runtime_payment_config(
            gate_mode="off",
            free_daily_calls_per_client=10,
            stripe_secret_key="",
        )


def test_india_locations_dropdown_strict_category_filter_and_mcp_streamable_http_405(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RINGSCOUT_PASSCODE", "owner-pass-777")
    from ringscout.google_lookup import is_relevant_for_query
    from ringscout.models import PlaceCategory

    # 1. Strict category filter blocks cross-vertical bleed (physio clinic on salon, Bosch repair on hotel)
    assert is_relevant_for_query(
        {"name": "BANDRA PHYSIOTHERAPY CENTRE", "category": "Physiotherapy clinic"}, "beauty salon hair spa"
    ) is False
    assert is_relevant_for_query(
        {"name": "Sana Beauty World & Academy", "category": "Beauty salon"}, "beauty salon hair spa"
    ) is True
    assert is_relevant_for_query(
        {"name": "Siemens Bosch Service Centre Mumbai", "category": "Appliance repair service"}, "hotels resorts boutique stays"
    ) is False
    assert is_relevant_for_query(
        {"name": "Executive Enclave", "category": "Hotel"}, "hotels resorts boutique stays"
    ) is True

    app = create_app(synthesize_audio=False, require_provenance=True)
    client = TestClient(app)

    # 2. Consumer City & Neighbourhood dropdown API serves Bandra West (400050) & Bandra East (400051)
    loc_resp = client.get("/api/locations")
    assert loc_resp.status_code == 200
    cities = loc_resp.json()["cities"]
    assert "Mumbai" in cities
    mumbai_areas = {a["name"]: a for a in cities["Mumbai"]}
    assert "Bandra West" in mumbai_areas
    assert "Bandra East" in mumbai_areas
    assert 400050 in mumbai_areas["Bandra West"]["pins"]
    assert 400051 in mumbai_areas["Bandra East"]["pins"]

    # 3. MCP Streamable HTTP 2025-06-18 spec: GET /mcp with Accept: text/event-stream returns 405
    sse_probe = client.get("/mcp", headers={"Accept": "text/event-stream"})
    assert sse_probe.status_code == 405
    card_probe = client.get("/mcp", headers={"Accept": "application/json"})
    assert card_probe.status_code == 200
    assert card_probe.json()["protocol"] == "mcp-jsonrpc-2.0"


def test_spa_vs_salon_specific_place_dashboard_and_zero_repo_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pathlib
    from ringscout.google_lookup import infer_place_category, is_relevant_for_query
    from ringscout.models import PlaceCategory

    # 1. Spa intent is cleanly separated from Hair Salons
    assert infer_place_category("", "60-min deep tissue massage slot at 7 PM", PlaceCategory.CUSTOM) == PlaceCategory.SPA
    assert infer_place_category("", "Thai foot spa this evening", PlaceCategory.SALON) == PlaceCategory.SPA
    assert infer_place_category("", "Haircut and blow-dry at 6 PM", PlaceCategory.SALON) == PlaceCategory.SALON

    spa_q = "day spa body massage thai spa wellness spa"
    assert is_relevant_for_query({"name": "Cut Da Crap Unisex Salon", "category": "Hair salon"}, spa_q) is False
    assert is_relevant_for_query({"name": "PEARL SALON/MAKEUP STUDIO", "category": "Beauty salon"}, spa_q) is False
    assert is_relevant_for_query({"name": "Four Fountains De-Stress Spa", "category": "Day spa"}, spa_q) is True
    assert is_relevant_for_query({"name": "Cut Da Crap Unisex Salon", "category": "Hair salon"}, spa_q, specific_place="Cut Da Crap") is True

    # 2. Fail-Closed Admin Auth when RINGSCOUT_PUBLIC_CONSUMER_ACCESS=1 and no passcode set
    monkeypatch.delenv("RINGSCOUT_PASSCODE", raising=False)
    monkeypatch.delenv("RINGSCOUT_ADMIN_PASSCODE", raising=False)
    monkeypatch.setenv("RINGSCOUT_PUBLIC_CONSUMER_ACCESS", "1")
    fail_closed_client = TestClient(create_app(synthesize_audio=False, require_provenance=True))
    assert fail_closed_client.get("/api/pstn/config").status_code == 401

    # 3. Public consumer access with RINGSCOUT_ADMIN_PASSCODE set
    monkeypatch.setenv("RINGSCOUT_ADMIN_PASSCODE", "admin-vault-999")

    async def fake_maps_lookup(req: Any) -> DiscoverResponse:
        return DiscoverResponse(
            query=req.purpose or req.specific_place or "spa",
            place_category=req.place_category,
            city=req.city,
            locality=req.locality,
            source="vertex_google_maps_grounding",
            shops=[
                IndianShopTarget(
                    shop_id="shop_spa_1",
                    name="Dream Spa Bandra",
                    phone_e164="+919892025216",
                    phone_display="+91 98920 25216",
                    address="Waterfield Road, Bandra West, Mumbai 400050",
                    locality="Bandra West",
                    city="Mumbai",
                    open_status="OPEN NOW",
                    is_open_now=True,
                    rating=4.9,
                    category="Day spa",
                )
            ],
        )

    import ringscout.server as _srv_mod
    monkeypatch.setattr(_srv_mod, "discover_indian_shops", fake_maps_lookup)

    app = create_app(synthesize_audio=False, require_provenance=True)
    client = TestClient(app)

    # Admin route is strictly locked without admin passcode
    assert client.get("/api/pstn/config").status_code == 401
    assert client.get("/api/pstn/config", headers={"X-RingScout-Passcode": "admin-vault-999"}).status_code == 200

    # Consumer + Add Specific Place by Google Maps name works with ZERO passcode!
    add_by_name = client.post(
        "/api/places/add",
        json={
            "place_name": "Dream Spa Bandra",
            "city": "Mumbai",
            "locality": "Bandra West",
            "place_category": "spa",
        },
    )
    assert add_by_name.status_code == 200
    assert add_by_name.json()["shops"][0]["phone_e164"] == "+919892025216"

    # P0 Guard: Unauthenticated consumer CANNOT inject an unlisted personal +91 mobile number!
    unlisted_anon = client.post(
        "/api/places/add",
        json={
            "place_name": "Random Person",
            "phone": "+91 98111 22334",
            "city": "Mumbai",
            "locality": "Bandra West",
            "place_category": "salon",
        },
    )
    assert unlisted_anon.status_code == 403
    assert "+919811122334" not in app.state.manager.verified_google_phones

    # Admin CAN add a known unlisted business +91 number
    add_admin = client.post(
        "/api/places/add",
        headers={"X-RingScout-Passcode": "admin-vault-999"},
        json={
            "place_name": "Jean-Claude Biguine Salon",
            "phone": "+91 22 6522 1100",
            "city": "Mumbai",
            "locality": "Bandra West",
            "place_category": "salon",
        },
    )
    assert add_admin.status_code == 200
    assert add_admin.json()["shops"][0]["phone_e164"] == "+912265221100"

    # Toll-free 1800 numbers are still blocked even for Admin
    bad_toll = client.post(
        "/api/places/add",
        headers={"X-RingScout-Passcode": "admin-vault-999"},
        json={"place_name": "Call Center", "phone": "1800-102-4455", "city": "Mumbai", "locality": "Bandra West"},
    )
    assert bad_toll.status_code == 400

    # 4. Quest ownership token (X-Quest-Token) & Dashboard privacy masking
    q_create = client.post(
        "/api/quests",
        json={
            "user_goal": "60-minute deep tissue massage slot today at 7 PM and price in ₹",
            "place_category": "spa",
            "city": "Mumbai",
            "locality": "Bandra West",
            "mode": "hinglish_swarm",
            "shops": add_by_name.json()["shops"],
        },
    )
    assert q_create.status_code == 200
    q_id = q_create.json()["quest"]["quest_id"]
    q_tok = q_create.json()["quest_token"]
    assert q_tok

    # Anonymous viewer without X-Quest-Token gets masked phones and CANNOT stop someone else's quest
    anon_get = client.get(f"/api/quests/{q_id}").json()
    assert anon_get["quest"]["shops"][0]["phone_e164"] == ""
    assert "•" in anon_get["quest"]["shops"][0]["phone_display"]
    assert client.post(f"/api/quests/{q_id}/stop").status_code == 403

    # Quest owner WITH X-Quest-Token gets full details and CAN stop their quest
    owner_get = client.get(f"/api/quests/{q_id}", headers={"X-Quest-Token": q_tok}).json()
    assert owner_get["quest"]["shops"][0]["phone_e164"] == "+919892025216"
    assert client.post(f"/api/quests/{q_id}/stop", headers={"X-Quest-Token": q_tok}).status_code == 200

    # 5. Live Dashboard & Official MCP Registry server.json endpoints + Connected-First Ranking & Dual-Price Extraction
    dash_html = client.get("/dashboard")
    assert dash_html.status_code == 200
    dash_api = client.get("/api/dashboard/summary")
    assert dash_api.status_code == 200
    assert len(dash_api.json()["mcp_directories"]) >= 8
    assert dash_api.json()["quests"][0]["quest_id"].startswith("q_••••")
    srv_json = client.get("/server.json")
    assert srv_json.status_code == 200
    assert srv_json.json()["remotes"][0]["type"] == "streamable-http"

    from ringscout.voice_engine import extract_hinglish_inr_amount
    assert extract_hinglish_inr_amount("For Thai dry is 1,900 and for deep tissue is 2,700.") == 1900.0

    from ringscout.models import InrQuote, QuestSession, ShopCallState, VocalTone
    s_skip = IndianShopTarget(
        shop_id="s_skip", name="Skipped Spa", phone_e164="+919892025216", phone_display="+91 98920 25216", address="Bandra"
    )
    s_conn = IndianShopTarget(
        shop_id="s_conn", name="Connected Spa", phone_e164="+918291652759", phone_display="+91 82916 52759", address="Bandra"
    )
    qs_rank = QuestSession(
        quest_id="q_rank",
        user_goal="Spa slot",
        city="Mumbai",
        locality="Bandra",
        preferred_tone=VocalTone.POLITE_INDIAN_ENGLISH,
        mode=CallMode.LIVE_PSTN,
        shops=[s_skip, s_conn],
        calls={
            "s_skip": ShopCallState(
                shop=s_skip,
                mode=CallMode.LIVE_PSTN,
                status=CallStatus.SKIPPED,
                outcome=CallOutcome.BUSY_NO_ANSWER,
                vocal_tone=VocalTone.POLITE_INDIAN_ENGLISH,
                quote=InrQuote(in_stock=True, price_inr=1500.0),
            ),
            "s_conn": ShopCallState(
                shop=s_conn,
                mode=CallMode.LIVE_PSTN,
                status=CallStatus.COMPLETED,
                outcome=CallOutcome.CONNECTED_QUOTE,
                vocal_tone=VocalTone.POLITE_INDIAN_ENGLISH,
                quote=InrQuote(in_stock=True, price_inr=1900.0),
            ),
        },
    )
    assert qs_rank.ranked_quotes()[0].shop.name == "Connected Spa"

    # 4. Automated Secret-Leak Guard across repository files
    import re as _re
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    forbidden_regexes = (
        "fnxbBS" + "Gif",
        "MA_FQA" + "V1Q20",
        "teh0sp" + "QOlGc",
        r"sk_live_[A-Za-z0-9]{16,}",
    )
    skip_parts = {".venv", "__pycache__", ".git", ".pytest_cache", "ringscout.egg-info"}
    for fp in repo_root.rglob("*"):
        if any(p in skip_parts or p.endswith(".egg-info") for p in fp.parts) or not fp.is_file():
            continue
        try:
            content = fp.read_text(encoding="utf-8")
        except Exception:
            continue
        for pat in forbidden_regexes:
            assert not _re.search(pat, content), f"Secret token leaked in {fp}"


def test_yauatcha_ivr_and_human_pickup_regression() -> None:
    """Verify exact Yauatcha BKC PBX hold -> multi-city keypad -> hostess 'Yes.' / 'how may I assist you?' flow."""
    from ringscout.voice_engine import (
        MAX_CALL_DURATION_SEC,
        extract_ivr_dtmf_digit,
        is_ivr_or_hold_announcement,
        is_live_human_pickup_after_ivr,
    )

    assert MAX_CALL_DURATION_SEC == 150.0

    # 1. Yauatcha Stage-1 PBX hold recording must enter IVR mute immediately (no premature 'Hi hello' blurt!)
    stage1 = "Thank you for calling Yawatcha. Your call will be answered shortly."
    assert is_ivr_or_hold_announcement(stage1) is True

    # 2. Yauatcha Stage-2 multi-city IVR must select '1' for Mumbai restaurant booking
    stage2 = (
        "Yauatcha, Mumbai, dial one. For Yauatcha, Bengaluru, dial two. "
        "For Yauatcha, Kolkata, dial three. This call may be recorded for internal training and monitoring purposes."
    )
    assert is_ivr_or_hold_announcement(stage2) is True
    digit = extract_ivr_dtmf_digit(
        stage2,
        preferred_tone=VocalTone.POLITE_INDIAN_ENGLISH,
        user_goal="Table for 4 tonight at 8:30 PM with outdoor seating",
    )
    assert digit == "1"

    # 3. Live hostess greetings ('how may I assist you?' and short 'Yes.') must immediately unmute IVR mode!
    assert is_ivr_or_hold_announcement("Yauatcha BKC, good afternoon, how may I assist you?") is False
    assert is_live_human_pickup_after_ivr("Yauatcha BKC, good afternoon, how may I assist you?") is True
    assert is_live_human_pickup_after_ivr("Yes.") is True
    assert is_live_human_pickup_after_ivr("Hello.") is True


def test_mcp_full_discovery_sep2127_sse_batch_prompts_resources_and_registry_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify 2026 MCP Directory & Agent Discovery stack: SEP-2127 Server Card, AI Catalog, 2025-12-11 server.json,
    CORS session headers, annotations, resources, prompts, JSON-RPC batches, HTTP domain auth, and legacy SSE."""
    monkeypatch.setenv("RINGSCOUT_PUBLIC_CONSUMER_ACCESS", "1")
    monkeypatch.setenv("RINGSCOUT_ADMIN_PASSCODE", "admin-vault-999")
    monkeypatch.setenv("MCP_REGISTRY_PUBLIC_KEY", "dGVzdF9lZDI1NTE5X3B1YmxpY19rZXlfYmFzZTY0")

    app = create_app(synthesize_audio=False, require_provenance=True)
    client = TestClient(app)

    # 1. Directory Uptime & Official 2025-12-11 Registry Schema invariants
    assert client.get("/ping").status_code == 200
    assert client.head("/ping").status_code == 200
    assert client.get("/mcp/health").status_code == 200
    assert client.head("/mcp").status_code == 200
    assert client.delete("/mcp").status_code == 204

    srv = client.get("/server.json").json()
    assert srv["$schema"].endswith("2025-12-11/server.schema.json")
    assert 1 <= len(srv["description"]) <= 100
    assert "status" not in srv
    assert {r["type"] for r in srv["remotes"]} == {"streamable-http", "sse"}

    # Checked-in repo server.json matches strict length & schema rules
    repo_srv = json.loads((Path(__file__).resolve().parents[1] / "server.json").read_text(encoding="utf-8"))
    assert repo_srv["$schema"] == srv["$schema"]
    assert 1 <= len(repo_srv["description"]) <= 100
    assert "status" not in repo_srv

    # 2. SEP-2127 Server Card, AI Catalog, and HTTP Domain Verification (/.well-known/mcp-registry-auth)
    sc_resp = client.get("/.well-known/mcp/server-card.json")
    assert sc_resp.status_code == 200
    assert "application/mcp-server-card+json" in sc_resp.headers["content-type"]
    sc_json = sc_resp.json()
    assert sc_json["$schema"].endswith("v1/server-card.schema.json")
    assert 1 <= len(sc_json["description"]) <= 100
    assert isinstance(sc_json["repository"], dict) and sc_json["repository"]["source"] == "github"
    assert "scout_places_on_google_maps" in sc_json["tools"]
    assert len(sc_json["prompts"]) >= 3
    assert len(sc_json["resources"]) >= 3

    cat_resp = client.get("/.well-known/ai-catalog.json")
    assert "application/ai-catalog+json" in cat_resp.headers["content-type"]
    cat_json = cat_resp.json()
    assert cat_json["specVersion"] == "1.0"
    assert len(cat_json["entries"]) >= 4
    assert all("identifier" in e and "type" in e and "url" in e for e in cat_json["entries"])
    assert any(e["type"] == "application/mcp-server-card+json" for e in cat_json["entries"])
    assert any(e["type"] == "application/a2a-agent-card+json" for e in cat_json["entries"])

    reg_auth = client.get("/.well-known/mcp-registry-auth")
    assert reg_auth.status_code == 200
    assert reg_auth.text == "v=MCPv1; k=ed25519; p=dGVzdF9lZDI1NTE5X3B1YmxpY19rZXlfYmFzZTY0"

    # 3. Browser MCP Inspector CORS preflight allows DELETE/HEAD, and POST /mcp exposes Mcp-Session-Id
    cors_opt = client.options(
        "/mcp",
        headers={
            "Origin": "https://inspector.modelcontextprotocol.io",
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "content-type,mcp-session-id",
        },
    )
    assert cors_opt.status_code == 200
    assert "DELETE" in cors_opt.headers.get("access-control-allow-methods", "")
    assert "mcp-session-id" in cors_opt.headers.get("access-control-allow-headers", "").lower()

    # 4. Full MCP Handshake: protocol negotiation, session ID, instructions, annotations, resources, prompts, batch
    init_resp = client.post(
        "/mcp",
        headers={"Origin": "https://inspector.modelcontextprotocol.io"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2099-99-99", "capabilities": {}},
        },
    )
    assert init_resp.status_code == 200
    assert "Mcp-Session-Id" in init_resp.headers.get("access-control-expose-headers", "")
    sid = init_resp.headers.get("mcp-session-id")
    assert sid and sid.startswith("mcp_")
    init_res = init_resp.json()["result"]
    assert init_res["protocolVersion"] == "2025-06-18"
    assert "RingScout" in init_res["instructions"]

    notif = client.post(
        "/mcp",
        headers={"Mcp-Session-Id": sid},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert notif.status_code == 202

    batch_resp = client.post(
        "/mcp",
        headers={"Mcp-Session-Id": sid},
        json=[
            {"jsonrpc": "2.0", "id": 10, "method": "ping"},
            {"jsonrpc": "2.0", "id": 11, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 12, "method": "resources/list"},
            {"jsonrpc": "2.0", "id": 13, "method": "prompts/list"},
        ],
    )
    assert batch_resp.status_code == 200
    batch_items = {item["id"]: item["result"] for item in batch_resp.json()}
    assert batch_items[10] == {}
    tools_by_name = {t["name"]: t for t in batch_items[11]["tools"]}
    assert tools_by_name["get_ringscout_pricing_and_status"]["annotations"]["readOnlyHint"] is True
    assert tools_by_name["scout_places_on_google_maps"]["annotations"]["openWorldHint"] is True
    assert len(batch_items[12]["resources"]) == 3
    assert len(batch_items[13]["prompts"]) == 3

    res_read = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 20,
            "method": "resources/read",
            "params": {"uri": "ringscout://locations/india-neighbourhoods"},
        },
    ).json()
    assert "Mumbai" in res_read["result"]["contents"][0]["text"]

    prompt_get = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 21,
            "method": "prompts/get",
            "params": {
                "name": "scout_restaurant_table",
                "arguments": {"city": "Mumbai", "locality": "Bandra West", "party_size": "4"},
            },
        },
    ).json()
    assert "scout_places_on_google_maps" in prompt_get["result"]["messages"][0]["content"]["text"]

    # Legacy POST /messages standalone compatibility + 404 on unknown session_id + batch size guard
    legacy_msg = client.post(
        "/messages",
        json={"jsonrpc": "2.0", "id": 30, "method": "ping"},
    )
    assert legacy_msg.status_code == 200
    assert legacy_msg.json()["result"] == {}
    assert client.post("/messages?session_id=nonexistent_sid", json={"jsonrpc": "2.0", "id": 31, "method": "ping"}).status_code == 404

    oversized_batch = client.post(
        "/mcp",
        json=[{"jsonrpc": "2.0", "id": i, "method": "ping"} for i in range(40)],
    ).json()
    assert oversized_batch["error"]["code"] == -32600

    unknown_tool = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 32, "method": "tools/call", "params": {"name": "no_such_tool", "arguments": {}}},
    ).json()
    assert unknown_tool["error"]["code"] == -32602





