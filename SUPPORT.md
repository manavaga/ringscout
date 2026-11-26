# RingScout Support

Need help with RingScout? This page explains where to ask questions, report defects, or request privacy and opt-out support.

## Get help

- **Bug reports and feature requests:** https://github.com/manavaga/ringscout/issues
- **Project and setup guide:** https://github.com/manavaga/ringscout
- **Live service:** https://ringscout-hom7ntreuq-uc.a.run.app/

For a useful support request, include:
1. What you were trying to find out (for example, table availability, a clinic slot, or a direct tariff).
2. The city and locality used.
3. The approximate time and date of the request, including time zone (`IST` / `UTC`).
4. The category and, if relevant, the public business name.
5. Any error message or request identifier (`quest_id`).

Please redact personal phone numbers, access tokens, and payment details, and never post call audio or transcripts in public issues.

## Common issues

### A business did not answer
RingScout depends on the business answering its listed Google Maps telephone number. A busy line, unanswered call, complex IVR, or stale listing may prevent an answer. Try another nearby business or retry later.

### The price or availability changed
A call reports what business staff communicated at the time of the call. Availability, slots, and `₹` prices can change at any time — confirm directly with the business before travelling or paying.

### Call audio or transcript expired
Call transcripts and `24kHz` WAV audio clips are accessible only to the initiating caller/session (and service administrator) and automatically expire after **24 hours**.

## Privacy, business opt-out, and deletion requests

- **Self-service immediate deletion:** Callers with a `quest_token` can delete their quest, transcript, and WAV clip immediately via `DELETE /api/quests/{quest_id}`.
- **Email support & privacy requests:** Email **agarwalmanav91@gmail.com** for access, correction, business opt-out, or deletion requests.

## Security vulnerabilities

Do not disclose exploitable security vulnerabilities in public issues. Follow [SECURITY.md](SECURITY.md).
