# Salesforce Chatter for Hermes Agent

A community gateway plugin that turns Salesforce Chatter mentions into Hermes Agent conversations. It acknowledges an accepted request with a Like and replies in the same thread, mentioning the requester. This project is maintained by zenplace Inc. and is not affiliated with, endorsed by, or supported by Salesforce.

**Start with `dry_run: true`, an explicit user allowlist, and a restricted test group.** Live mode can create Likes, comments, Files, and scheduled feed posts as your integration user. Chatter content and attachments enter your configured Hermes model and tool environment.

## Requirements

- Hermes Agent **0.21.3 or later**, with a configured model provider and gateway.
- Python **3.11 or later**. Plugin dependencies are `httpx>=0.27,<1`, `PyJWT[crypto]>=2.8,<3`, and `pillow>=10,<13`.
- A Salesforce org with Chatter and API access, an External Client App configured for OAuth JWT bearer authentication, and a dedicated integration/API-only user whose license and permission sets permit the required Chatter operations.
- A signing private key on the gateway host and its public certificate registered with the External Client App.
- Docker and a compatible Chromium sandbox image **only if you opt in to HTML previews**. Docker is not required for normal Chatter messaging.

## Salesforce setup

1. Create a dedicated integration user. Use an API-only integration configuration where supported by your license. Grant API access and only the Chatter, group/record visibility, and Files permissions needed for your intended workload through permission sets. Confirm your org's integration license supports those operations; license availability differs by org.
2. Create an External Client App. Enable OAuth and JWT bearer authentication, register the signing certificate, and configure the `api`, `chatter_api`, and `refresh_token` scopes for the preauthorized JWT flow. Set the policy to administrator-approved/preauthorized users and grant access through a permission set assigned to the bot only. Follow your org's Salesforce policy if its app setup differs.
3. Add the bot to a private test group. Record its User ID and the group's ID, not their display names. Group IDs begin with `0F9`; User IDs begin with `005`.
4. Store the private key outside the repository. Restrict it to the service account, for example with mode `0600`, and protect the containing directory. Never commit the key, certificate management secrets, or your `.env` file.
5. Keep the app's consumer key, bot username, bot User ID, and login URL available for configuration below. Production commonly uses `https://login.salesforce.com`; sandboxes commonly use `https://test.salesforce.com`.

The fast group scanner queries `CollaborationGroupFeed`. Do not assume an integration user can query `FeedItem` or `FeedComment` directly. Test actual permissions in a non-production org before enabling writes.

## Install

Install a reviewed revision from Git, replacing `<40-hex sha>` with the full commit SHA you intend to trust:

```bash
hermes plugins install 'zenplace-system/hermes-plugin-salesforce-chatter#salesforce_chatter' --ref '<40-hex sha>' --enable
```

Review the dependency and security prompts. `--enable` adds the plugin to `plugins.enabled`; installing code alone does not configure Salesforce or enable the platform. The plugin directory is `salesforce_chatter`, the plugin name is `salesforce-chatter`, and the platform key is `salesforce_chatter`.

**Once this plugin is listed in the Hermes catalog**, the equivalent catalog command will be:

```bash
hermes plugins install salesforce-chatter --enable
```

This README does not claim catalog admission. Catalog entries require human review and a full SHA pin; updates are reviewed SHA changes, not plugin self-updates. See the [Hermes catalog submission policy](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission).

## Configure Hermes

Place these values in the environment used by your Hermes gateway, or its protected `.env` file. Replace every placeholder with your own value:

```dotenv
SF_CHATTER_LOGIN_URL=https://login.salesforce.com
SF_CHATTER_CLIENT_ID=<external-client-app-consumer-key>
SF_CHATTER_USERNAME=<integration-user-username>
SF_CHATTER_PRIVATE_KEY_PATH=<absolute-path-to-private-key>
SF_CHATTER_BOT_USER_ID=<bot-user-id>
SF_CHATTER_ALLOWED_USERS=<allowed-user-id>,<another-allowed-user-id>
SF_CHATTER_ALLOWED_PARENT_IDS=<test-group-id>
```

Merge this into your Hermes `config.yaml` without removing other enabled plugins or platforms:

```yaml
plugins:
  enabled: [salesforce-chatter]
platforms:
  salesforce_chatter:
    enabled: true
    extra:
      dry_run: true
      poll_interval_seconds: 60
      probe_interval_seconds: 5
      max_replies_per_hour: 20
      unauthorized_reply: false
      html_preview: false
platform_toolsets:
  salesforce_chatter: []
```

