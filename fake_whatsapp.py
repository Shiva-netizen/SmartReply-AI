"""Pretend to be Meta: send a signed fake WhatsApp message to your local webhook.

Examples:
  python fake_whatsapp.py "Hi, can you build me a website?"
  python fake_whatsapp.py "Old message" --hours-ago 30      (tests the closed 24h window)
  python fake_whatsapp.py "Hello" --from 918888888888 --name "Priya"
"""

import argparse
import hashlib
import hmac
import json
import os
import time
import uuid

import requests
from dotenv import load_dotenv

load_dotenv()

p = argparse.ArgumentParser()
p.add_argument("text")
p.add_argument("--from", dest="sender", default="919999999999")
p.add_argument("--name", default="Test Client")
p.add_argument("--hours-ago", type=float, default=0)
p.add_argument("--url", default="http://localhost:8000/webhook")
args = p.parse_args()

secret = os.getenv("WA_APP_SECRET")
if not secret:
    raise SystemExit("Set WA_APP_SECRET in .env (any value works for local testing).")

payload = {
    "object": "whatsapp_business_account",
    "entry": [{
        "id": "FAKE_WABA_ID",
        "changes": [{
            "field": "messages",
            "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "15550000000", "phone_number_id": "FAKE_PHONE_ID"},
                "contacts": [{"profile": {"name": args.name}, "wa_id": args.sender}],
                "messages": [{
                    "from": args.sender,
                    "id": f"wamid.FAKE_{uuid.uuid4().hex}",
                    "timestamp": str(int(time.time() - args.hours_ago * 3600)),
                    "type": "text",
                    "text": {"body": args.text},
                }],
            },
        }],
    }],
}
raw = json.dumps(payload).encode()
sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
r = requests.post(args.url, data=raw, headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig}, timeout=10)
print(r.status_code, r.text)