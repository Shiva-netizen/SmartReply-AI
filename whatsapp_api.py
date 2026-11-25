"""Send WhatsApp messages through Meta's Cloud API (Graph API)."""

import logging
import os
import re
import uuid

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("whatsapp")

META_ERRORS = {
    190: "the WhatsApp access token is invalid or expired. Use a permanent system user token.",
    131030: "this number is not on the test number's allowed list. Add it in Meta's API Setup page.",
    131047: "the 24-hour reply window is closed. The person must message again first.",
    131026: "the message could not be delivered to this number.",
    131056: "too many messages to this number in a short time. Wait a bit.",
    130429: "Meta's rate limit was reached. Try again later.",
}

CODE_100_HINT = (
    "Meta rejected the request. Check that WA_PHONE_NUMBER_ID is the Phone number ID "
    "(digits only, from API Setup) and that the system user has the WhatsApp account "
    "assigned with full control."
)


class WhatsAppError(Exception):
    """Readable problem to show to the user."""


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}


def _describe_error(status: int, err: dict) -> str:
    """Build a readable message and log Meta's full error (never the token or the text)."""
    code = err.get("code")
    subcode = err.get("error_subcode")
    message = str(err.get("message") or "unknown error")[:300]
    trace = err.get("fbtrace_id")
    log.error(
        "meta error | status=%s | code=%s | subcode=%s | message=%s | fbtrace_id=%s",
        status, code, subcode, message, trace,
    )
    friendly = META_ERRORS.get(code)
    if friendly is None and code == 100:
        friendly = CODE_100_HINT
    detail = f"Meta error {code or status}" + (f"/{subcode}" if subcode else "") + f": {message}"
    return f"{friendly} ({detail})" if friendly else detail


def send_text(to: str, body: str) -> str:
    """Send a text message. Returns Meta's message id, or raises WhatsAppError."""
    if _flag("WA_FAKE_SEND"):  # local testing only: nothing leaves your Mac
        log.info("fake send | to=%s", to)
        return f"wamid.FAKE_SENT_{uuid.uuid4().hex}"

    token = os.getenv("WA_ACCESS_TOKEN", "").strip()
    phone_id = os.getenv("WA_PHONE_NUMBER_ID", "").strip()
    version = os.getenv("WA_GRAPH_VERSION", "v23.0").strip()
    if not token or not phone_id:
        raise WhatsAppError("WA_ACCESS_TOKEN or WA_PHONE_NUMBER_ID is missing in .env")
    if not phone_id.isdigit():
        raise WhatsAppError(
            "WA_PHONE_NUMBER_ID in .env must be the Phone number ID (digits only) from Meta's "
            "API Setup page, not the phone number itself."
        )
    if not re.fullmatch(r"\d{8,15}", to or ""):
        raise WhatsAppError("the recipient number must be digits only with the country code.")

    try:
        r = requests.post(
            f"https://graph.facebook.com/{version}/{phone_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"preview_url": False, "body": body},
            },
            timeout=20,
        )
    except requests.RequestException as e:
        log.error("network error | to=%s | %s", to, type(e).__name__)
        raise WhatsAppError(f"could not reach Meta ({type(e).__name__}). Try again later.")

    try:
        data = r.json()
    except ValueError:
        data = {}

    if r.status_code != 200:
        err = data.get("error", {}) if isinstance(data, dict) else {}
        raise WhatsAppError(_describe_error(r.status_code, err if isinstance(err, dict) else {}))

    try:
        message_id = data["messages"][0]["id"]
    except (KeyError, IndexError, TypeError):
        raise WhatsAppError("Meta accepted the request but returned no message id.")
    log.info("sent | to=%s | id=%s", to, message_id)
    return message_id