`platform_toolsets` controls the tools available to conversations on this platform. Start with no tools, then explicitly add only the Hermes toolsets your users should be able to invoke. Enabling terminal, file, web, or code execution gives model-driven actions a broader scope than this plugin's Salesforce client. Configure a Docker terminal backend separately if those tools require isolation; the plugin's optional preview container does not sandbox the whole agent.

### Recommended quiet Chatter delivery

Use explicit per-platform display settings on both Hermes 0.21.3 and current main. Global display settings can override platform defaults, and older cores do not apply plugin display tiers. Merge these keys into the blocks above:

```yaml
display:
  platforms:
    salesforce_chatter:
      tool_progress: "off"
      interim_assistant_messages: false
      long_running_notifications: false
      busy_ack_detail: false
      busy_steer_ack_enabled: false
      streaming: false
      suppress_warning_notifications: true
platforms:
  salesforce_chatter:
    gateway_restart_notification: false
    home_channel: {chat_id: "0F9xxxxxxxxxxxx"}
```

Replace the example home channel with a permitted group ID, or set `SF_CHATTER_HOME_CHANNEL`. This avoids repeated no-home-channel notices; `/sethome` is intentionally not allowed from Chatter. `suppress_warning_notifications` is optional and hides core warning diagnostics. To silence busy acknowledgments entirely, set the global environment variable `HERMES_GATEWAY_BUSY_ACK_ENABLED=false`; the display detail settings alone do not disable every busy acknowledgment.


### Environment variables

Nonempty environment values take precedence over the corresponding `extra` fields. List values accept comma-separated IDs; the `extra` equivalents also accept YAML lists. Keep credentials in the environment rather than plaintext config where possible.

| Variable | Default | Purpose / `extra` fallback |
|---|---|---|
| `SF_CHATTER_LOGIN_URL` | Required; none | OAuth login URL; `login_url` |
| `SF_CHATTER_CLIENT_ID` | Required; none | External Client App consumer key; `client_id` |
| `SF_CHATTER_USERNAME` | Required; none | Integration user username; `username` |
| `SF_CHATTER_PRIVATE_KEY_PATH` | Required; none | Local RS256 signing key path; `private_key_path` (`~` is expanded) |
| `SF_CHATTER_BOT_USER_ID` | Required; none | Bot User ID used to identify mentions; `bot_user_id` |
| `SF_CHATTER_ALLOWED_USERS` | Empty: **deny all requests** | Allowed requester User IDs; `allowed_user_ids` |
| `SF_CHATTER_ALLOWED_PARENT_IDS` | Empty: all visible parents eligible | Restrict group/record parents; `allowed_parent_ids` |
| `SF_CHATTER_ALLOW_ALL_USERS` | Unset / false | Hermes core authorization flag. It does **not** bypass this adapter's user allowlist; leave unset for restricted operation. No `extra` equivalent. |
| `SF_CHATTER_HOME_CHANNEL` | Unset; no default destination | Optional group/record ID for Hermes scheduled delivery as a new feed post. No `extra` equivalent. |

The adapter rejects requests with an empty user allowlist. An empty **parent** allowlist is different: it imposes no parent restriction on mentions the bot can see. Configure both lists for a restricted deployment. Use `SF_CHATTER_ALLOWED_USERS` rather than only the `extra` fallback so the adapter and Hermes gateway authorization see the same user list.

### `platforms.salesforce_chatter.extra`

The five required connection fields and two allowlist fields are documented in the environment table above. All remaining plugin-specific `extra` keys follow; numeric intervals are seconds unless stated otherwise.

