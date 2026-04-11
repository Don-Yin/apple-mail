---
name: apple-mail
description: read, write, search, and manage emails in macos mail.app via cli scripts. use when the user wants to send, read, search, draft, or organise emails.
disable-model-invocation: true
---

# apple mail skill

read, write, search, and manage emails in macos mail.app via cli. when this skill is called alone without any specific instruction, see `references/default-task.md`.

## quick start

```bash
# verify setup (mail.app running, full disk access, dependencies)
.cursor/skills/apple-mail/scripts/check-setup.sh

# build the search index (required for search and content previews)
.cursor/skills/apple-mail/scripts/mail.sh build-index

# list recent emails with content previews
.cursor/skills/apple-mail/scripts/mail.sh list-recent --include-content
```

## important: symlink path

this skill lives under `.cursor/skills/apple-mail/` which may be a symlink. all script invocations should use the resolved path from this skill directory. run `.cursor/skills/apple-mail/scripts/check-setup.sh` to see resolved paths.

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
| `list-accounts`                                                             | list all mail accounts                      | ~0.15 s   |
| `list-folders --account EMAIL`                                              | list folders with email counts              | ~1-2 s    |
| `list-recent [--limit N] [--include-content]`                               | recent emails from all inboxes              | ~0.3 s    |
| `list-emails --account EMAIL --folder NAME [--limit N] [--include-content]` | emails in a folder                          | ~0.3 s    |
| `list-drafts [--limit N] [--include-content]`                               | drafts across all accounts                  | ~0.25 s   |
| `read-email --message-id MID` or `--id ID`                                  | full email content, recipients, attachments | ~1-2 s    |
| `search --query TEXT [--scope all\|subject\|sender] [--limit N]`            | search emails                               | ~1 ms     |
| `compose-draft --account EMAIL --subject TEXT --body TEXT --to ADDR... [--attachments PATH...]`     | create a new draft                          | ~1 s      |
| `amend-draft --id ID [--subject TEXT] [--body TEXT] [--attachments PATH...]`                        | modify an existing draft                    | ~2 s      |
| `send-draft --id ID`                                                        | send a draft                                | ~2 s      |
| `reply-draft --message-id MID --body TEXT [--reply-all] [--attachments PATH...]` or `--id ID`       | create a reply draft                        | ~2 s      |
| `forward-draft --message-id MID --account EMAIL --body TEXT --to ADDR... [--attachments PATH...]`   | forward as draft                            | ~2 s      |
| `delete-email --message-ids MID [MID...]` or `--ids ID [ID...]`             | delete email(s) (single or batch)           | ~1-3 s    |
| `delete-draft --id ID`                                                      | delete a draft                              | ~1 s      |
| `move-email --message-id MID --to FOLDER [--to-account EMAIL]` or `--id ID` | move email to folder (cross-account if --to-account) | ~3-5 s    |
| `batch-move --message-ids MID [MID...] --to FOLDER [--to-account EMAIL]`    | batch-move emails to a folder               | ~5-15 s   |
| `build-index`                                                               | build/rebuild fts5 search index             | ~30-120 s |
| `index-status`                                                              | check background indexing progress          | instant   |
| `index-cancel`                                                              | cancel background indexing                  | instant   |

array arguments use space separation: `--to a@b.com c@d.com --cc x@y.com`

for detailed parameter docs and return shapes, see `references/tool-reference.md`.

## workflows

### triage inbox

1. `list-recent --include-content` -- scan recent emails with previews (note the `message_id` field in output)
2. `read-email --message-id MID` -- open specific email for full content (prefer `--message-id` over `--id`)
3. `move-email --message-id MID --to Archive` or `delete-email --message-ids MID` -- act on it

### reply to an email

1. `read-email --message-id MID` -- read the email
2. `reply-draft --message-id MID --body "..." [--reply-all]` -- create reply draft
3. `list-drafts` -- confirm draft and get stable id
4. `send-draft --id DRAFT_ID` -- send

### search and act

1. `search --query "invoice" --scope all` -- find matching emails
2. `read-email --message-id MID` -- read full content
3. take action (reply, forward, move, delete)

### compose and send

1. `compose-draft --account me@example.com --subject "Hello" --body "..." --to recipient@example.com`
2. `list-drafts` -- get stable draft id
3. `amend-draft --id ID --body "revised text"` -- (optional) revise
4. `send-draft --id ID` -- send

### bulk triage

for moving or deleting many emails at once, always use live data (not search):

1. `list-emails --account EMAIL --folder Inbox --limit 0` -- get live listing with stable message_ids
2. filter results by subject, sender, or date to identify target emails
3. confirm the list with the user
4. `batch-move --message-ids MID [MID...] --to FOLDER [--to-account EMAIL]` -- move all in one call
   or `delete-email --message-ids MID [MID...]` -- delete all in one call

important: never use `search --scope all` as the source of truth for bulk operations. the FTS index can be stale. always verify against live `list-emails` or `list-recent` output.

## safety rules

1. always draft first -- never send without creating and confirming a draft
2. never delete without confirmation -- always confirm with the user before deletion
3. prefer `--message-id` over `--id` -- the RFC 2822 message-id (the `message_id` field from list commands) is stable across exchange syncs; integer ids can shift
4. verify draft ids -- draft ids may change after creation; always re-list drafts for stable ids
5. content previews are partial -- previews are the first ~5000 chars, not full content. use `read-email` for the complete message
6. exchange sync delay -- move operations on exchange accounts need ~3 s for server sync
7. draft previews -- `--include-content` may not work for drafts on exchange accounts. use `read-email` for full draft content

## id shift recovery

mail.app integer ids can change after exchange sync. use `--message-id` (the stable RFC 2822 header) to avoid this problem entirely:

- list commands (list-emails, list-recent, list-drafts) return both `id` (integer) and `message_id` (stable string) for each email
- read, delete, move, reply, and forward commands all accept `--message-id` as the preferred identifier
- `--id` (integer) still works as a fallback for backward compatibility

if a `--message-id` lookup fails (edge case), re-list the folder:
```bash
.cursor/skills/apple-mail/scripts/mail.sh list-emails --account EMAIL --folder FOLDER
```

## background indexing

when `--include-content` is used and some emails aren't in the index, the system:
1. tries to find `.emlx` files on disk (instant)
2. falls back to jxa content fetch (up to 60 s)
3. if time runs out, spawns a background worker and returns partial results

check background progress: `.cursor/skills/apple-mail/scripts/mail.sh index-status`
cancel if needed: `.cursor/skills/apple-mail/scripts/mail.sh index-cancel`

## writing style

see `references/writing-style.md` for email composition guidelines.
