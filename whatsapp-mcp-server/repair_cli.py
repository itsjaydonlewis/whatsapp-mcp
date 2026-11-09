#!/usr/bin/env python3
"""Operator commands for the identity cache and one-shot history backfill.

history-status reads the local message database and sends nothing.
history-backfill is a dry run unless --execute is passed.
refresh-identities asks the already-running bridge to reread its local stores.
"""

import argparse
import json
import os
import sys

import whatsapp


def main() -> int:
    parser = argparse.ArgumentParser(description="WhatsApp bridge identity and history repair commands")
    parser.add_argument("--db", default=os.environ.get("WHATSAPP_MESSAGES_DB", ""), help="Path to messages.db")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("history-status", help="Compare stored history with pagination")
    status.add_argument("--chat-jid", required=True)
    status.add_argument("--limit", type=int, default=100)

    backfill = sub.add_parser("history-backfill", help="Plan or send one on-demand history request")
    backfill.add_argument("--chat-jid", required=True)
    backfill.add_argument("--count", type=int, default=50)
    backfill.add_argument("--execute", action="store_true", help="Send one peer history request. Omit for a dry run.")

    sub.add_parser("refresh-identities", help="Refresh the local identity cache through the bridge")

    args = parser.parse_args()
    if args.db:
        whatsapp.set_messages_db_path(args.db)

    if args.command == "history-status":
        print(json.dumps(whatsapp.get_chat_history_status(args.chat_jid, args.limit), indent=2))
        return 0
    if args.command == "history-backfill":
        result = whatsapp.request_history_backfill(args.chat_jid, count=args.count, dry_run=not args.execute)
        print(json.dumps(result, indent=2))
        return 0 if result.get("success") else 1
    if args.command == "refresh-identities":
        success, message = whatsapp.refresh_identity_cache()
        print(message)
        return 0 if success else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