| Key | Default | Meaning |
|---|---|---|
| `api_version` | `v66.0` | Salesforce REST API version |
| `feed` | `to_me` | Feed to scan: `to_me` or `news` |
| `poll_interval_seconds` | `60` | Feed polling interval; clamped to at least 60 |
| `probe_interval_seconds` | `5` | SOQL change detection for allowlisted `0F9` groups; clamped to at least 3 |
| `dry_run` | `true` | Suppress plugin Salesforce writes; polling, model execution, and local bookkeeping continue |
| `max_replies_per_hour` | `20` | Admission cap based on completed replies in the inbox's trailing hour; not a Salesforce API quota limiter |
| `max_catchup_hours` | `24` | Maximum scanner catch-up window after downtime |
| `max_attachment_mb` | `25` | Per-file download/upload cap in MiB (`1024 × 1024` bytes); not an aggregate session budget |
| `unauthorized_reply` | `true` | Post a rejection notice for an unauthorized request when writes are enabled; set false for silent rejection |
| `follow_up_without_mention` | `false` | Accept allowlisted users' comments after the bot has already commented in that thread, unless the comment mentions another user; unauthorized unmentioned follow-ups are always ignored silently |
| `html_preview` | `false` | Opt in to a Docker-rendered PNG alongside an outgoing HTML file |
| `preview_image` | `nousresearch/hermes-sandbox:desktop@sha256:669abbd2…` (digest-pinned) | Docker image for previews; pre-pull a trusted compatible image and preferably pin its digest |
| `failure_text` | `Failed to generate a reply. Please wait and mention me again.` | Failure notice |
| `approval_hint` | `Reply with a comment that mentions me: "@{bot_name} approve" or "@{bot_name} deny".` | Appended to execution-approval prompts; `{bot_name}` uses the bot's display name fetched once at connection, or `{bot}` if unavailable; empty string disables the hint |
| `unauthorized_text` | `Only authorized users can use this assistant.` | Unauthorized-request notice |
| `commands_text` | `Available Chatter commands: /new /reset /stop /approve /deny. Write questions in plain text.` | Rejected slash-command notice |
| `empty_post_text` | `(No message text)` | Input fallback for an empty post |
| `empty_reply_text` | `(Empty reply)` | Output fallback for an empty reply |
| `attachment_too_large_note` | `[Attachment '{title}' was not loaded because it is too large]` | Model context note for an oversized attachment |
| `attachment_failed_note` | `[Attachment '{title}' could not be loaded]` | Model context note for a failed attachment download |
| `attachment_unreadable_note` | `[Attachment '{title}' could not be loaded as an image]` | Model context note when media caching cannot read an attachment |
| `html_download_caption` | `{name} (download and open in a browser)` | HTML comment caption when a preview exists and the caller supplied no caption |
| `html_preview_caption` | `{name} preview (image)` | Caption for the rendered PNG comment |
| `html_preview_max_height` | `8000` | The preview is one full-page image (1280 px wide, any height up to this); open or download it to read at full size |
| `html_preview_truncated_note` | `The preview stops here; download {name} for the rest.` | Added to the last image when the page is longer than the rendered height |

Customize the text fields to localize notices. Attachment notes substitute `{title}` with the attachment title; HTML captions substitute `{name}` with the filename.

Language and persona belong in your Hermes instructions (for example, `SOUL.md`) or Hermes's native top-level platform hint configuration, not an `extra.platform_hint` key:

```yaml
platform_hints:
  salesforce_chatter:
    append: "Reply in the user's language and keep replies concise."
```

`append` preserves the plugin's generic Chatter formatting and thread guidance. Use `replace` only if you intend to replace that entire hint.

## Start and verify

```bash
hermes plugins list
hermes gateway start
```

Use the same Hermes home/profile for installation, configuration, and gateway operations. Do not start a second gateway against an already-running deployment.

1. With `dry_run: true`, mention the bot from an allowlisted user in the test group. Check the gateway log for detection and `[dry-run]` messages. Expect **no Salesforce Like or reply**.
2. Check model routing, allowed tools, and permissions. Dry run still invokes the model and may incur provider charges or tool side effects.
3. To test live delivery, set `dry_run: false`, restart with `hermes gateway restart`, and send a **new** mention. Verify a Like, a reply in the same thread, and the requester mention.
4. Test an unauthorized user and an oversized attachment. Decide whether rejection notices should be visible before expanding access.

Requests processed during dry run are recorded and are not automatically replayed when live mode is enabled.

## Behavior and risk disclosure

### Network and API usage

The plugin authenticates at the **configured Salesforce login host**, then sends API requests to the **instance host returned by Salesforce OAuth**. Attachment downloads follow HTTP redirects, which can contact Salesforce file/content hosts or other redirect destinations; this is **not a strict two-host egress allowlist**. `httpx` removes Authorization on cross-origin redirects except direct same-host HTTP-to-HTTPS upgrades. Validate the configured endpoint and use network policy if strict outbound controls are required.

Image URLs returned by Hermes tools trigger an additional outbound HTTPS fetch to the supplied URL and up to five HTTPS redirects, using a separate client with **no Salesforce Authorization header**. Every hop must pass Hermes's SSRF check (`tools.url_safety.is_safe_url`) before it is requested, and the client re-checks the resolved address when it connects, so private, loopback, link-local and cloud-metadata addresses are refused; a refused or failed fetch is posted as a plain link instead. The response must have an `image/*` content type and remain under `max_attachment_mb`; downloaded images are uploaded as Chatter Files. If fetching or uploading fails, the adapter posts the original URL as a link instead. URL credentials and HTTP/downgrade redirects are rejected. These image hosts are not restricted to Salesforce; apply network egress controls for your deployment. Dry run does not fetch outbound images.

