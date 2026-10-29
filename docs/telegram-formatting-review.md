# Telegram news formatting review — October 29, 2026

Manual review covered the formatter, delivery integration, API entry points, retry compatibility, tests, and documentation.

- Daily digests, feeds, and single-story sends use the same layout in private chats and channels: bold headings, source/priority metadata, TLDR and why-it-matters sections, restrained emojis, and clickable citations.
- Explicit Telegram entities keep source HTML/Markdown literal. UTF-16 offsets are rebased when messages are combined or split; short source-link labels stay together. Citation links accept HTTP(S) and reject embedded credentials.
- Normal stories stay whole where they fit. Oversized stories preserve their text and formatting across numbered continuation messages, within the conservative 3,800-unit limit.
- New delivery records persist text and entities together. Queued plain strings keep their original behavior. A restart or rate-limit retry preserves formatting and skips acknowledged parts without regenerating the delivery.
- Daily membership, destination/date keys, permissions, and scheduling behavior are unchanged.

Validation: 103 backend tests, Ruff, and whitespace checks passed. Tests cover markup-like source text, Unicode entity offsets, link protocols, whole-story packing, oversized continuations, plain-message upgrades, rich partial retries across sessions, and manual/API delivery entry points. The signed local release built successfully and v0.2.2 reopened with the existing archive, private destination, and background schedule.

Initial live verification sent one news preview to the configured private chat. Telegram confirmed one message with four bold ranges and a clickable source link. That check left the daily delivery receipt unchanged and sent no news to another destination.

## Timestamps and channel discovery

Stories now include a Published date/time in the configured digest timezone, including a timezone abbreviation and numeric UTC offset. Missing publication times use First seen from the discovery timestamp. Tests verify daylight-saving changes, naive UTC archive dates, non-default timezones, and date rollover.

Private-chat polling now requests channel-post and bot-membership events and preserves only destination ID/title/type before acknowledging them. Discovery reuses this bounded, per-bot cache; leave/kick updates remove destinations. Tests cover discovery after an update was consumed, bot isolation, and exclusion of message content. Manual review confirmed that destination connection still verifies current posting rights before activation.

All 109 backend tests, Ruff, and whitespace checks passed. The signed v0.2.3 release built and reopened with its existing Telegram destination and daily schedule preserved.

Live channel validation then verified administrator permissions, applied the existing Shiba newspaper logo and a description, and switched the verified destination to the requested channel. Telegram acknowledged both parts of the first channel edition; all eight stories had Published or First seen timestamp entities. The background job remained loaded with the existing schedule. A repeated scheduled request was skipped with an unchanged receipt. Channel identifiers and the private invitation are excluded from source and review artifacts.
