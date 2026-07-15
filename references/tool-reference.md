# Apple Mail Tool Reference

Detailed parameter documentation and return shapes for each CLI command.

All commands are invoked via `scripts/mail.sh <command> [args]`.
All output is JSON with the response contract: `{success, data, error, warnings, meta}`.

---

## server-info

No parameters. Returns skill version and metadata.

```json
{"name": "apple-mail-skill", "version": "1.2.0", "default_draft_backend": "auto", "default_draft_font": "provider-default", "exchange_adapter": {"protocol_version": 1, "configured": false}, "local_mail_mutation_safety_envelope": true}
```

## check-health

No parameters. Returns `{success: bool, message: str}`.

## local-mutation-preflight

No parameters. Non-destructive check for the local Mail mutation safety envelope. It verifies Mail health before and after a no-op action, captures/restores the foreground app, snapshots Mail crash reports, and returns `local_mail_safety` metadata.

This command does not authorize or perform a mailbox mutation. Run it before any live local mutation E2E test.

## generated local mutation E2E

Developer script, not a normal mail command:

```bash
APPLE_MAIL_ALLOW_LIVE_E2E=1 \
APPLE_MAIL_ALLOW_UI_MUTATION=1 \
APPLE_MAIL_ALLOW_LOCAL_MUTATION_E2E=1 \
scripts/dev/local_mutation_e2e.py full \
  --account EMAIL \
  --to TEST_RECIPIENT \
  --move-folder EXISTING_FOLDER
```

The script uses generated subject canaries only. It verifies draft create, draft amend, draft delete, send, move to an existing folder, delete generated message, focus restoration, pre/post Mail health, and Mail crash-report deltas. It writes a JSON log under `${TMPDIR:-/tmp}/apple-mail-local-mutation-e2e/`.

## list-accounts

No parameters. Returns list of account objects:

```json
[{"name": "Exchange", "user": "jdoe", "emails": "me@example.com,alias@example.com"}]
```

## list-folders

| Param | Required | Description |
|---|---|---|
| `--account EMAIL` | yes | Account email address |

Returns sorted list of folders:

```json
[{"folder_name": "Inbox", "email_count": 142, "folder_path": "Exchange/Inbox"}]
```

## list-recent

| Param | Required | Default | Description |
|---|---|---|---|
| `--limit N` | no | 128 | Max emails per inbox |
| `--include-content` | no | false | Add preview from search index |

Without `--include-content`: returns list of email dicts.
With `--include-content`: returns enrichment wrapper:

```json
{
  "emails": [
    {
      "id": "1234", "subject": "...", "sender": "...", "date_received": "...",
      "account_email": "...", "folder_name": "...",
      "preview": "first 5000 chars...",
      "preview_source": "indexed",
      "preview_truncated": false,
      "preview_available": true
    }
  ],
  "preview_coverage": {"covered": 18, "total": 20, "percentage": 90.0},
  "index_age": {"iso": "2026-02-23T10:30:00", "relative": "2 minutes ago"},
  "note": "Previews are first ~5000 chars only (not full content). Use read-email for cached/disk content, or build-index for deeper triage."
}
```

Per-message preview fields:
- `preview`: first 5000 chars of body, newlines replaced with spaces. Empty string if unavailable.
- `preview_source`: `"indexed"` | `"not_indexed"`
- `preview_truncated`: true if original content > 5000 chars
- `preview_available`: explicit boolean for agent branching

## list-emails

| Param | Required | Default | Description |
|---|---|---|---|
| `--account EMAIL` | yes | — | Account email address |
| `--folder NAME` | yes | — | Folder name (case-insensitive) |
| `--limit N` | no | 128 | Max emails |
| `--include-content` | no | false | Add preview from search index |

Same return shape as `list-recent`.

## list-drafts

| Param | Required | Default | Description |
|---|---|---|---|
| `--limit N` | no | 128 | Max drafts |
| `--include-content` | no | false | Add preview from search index |

