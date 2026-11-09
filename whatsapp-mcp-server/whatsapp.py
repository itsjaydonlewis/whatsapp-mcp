import sqlite3
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, List, Tuple
import os.path
import requests
import json
import audio
import identity

MESSAGES_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store', 'messages.db')
WHATSAPP_API_BASE_URL = "http://localhost:8080/api"
_messages_db_path = None

def set_messages_db_path(path: Optional[str]) -> None:
    global _messages_db_path
    _messages_db_path = path

def messages_db_path() -> str:
    return _messages_db_path or MESSAGES_DB_PATH

def connect_messages():
    return sqlite3.connect(messages_db_path())

@dataclass
class Message:
    timestamp: datetime
    sender: str
    content: str
    is_from_me: bool
    chat_jid: str
    id: str
    chat_name: Optional[str] = None
    media_type: Optional[str] = None

@dataclass
class Chat:
    jid: str
    name: Optional[str]
    last_message_time: Optional[datetime]
    last_message: Optional[str] = None
    last_sender: Optional[str] = None
    last_is_from_me: Optional[bool] = None
    alternate_jid: Optional[str] = None
    name_source: Optional[str] = None
    identity_status: Optional[str] = None
    stored_message_count: Optional[int] = None

    @property
    def is_group(self) -> bool:
        """Determine if chat is a group based on JID pattern."""
        return self.jid.endswith("@g.us")

@dataclass
class Contact:
    phone_number: str
    name: Optional[str]
    jid: str
    name_source: Optional[str] = None
    identity_status: Optional[str] = None
    alternate_jid: Optional[str] = None
    pn_jid: Optional[str] = None
    lid_jid: Optional[str] = None

@dataclass
class MessageContext:
    message: Message
    before: List[Message]
    after: List[Message]

def get_sender_name(sender_jid: str) -> str:
    try:
        directory = identity.load_directory(messages_db_path())
        return identity.resolve_sender_label(directory, sender_jid)
    except sqlite3.Error as e:
        print(f"Database error while getting sender name: {e}")
        return sender_jid

def _directory():
    return identity.load_directory(messages_db_path())

def _present_chat(chat: Optional[Chat]) -> Optional[Chat]:
    if chat is None:
        return None
    directory = _directory()
    name, source, status = identity.display_chat_name(directory, chat.jid, chat.name)
    chat.name = name
    chat.name_source = source
    chat.identity_status = status
    return chat

def format_message(message: Message, show_chat_info: bool = True) -> None:
    """Print a single message with consistent formatting."""
    output = ""
    
    if show_chat_info and message.chat_name:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] Chat: {message.chat_name} "
    else:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] "
        
    content_prefix = ""
    if hasattr(message, 'media_type') and message.media_type:
        content_prefix = f"[{message.media_type} - Message ID: {message.id} - Chat JID: {message.chat_jid}] "
    
    try:
        sender_name = get_sender_name(message.sender) if not message.is_from_me else "Me"
        output += f"From: {sender_name}: {content_prefix}{message.content}\n"
    except Exception as e:
        print(f"Error formatting message: {e}")
    return output

def format_messages_list(messages: List[Message], show_chat_info: bool = True) -> None:
    output = ""
    if not messages:
        output += "No messages to display."
        return output
    
    for message in messages:
        output += format_message(message, show_chat_info)
    return output

