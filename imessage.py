"""Everything textbot knows about Messages: read chat.db, send an iMessage.

Reading needs Full Disk Access for the app that runs the script. Sending only needs
permission to control Messages, which macOS asks for the first time.
"""
import sqlite3
import subprocess
from pathlib import Path

DB = Path.home() / "Library/Messages/chat.db"

# The message text is passed in as an argument (argv), never pasted into this source,
# so a quote in the text can't break the script and a crafted message can't run AppleScript.
SEND_SCRIPT = """
on run argv
    tell application "Messages"
        set svc to 1st account whose service type = iMessage
        send (item 2 of argv) to participant (item 1 of argv) of svc
    end tell
end run
"""

# New incoming messages, 1:1 chats only. A 1:1 chat's identifier is the other person's
# handle; group chats have ids like "chat1234...", so they never match.
NEW_MESSAGES = """
SELECT m.ROWID, h.id, m.text, m.attributedBody
FROM message m
JOIN handle h ON m.handle_id = h.ROWID
JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
JOIN chat c ON c.ROWID = cmj.chat_id
WHERE m.ROWID > ? AND m.is_from_me = 0 AND m.associated_message_type = 0
  AND c.chat_identifier = h.id
ORDER BY m.ROWID
"""

# The last N messages of the 1:1 chat with one handle, both directions, newest first.
CONVERSATION = """
SELECT m.is_from_me, m.text, m.attributedBody
FROM message m
JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
JOIN chat c ON c.ROWID = cmj.chat_id
WHERE c.chat_identifier = ? AND m.ROWID <= ? AND m.associated_message_type = 0
ORDER BY m.ROWID DESC
LIMIT ?
"""


def send(handle, text):
    """Send `text` to a phone number or email over iMessage. Raises if osascript fails."""
    subprocess.run(["osascript", "-e", SEND_SCRIPT, handle, text],
                   check=True, capture_output=True, text=True, timeout=30)


def connect():
    """Open chat.db read-only: textbot never writes it."""
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def latest_id(db):
    """The newest message's ROWID, so a fresh start skips old history."""
    return db.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0]


def new_messages(db, after):
    """[(rowid, handle, text)] received in 1:1 chats after `after`. text can be None."""
    return [(rowid, handle, text or decode_body(body))
            for rowid, handle, text, body in db.execute(NEW_MESSAGES, (after,)).fetchall()]


def conversation(db, handle, upto, limit):
    """[(from_me, text)] for the last `limit` messages with `handle`, oldest first."""
    rows = db.execute(CONVERSATION, (handle, upto, limit)).fetchall()
    pairs = [(bool(from_me), text or decode_body(body)) for from_me, text, body in reversed(rows)]
    return [(from_me, text) for from_me, text in pairs if text]


def decode_body(blob):
    """Pull the plain text out of an attributedBody blob (an Apple 'typedstream')."""
    if not blob:
        return None
    i = blob.find(b"NSString")
    if i < 0:
        return None
    i += len(b"NSString") + 5          # skip the class header bytes, up to the '+' marker
    n = blob[i]; i += 1                # one length byte, or a marker for a longer length
    if n == 0x81:
        n = int.from_bytes(blob[i:i + 2], "little"); i += 2
    elif n == 0x82:
        n = int.from_bytes(blob[i:i + 4], "little"); i += 4
    return blob[i:i + n].decode("utf-8", errors="replace")
