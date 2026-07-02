"""Fetches Sigen data export emails from Gmail via IMAP.

Looks for unread messages with ZIP attachments, downloads them,
and marks them as read.
"""

import os
import sys
import logging
import email
from email.header import decode_header
from pathlib import Path
from datetime import datetime

from imapclient import IMAPClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("mail")

DOWNLOAD_DIR = Path(__file__).parent.parent / "data" / "sigen_downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


def decode_str(s):
    """Decode email header that may be RFC2047 encoded."""
    if s is None:
        return ""
    if isinstance(s, bytes):
        s = s.decode("utf-8", errors="replace")
    parts = decode_header(s)
    return "".join(
        (b.decode(c or "utf-8", errors="replace") if isinstance(b, bytes) else b)
        for b, c in parts
    )


def fetch_sigen_mails():
    user = os.environ.get("GMAIL_USER")
    pwd = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not pwd:
        log.error("GMAIL_USER and GMAIL_APP_PASSWORD must be set")
        return 1

    log.info("Connecting to imap.gmail.com as %s...", user)
    try:
        with IMAPClient("imap.gmail.com", ssl=True) as server:
            server.login(user, pwd)
            server.select_folder("INBOX")

            # Search unread mails likely from Sigen
            messages = server.search([
                "UNSEEN",
                "OR", "OR", "OR",
                "FROM", "sigen",
                "FROM", "sigencloud",
                "SUBJECT", "stationData",
                "SUBJECT", "Sigen",
            ])

            log.info("Found %d candidate messages", len(messages))

            saved_files = []

            for uid in messages:
                msg_data = server.fetch([uid], ["RFC822"])
                raw = msg_data[uid][b"RFC822"]
                msg = email.message_from_bytes(raw)

                subject = decode_str(msg.get("Subject", ""))
                sender = decode_str(msg.get("From", ""))
                log.info("Processing UID %d | %s | %s", uid, sender, subject)

                found_zip = False
                for part in msg.walk():
                    disposition = str(part.get("Content-Disposition", ""))
                    if "attachment" not in disposition.lower():
                        continue
                    filename = decode_str(part.get_filename() or "")
                    if not filename.lower().endswith(".zip"):
                        continue

                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue

                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    safe_filename = f"{ts}_{filename}"
                    target = DOWNLOAD_DIR / safe_filename
                    target.write_bytes(payload)
                    log.info("  Saved %s (%d bytes)", safe_filename, len(payload))
                    saved_files.append(target)
                    found_zip = True

                if found_zip:
                    server.add_flags([uid], [b"\\Seen"])
                    log.info("  Marked UID %d as read", uid)
                else:
                    log.info("  No ZIP attachment, leaving unread")

            log.info("Done. %d ZIP file(s) saved.", len(saved_files))
            return 0

    except Exception as e:
        log.error("IMAP failed: %s", e)
        return 2


if __name__ == "__main__":
    sys.exit(fetch_sigen_mails())