def list_messages(
    after: Optional[str] = None,
    before: Optional[str] = None,
    sender_phone_number: Optional[str] = None,
    chat_jid: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1
) -> List[Message]:
    """Get messages matching the specified criteria with optional context."""
    try:
        conn = connect_messages()
        cursor = conn.cursor()
        
        # Build base query
        query_parts = ["SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, messages.chat_jid, messages.id, messages.media_type FROM messages"]
        query_parts.append("LEFT JOIN chats ON messages.chat_jid = chats.jid")
        where_clauses = []
        params = []
        
        # Add filters
        if after:
            try:
                after = datetime.fromisoformat(after)
            except ValueError:
                raise ValueError(f"Invalid date format for 'after': {after}. Please use ISO-8601 format.")
            
            where_clauses.append("messages.timestamp > ?")
            params.append(after)

        if before:
            try:
                before = datetime.fromisoformat(before)
            except ValueError:
                raise ValueError(f"Invalid date format for 'before': {before}. Please use ISO-8601 format.")
            
            where_clauses.append("messages.timestamp < ?")
            params.append(before)

        if sender_phone_number:
            sender_values = identity.sender_match_values(_directory(), sender_phone_number)
            if sender_values:
                placeholders = ",".join("?" for _ in sender_values)
                where_clauses.append(f"messages.sender IN ({placeholders})")
                params.extend(sender_values)
            else:
                where_clauses.append("messages.sender = ?")
                params.append(sender_phone_number)
            
        if chat_jid:
            where_clauses.append("messages.chat_jid = ?")
            params.append(chat_jid)
            
        if query:
            where_clauses.append("LOWER(messages.content) LIKE LOWER(?)")
            params.append(f"%{query}%")
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Stable order so tied timestamps cannot skip or repeat rows across pages.
        offset = page * limit
        query_parts.append("ORDER BY messages.timestamp DESC, messages.id DESC")
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        messages = cursor.fetchall()
        directory = _directory()
        
        result = []
        for msg in messages:
            message = Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=identity.display_chat_name(directory, msg[5], msg[2])[0],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            )
            result.append(message)
            
        if include_context and result:
            # Add context for each message
            messages_with_context = []
            for msg in result:
                context = get_message_context(msg.id, context_before, context_after)
                messages_with_context.extend(context.before)
                messages_with_context.append(context.message)
                messages_with_context.extend(context.after)
            
            return format_messages_list(messages_with_context, show_chat_info=True)
            
        # Format and display messages without context
        return format_messages_list(result, show_chat_info=True)    
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5
) -> MessageContext:
    """Get context around a specific message."""
    try:
        conn = connect_messages()
        cursor = conn.cursor()
        
        # Get the target message first
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, messages.chat_jid, messages.id, messages.chat_jid, messages.media_type
            FROM messages
            LEFT JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.id = ?
        """, (message_id,))
        msg_data = cursor.fetchone()
        
        if not msg_data:
            raise ValueError(f"Message with ID {message_id} not found")
            
        target_message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[8]
        )
        
        # Get messages before
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, messages.chat_jid, messages.id, messages.media_type
            FROM messages
            LEFT JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp < ?
            ORDER BY messages.timestamp DESC, messages.id DESC
            LIMIT ?
        """, (msg_data[7], msg_data[0], before))
        
        before_messages = []
        for msg in cursor.fetchall():
            before_messages.append(Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            ))
        
        # Get messages after
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, messages.chat_jid, messages.id, messages.media_type
            FROM messages
            LEFT JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp > ?
            ORDER BY messages.timestamp ASC, messages.id ASC
            LIMIT ?
        """, (msg_data[7], msg_data[0], after))
        
        after_messages = []
        for msg in cursor.fetchall():
            after_messages.append(Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            ))
        
        return MessageContext(
            message=target_message,
            before=before_messages,
            after=after_messages
        )
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        raise
    finally:
        if 'conn' in locals():
            conn.close()


def _last_message_join(chat_alias="chats", message_alias="messages"):
    return f"""
        LEFT JOIN messages {message_alias} ON {message_alias}.id = (
            SELECT m2.id FROM messages m2
            WHERE m2.chat_jid = {chat_alias}.jid
              AND m2.timestamp = {chat_alias}.last_message_time
            ORDER BY m2.id DESC
            LIMIT 1
        )
    """

def _chat_from_row(chat_data) -> Chat:
    return Chat(
        jid=chat_data[0],
        name=chat_data[1],
        last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
        last_message=chat_data[3],
        last_sender=chat_data[4],
        last_is_from_me=chat_data[5],
    )

def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active"
) -> List[Chat]:
    """Get chats matching the specified criteria."""
    try:
        conn = connect_messages()
        cursor = conn.cursor()
        order_by = "chats.last_message_time DESC" if sort_by == "last_active" else "chats.name"
        sql = f"""
            SELECT
                chats.jid,
                chats.name,
                chats.last_message_time,
                messages.content as last_message,
                messages.sender as last_sender,
                messages.is_from_me as last_is_from_me
            FROM chats
            {_last_message_join()}
            ORDER BY {order_by}
        """
        cursor.execute(sql)
        rows = cursor.fetchall()
        directory = _directory()
        result = []
        needle = (query or "").strip().casefold()
        for chat_data in rows:
            chat = _present_chat(_chat_from_row(chat_data))
            if needle:
                haystack = " ".join([
                    chat.name or "",
                    chat.jid or "",
                    chat_data[1] or "",
                ]).casefold()
                if needle not in haystack:
                    continue
            if not include_last_message:
                chat.last_message = None
                chat.last_sender = None
                chat.last_is_from_me = None
            result.append(chat)
        offset = page * limit
        return result[offset:offset + limit]
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def search_contacts(query: str) -> List[Contact]:
    """Search contacts by name or exact phone number."""
    try:
        directory = _directory()
        rows = identity.search_contact_records(directory, query)
        return [Contact(**row) for row in rows]
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []


def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> List[Chat]:
    """Get all chats involving the contact.

    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    try:
        directory = _directory()
        values = identity.sender_match_values(directory, jid)
        if not values:
            return []
        conn = connect_messages()
        cursor = conn.cursor()
        placeholders = ",".join("?" for _ in values)
        cursor.execute(f"""
            SELECT
                c.jid,
                c.name,
                c.last_message_time,
                messages.content as last_message,
                messages.sender as last_sender,
                messages.is_from_me as last_is_from_me
            FROM chats c
            {_last_message_join("c")}
            WHERE c.jid IN ({placeholders})
               OR c.jid IN (
                    SELECT DISTINCT m.chat_jid FROM messages m
                    WHERE m.sender IN ({placeholders})
               )
            ORDER BY c.last_message_time DESC, c.jid
        """, tuple(values + values))
        rows = cursor.fetchall()
        chats = [_present_chat(_chat_from_row(row)) for row in rows]
        offset = page * limit
        return chats[offset:offset + limit]
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_last_interaction(jid: str) -> str:
    """Get most recent message involving the contact."""
    try:
        directory = _directory()
        values = identity.sender_match_values(directory, jid)
        if not values:
            return None
        conn = connect_messages()
        cursor = conn.cursor()
        placeholders = ",".join("?" for _ in values)
        cursor.execute(f"""
            SELECT
                m.timestamp,
                m.sender,
                c.name,
                m.content,
                m.is_from_me,
                m.chat_jid,
                m.id,
                m.media_type
            FROM messages m
            LEFT JOIN chats c ON m.chat_jid = c.jid
            WHERE m.sender IN ({placeholders}) OR m.chat_jid IN ({placeholders})
            ORDER BY m.timestamp DESC, m.id DESC
            LIMIT 1
        """, tuple(values + values))
        msg_data = cursor.fetchone()
        if not msg_data:
            return None
        message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=identity.display_chat_name(directory, msg_data[5], msg_data[2])[0],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[7]
        )
        return format_message(message)
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_chat(chat_jid: str, include_last_message: bool = True) -> Optional[Chat]:
    """Get chat metadata by JID."""
    try:
        conn = connect_messages()
        cursor = conn.cursor()
        canon = identity.canonicalize_jid(chat_jid)
        cursor.execute(f"""
            SELECT
                c.jid,
                c.name,
                c.last_message_time,
                messages.content as last_message,
                messages.sender as last_sender,
                messages.is_from_me as last_is_from_me
            FROM chats c
            {_last_message_join("c")}
            WHERE c.jid = ?
        """, (canon,))
        chat_data = cursor.fetchone()
        if not chat_data:
            return None
        chat = _present_chat(_chat_from_row(chat_data))
        count = cursor.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_jid = ?",
            (canon,),
        ).fetchone()[0]
        chat.stored_message_count = count
        if not include_last_message:
            chat.last_message = None
            chat.last_sender = None
            chat.last_is_from_me = None
        return chat
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_direct_chat_by_contact(sender_phone_number: str) -> Optional[Chat]:
    """Get the direct chat for an exact phone number, following an explicit LID link."""
    try:
        directory = _directory()
        jids = identity.direct_chat_jids(directory, sender_phone_number)
        if not jids:
            return None
        conn = connect_messages()
        cursor = conn.cursor()
        placeholders = ",".join("?" for _ in jids)
        cursor.execute(f"""
            SELECT
                c.jid,
                c.name,
                c.last_message_time,
                messages.content as last_message,
                messages.sender as last_sender,
                messages.is_from_me as last_is_from_me
            FROM chats c
            {_last_message_join("c")}
            WHERE c.jid IN ({placeholders})
              AND c.jid NOT LIKE '%@g.us'
            ORDER BY c.last_message_time DESC, c.jid
        """, tuple(jids))
        rows = cursor.fetchall()
        if not rows:
            return None
        chat = _present_chat(_chat_from_row(rows[0]))
        if len(rows) > 1:
            chat.alternate_jid = rows[1][0]
        return chat
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()

