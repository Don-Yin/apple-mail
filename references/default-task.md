do all work in the main chat thread. do not delegate to subagents.

## steps

1. run `list-recent --include-content` (no --limit, so it fetches all inbox emails). check the `total_inbox` and `showing` fields to confirm you have everything. note that results include an `email_type` field (human, notification, auto-reply) which helps with triage decisions.

2. for emails where `preview_truncated` is true or `preview_available` is false, call `read-email --message-id MID` to get the full content. for the rest, the preview is sufficient - do not call read-email on every single email.

3. present multiple summary tables in the chat, one table per destination. each destination is identified by `account -> folder` (e.g. `me@example.com -> projects`, `work@example.com -> team-updates`). use the folder map below to match emails to the correct destination. each table has columns: #, sender, subject (truncated to 60 chars), date, proposed action. proposed action is one of: reply, archive, follow up, flag. the destination is the table heading, so it is not repeated in every row.

4. after confirming each destination table with the user, execute moves using one `batch-move` call per table: `batch-move --message-ids MID [MID...] --to FOLDER [--to-account EMAIL]`. this matches the bulk triage workflow from SKILL.md - one batch per destination keeps execution simple.

5. present a separate safe-to-delete table with the `message_id` for each entry. safe-to-delete means: expired transactional notifications, automated digests, marketing newsletters, past event reminders, old calendar responses, self-sent copies, auto-replies/OOO, ticket auto-confirmations. include the reason for each. execute deletions with `delete-email --message-ids MID [MID...]`.

6. ask the user to confirm before executing any action (delete, archive, move).

## folder discovery

before proposing destinations, run `list-accounts` and `list-folders --account EMAIL` for every account to build a complete folder map across all accounts. when deciding where to move an email, consider folders in any account, not just the account the email arrived in. pick the best-fitting folder by topic, sender name, or project regardless of which account owns it. use the `--to-account` flag when the target folder lives in a different account.

never use the generic `Archive` folder. if no existing folder is a good match, ask the user where it should go rather than defaulting to Archive.
