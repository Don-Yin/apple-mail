---
name: apple-mail
description: read, search, and safely draft emails from macos mail data. production draft creation uses hidden Apple Mail save for sync; visible compose and destructive Mail writes are disabled.
disable-model-invocation: true
---
# apple mail skill

read, search, and safely draft emails from macos mail data via cli. when this skill is called alone without any specific instruction, see `references/default-task.md`.

## quick start

```bash
# verify setup (mail.app running, full disk access, dependencies)
.cursor/skills/apple-mail/scripts/check-setup.sh

# build the search index (required for search and content previews)
.cursor/skills/apple-mail/scripts/mail.sh build-index

# list a focused recent batch with content previews where already available
.cursor/skills/apple-mail/scripts/mail.sh list-recent --limit 50 --include-content
```

## important: symlink path

this skill lives under `.cursor/skills/apple-mail/` which may be a symlink. all script invocations should use the resolved path from this skill directory. run `.cursor/skills/apple-mail/scripts/check-setup.sh` to see resolved paths.

## production write/delete policy

production must not drive visible Apple Mail compose windows, draft deletion, sending, forwarding, replies, or arbitrary moves. those paths have crashed Mail during testing, including crashes in Mail's scripting delete handlers. `delete-email` is the one production exception: after user confirmation, it moves the message to its own account's Trash/Deleted Items and remains recoverable.

`compose-draft` uses the hidden Apple Mail backend by default so drafts sync through the user's already-configured Mail account. it creates a Mail outgoing message with `visible:false`, saves it to drafts, and restores the previous foreground app after the operation. the response includes `backend: "mailapp"`, `mail_app_written: true`, and `visible: false`.

Outlook/Exchange sync is not local-only: Apple Mail writes a local Mail draft and Mail/Exchange performs the server sync. for a pure local no-Mail file, use `compose-draft --backend artifact` or set `APPLE_MAIL_DRAFT_BACKEND=artifact`; that writes an RFC 5322 `.eml` file plus a JSON manifest to `~/Documents/apple-mail-draft-artifacts` (or `--output-dir`) and returns `mail_app_written: false`.

`delete-email` is enabled by default and implemented as a same-account move to Trash inside the safety envelope. the following other live Mail mutation commands remain disabled by default: `amend-draft`, `send-draft`, `reply-draft`, `forward-draft`, `delete-draft`, `move-email`, and `batch-move`. they return `MAIL_UI_MUTATION_DISABLED` unless `--allow-live-mail-mutation` is passed for that exact command. this override is development-only and should only be used on a disposable mailbox until the local mutation E2E suite passes repeatedly for that operation. direct developer calls must set both `APPLE_MAIL_ALLOW_UI_MUTATION=1` and `APPLE_MAIL_ALLOW_UI_MUTATION_COMMAND=<exact command>`.

authorized local Mail mutations now run inside a safety envelope: pre/post Mail health checks, foreground-app capture/restore, and Mail crash-report delta checks. use `local-mutation-preflight` for a non-destructive check of that envelope before any live mutation testing.

the dev focus/cleanup E2E script is also disabled by default. it requires both `APPLE_MAIL_ALLOW_LIVE_E2E=1` and `APPLE_MAIL_ALLOW_UI_MUTATION=1`.

the generated-message local mutation E2E is `scripts/dev/local_mutation_e2e.py full --account EMAIL --to TEST_RECIPIENT --move-folder EXISTING_FOLDER`. it is disabled unless `APPLE_MAIL_ALLOW_LIVE_E2E=1`, `APPLE_MAIL_ALLOW_UI_MUTATION=1`, and `APPLE_MAIL_ALLOW_LOCAL_MUTATION_E2E=1` are all set. it creates only generated canary subjects, then verifies draft create, amend, draft delete, send, move, delete, focus restoration, Mail health, and crash-report deltas.

## tool reference

all commands are invoked via `.cursor/skills/apple-mail/scripts/mail.sh <command> [args]`.

all output is json with this contract:

```json
{"success": bool, "data": ..., "error": ..., "warnings": [], "meta": {...}}
```

