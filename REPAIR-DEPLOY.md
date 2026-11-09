# LID name and history repair — deploy and rollback

This change is **not applied** to a live bridge by merging the pull request. The live names and history issue stays open until this build is deployed on the coordinator checkout and checked there. Do not open a pull request against `lharries/whatsapp-mcp`.

The patch is based on upstream `7d6a06d` plus:

- `go.mau.fi/whatsmeow` `v0.0.0-20261102152715-54038813c47f` (2 Nov 2026)
- context arguments required by that library (`sqlstore.New`, `Download`, `GetGroupInfo`, contact and LID stores)
- local identity cache, raw-name repair, stable pagination, and one explicit history backfill command

The live checkout already runs whatsmeow `v0.0.0-20261030132714-6f46ecaf9e95` (30 Oct 2026) with local `context.Background` fixes, `PAIR_PHONE`, `QR_CODE` logging, and restart/QR wrappers. The sqlstore upgrade files are the same in the 30 Oct and 2 Nov builds, so this bump does not add a new session-schema migration beyond the 30 Oct build. Keep the 2 Nov module. Do not downgrade to the March 2025 module in upstream `7d6a06d`.

## What this repair changes in the databases

- Reads the whatsmeow session database. It does not delete it, vacuum it, or start a new pairing.
- Adds `contact_cache`, `lid_map`, `identity_conflict`, and `chat_history_meta` to `messages.db`, plus an index on messages. Old code ignores those tables.
- Updates `chats.name` only when the stored name is empty or equal to the raw JID / user id (or a linked alias, or `Group <id>`), and a real contact or group name is known.
- Does not delete messages, mark chats read, send chat messages, or unlink the device.
- Startup and `POST /api/identities/refresh` rebuild the cache from the local contact and LID stores.

A full lifetime history is **not** available. `BuildHistorySyncRequest` plus `SendPeerMessage` can ask the primary phone for at most 50 messages immediately before the oldest stored message in one chat, once per explicit call. The phone may return fewer, none, or report that older messages remain but the linked device cannot access them (`NO_ACCESS`). A chat with zero stored messages has no anchor and cannot be backfilled. Do not delete the databases to “reload history”; that wipes the local archive and forces a new link.

## Before you touch the live checkout

1. Stop the bridge process. Leave the session logged in. Do not scan a QR code and do not set `PAIR_PHONE`.
2. Copy the whole store directory, including WAL/SHM sidecars, to a backup outside the repo:

   ```bash
   cd /path/to/live/whatsapp-mcp/whatsapp-bridge
   cp -a store "/path/to/backup/store-$(date -u +%Y%m%dT%H%M%SZ)"
   ```

3. Copy any local-only files that are not in git, especially restart/QR wrapper scripts and the current `main.go` if it contains `PAIR_PHONE` or `QR_CODE` logging.
4. Confirm the backup contains `store/whatsapp.db` and `store/messages.db` and is not inside the working tree you are about to update.

## Deploy

1. Fetch this fork branch (not `lharries/whatsapp-mcp`) and check out the repair commit in the live repo, or cherry-pick it onto the live branch.
2. Re-apply only the local `PAIR_PHONE` / `QR_CODE` log lines and restart/QR wrappers onto the new `main.go`. The QR loop in this commit is the upstream loop plus a comment. Do not commit QR tokens or session files.
3. The context-aware whatsmeow calls in this commit replace the earlier local context fixes. Keep this commit’s `go.mod` (`v0.0.0-20261102152715-54038813c47f`). Go 1.26 or newer is required (`go 1.26.0`; the module’s toolchain line may select Go 1.27).
4. Build from `whatsapp-bridge` without deleting `store/`:

   ```bash
   cd whatsapp-bridge
   go build -o whatsapp-bridge .
   ```

5. Start the bridge the same way as before (the existing restart wrapper is fine). The first start reads the session store and refreshes the identity cache. It should reconnect the saved session and must not show a new QR code. If it does, stop immediately and restore the backup; do not scan.
6. Restart the Python MCP process so it loads the new `whatsapp.py`. No new login is required.
7. Check counts only, not contact lists, in the bridge log: `Identity cache refreshed: contacts=… mappings=… conflicts=… chat_names_repaired=…`.
8. From `whatsapp-mcp-server`, dry-run one known short chat (use a JID you already have; do not paste message text into tickets):

   ```bash
   python repair_cli.py history-status --chat-jid '<chat jid>' --limit 100
   python repair_cli.py history-backfill --chat-jid '<chat jid>' --count 50
   ```

   The second command must say that no history request was sent. `pagination_matches_store` should be true. If `stored_message_count` is 18 and page 1 is empty, that chat really has 18 stored messages; that is not a pagination bug.
9. Optional, one chat at a time, only after the dry run looks right:

   ```bash
   python repair_cli.py history-backfill --chat-jid '<chat jid>' --count 50 --execute
   ```

   Do not script a loop. Wait for the history sync log line, then run `history-status` again. The stored count may grow by up to 50, or not at all. Stop if the phone reports `NO_ACCESS`.

## How to verify names

- A LID direct chat whose stored name was only the LID digits should show the address-book full name, otherwise the business name, push name, or first name from the linked phone contact.
- Group chat titles stay group titles. Group sender lines should use the same contact name when an explicit LID link or contact row exists.
- `search_contacts` with the person’s name should return that identity even when the only chat JID is `@lid`.
- `get_direct_chat_by_contact` with the full phone number should return the LID chat when the LID map links them. A shorter or longer number must not match.
- Two contacts with similar names and no LID link stay two contacts. A conflicting LID map does not copy a name across.

## Rollback

1. Stop the bridge.
2. If the new process failed before it opened the databases, switch the code back and start the previous binary. Leave `store/` as it is.
3. If you need the previous raw chat names, or the new process failed while opening `whatsapp.db`, stop the bridge and restore the backup copy of `store/` over the live `store/`. Restoring the backup also drops messages that arrived after the copy was made. Prefer a code-only rollback when the process is healthy: the new tables are unused by the old Python code, and repaired names are real labels rather than corrupted rows.
4. Check out the previous commit (live baseline: upstream `7d6a06d` plus the local whatsmeow bump, context fixes, `PAIR_PHONE`, `QR_CODE` logging, and restart/QR wrappers). Rebuild and start with the same wrapper.
5. Confirm the session reconnects without a QR code. Do not delete `messages.db` or `whatsapp.db` as the upstream README suggests for “out of sync” — that deletes history and unpairs the store.

## Manual prerequisites

- Go 1.26+ on the coordinator host to build this module.
- The existing `store/` directory and the saved linked-device session.
- The bridge listening on `127.0.0.1:8080` as it does today, for refresh and for an explicit backfill. Dry-run history status works from `messages.db` even if the bridge is down.
- Re-applied local `PAIR_PHONE` / QR / restart wrappers if they lived only in the previous working tree.