Same return shape as `list-recent`.

## read-email

| Param | Required | Description |
|---|---|---|
| `--message-id MID` | no | RFC 2822 message-id (preferred, stable across syncs) |
| `--id ID` | no | Email integer ID (fallback) |

Provide one of `--message-id` or `--id`.

Returns full email:

```json
{
  "id": "1234", "subject": "...", "content": "full body...",
  "content_source": "search_index",
  "sender": "...", "sender_name": "...",
  "date_received": "...", "date_sent": "...",
  "read_status": true, "flagged_status": false,
  "account_email": "...", "folder_name": "...",
  "to_recipients": ["a@b.com"], "cc_recipients": [], "bcc_recipients": [],
  "attachments": [{"name": "file.pdf", "size": "12345"}]
}
```

Content retrieval uses a two-phase approach for resilience:
- **Phase 1** (always fast): metadata, recipients, attachments via JXA (~0.5 s)
- **Phase 2** (safe content lookup): search index -> disk `.emlx` -> unavailable

`content_source` values: `"search_index"` | `"disk"` | `"unavailable"`.
When `"unavailable"`, `content_note` explains that full content was not available from the safe cache/disk paths. do not retry in a loop; run `build-index` before a deeper triage session or ask the user to inspect the email in Mail.app.

On stale ID: `error.code = "EMAIL_NOT_FOUND"` with `error.details.recovery` containing a relist command.

After successful fetch, content is automatically cached in the search index (cache-on-read).

## search

| Param | Required | Default | Description |
|---|---|---|---|
| `--query TEXT` | yes | — | Search query (FTS5 syntax for scope=all) |
| `--scope` | no | `all` | `all` (FTS5), `subject` (JXA), `sender` (JXA) |
| `--account EMAIL` | no | — | Limit to account (warning if used with scope=all) |
| `--limit N` | no | 20 | Max results |

Returns list of result dicts with `id`, `subject`, `sender`, `date_received`, `snippet`, `score`.

## exchange-auth-status

| Param | Required | Description |
|---|---|---|
| `--account EMAIL` | yes | Exchange account email |

Non-mutating readiness probe for an explicitly configured Exchange adapter account. The public skill ships no adapter or authentication implementation. The adapter receives a versioned JSON request over stdin and must return the authenticated `account_email` in its JSON response.

## exchange-auth-login

| Param | Required | Description |
|---|---|---|
| `--account EMAIL` | yes | Exchange account email |

Requests interactive authentication from the configured external adapter. Whether this opens a browser or uses another mechanism is adapter-defined. Use it only after `exchange-auth-status` or `compose-draft` returns `EXCHANGE_AUTH_REQUIRED`.

### Exchange adapter protocol v1

Apple Mail launches the configured executable with no draft data in command-line arguments and writes one JSON request to stdin:

```json
{
  "protocol_version": 1,
  "operation": "compose-draft",
  "account": "exchange-user@example.com",
  "auth_mode": "background",
  "payload": {
    "subject": "Subject",
    "body": "Body or verified HTML",
    "to": ["recipient@example.com"],
    "cc": [],
    "bcc": [],
    "attachments": []
  }
}
```

Supported operations are `status`, `login`, `compose-draft`, and `read-email`. The adapter returns exactly one JSON object on stdout:

```json
{
  "protocol_version": 1,
  "success": true,
  "data": {
    "account_email": "exchange-user@example.com",
    "id": "SERVER_DRAFT_ID"
  }
}
```

Every successful response must contain the authenticated `account_email`. Apple Mail rejects protocol or account mismatches. `compose-draft` is followed by `read-email`; the adapter must return draft state, subject, content, recipients, message ID, and account for verification. Apple Mail ships no adapter and does not endorse a particular browser, token, Graph, EWS, or enterprise authentication implementation.

## compose-draft

