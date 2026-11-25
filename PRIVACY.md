# RingScout Privacy Notice

**Operator:** Manav Agarwal  
**Contact:** agarwalmanav91@gmail.com  
**Last updated:** 25 November 2026  
**Status:** Draft — production security and call-disclosure changes required before public launch

This notice describes RingScout's current design as inspected in the public source repository. It is not a claim that every production setting has been independently verified. The operator must keep this notice aligned with the deployed service and applicable Indian law.

## 1. Who operates RingScout

RingScout is operated by **Manav Agarwal**, as an individual developer.

- **Privacy and data requests:** agarwalmanav91@gmail.com
- **Support:** https://github.com/manavaga/ringscout/issues
- **Project:** https://github.com/manavaga/ringscout
- **Service:** https://ringscout-hom7ntreuq-uc.a.run.app/

Do not send passwords, API keys, payment card details or unrelated sensitive information through public GitHub issues.

## 2. What RingScout does

RingScout searches for public business listings in India and can place outbound calls to listed business telephone numbers. It asks about current availability, waiting times, appointment slots, stock, services and quoted prices. It may return call status, transcripts, structured answers and audio proof.

RingScout provides information communicated during a call. It does not itself guarantee availability or complete a reservation, appointment, purchase or payment.

## 3. Information processed

Depending on the workflow, RingScout may process:

- **Request information:** search terms, city, locality, category, user question, caller name and requested service.
- **Business information:** business name, address, public listing information and listed business phone number.
- **Call information:** destination number, carrier call identifier, call status, timestamps, duration, keypad/IVR actions, transcript turns, extracted availability and price information.
- **Call audio:** live PSTN audio from the business side and the AI voice side is buffered and assembled into a WAV file for audio proof.
- **Technical information:** IP-derived rate-limit fingerprint, request path, HTTP method, status, latency, user-agent and diagnostic/error information.
- **Payment and quota information:** usage counters and payment authorization/receipt metadata if a paid payment method is enabled.

## 4. Where information is processed and stored

### Application memory

The source currently keeps active and completed quest objects in an in-memory QuestManager dictionary. These objects can include the request, caller name, business telephone numbers, call transcripts, extracted answers and call metadata.

The source does not show a database or durable object-storage write for these quest records. There is also no explicit time-to-live or automatic deletion routine for quest objects. They remain in the running process's memory until the process is restarted or the instance is otherwise replaced. This is not a guaranteed fixed retention period.

### Audio proof

For live PSTN calls, the application records inbound and outbound audio frames during the call, assembles them into a WAV file and keeps the resulting clip in a bounded in-memory store. The store holds up to 64 clips and evicts older clips first when it reaches capacity. It has no configured time-based expiry in the inspected source. Clips are also lost when the process restarts.

The audio endpoint returns a WAV file with Cache-Control: no-store. A signed audio URL can be used to retrieve a clip while it remains in memory.

### Operational and request logs

The application keeps a bounded in-memory request history (up to 4,000 HTTP records) and a bounded in-memory list of recent MCP tool-call summaries (up to 100). These records can include timestamps, request paths, HTTP methods, response status, latency, user-agent classification and a short summary of the requested task.

The service also reads Cloud Run request logs from Google Cloud Logging for its operator analytics. The source queries a rolling window of up to 720 hours; this is an analytics query window, not a configured deletion schedule. Google Cloud's _Default log bucket retains logs for 30 days by default unless the project has configured a different retention period. The actual retention setting for RingScout's Google Cloud project has not yet been verified.

The application derives a truncated SHA-256 fingerprint from the request IP address for rate limiting. This is pseudonymous information, not anonymous data.

## 5. AI processing, telephony and service providers

The inspected production configuration and repository identify these service providers:

- **Google Cloud Run:** application hosting.
- **Google Vertex AI / Gemini:** live voice interaction, speech transcription and extraction of call answers.
- **Vobiz:** outbound Indian telephone calls and bidirectional call-media transport.
- **Google Maps / Google business listing services:** discovery of businesses and their public listing details.

The source also contains adapters for Plivo and Twilio, but the operator has confirmed that Vobiz is the intended active telephony provider for the current deployment. The operator should update this notice if the active provider changes.

Call audio and conversation content are transmitted to Google Vertex AI/Gemini to provide the AI voice interaction and answer extraction. Vobiz processes the telephone call and associated call metadata/media. These providers process data under their own service terms and privacy documentation.

