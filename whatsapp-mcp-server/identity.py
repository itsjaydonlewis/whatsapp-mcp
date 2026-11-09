"""Local identity resolution for LID-keyed WhatsApp chats.

Names come from the bridge's contact_cache and explicit lid_map rows.
Those rows are filled from the documented whatsmeow contact store and LID
mapping store. Nothing here infers that two people are the same because
their names look alike, and redacted phone strings are never match keys.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

PHONE_SERVER = "s.whatsapp.net"
LID_SERVER = "lid"
GROUP_SERVER = "g.us"

HISTORY_BACKFILL_MAX = 50
HISTORY_COVERAGE = (
    "On-demand history sync asks the primary phone for at most 50 messages "
    "immediately before the oldest stored message in one chat. WhatsApp may "
    "return fewer, none, or report that older messages remain on the phone "
    "but are not available to a linked device. One call does not recover the "
    "entire lifetime history, and this repair never loops the request or resets "
    "the session."
)
NO_ANCHOR_REASON = (
    "no stored anchor message; on-demand history sync requires the oldest "
    "stored message id, timestamp, and from-me flag. A chat with no stored "
    "messages cannot be backfilled without resetting the linked device, which "
    "this repair will not do."
)


@dataclass
class ContactRecord:
    jid: str
    full_name: str = ""
    first_name: str = ""
    push_name: str = ""
    business_name: str = ""


@dataclass
class Directory:
    contacts: dict[str, ContactRecord] = field(default_factory=dict)
    lid_to_pn: dict[str, str] = field(default_factory=dict)
    pn_to_lid: dict[str, str] = field(default_factory=dict)
    conflict_jids: set[str] = field(default_factory=set)
    chat_names: dict[str, str] = field(default_factory=dict)
    chat_times: dict[str, Optional[str]] = field(default_factory=dict)


def canonicalize_jid(value: str) -> str:
    value = (value or "").strip()
    if "@" not in value:
        return value
    user, server = value.split("@", 1)
    if ":" in user:
        user = user.split(":", 1)[0]
    if "." in user:
        left, right = user.split(".", 1)
        if right.isdigit():
            user = left
    return f"{user}@{server}"


def user_part(jid: str) -> str:
    canon = canonicalize_jid(jid)
    if "@" in canon:
        return canon.split("@", 1)[0]
    return canon


def classify_jid(jid: str) -> str:
    canon = canonicalize_jid(jid)
    if "@" not in canon:
        return "unknown"
    server = canon.split("@", 1)[1]
    if server == PHONE_SERVER:
        return "phone"
    if server == LID_SERVER:
        return "lid"
    if server == GROUP_SERVER:
        return "group"
    return "other"


def is_raw_label(name: Optional[str], jid: str, aliases: Optional[list[str]] = None) -> bool:
    text = (name or "").strip()
    if not text:
        return True
    candidates = []
    if jid:
        candidates.append(jid)
    if aliases:
        candidates.extend(aliases)
    for candidate in candidates:
        candidate = (candidate or "").strip()
        if not candidate:
            continue
        canon = canonicalize_jid(candidate)
        user = user_part(canon)
        if text == candidate or text == canon or (user and text == user):
            return True
        if classify_jid(canon) == "group" and text == f"Group {user}":
            return True
    return False


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def load_directory(db_path: str) -> Directory:
    directory = Directory()
    conn = sqlite3.connect(db_path)
    try:
        if _table_exists(conn, "contact_cache"):
            for jid, full_name, first_name, push_name, business_name in conn.execute(
                "SELECT jid, full_name, first_name, push_name, business_name FROM contact_cache"
            ):
                canon = canonicalize_jid(jid)
                directory.contacts[canon] = ContactRecord(
                    jid=canon,
                    full_name=full_name or "",
                    first_name=first_name or "",
                    push_name=push_name or "",
                    business_name=business_name or "",
                )
        if _table_exists(conn, "lid_map"):
            for lid, pn in conn.execute("SELECT lid, pn FROM lid_map"):
                lid_c = canonicalize_jid(lid)
                pn_c = canonicalize_jid(pn)
                if classify_jid(lid_c) == "lid" and classify_jid(pn_c) == "phone":
                    directory.lid_to_pn[lid_c] = pn_c
                    directory.pn_to_lid[pn_c] = lid_c
        if _table_exists(conn, "identity_conflict"):
            for lid, pn in conn.execute("SELECT lid, pn FROM identity_conflict"):
                directory.conflict_jids.add(canonicalize_jid(lid))
                directory.conflict_jids.add(canonicalize_jid(pn))
        if _table_exists(conn, "chats"):
            for jid, name, last_time in conn.execute(
                "SELECT jid, name, last_message_time FROM chats"
            ):
                canon = canonicalize_jid(jid)
                directory.chat_names[canon] = name or ""
                directory.chat_times[canon] = last_time
    finally:
        conn.close()
    _drop_conflicted_links(directory)
    return directory


def _drop_conflicted_links(directory: Directory) -> None:
    # A stored conflict, or a map that is not one-to-one, must not link identities.
    lid_targets: dict[str, set[str]] = {}
    pn_targets: dict[str, set[str]] = {}
    for lid, pn in directory.lid_to_pn.items():
        lid_targets.setdefault(lid, set()).add(pn)
        pn_targets.setdefault(pn, set()).add(lid)
    bad = set(directory.conflict_jids)
    for lid, pns in lid_targets.items():
        if len(pns) > 1:
            bad.add(lid)
            bad.update(pns)
    for pn, lids in pn_targets.items():
        if len(lids) > 1:
            bad.add(pn)
            bad.update(lids)
    directory.conflict_jids = bad
    for lid, pn in list(directory.lid_to_pn.items()):
        if lid in bad or pn in bad:
            directory.lid_to_pn.pop(lid, None)
            directory.pn_to_lid.pop(pn, None)


def linked_peer(directory: Directory, jid: str) -> Optional[str]:
    canon = canonicalize_jid(jid)
    if canon in directory.conflict_jids:
        return None
    kind = classify_jid(canon)
    if kind == "lid":
        peer = directory.lid_to_pn.get(canon)
    elif kind == "phone":
        peer = directory.pn_to_lid.get(canon)
    else:
        return None
    if not peer or peer in directory.conflict_jids:
        return None
    return peer


def identity_aliases(directory: Directory, jid: str) -> list[str]:
    canon = canonicalize_jid(jid)
    aliases = [canon]
    peer = linked_peer(directory, canon)
    if peer:
        aliases.append(peer)
    return aliases


def _record_name(records: list[Optional[ContactRecord]]) -> tuple[str, str]:
    fields = (
        ("full_name", "full_name"),
        ("business_name", "business_name"),
        ("push_name", "push_name"),
        ("first_name", "first_name"),
    )
    for attr, source in fields:
        for record in records:
            if record is None:
                continue
            value = (getattr(record, attr) or "").strip()
            if value:
                return value, source
    return "", ""


def choose_display_name(directory: Directory, jid: str) -> tuple[str, str, str]:
    """Return (name, source, status).

    status is linked, unlinked, conflict, or unknown.
    A conflict never borrows the other JID's name.
    """
    canon = canonicalize_jid(jid)
    if classify_jid(canon) == "group":
        stored = directory.chat_names.get(canon, "")
        if stored and not is_raw_label(stored, canon):
            return stored, "chat_name", "unlinked"
        return "", "", "unknown"
    if canon in directory.conflict_jids:
        name, source = _record_name([directory.contacts.get(canon)])
        if not name:
            stored = directory.chat_names.get(canon, "")
            if stored and not is_raw_label(stored, canon, [canon]):
                return stored, "chat_name", "conflict"
            return "", "", "conflict"
        return name, source, "conflict"

    peer = linked_peer(directory, canon)
    own = directory.contacts.get(canon)
    if peer:
        other = directory.contacts.get(peer)
        if classify_jid(canon) == "phone":
            records = [own, other]
        else:
            records = [other, own]
        name, source = _record_name(records)
        if name:
            return name, source, "linked"
        for candidate in (canon, peer) if classify_jid(canon) == "phone" else (peer, canon):
            stored = directory.chat_names.get(candidate, "")
            aliases = identity_aliases(directory, canon)
            if stored and not is_raw_label(stored, candidate, aliases):
                return stored, "chat_name", "linked"
        return "", "", "linked"

    name, source = _record_name([own])
    if name:
        return name, source, "unlinked"
    stored = directory.chat_names.get(canon, "")
    if stored and not is_raw_label(stored, canon, [canon]):
        return stored, "chat_name", "unlinked"
    return "", "", "unknown"


def display_chat_name(directory: Directory, jid: str, stored_name: Optional[str]) -> tuple[str, str, str]:
    canon = canonicalize_jid(jid)
    name, source, status = choose_display_name(directory, canon)
    aliases = identity_aliases(directory, canon)
    if classify_jid(canon) == "group":
        if stored_name and not is_raw_label(stored_name, canon):
            return stored_name, "chat_name", "unlinked"
        if name:
            return name, source, status
        return stored_name or canon, "unresolved", "unknown"
    if stored_name and not is_raw_label(stored_name, canon, aliases):
        return stored_name, "chat_name", status if status != "unknown" else "unlinked"
    if name:
        return name, source, status
    if stored_name:
        return stored_name, "unresolved", status
    return canon, "unresolved", status


def resolve_sender_label(directory: Directory, sender: str) -> str:
    sender = (sender or "").strip()
    if not sender:
        return sender
    if "@" in sender:
        canon = canonicalize_jid(sender)
        name, _source, _status = choose_display_name(directory, canon)
        if name:
            return name
        return sender

    matches = []
    seen = set()
    for jid in list(directory.contacts) + list(directory.chat_names) + list(directory.lid_to_pn) + list(directory.pn_to_lid):
        canon = canonicalize_jid(jid)
        if canon in seen:
            continue
        seen.add(canon)
        if user_part(canon) == sender and classify_jid(canon) in {"phone", "lid"}:
            matches.append(canon)
    identities = {}
    for jid in matches:
        if jid in directory.conflict_jids:
            identities.setdefault("conflict:" + jid, jid)
            continue
        peer = linked_peer(directory, jid)
        if peer and classify_jid(jid) == "lid":
            identities.setdefault("id:" + peer, jid)
        else:
            identities.setdefault("id:" + jid, jid)
    if len(identities) == 1:
        jid = next(iter(identities.values()))
        name, _source, _status = choose_display_name(directory, jid)
        if name:
            return name
    return sender


def digits_only(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def phone_jid(digits: str) -> str:
    return f"{digits}@{PHONE_SERVER}"


def direct_chat_jids(directory: Directory, phone_query: str) -> list[str]:
    """Exact phone match, plus the explicit LID alias. Never a substring."""
    digits = digits_only(phone_query)
    if not digits or digits != (phone_query or "").strip().lstrip("+"):
        # Allow spaces and punctuation, but the digits themselves must be the whole query
        # aside from a leading plus and separators. Reject when letters are present.
        raw = (phone_query or "").strip()
        if any(ch.isalpha() for ch in raw):
            return []
    if not digits:
        return []
    pn = phone_jid(digits)
    jids = [pn]
    peer = linked_peer(directory, pn)
    if peer:
        jids.append(peer)
    return jids


def sender_match_values(directory: Directory, query: str) -> list[str]:
    """Exact sender strings for one identity, including legacy bare user parts."""
    query = (query or "").strip()
    if not query:
        return []
    if "@" in query:
        canon = canonicalize_jid(query)
        if canon in directory.conflict_jids:
            return [canon, user_part(canon)]
        values = []
        for jid in identity_aliases(directory, canon):
            values.append(jid)
            user = user_part(jid)
            if user:
                values.append(user)
        return list(dict.fromkeys(values))
    if any(ch.isalpha() for ch in query):
        return [query]
    digits = digits_only(query)
    if not digits:
        return [query]
    pn = phone_jid(digits)
    # A bare number is a phone lookup. An unlinked LID that happens to use the
    # same digits is a different identity and is not included.
    values = [pn, digits]
    peer = linked_peer(directory, pn)
    if peer:
        values.append(peer)
        values.append(user_part(peer))
    return list(dict.fromkeys(values))


def _nonraw_chat_name(directory: Directory, jid: str) -> str:
    stored = directory.chat_names.get(jid, "")
    aliases = identity_aliases(directory, jid)
    if stored and not is_raw_label(stored, jid, aliases):
        return stored
    return ""


def search_contact_records(directory: Directory, query: str) -> list[dict]:
    query_text = (query or "").strip()
    if not query_text:
        return []
    needle = query_text.casefold()
    digits = digits_only(query_text)
    exact_phone = bool(digits) and not any(ch.isalpha() for ch in query_text)

    candidates = set(directory.contacts) | {
        jid for jid in directory.chat_names if classify_jid(jid) in {"phone", "lid"}
    }
    matched: dict[str, dict] = {}
    for jid in candidates:
        if classify_jid(jid) not in {"phone", "lid"}:
            continue
        name, source, status = choose_display_name(directory, jid)
        record = directory.contacts.get(jid)
        haystacks = [name]
        if record is not None:
            haystacks.extend([record.full_name, record.first_name, record.push_name, record.business_name])
        chat_name = _nonraw_chat_name(directory, jid)
        if chat_name:
            haystacks.append(chat_name)
        name_match = any(needle in (item or "").casefold() for item in haystacks if item)
        jid_match = canonicalize_jid(query_text) == jid if "@" in query_text else False
        phone_match = False
        pn = None
        if exact_phone:
            if classify_jid(jid) == "phone" and user_part(jid) == digits:
                phone_match = True
                pn = jid
            elif classify_jid(jid) == "lid":
                peer = linked_peer(directory, jid)
                if peer and user_part(peer) == digits:
                    phone_match = True
                    pn = peer
        if not (name_match or jid_match or phone_match):
            continue
        if status == "conflict" or jid in directory.conflict_jids:
            key = "conflict:" + jid
        else:
            peer = linked_peer(directory, jid)
            if peer and classify_jid(jid) == "lid":
                key = "id:" + peer
            elif peer:
                key = "id:" + jid
            else:
                key = "id:" + jid
        current = matched.get(key)
        if current is None:
            matched[key] = {
                "jids": {jid},
                "name": name or None,
                "name_source": source or "unresolved",
                "identity_status": status,
                "pn": pn,
            }
        else:
            current["jids"].add(jid)
            if not current["name"] and name:
                current["name"] = name
                current["name_source"] = source
            if pn and not current["pn"]:
                current["pn"] = pn

    results = []
    for item in matched.values():
        jids = list(item["jids"])
        chats = [jid for jid in jids if jid in directory.chat_names]
        chats.sort(key=lambda jid: directory.chat_times.get(jid) or "", reverse=True)
        primary = chats[0] if chats else sorted(jids)[0]
        alternate = next((jid for jid in chats if jid != primary), None)
        pn = item["pn"]
        if pn is None and classify_jid(primary) == "phone" and primary not in directory.conflict_jids:
            pn = primary
        if pn is None:
            peer = linked_peer(directory, primary)
            if peer and classify_jid(peer) == "phone":
                pn = peer
        phone_number = user_part(pn) if pn else ""
        lid = None
        if classify_jid(primary) == "lid":
            lid = primary
        elif pn:
            lid = directory.pn_to_lid.get(pn) if pn not in directory.conflict_jids else None
        results.append(
            {
                "phone_number": phone_number,
                "name": item["name"],
                "jid": primary,
                "name_source": item["name_source"],
                "identity_status": item["identity_status"],
                "alternate_jid": alternate,
                "pn_jid": pn,
                "lid_jid": lid,
            }
        )
    results.sort(key=lambda row: ((row["name"] or "").casefold(), row["jid"]))
    return results[:50]


def plan_history_backfill(chat_jid: str, stored_count: int, anchor: Optional[dict], requested_count: int) -> dict:
    count = requested_count if requested_count and requested_count > 0 else HISTORY_BACKFILL_MAX
    if count > HISTORY_BACKFILL_MAX:
        count = HISTORY_BACKFILL_MAX
    plan = {
        "chat_jid": canonicalize_jid(chat_jid),
        "stored_count": stored_count,
        "request_count": count,
        "can_request": False,
        "block_reason": "",
        "coverage": HISTORY_COVERAGE,
        "anchor_id": "",
        "anchor_timestamp": "",
        "anchor_from_me": False,
    }
    if stored_count <= 0 or not anchor or not anchor.get("id") or not anchor.get("timestamp"):
        plan["request_count"] = 0
        plan["block_reason"] = NO_ANCHOR_REASON
        return plan
    plan["can_request"] = True
    plan["anchor_id"] = anchor["id"]
    plan["anchor_timestamp"] = anchor["timestamp"]
    plan["anchor_from_me"] = bool(anchor.get("from_me"))
    return plan


def _history_note(meta: Optional[dict]) -> str:
    note = HISTORY_COVERAGE
    if not meta:
        return note
    end_type = meta.get("end_of_history_transfer_type") or ""
    if "NO_ACCESS" in end_type:
        note += " The phone reported that older messages exist but this linked device cannot access them."
    elif "MORE" in end_type:
        note += " The phone reported that older messages may still remain. Another explicit one-shot can be requested manually later; it is not run in a loop and may still stop short of the start of the chat."
    elif "NO_MORE" in end_type:
        note += " The phone reported that no older messages remain on the primary device for this sync. That is the phone's report, not proof of a lifetime archive."
    return note


def chat_history_status(db_path: str, chat_jid: str, limit: int = 100) -> dict:
    canon = canonicalize_jid(chat_jid)
    limit = max(1, int(limit))
    conn = sqlite3.connect(db_path)
    try:
        stored = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_jid = ?",
            (canon,),
        ).fetchone()[0]
        visible = conn.execute(
            """
            SELECT COUNT(*)
            FROM messages
            LEFT JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ?
            """,
            (canon,),
        ).fetchone()[0]
        page0 = _page_count(conn, canon, limit, 0)
        page1 = _page_count(conn, canon, limit, 1)
        anchor_row = conn.execute(
            """
            SELECT id, timestamp, is_from_me
            FROM messages
            WHERE chat_jid = ? AND id != ''
            ORDER BY timestamp ASC, id ASC
            LIMIT 1
            """,
            (canon,),
        ).fetchone()
        meta = None
        if _table_exists(conn, "chat_history_meta"):
            row = conn.execute(
                """
                SELECT sync_type, end_of_history_transfer, end_of_history_transfer_type
                FROM chat_history_meta WHERE chat_jid = ?
                """,
                (canon,),
            ).fetchone()
            if row:
                meta = {
                    "sync_type": row[0] or "",
                    "end_of_history_transfer": None if row[1] is None else bool(row[1]),
                    "end_of_history_transfer_type": row[2] or "",
                }
    finally:
        conn.close()

    expected0 = min(stored, limit)
    expected1 = min(limit, max(0, stored - limit))
    consistent = page0 == expected0 and page1 == expected1 and stored == visible
    anchor = None
    if anchor_row:
        anchor = {"id": anchor_row[0], "timestamp": anchor_row[1], "from_me": bool(anchor_row[2])}
    plan = plan_history_backfill(canon, stored, anchor, HISTORY_BACKFILL_MAX)
    plan["coverage"] = _history_note(meta)
    if stored <= limit:
        interpretation = (
            f"The local store contains {stored} messages for this chat. "
            "An empty following page is expected and is not a pagination bug. "
            "Messages the phone never delivered to the linked device are not in this store."
        )
    else:
        interpretation = (
            f"The local store contains {stored} messages. Page 0 holds {page0} and page 1 holds {page1} "
            f"at limit {limit}."
        )
    if not consistent:
        interpretation = (
            "Pagination does not match the stored rows. "
            + interpretation
        )
    return {
        "chat_jid": canon,
        "stored_message_count": stored,
        "visible_message_count": visible,
        "limit": limit,
        "page0_count": page0,
        "page1_count": page1,
        "pagination_matches_store": consistent,
        "interpretation": interpretation,
        "history_meta": meta,
        "backfill": plan,
        "coverage": plan["coverage"],
    }


def _page_count(conn: sqlite3.Connection, chat_jid: str, limit: int, page: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT messages.id
            FROM messages
            LEFT JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ?
            ORDER BY messages.timestamp DESC, messages.id DESC
            LIMIT ? OFFSET ?
        )
        """,
        (chat_jid, limit, page * limit),
    ).fetchone()
    return row[0]


def message_ids_page(db_path: str, chat_jid: str, limit: int, page: int) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT messages.id
            FROM messages
            WHERE messages.chat_jid = ?
            ORDER BY messages.timestamp DESC, messages.id DESC
            LIMIT ? OFFSET ?
            """,
            (canonicalize_jid(chat_jid), limit, page * limit),
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]