| command                                                                     | what it does                                | speed     |
| --------------------------------------------------------------------------- | ------------------------------------------- | --------- |
| `server-info`                                                               | skill version and metadata                  | instant   |
| `check-health`                                                              | verify mail.app is responding               | ~1 s      |
| `local-mutation-preflight`                                                  | non-destructive health/focus/crash-report safety-envelope check | ~1 s |
| `list-accounts`                                                             | list all mail accounts                      | ~0.15 s   |
| `list-folders --account EMAIL`                                              | list folders with email counts              | ~1-2 s    |
| `list-recent [--limit N] [--include-content]`                               | recent emails from all inboxes              | ~0.3 s    |
| `list-emails --account EMAIL --folder NAME [--limit N] [--include-content]` | emails in a folder                          | ~0.3 s    |
| `list-drafts [--limit N] [--include-content]`                               | drafts across all accounts                  | ~0.25 s   |
| `read-email --message-id MID` or `--id ID`                                  | metadata plus cached/disk content           | ~1-2 s    |
| `search --query TEXT [--scope all\|subject\|sender] [--limit N]`            | search emails                               | ~1 ms     |
| `compose-draft --account EMAIL --subject TEXT --body TEXT --to ADDR... [--attachments PATH...]` | create hidden synced Mail draft without opening a compose window | ~1 s      |
| `compose-draft --backend artifact --account EMAIL --subject TEXT --body TEXT --to ADDR... [--attachments PATH...] [--output-dir DIR]` | create non-ui RFC 5322 `.eml` draft artifact | instant   |
| `amend-draft --id ID [--subject TEXT] [--body TEXT] [--attachments PATH...]`                        | dev-only Mail.app draft mutation; disabled by default | ~2 s      |
| `send-draft --id ID`                                                        | dev-only Mail.app send; disabled by default  | ~2 s      |
| `reply-draft --message-id MID --body TEXT [--reply-all] [--attachments PATH...]` or `--id ID`       | dev-only Mail.app reply draft; disabled by default | ~2 s      |
| `forward-draft --message-id MID --account EMAIL --body TEXT --to ADDR... [--attachments PATH...]`   | dev-only Mail.app forward draft; disabled by default | ~2 s      |
| `delete-email --message-ids MID [MID...]` or `--ids ID [ID...]`             | confirmed deletion by recoverable same-account move to Trash | ~1-3 s    |
| `delete-draft --id ID`                                                      | dev-only Mail.app draft deletion; disabled by default | ~1 s      |
| `move-email --message-id MID --to FOLDER [--to-account EMAIL]` or `--id ID` | dev-only Mail.app move; disabled by default | ~3-5 s    |
| `batch-move --message-ids MID [MID...] --to FOLDER [--to-account EMAIL]`    | dev-only Mail.app batch move; disabled by default | ~5-15 s   |
| `fix-spotlight`                                                             | disable Spotlight for ~/Library/Mail         | instant   |
| `build-index`                                                               | build/rebuild fts5 search index             | ~30-120 s |

array arguments use space separation: `--to a@b.com c@d.com --cc x@y.com`

for detailed parameter docs and return shapes, see `references/tool-reference.md`.

## workflows

### triage inbox

1. `list-recent --limit 50 --include-content` -- scan a focused recent batch with previews where available (note the `message_id` field in output)
2. `read-email --message-id MID` -- open specific email content from the index or disk cache (prefer `--message-id` over `--id`)
3. propose an action to the user. after explicit confirmation, `delete-email` may move the message to its account's Trash; `move-email` remains development-only.

### reply to an email

1. `read-email --message-id MID` -- read the email
2. draft the reply as a hidden synced draft with `compose-draft --account me@example.com --subject "Re: ..." --body "..." --to sender@example.com`
3. verify with `list-drafts --limit 10` after a short delay; do not delete cleanup drafts through Mail.app scripting

### search and act

1. `search --query "invoice" --scope all` -- find matching emails
2. `read-email --message-id MID` -- read full content
3. take action (reply, forward, move, delete)

### compose and send

1. `compose-draft --account me@example.com --subject "Hello" --body "..." --to recipient@example.com`
2. confirm the response has `backend: "mailapp"`, `mail_app_written: true`, and `visible: false`
3. verify with `list-drafts --limit 10` after a short delay so Apple Mail/Exchange has time to surface the draft
4. for revisions, create a new draft. do not use live `amend-draft` or `send-draft` in production.
5. if Mail becomes unstable, switch only the draft backend with `APPLE_MAIL_DRAFT_BACKEND=artifact` or `compose-draft --backend artifact`

### bulk triage

for moving or deleting many emails at once, always use live data (not search):

