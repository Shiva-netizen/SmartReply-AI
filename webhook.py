"""FastAPI server that Meta calls for every WhatsApp message. Run: uvicorn webhook:app --port 8000"""

import hashlib
import hmac
import json
import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse

import whatsapp_store as store

load_dotenv()

logging.basicConfig(
    filename="whatsapp_webhook.log",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("whatsapp_webhook")  # never logs message text

app = FastAPI()


def signature_ok(raw: bytes, header: str) -> bool:
    secret = os.getenv("WA_APP_SECRET", "")
    if not secret or not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def describe(m: dict) -> str:
    kind = m.get("type", "unknown")
    if kind == "text":
        return (m.get("text") or {}).get("body", "")
    if kind in ("image", "video", "document", "audio", "sticker"):
        media = m.get(kind) or {}
        extra = media.get("caption") or media.get("filename") or ""
        return f"[{kind} received{': ' + extra if extra else ''}]"
    if kind == "button":
        return (m.get("button") or {}).get("text", "")
    return f"[{kind} message, not supported yet]"


def handle_payload(payload: dict) -> int:
    """Save new messages and delivery statuses. Returns how many new messages were saved."""
    saved = 0
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in value.get("contacts") or []}
            for m in value.get("messages") or []:
                sender = m.get("from")
                if store.save_inbound(m.get("id"), sender, names.get(sender, ""), describe(m),
                                      m.get("timestamp"), m.get("type", "unknown")):
                    saved += 1
                    log.info("message saved | from=%s | type=%s", sender, m.get("type"))
            for s in value.get("statuses") or []:
                errors = s.get("errors") or [{}]
                store.update_status(s.get("id"), s.get("status", ""), errors[0].get("title", ""))
    return saved


@app.get("/webhook")
def verify(request: Request):
    """One-time handshake when you click 'Verify and save' in Meta."""
    p = request.query_params
    expected = os.getenv("WA_VERIFY_TOKEN", "").strip().strip('"').strip("'")
    received = p.get("hub.verify_token", "").strip()
    if not expected:
        reason = "WA_VERIFY_TOKEN is empty in .env (or uvicorn was started from another folder)"
    elif p.get("hub.mode") != "subscribe":
        reason = f"hub.mode was {p.get('hub.mode')!r}, expected 'subscribe'"
    elif not hmac.compare_digest(received.encode(), expected.encode()):
        reason = f"token mismatch (Meta sent {len(received)} characters, .env has {len(expected)})"
    else:
        log.info("verify ok")
        return PlainTextResponse(p.get("hub.challenge", ""))
    log.warning("verify failed | %s", reason)
    print(f"WEBHOOK VERIFY FAILED: {reason}", flush=True)
    return Response(status_code=403)


@app.post("/webhook")
async def receive(request: Request):
    raw = await request.body()
    if not signature_ok(raw, request.headers.get("X-Hub-Signature-256", "")):
        log.warning("rejected request with bad signature")
        return Response(status_code=403)
    try:
        payload = json.loads(raw)
    except ValueError:
        return Response(status_code=400)
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return {"ok": True, "saved": 0}
    try:
        saved = handle_payload(payload)
    except Exception:
        log.exception("could not save payload")
        return Response(status_code=500)  # Meta retries; duplicates are ignored
    return {"ok": True, "saved": saved}