The background gateway polls the To Me feed every **60 seconds** by default. It probes allowlisted groups with SOQL every **5 seconds** and fetches changed threads through Connect REST. SOQL still consumes the org's Salesforce API allowance even though it is separate from Chatter's hourly Connect REST limit. More groups, attachments, and active threads increase calls. Monitor Salesforce quotas; `max_replies_per_hour` does not bound API calls.

The plugin has **no telemetry or usage-reporting service** and no self-updater. Hermes model providers, tools, dependency installation, and optional Docker image pulls have their own network behavior and costs; they are not restricted to Salesforce by this plugin.

### Local reads and stored state

- Reads the configured private-key file to sign short-lived JWT assertions. Access tokens are kept in process memory; the plugin does not rotate another client's credentials or read a vendor CLI/browser credential store.
- Stores scan cursors, request IDs, requester IDs, timestamps, and processing status in `$HERMES_HOME/plugin-data/salesforce-chatter/inbox.sqlite`. Preserve this state to avoid losing duplicate-suppression history. Protect it as operational data.
- Downloads attachments into Hermes's media cache and reads local outgoing files supplied through Hermes media delivery. Thread text and recent comments become model context; session retention, logging, and provider data handling are governed by Hermes and your provider configuration.
- Logs operational IDs, statuses, filenames, and errors. The client avoids logging tokens and Salesforce response bodies; do not assume the broader Hermes deployment contains no sensitive conversation data.

### Salesforce writes and authorization

With `dry_run: false`, the bot can Like accepted requests, post reply/error/rejection comments, upload files to **the bot's Salesforce Files**, and attach those files to comments. Optional home-channel delivery creates new group/record feed posts. File uploads and comment creation are separate requests, so a failed comment can leave an uploaded file behind. There is no automatic deletion or rollback of these writes.

Authorization is checked before command dispatch. Only `/new`, `/reset`, `/stop`, `/approve`, and `/deny` pass the plugin's slash-command filter. `/approve` and `/deny` remain subject to Hermes's approval handling; this plugin does not auto-approve tool execution. Restrict the allowlist to people you trust with the configured model/tools. The filter is not a sandbox against malicious prompts or attachments.

Unauthorized mentions can receive one rejection notice per claimed source item by default, even though no model reply is generated. Set `unauthorized_reply: false` to avoid that write. The default dry-run setting suppresses these notices too.

### Replies, approvals, and follow-ups

Final replies mention the requester once, on the first comment chunk, rather than on interim or approval messages. Legacy sends without notification markers retain first-send behavior. Replies are split at paragraph or line boundaries so each comment stays within 9,000 characters as Salesforce stores it (rich-text comments are stored as HTML, and the 10,000-character limit counts tags, escaped characters and links), with balanced Chatter markup and space reserved for unresolved mention display names. Trailing blank paragraphs are removed. The adapter declares message editing unsupported so Hermes does not stream editable partial comments. If delivery stops after a chunk succeeds, that partial reply is not automatically replayed in full.

Execution approvals still require a Chatter comment mentioning the bot: `@Bot /approve`, `@Bot /approve session`, `@Bot /approve always`, or `@Bot /deny`. While an approval is pending, `@Bot yes` and `@Bot approve` also work. The actual bot name is shown in the prompt hint. Inline command and clarification responses are recorded as handled, not left pending. `liked` and `handled` inbox rows do not count toward the reply admission cap.

Unmentioned follow-ups are off by default. With `follow_up_without_mention: true`, only comments from allowlisted users after an earlier bot comment qualify; comments directed at another user do not. Both scanning paths apply the same rule, existing catch-up limits, and source-ID duplicate suppression. The scanner can only recognize earlier bot comments included in the retrieved thread; it does not search the full historical conversation.

Turn failures produce the configured `failure_text` once, without an additional core-generated exception-detail comment. Permanent Salesforce 400/401/403/404 delivery refusals do not trigger a plain-text resend; `REQUEST_LIMIT_EXCEEDED` and server failures remain retryable.

### Optional HTML previews

