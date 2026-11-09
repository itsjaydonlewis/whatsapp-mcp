import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import identity
import whatsapp


PN = "15550001111@s.whatsapp.net"
LID = "999888777666@lid"
OTHER = "15550002222@s.whatsapp.net"
LONGER = "155500011119@s.whatsapp.net"
GROUP = "120363000000@g.us"


def init_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE chats (
            jid TEXT PRIMARY KEY,
            name TEXT,
            last_message_time TIMESTAMP
        );
        CREATE TABLE messages (
            id TEXT,
            chat_jid TEXT,
            sender TEXT,
            content TEXT,
            timestamp TIMESTAMP,
            is_from_me BOOLEAN,
            media_type TEXT,
            filename TEXT,
            url TEXT,
            media_key BLOB,
            file_sha256 BLOB,
            file_enc_sha256 BLOB,
            file_length INTEGER,
            PRIMARY KEY (id, chat_jid)
        );
        CREATE TABLE contact_cache (
            jid TEXT PRIMARY KEY,
            full_name TEXT NOT NULL DEFAULT '',
            first_name TEXT NOT NULL DEFAULT '',
            push_name TEXT NOT NULL DEFAULT '',
            business_name TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE lid_map (
            lid TEXT PRIMARY KEY,
            pn TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE identity_conflict (
            lid TEXT NOT NULL,
            pn TEXT NOT NULL,
            reason TEXT NOT NULL,
            PRIMARY KEY (lid, pn, reason)
        );
        CREATE TABLE chat_history_meta (
            chat_jid TEXT PRIMARY KEY,
            sync_type TEXT NOT NULL DEFAULT '',
            end_of_history_transfer INTEGER,
            end_of_history_transfer_type TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );
        """
    )
    conn.commit()
    return conn


class IdentityRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "messages.db")
        self.conn = init_db(self.db)
        whatsapp.set_messages_db_path(self.db)

    def tearDown(self):
        self.conn.close()
        whatsapp.set_messages_db_path(None)
        self.tmp.cleanup()

    def add_chat(self, jid, name, when):
        self.conn.execute(
            "INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
            (jid, name, when),
        )

    def add_message(self, mid, chat, sender, content, when, from_me=0):
        self.conn.execute(
            """
            INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (mid, chat, sender, content, when, from_me),
        )

    def add_contact(self, jid, full="", first="", push="", business=""):
        self.conn.execute(
            """
            INSERT INTO contact_cache (jid, full_name, first_name, push_name, business_name)
            VALUES (?, ?, ?, ?, ?)
            """,
            (jid, full, first, push, business),
        )

    def finish(self):
        self.conn.commit()

    def test_mapping_direction_names_and_lookups(self):
        self.add_contact(PN, full="Ada Example", push="Ada Push")
        self.add_contact(LID, push="Ada Push")
        self.add_contact(OTHER, full="Ada Examples")
        self.conn.execute("INSERT INTO lid_map (lid, pn, source) VALUES (?, ?, 'store')", (LID, PN))
        self.add_chat(LID, "999888777666", "2026-11-06T12:00:00")
        self.add_chat(PN, "Nickname", "2026-11-05T12:00:00")
        self.add_chat(OTHER, "15550002222", "2026-11-04T12:00:00")
        self.add_chat(GROUP, "Book Club", "2026-11-06T12:00:00")
        self.add_message("g1", GROUP, LID, "synthetic-group", "2026-11-06T12:00:00")
        self.add_message("d1", LID, LID, "synthetic-dm", "2026-11-06T12:00:00")
        self.finish()

        self.assertEqual(whatsapp.get_sender_name(LID), "Ada Example")
        self.assertEqual(whatsapp.get_sender_name("999888777666"), "Ada Example")
        chat = whatsapp.get_chat(LID, include_last_message=False)
        self.assertEqual(chat.name, "Ada Example")
        self.assertEqual(chat.name_source, "full_name")
        self.assertEqual(whatsapp.get_chat(PN, include_last_message=False).name, "Nickname")
        self.assertEqual(whatsapp.get_chat(GROUP, include_last_message=False).name, "Book Club")

        found = whatsapp.search_contacts("Ada Example")
        # "Ada Example" is also a substring of "Ada Examples"; the two people stay separate.
        self.assertEqual(len(found), 2)
        ada = next(row for row in found if row.phone_number == "15550001111")
        self.assertEqual(ada.name, "Ada Example")
        self.assertIn(ada.jid, {LID, PN})
        self.assertTrue(ada.alternate_jid)
        self.assertEqual({row.phone_number for row in found}, {"15550001111", "15550002222"})

        similar = whatsapp.search_contacts("Ada Examples")
        self.assertEqual(len(similar), 1)
        self.assertEqual(similar[0].jid, OTHER)
        self.assertEqual(similar[0].phone_number, "15550002222")

        direct = whatsapp.get_direct_chat_by_contact("15550001111")
        self.assertEqual(direct.jid, LID)
        self.assertEqual(direct.alternate_jid, PN)
        self.assertIsNone(whatsapp.get_direct_chat_by_contact("5550001111"))
        self.assertIsNone(whatsapp.get_direct_chat_by_contact("155500011119"))

    def test_conflict_and_ambiguous_bare_sender_do_not_merge(self):
        self.add_contact(PN, full="Ada Example")
        self.add_contact(LID, full="Bea Example")
        self.conn.execute(
            "INSERT INTO identity_conflict (lid, pn, reason) VALUES (?, ?, ?)",
            (LID, PN, "lid_maps_to_multiple_phone_jids"),
        )
        self.conn.execute("INSERT INTO lid_map (lid, pn, source) VALUES (?, ?, 'store')", (LID, PN))
        self.add_chat(LID, "999888777666", "2026-11-06T12:00:00")
        self.add_chat(PN, "15550001111", "2026-11-06T11:00:00")
        self.finish()

        directory = identity.load_directory(self.db)
        self.assertIsNone(identity.linked_peer(directory, LID))
        self.assertEqual(whatsapp.get_sender_name(LID), "Bea Example")
        self.assertEqual(whatsapp.get_sender_name(PN), "Ada Example")
        self.assertEqual(whatsapp.get_sender_name("999888777666"), "Bea Example")
        colliding = "999888777666@s.whatsapp.net"
        self.add_contact(colliding, full="Cara Example")
        self.add_chat(colliding, "999888777666", "2026-11-06T10:00:00")
        self.finish()
        self.assertEqual(whatsapp.get_sender_name("999888777666"), "999888777666")
        direct = whatsapp.get_direct_chat_by_contact("15550001111")
        self.assertEqual(direct.jid, PN)
        self.assertIsNone(direct.alternate_jid)

    def test_group_sender_is_not_the_group_name(self):
        self.add_contact(PN, full="Ada Example")
        self.conn.execute("INSERT INTO lid_map (lid, pn, source) VALUES (?, ?, 'store')", (LID, PN))
        self.add_chat(GROUP, "Book Club", "2026-11-06T12:00:00")
        self.add_message("g1", GROUP, "999888777666", "synthetic", "2026-11-06T12:00:00")
        self.finish()
        self.assertEqual(whatsapp.get_sender_name("999888777666"), "Ada Example")
        self.assertEqual(whatsapp.get_chat(GROUP, include_last_message=False).name, "Book Club")
        chats = whatsapp.get_contact_chats(PN, limit=5, page=0)
        self.assertEqual([chat.jid for chat in chats], [GROUP])

    def test_pagination_separates_short_history_from_duplicates(self):
        self.add_chat(PN, "Ada Example", "2026-11-06T12:00:00")
        self.add_chat(LID, "999888777666", "2026-11-06T12:00:00")
        self.add_chat(GROUP, "Book Club", "2026-11-06T12:00:00")
        base = "2026-11-06T12:00:00"
        for i in range(120):
            self.add_message(f"p{i:03d}", PN, PN, f"synthetic-{i}", f"2026-11-06T12:{i % 60:02d}:{i % 60:02d}")
        for i in range(18):
            self.add_message(f"s{i:03d}", LID, LID, f"synthetic-{i}", f"2026-11-05T12:00:{i:02d}")
        for i in range(5):
            self.add_message(f"t{i}", GROUP, "sender", f"synthetic-{i}", base)
        self.finish()

        page0 = whatsapp.list_messages(chat_jid=PN, limit=100, page=0, include_context=False)
        page1 = whatsapp.list_messages(chat_jid=PN, limit=100, page=1, include_context=False)
        self.assertEqual(page0.count("synthetic-"), 100)
        self.assertEqual(page1.count("synthetic-"), 20)
        self.assertIn("No messages", whatsapp.list_messages(chat_jid=LID, limit=100, page=1, include_context=False))

        long_status = whatsapp.get_chat_history_status(PN, limit=100)
        self.assertEqual(long_status["stored_message_count"], 120)
        self.assertEqual(long_status["page0_count"], 100)
        self.assertEqual(long_status["page1_count"], 20)
        self.assertTrue(long_status["pagination_matches_store"])

        short_status = whatsapp.get_chat_history_status(LID, limit=100)
        self.assertEqual(short_status["stored_message_count"], 18)
        self.assertEqual(short_status["page0_count"], 18)
        self.assertEqual(short_status["page1_count"], 0)
        self.assertTrue(short_status["pagination_matches_store"])
        self.assertIn("not a pagination bug", short_status["interpretation"])
        self.assertTrue(short_status["backfill"]["can_request"])
        self.assertEqual(short_status["backfill"]["request_count"], 50)
        self.assertIn("does not recover the entire lifetime history", short_status["coverage"])

        ids = []
        for page in range(3):
            ids.extend(identity.message_ids_page(self.db, GROUP, limit=2, page=page))
        self.assertEqual(ids, ["t4", "t3", "t2", "t1", "t0"])

        listed = whatsapp.list_chats(limit=2, page=0, include_last_message=True)
        listed_page_1 = whatsapp.list_chats(limit=2, page=1, include_last_message=True)
        seen = [chat.jid for chat in listed + listed_page_1]
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(listed), 2)
        self.assertEqual(len(listed_page_1), 1)

        contact_chats = whatsapp.get_contact_chats(PN, limit=5, page=0)
        self.assertEqual({chat.jid for chat in contact_chats}, {PN})

    def test_empty_chat_cannot_backfill_and_dry_run_does_not_post(self):
        self.add_chat(LID, "999888777666", "2026-11-06T12:00:00")
        self.finish()
        status = whatsapp.get_chat_history_status(LID, limit=100)
        self.assertEqual(status["stored_message_count"], 0)
        self.assertFalse(status["backfill"]["can_request"])
        self.assertIn("no stored anchor message", status["backfill"]["block_reason"])

        with mock.patch("whatsapp.requests.post", side_effect=AssertionError("posted")):
            result = whatsapp.request_history_backfill(LID, count=100, dry_run=True)
        self.assertFalse(result["executed"])
        self.assertIn("No history request was sent", result["message"])

    def test_execute_posts_one_peer_request_without_a_chat_body(self):
        self.add_chat(PN, "Ada Example", "2026-11-06T12:00:00")
        self.add_message("m1", PN, PN, "synthetic", "2026-11-06T12:00:00", from_me=1)
        self.finish()

        class Response:
            status_code = 200
            text = "{}"

            def json(self):
                return {
                    "success": True,
                    "executed": True,
                    "message": "sent",
                    "plan": {"request_count": 50, "can_request": True},
                }

        with mock.patch("whatsapp.requests.post", return_value=Response()) as post:
            result = whatsapp.request_history_backfill(PN, count=100, dry_run=False)
        self.assertTrue(result["executed"])
        self.assertEqual(post.call_count, 1)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["chat_jid"], PN)
        self.assertEqual(payload["count"], 100)
        self.assertFalse(payload["dry_run"])
        self.assertNotIn("message", payload)
        self.assertNotIn("recipient", payload)
        self.assertTrue(post.call_args.args[0].endswith("/history-backfill"))

    def test_history_cap_and_device_suffix(self):
        self.assertEqual(identity.canonicalize_jid("15550001111:1@s.whatsapp.net"), PN)
        self.assertEqual(identity.canonicalize_jid("999888777666:3@lid"), LID)
        plan = identity.plan_history_backfill(PN, 18, {"id": "m1", "timestamp": "2026-11-06T12:00:00", "from_me": False}, 100)
        self.assertTrue(plan["can_request"])
        self.assertEqual(plan["request_count"], 50)
        self.assertEqual(identity.classify_jid(GROUP), "group")


if __name__ == "__main__":
    unittest.main()
