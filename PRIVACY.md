# RingScout Privacy Notice

**Operator:** Manav Agarwal  
**Contact:** agarwalmanav91@gmail.com  
**Last updated:** 26 November 2026  
**Governing law:** India

This Privacy Notice explains how RingScout processes information when you use its website, remote MCP endpoint (`/mcp`), Google A2A endpoint (`/a2a`), OpenAPI Actions (`/api/v1/scout`), or related interfaces, and when RingScout places outbound inquiries to public business telephone numbers in India.

## 1. Operator and scope

RingScout is operated independently by **Manav Agarwal** (`agarwalmanav91@gmail.com`). It searches public business listings on Google Maps in India and places short, AI-assisted voice inquiries to verify live availability, wait times, appointment slots, services, and prices.

## 2. Information processed

Depending on the workflow, RingScout processes:

- **Request information:** search terms, city, locality, place category, user question, optional caller first name (never impersonated), and requested service.
- **Public business information:** business name, public address, public listing metadata, and listed business telephone number on Google Maps (`1800` toll-free numbers and unlisted personal numbers are blocked).
- **Call information:** destination business number, carrier call identifier, call status, timestamps, duration, IVR keypad actions, post-disclosure transcript turns, and extracted availability and `₹` price details.
- **Call audio proof:** post-disclosure PSTN audio assembled into a bounded in-memory `24kHz` WAV file (subject to recipient consent/objection handling and 24-hour automatic expiry).
- **Technical and quota information:** truncated SHA-256 IP fingerprint (`14` hex chars) for rate limiting, HTTP method, normalized request path, status code, latency, User-Agent classification, and optional payment receipt metadata if metered access is used.

## 3. Proactive AI identity and recording disclosure

Every RingScout call proactively discloses upfront in its opening human turn (`Turn 1`) that the caller is the **RingScout AI assistant** calling on a **recorded call/line**. If an optional caller name is supplied, RingScout states that it is calling *on behalf of* that name (`"for <Name>"` / `"<Name> ke liye"`) and never impersonates a human person.

- **No pre-disclosure recording:** Inbound recipient audio buffering into the WAV recorder (`recorder.append_inbound`) is gated in code (`call.disclosure_delivered`) so recipient speech is recorded only after RingScout starts delivering the AI and recording disclosure.
- **Immediate objection & opt-out handling:** If a call recipient objects to recording or automated AI calls (for example, *"don't record"*, *"record mat karo"*, *"no AI calls"*, or *"do not call"*), RingScout immediately terminates the call and discards any audio buffer (`call.audio_url = None`).

## 4. Access controls and public dashboard privacy

- **Public views redacted:** Public dashboard (`/api/dashboard/summary`), funnel analytics (`/api/tracker/endpoints`), and unauthenticated quest lookups (`/api/quests/{quest_id}`) expose only aggregate telemetry, status/outcome codes, and masked business phone numbers. They **never** expose call transcripts, caller names, raw personal prompts, or signed WAV audio links to other visitors.
- **Caller & administrator access only:** Call transcripts and signed `24kHz` WAV audio URLs (`Cache-Control: no-store`) are accessible only to the initiating caller/session (via HMAC-signed `X-Quest-Token` or signed `?sig=` receipt token) and the service administrator (`X-RingScout-Passcode`).

## 5. Storage, retention, and deletion schedule

RingScout enforces predictable, automated retention limits across both application memory and cloud infrastructure:

- **Quest records, caller names, business numbers, and transcripts (`24-hour TTL`):** Stored only in bounded application memory (`max 256` quests, no database write) and automatically purged after **24 hours (`86,400 seconds`)** via inline read/write checks and a periodic background sweeper (or earlier upon instance restart or self-service deletion via `DELETE /api/quests/{quest_id}`).
- **WAV call audio clips (`24-hour TTL`):** Stored only in bounded application memory (`BoundedAudioStore`, `max 64` clips with FIFO cap) and automatically purged after **24 hours (`86,400 seconds`)**, or immediately discarded if the recipient objects to recording.
- **In-memory operational counters (`24-hour TTL`):** Bounded in-memory HTTP telemetry (`max 4,000` records) and non-personal MCP tool summaries (`max 100` records, storing category/city labels rather than raw prompts) expire within **24 hours**.
- **Google Cloud Run operational request logs (`30-day retention`, verified):** Cloud Run request logs in Google Cloud project `manav-chub` are stored in the verified `_Default` Cloud Logging bucket (`projects/manav-chub/locations/global/buckets/_Default`), which is configured with **`retentionDays: 30`** (`ACTIVE`) and automatically deletes log entries after **30 days**.

## 6. AI processing, telephony, and service providers

RingScout uses the following infrastructure providers to deliver the service:

- **Google Cloud Run (`us-central1`):** container hosting and request logging.
- **Google Vertex AI / Gemini (`us-central1` / `global`):** Gemini Live duplex voice interaction, speech transcription, and out-of-band structured extraction.
- **Google Maps grounding services:** public business discovery and phone provenance verification.
- **Vobiz:** licensed outbound Indian PSTN telephony and bidirectional call-media transport (the codebase also contains optional Plivo and Twilio adapters; this notice will be updated if the active carrier changes).

These providers process call media and metadata under their respective Google Cloud and carrier service terms. Infrastructure processing may occur in regions outside India (including `us-central1` in the United States).

## 7. Purposes of processing and sharing

Information is processed solely to discover public businesses, place requested voice checks with upfront AI/recording disclosure, return availability and `₹` price answers to the requesting caller, enforce abuse/rate limits, and maintain security and reliability. RingScout **does not sell personal data or call audio** and does not use call audio for third-party advertising.

## 8. Your rights and deletion requests (India / DPDP Act)

Requests are handled in accordance with applicable Indian law, including the Digital Personal Data Protection Act, 2023. Callers holding a `quest_token` can delete their quest, transcript, and WAV clip immediately via `DELETE /api/quests/{quest_id}`. Anyone (including a contacted business) may also request access, correction, opt-out, or immediate deletion by emailing **agarwalmanav91@gmail.com** with the approximate call time, city, and business name (please do not send passwords or payment credentials).

## 9. Children and security

RingScout is not intended for children under 18. Carrier, HMAC, and admin credentials are stored in Google Cloud Secret Manager. Report security issues privately per [SECURITY.md](SECURITY.md).

## 10. Contact

**Operator:** Manav Agarwal  
**Email:** agarwalmanav91@gmail.com  
**Support:** https://github.com/manavaga/ringscout/issues  
**Repository:** https://github.com/manavaga/ringscout