| Param | Required | Description |
|---|---|---|
| `--account EMAIL` | yes | Sending account |
| `--subject TEXT` | yes | Subject line |
| `--body TEXT` | yes | Email body |
| `--to ADDR...` | yes | Recipient(s) |
| `--cc ADDR...` | no | CC recipient(s) |
| `--bcc ADDR...` | no | BCC recipient(s) |
| `--attachments PATH...` | no | File paths to attach |
| `--backend auto\|artifact\|exchange-rest\|mailapp` | no | `auto` uses `exchange-rest` only when both an adapter executable and the account allowlist are configured; otherwise it uses `mailapp` |
| `--font arial\|provider-default` | no | Exchange adapter font request; defaults to `APPLE_MAIL_DRAFT_FONT` or `provider-default` |
| `--output-dir DIR` | no | Artifact backend output directory; default `~/Documents/apple-mail-draft-artifacts` |
| `--allow-live-mail-mutation` | no | Ignored for default compose; reserved for development-only mutation commands |

Exchange is disabled by default. Set `APPLE_MAIL_WEB_EXCHANGE_CLI` to an executable protocol-v1 adapter and list routed accounts in `APPLE_MAIL_EXCHANGE_REST_ACCOUNTS`. Requests are one JSON object over stdin; private draft content is never placed in process arguments. Successful adapter responses must identify the authenticated account, and Apple Mail reads the new draft back before reporting success.

For local preferences without modifying the public repository, create `~/.config/apple-mail/env` with shell assignments such as `APPLE_MAIL_WEB_EXCHANGE_CLI=...`, `APPLE_MAIL_EXCHANGE_REST_ACCOUNTS=exchange-user@example.com`, and optionally `APPLE_MAIL_DRAFT_FONT=arial`. `scripts/mail.sh` and `scripts/check-setup.sh` load this user-owned file automatically. Keep it mode `0600` when it contains account-specific paths or settings.

For unconfigured accounts, `mailapp` creates a hidden Apple Mail outgoing message with `visible:false`, saves it to drafts, restores the previous foreground app, and verifies a durable Drafts message. If no durable message appears, it returns `DRAFT_NOT_DURABLE`.

`--backend artifact` writes an RFC 5322 `.eml` plus a JSON manifest and does not open or modify Mail.app. Returns fields including `backend`, `eml_path`, `manifest_path`, `message_id`, and `mail_app_written: false`. Verify with filesystem checks, not `list-drafts`.

## amend-draft

| Param | Required | Description |
|---|---|---|
| `--id ID` | yes | Draft ID |
| `--subject TEXT` | no | New subject |
| `--body TEXT` | no | New body |
| `--cc ADDR...` | no | Replace CC list |
| `--bcc ADDR...` | no | Replace BCC list |
| `--attachments PATH...` | no | Additional attachments |
| `--allow-live-mail-mutation` | no | Development-only override |

Live Mail.app mutation. Disabled by default with `MAIL_UI_MUTATION_DISABLED`. Do not use in production; create a new hidden synced draft with `compose-draft` instead.

## send-draft

| Param | Required | Description |
|---|---|---|
| `--id ID` | yes | Draft ID |
| `--allow-live-mail-mutation` | no | Development-only override |

Live Mail.app send. Disabled by default with `MAIL_UI_MUTATION_DISABLED`. Do not use in production.

## reply-draft

| Param | Required | Description |
|---|---|---|
| `--message-id MID` | no | Original email RFC 2822 message-id (preferred) |
| `--id ID` | no | Original email integer ID (fallback) |
| `--body TEXT` | yes | Reply body |
| `--reply-all` | no | Include all original recipients |
| `--cc ADDR...` | no | Additional CC |
| `--bcc ADDR...` | no | Additional BCC |
| `--attachments PATH...` | no | File paths to attach |
| `--allow-live-mail-mutation` | no | Development-only override |

Provide one of `--message-id` or `--id`.
Live Mail.app reply draft. Disabled by default with `MAIL_UI_MUTATION_DISABLED`. In production, compose a new hidden synced reply draft with `compose-draft` instead.