HTML previews are **off by default**. With `html_preview: true`, the HTML file is posted first, then the whole page as one image (not split; open or download it to read at full size). Outgoing HTML (including model-generated HTML) is rendered by Chromium in a Docker container, never opened in the host browser. The renderer uses no container network, a read-only root filesystem, dropped Linux capabilities, `no-new-privileges`, a 1 GiB memory limit, one CPU, and a 256-process limit. A dedicated temporary directory containing the input and output is mounted read-write; `/tmp` is a 256 MiB temporary filesystem for Chromium's profile. The host waits up to 60 seconds for rendering and kills the Docker CLI on timeout; administrators should check for a surviving container after a timeout.

Treat Docker and the configured image as trusted execution dependencies. Pre-pull and inspect the image before enabling previews; an image pull can contact a registry even though the renderer's container network is disabled. If preview rendering fails, the HTML file is still delivered without the PNG. Do not open untrusted HTML locally merely because a preview was generated.

## Emergency stop

Choose the appropriate containment level; use the correct Hermes home/profile for each command.

| Action | Effect | Recovery / caveat |
|---|---|---|
| Set `platforms.salesforce_chatter.extra.dry_run: true`, then `hermes gateway restart` | Stops plugin Likes, comments, uploads, and feed posts | Polling, model charges, and agent tool actions may continue. Set false and restart to resume; dry-run requests are not replayed. |
| `hermes gateway stop` | Stops that gateway's processing, including its other platforms | `hermes gateway start` resumes; eligible missed requests may be caught up within the configured window. |
| Freeze the integration user **and revoke its active sessions/tokens** in Salesforce | Blocks new authentication and invalidates issued access where revoked | Do both: freezing alone should not be assumed to revoke every existing session. Review the incident before unfreezing. |
| Disable/block the External Client App and revoke its access in Salesforce | Stops the app's authentication/access across deployments | Re-enable only after reviewing the app and credentials. |

Have a Salesforce administrator review and remove unwanted bot comments, posts, or Files. Stopping the plugin does not remove existing content. To retire the integration, stop the gateway, disable the plugin/platform, revoke the app and bot access, and handle retained local data under your retention policy.

## Limitations

- The first run does not answer historical mentions from before initialization. After a restart, catch-up is limited by `max_catchup_hours` and Salesforce feed visibility/pagination.
- Fast polling applies only to allowlisted groups whose IDs start with `0F9`. Other eligible mentions arrive through the selected feed and may take at least a polling interval; delivery is not real-time.
- Adding a mention by editing an old comment is not guaranteed to trigger a new scan; post a new comment instead.
- The inbox favors duplicate suppression. A crash after claiming a request can leave it without a reply; Hermes recovery is not an exactly-once delivery guarantee.
- One parent post/thread maps to one Hermes session. Recent thread context is limited (up to 10 preceding comments); a large or busy thread is not imported in full.
- Chatter has no native Markdown headings or tables. The adapter converts supported formatting and flattens unsupported structures. Like is an acknowledgement, not proof that a reply completed.
- Attachment handling depends on Salesforce visibility, file size, Hermes media support, and model capabilities. Files that cannot be loaded are reported as context notes, not silently promised as model-readable.
- The reply cap is an admission check against completed replies, not a reservation of concurrent in-flight work. It is not a hard billing or write-volume ceiling.

## Development

```text
salesforce_chatter/       Installable plugin directory
  plugin.yaml            Hermes manifest v2
  pyproject.toml         Python dependency and package metadata
  adapter.py             Hermes platform registration and delivery
  sfchatter/             Salesforce client, scanning, settings, state, rendering
tests/                   Contract and behavior tests
```

Use a development environment containing Hermes and the plugin dependencies. From the repository root:

```bash
python -m pytest -q tests
hermes plugins doctor salesforce_chatter --ci
hermes plugins validate salesforce_chatter
```

Run validation in a disposable Hermes home rather than against production credentials. For local installation, use `hermes plugins install 'file:///absolute/path/to/checkout#salesforce_chatter' --enable`. Changes to this repository do not automatically update an installed copy.

The adapter uses Hermes's platform registration and gateway interfaces rather than patching core. Recheck contract tests and plugin validation when upgrading Hermes. For catalog submission, follow the upstream admission policy: review dependencies, disclose network/file/process behavior, and pin an immutable full commit SHA. No catalog or GitHub publication is performed by the plugin.

## License

[MIT](https://github.com/zenplace-system/hermes-plugin-salesforce-chatter/blob/main/LICENSE). Copyright (c) 2026 zenplace Inc.
