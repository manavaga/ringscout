# RingScout FAQ

## What is RingScout?

RingScout finds local businesses on Google Maps in India and places short AI-powered voice calls to their listed public business phone numbers to verify real-time availability, wait times, appointment slots, services, and `₹` prices.

## Does the voice agent disclose that it is AI and that the call is recorded?

Yes. On every call, RingScout proactively identifies itself upfront on Turn 1 as the **RingScout AI assistant** calling on a **recorded call** (never impersonating a human person). Inbound recipient audio recording begins only after this disclosure starts, and if a recipient objects to recording or AI calls, RingScout ends the call immediately and discards any audio buffer.

## Who can access call transcripts and WAV audio?

Only the session/caller that initiated the call (via signed capability token) and the service administrator. Public dashboard (`/dashboard`) and analytics views redact all transcripts, caller names, and WAV audio links.

## How long are recordings, transcripts, and logs retained?

- **Call audio, transcripts, caller names, and quest records:** Held only in bounded application memory (`max 64` clips / `max 256` quests) and **automatically purged after 24 hours** (or earlier upon self-service `DELETE /api/quests/{quest_id}` or instance restart).
- **Operational logs:** In-memory request telemetry expires within **24 hours**; Google Cloud Run request logs in the verified `_Default` Cloud Logging bucket expire automatically after **30 days**.

## Does RingScout make bookings or purchases?

No. RingScout is an information-gathering service. Contact the business directly to confirm and complete any reservation, appointment, or purchase.

## Which languages and categories are supported?

RingScout speaks Indian English (`en-IN`), Hindi, and Hinglish (`hi-IN`) across restaurants, hotels, salons, spas, clinics, diagnostic centres, petrol pumps, pharmacies, auto shops, and custom local searches in India (up to 5 businesses in parallel per request).

## Where can I read the policies or get help?

- [Privacy Notice](PRIVACY.md) (`https://ringscout-hom7ntreuq-uc.a.run.app/privacy`)
- [Terms of Use](TERMS.md) (`https://ringscout-hom7ntreuq-uc.a.run.app/terms`)
- [Support](SUPPORT.md)
- [Security Policy](SECURITY.md)
