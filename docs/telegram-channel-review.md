# Telegram channel integration review — October 29, 2026

Manual review covered the complete backend/native/packaging diff, including the new configuration and scheduled-runner modules. No unresolved correctness findings remain in the reviewed implementation. Channel activation remains dependent on a verified destination and posting permission.

Findings resolved during review:

- Destination-specific status and delivery keys prevent receipt mixing and repeated publication after restarts or schedule-hour changes.
- Atomic send claims prevent concurrent workers, including stale database sessions, from posting the same pending chunk.
- Definite connection failures and rate limits back off; uncertain sends require a channel check before manual retry. Acknowledged chunks remain skipped.
- Daily windows include late discoveries and do not inherit manual digest cutoffs. Generated TLDRs retain the correct label.
- Runner credentials are scoped to local scheduled publishing, headless cancellation releases its service, and installation refuses to replace a running scheduled helper.
- The native daily-publish timeout accommodates source preparation; credential validation responses do not echo tokens.

Verification: 88 backend tests passed; Ruff and whitespace checks passed; native archive/decoding checks passed; debug and release builds passed. The frozen service passed startup, version, source persistence, configuration redaction, and unconfigured-runner checks. The installed app reopened with its existing 4,675-story archive and displayed the saved bot and channel-permission error correctly.

Initial channel verification sent no live Telegram messages. External API behavior is mocked in automated tests; initial bot identity and destination-permission checks were read-only. The local background publisher requires an awake, online Mac. Git publication requires a configured remote.

## Private-chat follow-up

The owner selected private-chat delivery and pressed Start. Settings now supports both private chats and channels, with discovery and connection labels for each mode. Private connections require a user chat ID and reject the bot's own username and numeric ID. Manual review covered the follow-up diff, including preservation of the saved destination during view loading and clearing unsaved selection when switching delivery modes.

All 90 backend tests passed, followed by 14 focused Telegram tests after adding private-chat discovery coverage. Ruff, whitespace checks, native archive checks, and debug/release builds passed. The installed v0.2.1 app preserved the archive and displayed the connected private chat. Enabling the schedule loaded the credential-free background job, and Telegram acknowledged both messages of the first daily digest. A second scheduled request was skipped with the same receipt and no repeated send. The app displayed the enabled schedule and Sent 2 / 2.