## 6. AI identity and recording disclosure — launch blocker

The source code was inspected on 25 November 2026. It currently instructs the voice agent to identify itself as the RingScout AI assistant only if the person answering explicitly asks who is calling. The initial greeting may instead use the caller name supplied in the request. The inspected prompt does not include a proactive recording disclosure.

This does not match a policy promise that every call proactively identifies itself as AI and announces recording. RingScout must not be publicly launched with a claim that those disclosures always occur until the call flow is updated and tested. The operator should ensure that the recipient receives clear notice of the AI nature and recording before recording begins, and that the call respects objections or requests to stop.

## 7. Public dashboard and audio-link access — launch blocker

The inspected source includes public dashboard/analytics responses that currently contain recent call details, including transcript content and signed audio links. Phone numbers are masked in some public views, but transcript and audio details may still be exposed.

The operator should restrict these responses to non-sensitive aggregate data, or require appropriate authorization before returning transcripts and audio. This exposure should be fixed and tested before the public privacy notice is treated as final.

## 8. Purposes of processing

Information is used to:
- discover relevant businesses and place the requested call;
- conduct AI-assisted voice conversations and produce transcripts and answers;
- return call status, reported availability, prices, summaries and audio proof;
- enforce quotas, rate limits and abuse-prevention controls;
- operate, secure, debug and improve the service;
- administer payment flows if enabled;
- respond to support requests and comply with legal obligations.

## 9. Sharing and disclosure

Information is shared with the service providers listed above to operate the requested features. It may also be disclosed where required by law, to protect the service or users, or in connection with a lawful business transfer.

RingScout does not sell personal data for advertising. The operator should not use call audio or personal data for unrelated marketing without an appropriate legal basis and any required consent.

## 10. Retention and deletion

The source currently implements process-memory retention, not a fixed retention schedule, for quest records and audio clips:

- Quest records and transcripts: held in the running process's memory; no explicit TTL or user-facing deletion function was found in the inspected source.
- WAV audio clips: in-memory, up to 64 clips, oldest-first eviction; no time-based TTL found.
- In-memory HTTP request history: up to 4,000 records per process.
- In-memory MCP tool-call summaries: up to 100 records per process.
- Google Cloud request logs: stored under the project's Cloud Logging configuration. The default _Default bucket retention is 30 days, but the actual project setting and any custom log sinks have not been verified.

A process restart clears the application-memory stores, but it does not itself delete Cloud Logging entries or information held by third-party providers. The operator must verify Cloud Logging bucket settings and provider-side retention terms before making a fixed retention promise.

To request access, correction or deletion, email **agarwalmanav91@gmail.com** with the approximate request date/time, city and business name if known. Do not include passwords, payment credentials or unnecessary sensitive data. The operator will assess the request under applicable law and explain any technical or legal limitation. The current source does not implement automated self-service deletion.

## 11. Security

The repository describes using Google Cloud Secret Manager for telephony and administrative credentials. The service uses passcodes, signed tokens and phone-number masking in selected views. These controls do not eliminate all risk. The public dashboard/audio exposure described above must be fixed before public launch.

Do not post secrets, personal phone numbers or call recordings in public issue trackers. Report security concerns privately by emailing **agarwalmanav91@gmail.com** with the subject "RingScout Security".

## 12. International processing

The service is intended for Indian business calls. Google Cloud and other service providers may process data in infrastructure regions outside India. The operator must verify the actual processing regions and applicable safeguards with the providers.

## 13. Children

RingScout is not designed for children. Do not use the service on behalf of a child unless you are legally authorized to do so.

## 14. Your rights

Requests will be handled in accordance with applicable Indian law, including the Digital Personal Data Protection Act, 2023 and rules in force to the extent applicable. Depending on the law and circumstances, individuals may have rights to access information, request correction or erasure, raise grievances and nominate another person to exercise rights.

Contact **agarwalmanav91@gmail.com** for privacy requests or grievances. The operator may need to verify identity and may retain information where legally required.

## 15. Changes

This notice may be updated when RingScout's features, service providers, storage practices or legal requirements change. The latest version will be published at this URL with an updated date.

## 16. Contact

**Operator:** Manav Agarwal  
**Email:** agarwalmanav91@gmail.com  
**Support:** https://github.com/manavaga/ringscout/issues  
**Repository:** https://github.com/manavaga/ringscout