1. `list-emails --account EMAIL --folder Inbox --limit 0` -- get live listing with stable message_ids
2. filter results by subject, sender, or date to identify target emails
3. confirm the list with the user
4. present the confirmed list to the user, then delete with `delete-email --message-ids ...`. It moves each message to the corresponding account's Trash and remains recoverable. `batch-move` stays development-only.

important: never use `search --scope all` as the source of truth for bulk operations. the FTS index can be stale. always verify against live `list-emails` or `list-recent` output.

## safety rules

1. production write/delete safety -- production Mail writes are limited to hidden `compose-draft` create/save and confirmed `delete-email` same-account moves to Trash. do not use visible compose windows or live Mail.app amend/send/move/reply/forward commands in production.
2. always draft first -- never send without creating and confirming a draft
3. never delete without confirmation -- after confirmation, use `delete-email` with stable message IDs whenever possible; deletion is recoverable from Trash/Deleted Items
4. prefer `--message-id` over `--id` -- the RFC 2822 message-id (the `message_id` field from list commands) is stable across exchange syncs; integer ids can shift
5. verify draft outputs -- for default `compose-draft`, verify the hidden Mail draft with `list-drafts`; for `--backend artifact`, verify the returned `.eml` and `.json` paths exist and the manifest says `mail_app_written: false`
6. content previews are partial -- previews are the first ~5000 chars, not full content. use `read-email` for cached/disk content and expect `content_source: "unavailable"` when safe sources have no body
7. exchange sync delay -- move operations on exchange accounts need ~3 s for server sync when the development override is explicitly enabled
8. draft previews -- `--include-content` may not work for drafts on exchange accounts. use `read-email` for full draft content
9. never delete `/tmp/apple-mail-skill.lock`. if a command reports a lock timeout, wait or ask the user; deleting the lock can create concurrent Mail.app readers and freeze macos.

## verify after writes

prove every safe write took effect before yielding. if a verification fails, surface it to the user before chaining another action -- do not silently retry.

1. after production `compose-draft` -- confirm the response has `backend: "mailapp"`, `mail_app_written: true`, and `visible: false`; after a short delay, verify with `list-drafts --limit 10` or `read-email` if a stable draft id is available.
2. after `compose-draft --backend artifact` -- inspect the response paths; confirm the `.eml` and `.json` files exist, the `.eml` starts with an RFC 5322 header such as `From:`, and the manifest has `mail_app_written: false`.
3. after `delete-email`, confirm the response reports a move to Trash/Deleted Items and re-list the source folder when verification matters. use development-only checks only for the other live mutation commands.

if verification is ambiguous (exchange sync taking longer than expected, partial bulk delete with mixed success/failure, unsure whether the user wanted draft-only or send-now), ask the user with a bounded question rather than guessing.

## id shift recovery

mail.app integer ids can change after exchange sync. use `--message-id` (the stable RFC 2822 header) to avoid this problem entirely:

- list commands (list-emails, list-recent, list-drafts) return both `id` (integer) and `message_id` (stable string) for each email
- read, delete, move, reply, and forward commands all accept `--message-id` as the preferred identifier
- `--id` (integer) still works as a fallback for backward compatibility

if a `--message-id` lookup fails (edge case), re-list the folder:
```bash
.cursor/skills/apple-mail/scripts/mail.sh list-emails --account EMAIL --folder FOLDER
```

## content previews

when `--include-content` is used and some emails aren't in the index, the system:
1. tries to find `.emlx` files on disk (instant)
2. returns partial results if content is still missing

this is intentional: the skill never pulls full message bodies through Mail.app as a normal fallback because large HTML/inline-content messages can freeze low-memory Macs. if content is unavailable, use metadata plus the available preview, or run `build-index` before a deeper triage session.

## spotlight indexing

macos Spotlight indexes `~/Library/Mail` by default. this is redundant (the skill has its own FTS5 search index) and can cause `mds_stores` to consume 100%+ CPU indefinitely if the Spotlight index becomes corrupted.

on any new mac where this skill is used, run `fix-spotlight` during setup:

```bash
.cursor/skills/apple-mail/scripts/mail.sh fix-spotlight
```

this places a `.metadata_never_index` marker and (with sudo) adds `~/Library/Mail` to the Spotlight exclusion plist. `check-setup.sh` detects and warns if this hasn't been done.

## writing style

see `references/writing-style.md` for email composition guidelines.
