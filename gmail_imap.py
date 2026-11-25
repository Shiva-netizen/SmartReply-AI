"""Read-only Gmail access over IMAP (never marks emails as read)."""

import imaplib
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from email import message_from_bytes, policy
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

from dotenv import load_dotenv

load_dotenv()

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
MAX_BODY_CHARS = 3000   # keeps token usage and Groq rate limits under control
MAX_LIST = 20
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class MailError(Exception):
    """Readable problem to show to the user."""


# ---------- text cleaning ----------
class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6"}
    HIDDEN = {"script", "style", "head"}

    def __init__(self):
        super().__init__()
        self.parts, self._hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.HIDDEN:
            self._hidden += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.HIDDEN:
            self._hidden = max(0, self._hidden - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._hidden:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _HTMLText()
    parser.feed(html)
    return "".join(parser.parts)


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.split("\n") if not ln.lstrip().startswith(">")]  # drop quoted replies
    text = "\n".join(lines)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------- connection ----------
@contextmanager
def _session():
    user = os.getenv("SENDER_EMAIL")
    password = (os.getenv("SENDER_PASSWORD") or "").replace(" ", "")
    if not user or not password:
        raise MailError("SENDER_EMAIL or SENDER_PASSWORD is missing in .env")
    m = None
    try:
        m = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=30)
        m.login(user, password)
        typ, _ = m.select("INBOX", readonly=True)  # readonly = can never change flags
        if typ != "OK":
            raise MailError("Could not open the inbox.")
        yield m
    except imaplib.IMAP4.error:
        raise MailError(
            "Gmail rejected the IMAP login. Check the app password, and that IMAP is "
            "enabled in Gmail settings (Forwarding and POP/IMAP)."
        )
    except (OSError, TimeoutError) as e:
        raise MailError(f"Could not reach Gmail ({type(e).__name__}).")
    finally:
        if m is not None:
            try:
                m.logout()
            except Exception:
                pass


def _quote(term: str) -> str:
    term = re.sub(r'["\\\r\n]', " ", term).strip()
    if not term.isascii():
        raise MailError("Search terms must use plain English letters and numbers.")
    return f'"{term}"'


# ---------- listing ----------
def list_emails(limit=10, unread_only=False, sender="", subject="", text="", since_days=0):
    """Newest first. Returns a list of dicts (uid, unread, from_name, from_addr, subject, date)."""
    limit = max(1, min(int(limit or 10), MAX_LIST))
    criteria = []
    if unread_only:
        criteria.append("UNSEEN")
    if sender:
        criteria += ["FROM", _quote(sender)]
    if subject:
        criteria += ["SUBJECT", _quote(subject)]
    if text:
        criteria += ["TEXT", _quote(text)]
    if since_days and int(since_days) > 0:
        d = datetime.now() - timedelta(days=int(since_days))
        criteria += ["SINCE", f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"]
    if not criteria:
        criteria = ["ALL"]

    with _session() as m:
        typ, data = m.uid("SEARCH", None, *criteria)
        if typ != "OK":
            raise MailError("Inbox search failed.")
        uids = [u.decode() for u in data[0].split()][-limit:][::-1]
        if not uids:
            return []
        typ, data = m.uid("FETCH", ",".join(uids), "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        if typ != "OK":
            raise MailError("Could not read the email headers.")

    found = {}
    for i, item in enumerate(data):
        if not isinstance(item, tuple):
            continue
        trailer = data[i + 1] if i + 1 < len(data) and isinstance(data[i + 1], bytes) else b""
        meta = item[0].decode(errors="ignore") + " " + trailer.decode(errors="ignore")
        mu = re.search(r"UID (\d+)", meta)
        if not mu:
            continue
        msg = message_from_bytes(item[1], policy=policy.default)
        name, addr = parseaddr(str(msg.get("From", "")))
        found[mu.group(1)] = {
            "uid": mu.group(1),
            "unread": "\\Seen" not in meta,
            "from_name": name or addr,
            "from_addr": addr,
            "subject": " ".join(str(msg.get("Subject", "(no subject)")).split()),
            "date": _short_date(str(msg.get("Date", ""))),
        }
    return [found[u] for u in uids if u in found]


def _short_date(value: str) -> str:
    try:
        return parsedate_to_datetime(value).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return value[:25]


# ---------- reading one email ----------
def get_email(uid) -> dict:
    uid = str(uid).strip()
    if not uid.isdigit():
        raise MailError("Invalid email id.")
    with _session() as m:
        typ, data = m.uid("FETCH", uid, "(BODY.PEEK[])")
    raw = next((i[1] for i in (data or []) if isinstance(i, tuple)), None)
    if typ != "OK" or not raw:
        raise MailError(f"Email {uid} was not found.")

    msg = message_from_bytes(raw, policy=policy.default)
    text = ""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is not None:
        try:
            content = part.get_content()
        except Exception:
            content = ""
        text = html_to_text(content) if part.get_content_type() == "text/html" else content
    text = clean_text(text)

    try:
        attachments = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
    except Exception:
        attachments = []

    from_name, from_addr = parseaddr(str(msg.get("From", "")))
    _, reply_addr = parseaddr(str(msg.get("Reply-To", "")))
    return {
        "uid": uid,
        "message_id": str(msg.get("Message-ID", "")).strip(),
        "references": " ".join(str(msg.get("References", "")).split()),
        "from_name": from_name or from_addr,
        "from_addr": from_addr,
        "reply_to": reply_addr or from_addr,
        "subject": " ".join(str(msg.get("Subject", "(no subject)")).split()),
        "date": _short_date(str(msg.get("Date", ""))),
        "body": text[:MAX_BODY_CHARS],
        "truncated": len(text) > MAX_BODY_CHARS,
        "attachments": attachments,
    }