## forward-draft

| Param | Required | Description |
|---|---|---|
| `--message-id MID` | no | Original email RFC 2822 message-id (preferred) |
| `--id ID` | no | Original email integer ID (fallback) |
| `--account EMAIL` | yes | Sending account |
| `--body TEXT` | yes | Forward message body |
| `--to ADDR...` | yes | Recipient(s) |
| `--cc ADDR...` | no | CC recipient(s) |
| `--bcc ADDR...` | no | BCC recipient(s) |
| `--attachments PATH...` | no | File paths to attach |
| `--allow-live-mail-mutation` | no | Development-only override |

Provide one of `--message-id` or `--id`.
Live Mail.app forward draft. Disabled by default with `MAIL_UI_MUTATION_DISABLED`. In production, compose a new hidden synced forward draft with `compose-draft` instead.

## delete-email

| Param | Required | Description |
|---|---|---|
| `--message-ids MID...` | no | RFC 2822 message-id(s) (preferred, stable across syncs) |
| `--ids ID...` | no | Email integer ID(s) -- UNSAFE: ids shift/collide; gated (see below) |
| `--force-int-ids` | no | opt-in required to delete by raw `--ids` |
| `--allow-live-mail-mutation` | no | Development-only override |

Enabled production deletion implemented as a recoverable same-account move to Trash/Deleted Items inside the Mail safety envelope. Provide `--message-ids` wherever possible. Raw `--ids` is refused with `UNSAFE_INT_IDS` unless `--force-int-ids` is passed.

## delete-draft

| Param | Required | Description |
|---|---|---|
| `--id ID` | yes | Draft ID |
| `--allow-live-mail-mutation` | no | Development-only override |

Live Mail.app draft deletion. Disabled by default with `MAIL_UI_MUTATION_DISABLED`.

## move-email

| Param | Required | Description |
|---|---|---|
| `--message-id MID` | no | RFC 2822 message-id (preferred, stable across syncs) |
| `--id ID` | no | Email integer ID (fallback) |
| `--to FOLDER` | yes | Destination folder name (just the name, not path) |
| `--to-account EMAIL` | no | Destination account for cross-account moves |
| `--allow-live-mail-mutation` | no | Development-only override |

Live Mail.app move. Disabled by default with `MAIL_UI_MUTATION_DISABLED`. Do not use in production; use provider/API move instead.

## batch-move

| Param | Required | Description |
|---|---|---|
| `--message-ids MID...` | no | RFC 2822 message-id(s) (preferred, stable across syncs) |
| `--ids ID...` | no | Email integer ID(s) |
| `--to FOLDER` | yes | Destination folder name |
| `--to-account EMAIL` | no | Destination account for cross-account moves |
| `--allow-live-mail-mutation` | no | Development-only override |

Live Mail.app batch move. Disabled by default with `MAIL_UI_MUTATION_DISABLED`. Do not use in production; use provider/API move instead.

## build-index

No parameters. Full FTS5 rebuild from disk. Requires Full Disk Access. Returns `{indexed, mailboxes, elapsed_seconds, db_size_mb}`.

---

## Error Codes

| Code | When |
|---|---|
| `EMAIL_NOT_FOUND` | read/delete/move with stale ID |
| `DRAFT_NOT_FOUND` | draft operation with stale ID |
| `FOLDER_NOT_FOUND` | move to nonexistent folder |
| `ACCOUNT_NOT_FOUND` | operation with unknown account |
| `INVALID_ID` | non-numeric ID |
| `MISSING_ID` | neither --id nor --message-id provided |
| `JXA_TIMEOUT` | Mail.app unresponsive |
| `LOCK_TIMEOUT` | another mail command has held the runtime lock for too long |
| `DISK_FULL` | write failed due to disk space |
| `MICROMAMBA_NOT_FOUND` | launcher bootstrap failure |
| `INTERNAL_ERROR` | unhandled exception |