def send_message(recipient: str, message: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "message": message,
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_file(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_audio_message(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"

        if not media_path.endswith(".ogg"):
            try:
                media_path = audio.convert_to_opus_ogg_temp(media_path)
            except Exception as e:
                return False, f"Error converting file to opus ogg. You likely need to install ffmpeg: {str(e)}"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def download_media(message_id: str, chat_jid: str) -> Optional[str]:
    """Download media from a message and return the local file path.
    
    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message
    
    Returns:
        The local file path if download was successful, None otherwise
    """
    try:
        url = f"{WHATSAPP_API_BASE_URL}/download"
        payload = {
            "message_id": message_id,
            "chat_jid": chat_jid
        }
        
        response = requests.post(url, json=payload)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("success", False):
                path = result.get("path")
                print(f"Media downloaded successfully: {path}")
                return path
            else:
                print(f"Download failed: {result.get('message', 'Unknown error')}")
                return None
        else:
            print(f"Error: HTTP {response.status_code} - {response.text}")
            return None
            
    except requests.RequestException as e:
        print(f"Request error: {str(e)}")
        return None
    except json.JSONDecodeError:
        print(f"Error parsing response: {response.text}")
        return None
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        return None

def get_chat_history_status(chat_jid: str, limit: int = 100) -> dict:
    """Compare stored messages with pagination and describe on-demand backfill limits."""
    return identity.chat_history_status(messages_db_path(), chat_jid, limit=limit)

def refresh_identity_cache() -> Tuple[bool, str]:
    """Ask the local bridge to reread its contact and LID stores. Sends nothing."""
    try:
        response = requests.post(f"{WHATSAPP_API_BASE_URL}/identities/refresh", json={}, timeout=60)
        if response.status_code == 200:
            return True, response.text
        return False, f"Error: HTTP {response.status_code} - {response.text}"
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"

def request_history_backfill(chat_jid: str, count: int = 50, dry_run: bool = True) -> dict:
    """Plan or send one on-demand history request.

    dry_run defaults to true and does not contact the bridge. An explicit
    dry_run of false sends one peer history request via the bridge. It is not
    a chat message, and it does not loop.
    """
    status = get_chat_history_status(chat_jid, limit=100)
    plan = status["backfill"]
    if count and count > 0:
        plan = identity.plan_history_backfill(
            chat_jid,
            status["stored_message_count"],
            {
                "id": plan.get("anchor_id"),
                "timestamp": plan.get("anchor_timestamp"),
                "from_me": plan.get("anchor_from_me"),
            } if plan.get("anchor_id") else None,
            count,
        )
        plan["coverage"] = status["coverage"]
    result = {
        "success": True,
        "executed": False,
        "dry_run": dry_run,
        "message": "Dry run only. No history request was sent.",
        "plan": plan,
        "stored_message_count": status["stored_message_count"],
        "pagination_matches_store": status["pagination_matches_store"],
        "coverage": status["coverage"],
    }
    if dry_run:
        return result
    if not plan.get("can_request"):
        result["success"] = False
        result["message"] = plan.get("block_reason") or "History backfill cannot be requested."
        return result
    try:
        response = requests.post(
            f"{WHATSAPP_API_BASE_URL}/history-backfill",
            json={"chat_jid": chat_jid, "count": count, "dry_run": False},
            timeout=60,
        )
        body = response.json()
    except requests.RequestException as e:
        result["success"] = False
        result["message"] = f"Request error: {str(e)}"
        return result
    except json.JSONDecodeError:
        result["success"] = False
        result["message"] = f"Error parsing response: {response.text}"
        return result
    result["success"] = bool(body.get("success")) and response.status_code == 200
    result["executed"] = bool(body.get("executed"))
    result["message"] = body.get("message", "")
    if body.get("plan"):
        result["plan"] = body["plan"]
    return result
