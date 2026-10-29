# News Feeder

Use **Settings → Appearance** or the toolbar's reading appearance button to choose the system font, serif, or rounded text; adjust reading size from **14–26 pt**; and select **System**, **Light**, or **Dark** appearance. Font and theme changes apply immediately and persist on this Mac. The View menu also provides text-size shortcuts. The default is the macOS system font at 18 pt with roomier line spacing.

Personal relevance is assessed by Secondstack's `typesafe/jev-1.13` through `/gateway/api/v1/systemone`, using the same saved gateway key as summaries. Edit your interests and priorities in **Settings → Profile**. The **For you** feed orders stories by relevance; **Latest** preserves chronological browsing. Feed rows display **High**, **Medium**, or **Low** relevance tags; the article reader focuses on the story without a relevance section. Assessments are cached offline. Scores remain internal for ranking: High ≥75, Medium ≥40 and <75, Low <40. Saving a profile refreshes assessments automatically. Newly prepared digests and Telegram feeds use the current ranking. Use **Retry Missing Assessments** after a gateway failure.

A native Mac reader for AI industry updates, with an offline archive, configurable sources, LiteLLM summaries, daily digests, and optional Telegram delivery.

Read the agreed [design decisions](docs/design-decisions.md). This is a working v0, with the current limits listed below.

The Shiba Inu bringing a newspaper [logo](assets/branding/shiba-news-logo.png) is available as a transparent PNG and used as the app icon. Its [generation prompt](assets/branding/shiba-news-logo.prompt.txt) is saved alongside it. The original stick logo is retained as the edit reference.

## Install as a local macOS app

Run `python3 scripts/install-local-app.py` to build and install the current release in `/Applications/News Feeder.app` (or `~/Applications` if the system Applications folder is not writable). Quit the app before installing an update. Once installed, open **News Feeder** from Applications or Spotlight; no terminal, running development server, Homebrew Python, or mounted project folder is needed.

The app includes an ARM64 release build and its own frozen Python collection service. The service listens on `127.0.0.1:8000`, starts when the app connects locally, and stops when the app quits. A parent watcher also stops it if the native app crashes. Enabled daily delivery can start a temporary background service while the app is closed. Remote backend connections and the development launcher remain supported. The local app uses an ad-hoc signature for this Mac; App Store distribution/notarization is not part of this installation.

Data lives in `~/Library/Application Support/NewsFeeder/`: the backend archive and gateway configuration in `service/newsfeeder.db`, the offline reading archive in the root, and service logs in `logs/service.log`. On first installation the installer imports the existing development databases using SQLite backups (including WAL contents), retaining article IDs, source/category configuration, profile, summaries, gateway settings, and read/saved flags. Later installs preserve installed data. Credentials and user data are excluded from the application bundle. An older installed app is retained under the support folder's `backups/` directory when updating; the source checkout's databases are left untouched.

Build without installing using `bash scripts/build-local-app.sh`. Packaging dependencies are in the backend's `packaging` dependency group. The installer verifies the staged app signature before replacing an existing installation. Enabling verified daily Telegram delivery installs a user background job; disabling delivery removes it. Let any scheduled digest finish before installing an update.

## Development: live feed

