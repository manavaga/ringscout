# RingScout FAQ

## What is RingScout?

RingScout is an AI-powered voice agent that finds businesses listed on Google Maps and can call their public business telephone numbers to ask about current availability, waiting times, appointments, services and prices.

## Does it really call businesses?

Yes. RingScout is designed to place real outbound calls to business phone numbers in India. A successful result depends on the business being reachable and someone or an IVR answering.

## Which businesses can it contact?

The project supports workflows for restaurants, hotels, spas, salons, clinics, diagnostic centres, petrol stations, auto shops, pharmacies and custom searches, subject to availability and configuration.

## Which languages are supported?

The documented voice modes include Indian English and Hindi/Hinglish. Actual language behavior can vary with the call and recipient.

## Can it call multiple businesses at once?

The documented service supports parallel calls, with a maximum of five businesses per request in the current server configuration. Limits may change.

## Does it make bookings?

No. RingScout gathers information. Unless a future workflow explicitly says otherwise, you must contact the business directly to confirm and complete a reservation, appointment or purchase.

## Are prices and availability guaranteed?

No. Answers are reported as communicated during the call and may change. Confirm important details directly with the business before relying on them.

## Does RingScout provide call audio?

Yes. For live PSTN calls, the server records inbound and outbound audio into a WAV proof clip. The current source keeps up to 64 clips in process memory and evicts the oldest clips first; there is no time-based expiry configured in the inspected code. See the [Privacy Notice](PRIVACY.md) for details.

## Does the voice agent disclose that it is AI and that the call is recorded?

The current source does not proactively disclose either fact in its opening greeting. It only instructs the agent to identify itself as RingScout AI if the recipient asks who is calling. This must be corrected and tested before public launch.

## What if nobody answers?

An unanswered number, busy line, incorrect listing, IVR or network issue may prevent RingScout from collecting an answer. Try another business or retry later.

## Where can I get help?

Open an issue at https://github.com/manavaga/ringscout/issues. Do not post sensitive personal information, payment details or private audio publicly.

## Where can I read the policies?

- [Privacy Notice](PRIVACY.md)
- [Terms of Use](TERMS.md)
- [Support](SUPPORT.md)
- [Security](SECURITY.md)
