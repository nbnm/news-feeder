# Telegram news formatting review — October 29, 2026

Manual review covered the formatter, delivery integration, API entry points, retry compatibility, tests, and documentation.

- Daily digests, feeds, and single-story sends use the same layout in private chats and channels: bold headings, source/priority metadata, TLDR and why-it-matters sections, restrained emojis, and clickable citations.
- Explicit Telegram entities keep source HTML/Markdown literal. UTF-16 offsets are rebased when messages are combined or split; short source-link labels stay together. Citation links accept HTTP(S) and reject embedded credentials.
- Normal stories stay whole where they fit. Oversized stories preserve their text and formatting across numbered continuation messages, within the conservative 3,800-unit limit.
- New delivery records persist text and entities together. Queued plain strings keep their original behavior. A restart or rate-limit retry preserves formatting and skips acknowledged parts without regenerating the delivery.
- Daily membership, destination/date keys, permissions, and scheduling behavior are unchanged.

Validation: 103 backend tests, Ruff, and whitespace checks passed. Tests cover markup-like source text, Unicode entity offsets, link protocols, whole-story packing, oversized continuations, plain-message upgrades, rich partial retries across sessions, and manual/API delivery entry points. The signed local release built successfully and v0.2.2 reopened with the existing archive, private destination, and background schedule.

Live verification sent one news preview to the configured private chat. Telegram confirmed one message with four bold ranges and a clickable source link. The daily delivery receipt remained unchanged. No news was sent to Slack or to another destination.