Requires macOS 14+, a Swift 6 toolchain (Xcode or compatible Command Line Tools), Python 3.12+, and [uv](https://docs.astral.sh/uv/). A paid Apple Developer membership is not required for local use.

```sh
cd backend
uv sync
cp .env.example .env
cd ..
python3 scripts/run.py --detach
```

The launcher builds `dist/News Feeder.app`, starts the real collection service at `http://127.0.0.1:8000`, and opens the native app. `--detach` keeps the app running independently of the terminal or chat command session. Quit the app to stop the local service. For foreground development, omit `--detach` and use Ctrl-C to stop both. Startup, exit codes, and termination signals are recorded in `dist/launcher.log`; native output is in `dist/app.log` and collection-service output in `dist/server.log`. A launcher lock prevents duplicate local instances. The twelve default sources are enabled and grouped into seven categories. Enter a gateway key and select a model in Settings → Models. Its database and the local archive survive restarts. Unchanged sync snapshots do not rewrite the archive. Archive decoding, persistence, and feed indexing run in background tasks; switching categories filters preordered stories without database access or repeated sorting.

Each story has a TLDR in the list and reader. Generated TLDRs contain one or two sentences and retain the summary's source evidence. While generation is pending or fails, the preview is a clearly labeled source excerpt; when source text is unavailable, it shows the headline. Collection downloads four articles at a time and publishes them in groups; gateway summaries run in groups of three.

For optional development fixtures, `python3 scripts/demo.py` still starts an isolated fictional demo at port 8765 with external collection, summary calls, and Telegram delivery disabled. It is not the default feed.

## Run with real sources

```sh
cd backend
uv sync
cp .env.example .env
# Optionally set a gateway virtual key/model here; you can also use Mac Settings → Models.
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

From another terminal:

```sh
./scripts/build-macos.sh
open "dist/News Feeder.app"
```

In Mac app Settings → Connection, set the backend URL to `http://127.0.0.1:8000`. If `APP_API_KEY` is configured, enter it as the backend access key; the app stores it in Keychain. API docs are available at `http://127.0.0.1:8000/docs`.

All LLM traffic defaults to the [T1A LiteLLM gateway](https://secondstack.t1a.io/gateway/), with API base `https://secondstack.t1a.io/gateway/api/v1`. The backend reads `/models` and sends summaries to `/chat/completions` under that base.

In Mac Settings → Models, enter your gateway virtual key, click Save Key & Load Models, choose a model from the live catalog, and click Use Selected Model. The gateway requires a virtual key to list models. The key and model choice are stored on the collection service and survive restarts; the key is never returned to the client or included in archive sync. A saved key is scoped to its gateway URL. Environment variables `LITELLM_API_KEY` and `LITELLM_MODEL` remain supported as defaults; saved choices take precedence for that gateway. Changing `LITELLM_BASE_URL` starts with that gateway's environment defaults.

The selected model applies to the next collection cycle; existing summaries remain archived. The gateway/model must support strict JSON-schema structured outputs. A failure is visible alongside the readable article; the app does not substitute a fabricated analysis. No provider key is needed in the app. The fictional demo permits gateway configuration and catalog reads but does not make summary calls.

The scheduler checks for work every 30 seconds and respects each source's refresh interval. Collect forces a refresh; Command-R syncs existing backend content, and Command-Shift-R collects sources. Summarization processes at most 50 pending articles per worker cycle, with three concurrent calls. The model selects numbered source passages for evidence, and the backend copies their exact text into citations. Successful summaries are reused by content hash and model alias.

## Configure sources

The sidebar lists categories; select one to combine its stories, or expand it to browse an individual source. Settings → Sources supports editable defaults, adding sources, enabling/disabling, previewing results, editing topics and collection intervals, and removing a source while retaining articles. Use **Categories** to add, rename, or remove a category, and the source editor's **Category** picker to assign a source. Removing a category moves its sources to Uncategorized. Categories and source assignments work offline. Shared URLs appear in every matching source/category while retaining a single article and its read/saved state.

**Databricks** combines the vendor blog with four GitHub organization sources: **databricks-solutions**, **databricks-industry-solutions**, **databrickslabs**, and **databricks**. Organizations stay at that level in the sidebar and Settings; their repositories never become separate subscriptions. The October 14 upgrade replaces existing Databricks repository subscriptions with their parent organizations while preserving article IDs, reading flags, summaries, and category membership. **MLflow** remains separate, with stable GitHub releases and the official blog RSS. GitHub sources refresh every two hours; each interval is editable. Later restarts respect configuration changes and removals.

- RSS/Atom: a feed URL. Available excerpts remain readable if full-text extraction fails.
- Website: a listing URL plus a CSS link selector and an optional article path regex. It works with links present in the fetched HTML; it does not execute website JavaScript.
- GitHub organization: an organization URL such as `https://github.com/databrickslabs`. Follows published releases and newly created/public repositories. Release notes and repository READMEs supply the TLDR/summary pipeline. Sources remain organization-level; a paginated repository inventory discovers new projects without creating source entries. The public events API exposes up to 300 events from the past 30 days and can lag, so this is a recent news feed rather than an exhaustive activity log. Commits, branches, issues, comments, and stars are not news items.
- GitHub releases: a repository URL such as `https://github.com/langchain-ai/langgraph`. Published release notes are stored in the archive. An optional release-tag regex limits collection and source/category views; MLflow uses `^v?\d+\.\d+\.\d+$` to exclude catalog snapshots and prereleases. Older filtered entries remain in All stories and Saved. Rate-limited GitHub API requests fall back to the public releases Atom feed. These feeds track selected repositories' releases, not organization-wide commits or issues.
- GitHub Trending: `https://github.com/trending?since=daily`. Collects the first 10 repositories across all languages in GitHub's order. The source view shows only the latest successful top 10, with rank, language, daily star gains, total stars, and snapshot time; older repositories stay in All stories and Saved. README text is cached when a repository is first discovered, with the Trending description as an explicit fallback. TLDRs and Jev tags use the existing pipeline. Star-count updates do not regenerate summaries. The refresh interval is configurable; the enabled source uses hourly refreshes. A failed fetch preserves the previous snapshot and reports a source error. GitHub page markup changes can require a collector update.
- Hacker News: `https://hacker-news.firebaseio.com/v0`, with optional headline topic filters. The collector samples top and new stories and attaches discussion evidence by canonical article URL.

Public HTTP(S) source URLs are supported; local/private network addresses are rejected. A website parser or anti-bot block can fail independently, with the error shown in Sources. Custom source types requiring authentication or JavaScript need another connector.

## Telegram

1. Create a bot using [BotFather](https://t.me/BotFather). In **Settings → Digest & Telegram**, paste its token and select **Save Bot**. The service verifies and stores the token privately; it never returns it to the app or archive sync.
2. Create a Telegram channel and add the bot as an administrator with permission to post messages. Enter the channel's own `@name`, `https://t.me/name` link, or numeric ID, then select **Verify & Connect Channel**. The bot's profile is not a destination channel. For private channels, **Find Channels** can discover the ID from a recent administrator update.
3. Choose the hour, IANA timezone, and story count; enable **Publish a daily digest to the channel** and **Save Schedule**. **Publish Today's Digest Now** publishes the same daily edition immediately and does not repeat one already sent.
4. Use Send Feed in the toolbar, Send this story in the reader, or Send Digest on the digest screen for additional manual posts. Daily editions contain prioritized stories, TLDRs, and source links; excerpt/headline fallbacks are labeled.

The installed app enables `~/Library/LaunchAgents/app.newsfeeder.digest.plist` only after a channel is verified and daily delivery is enabled. It checks every 15 minutes and at login, shares the app's running service or starts a temporary one, refreshes sources, and publishes after the selected local hour. News Feeder can be closed, but this Mac must be awake and online. A missed check catches up after wake/login with the current day's edition; it does not flood the channel with missed days. Hosted deployments need their backend worker running. Credentials stay in the private service database, outside the launch agent and app bundle.

Daily membership and its discovery window are frozen once per local day. Manual digest preparation does not shorten the next daily window. Persisted destination/date delivery keys prevent repeat posts after restarts or schedule-hour changes. Delivery receipts track acknowledged message IDs and progress; definite failures retry with backoff, including Telegram rate limits. An ambiguous network timeout or crash during a send is not automatically retried. **Retry Missing Posts** requires checking the channel first and resumes only unacknowledged posts. Delivery results are also available at `/telegram/deliveries`.

Environment defaults `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `TELEGRAM_TARGET_TYPE` remain supported. Saved configuration takes precedence. Legacy private-chat setup supports `/feed`, `/digest`, and `/help` only from its configured numeric chat ID; channel delivery does not poll commands. For private delivery, start the bot from the intended Telegram account before configuring its chat ID through the API.

## Hosted backend

`compose.yaml` runs the service with PostgreSQL. Configure `POSTGRES_PASSWORD`, `APP_API_KEY`, and optional gateway/Telegram variables in a root `.env` used by Docker Compose; this is separate from `backend/.env`. Then:

```sh
docker compose up --build -d
```

The example binds the API to loopback. Expose it through an HTTPS reverse proxy for remote use and point the Mac app at that address. Production mode requires `APP_API_KEY`. Run exactly one application worker: the initial scheduler is embedded in the API process. The service creates its v0 schema on first startup; production schema migration tooling is a later milestone. PostgreSQL data is persisted in the named volume. Gateway and Telegram secrets remain server-side.

## Verification

```sh
cd backend
uv run pytest -q
uv run ruff check app tests
cd ../macos
swift run NewsFeederChecks
```

The backend tests cover source configuration, feed and website parsing, duplicate collection, source failures, citation validation, gateway routing, digest cutoffs including DST, authentication, and Telegram authorization/message splitting/idempotency/uncertain sends. External services are mocked; live verification requires your gateway and bot configuration. The native checks exercise persistent offline storage and preserve read/saved state during sync.

## Current implementation limits

- Feed items are articles; grouping related coverage into one development is pending.
- Search is local text filtering. Native sync currently downloads a full snapshot; pagination and incremental sync are pending.
- HN buzz uses visible points, comments, and observed growth. Its labels are simple evidence bands, not a calibrated industry hype score. Missing coverage is labeled insufficient evidence.
- Daily digest membership is saved, but article content is the latest archived version. Full source revision history is pending.
- Read/saved state belongs to the local Mac archive and is not synchronized to other Macs.
- Full-text extraction depends on accessible public content. Paywalls, dynamic sites, and anti-bot measures may leave excerpts or visible errors.
- App packaging uses local ad-hoc signing. Notarization, App Store distribution, auto-updates, and multiuser authentication are outside this milestone.
