# RingScout Privacy Notice

**Draft status:** Operator verification required before this notice is treated as final  
**Last updated:** 25 November 2026

> **Publication gate:** This notice describes categories of data that may be involved based on the public RingScout project description. It is not a verified inventory of production processing. Before publishing as the definitive privacy policy or submitting RingScout for directory review, the operator must replace the marked fields and confirm actual data flows, retention, processors and deletion procedures. This document is not legal advice.

## 1. Who operates RingScout

RingScout is presented at https://github.com/manavaga/ringscout and its live service is hosted at https://ringscout-hom7ntreuq-uc.a.run.app/.

**Operator / data controller:** [CONFIRM INDIVIDUAL LEGAL NAME OR REGISTERED ENTITY]  
**Privacy contact:** [ADD MONITORED PRIVACY EMAIL]  
**Support:** https://github.com/manavaga/ringscout/issues

Do not submit passwords, access tokens, payment credentials or sensitive personal information through public GitHub issues.

## 2. What RingScout does

RingScout can search for businesses listed on Google Maps and place outbound voice calls to business telephone numbers in India. The voice agent may ask about current availability, waiting time, appointment slots, services, stock and quoted prices. Depending on the workflow, RingScout may return call summaries and audio proof.

RingScout provides information gathered during a call. It does not itself guarantee a reservation, appointment, purchase, price or service.

## 3. Information that may be processed

Depending on how you access RingScout and which workflow you request, the service may process:

- **Request details:** search terms, category, city, locality, requested service, questions to ask and timing.
- **Business details:** business name, public listing information, publicly listed telephone number and information communicated by the business.
- **Call data:** call status, timestamps, duration, telephony metadata, IVR interactions, call audio, audio-derived text, summaries and extracted answers, where enabled.
- **Technical and diagnostic data:** service logs, error details, request identifiers, IP or client metadata and security events, to the extent collected by the deployed infrastructure.
- **Payment and entitlement data:** free-tier usage or payment-related references if a paid or metered workflow is enabled. RingScout should not store full card credentials; payment credentials are handled by the applicable payment provider where used.

**Operator verification required:** confirm exactly which of these fields are stored, for how long, and which are processed transiently only.

## 4. Voice calls, AI and recordings

RingScout uses AI-assisted voice technology and telephony services to place calls to business numbers. The public project documentation describes Indian English and Hindi/Hinglish voice interactions and the return of playable call audio.

The operator has stated that the voice agent identifies itself as an AI assistant calling on the user's behalf and discloses recording. The production call flow must be tested to ensure this disclosure is made consistently before recording begins. If a call recipient objects or declines, the call flow should respect that response and end or avoid recording as configured.

Call audio can contain the voice and statements of a business employee or other person answering the listed number. Users must not republish or use recordings beyond their lawful purpose. Audio returned to a user may be accessible to that user and may be processed by service providers to operate the feature.

**Operator verification required:** confirm the exact disclosure wording, whether recording is always enabled or conditional, whether transcripts are generated, where audio is stored, who can access it, retention period and deletion procedure.

## 5. How information is used

Information may be used to:

- find relevant businesses and initiate the requested calls;
- conduct voice conversations and extract answers;
- return call status, reported availability, rates, summaries and audio where available;
- operate, secure, debug and improve the service;
- enforce usage limits, prevent abuse and administer billing where enabled;
- respond to support requests and comply with applicable legal obligations.

RingScout should not use call audio or personal data for unrelated marketing without an appropriate legal basis and any required consent.

## 6. Service providers and disclosures

The public repository describes deployment on Google Cloud, AI processing using Gemini, and telephony integrations that may include Vobiz, Plivo or Twilio. Google Maps listing information is used to discover businesses.

Depending on the deployed configuration, relevant data may be processed by:
- cloud hosting and logging providers;
- AI model providers;
- telephony and voice infrastructure providers;
- mapping or business listing providers;
- payment providers, if paid workflows are enabled.

**Operator verification required:** publish the current production provider list, each provider's role, relevant data categories, processing locations and links to their privacy terms. Do not assume every integration named in the source repository is active in production.

Information may also be disclosed where required by law, to protect users or the service, or in connection with a legitimate business transfer, subject to applicable law.

## 7. Retention and deletion

**Retention periods are not stated here because they have not yet been verified.** Before publication, the operator must specify retention periods separately for:
- call recordings and generated audio files;
- transcripts and extracted call results;
- request and application logs;
- account, usage and entitlement records;
- support correspondence and security logs.

The operator must also confirm how a user or call recipient can request deletion, what information can be deleted, and what must be retained for security, billing or legal reasons.

Privacy requests: **[ADD MONITORED PRIVACY EMAIL]**. Include enough information to locate the relevant request, but do not send payment credentials or unrelated sensitive details.

## 8. Security

The project documentation describes the use of Google Cloud Secret Manager for telephony and administrative credentials. The operator applies reasonable technical and organizational measures appropriate to the service, but no internet-connected system can be guaranteed completely secure.

Do not send secrets or sensitive personal data through public issue trackers. Security vulnerability reports should follow [SECURITY.md](SECURITY.md).

## 9. International processing

Users may access RingScout from different countries. Infrastructure and service providers may process data in locations outside the user's country. **The operator must confirm the actual production regions and applicable transfer safeguards before publication.**

## 10. Children

RingScout is not designed for children and should not be used by anyone below the age required to enter a binding agreement in their jurisdiction without the involvement and authorization of a parent or legal guardian.

## 11. Your choices and rights

Depending on your location and applicable law, you may have rights to request access, correction, deletion, restriction or objection to certain processing. To make a request, contact **[ADD MONITORED PRIVACY EMAIL]**. The operator may need to verify the requester's identity and may be unable to fulfill requests that conflict with legal obligations or the rights of others.

## 12. Changes

This notice may be updated as RingScout's features, infrastructure or legal requirements change. The "Last updated" date will be changed when a revised notice is published.

## 13. Contact

**Operator:** [CONFIRM INDIVIDUAL LEGAL NAME OR REGISTERED ENTITY]  
**Privacy email:** [ADD MONITORED PRIVACY EMAIL]  
**Support:** https://github.com/manavaga/ringscout/issues
