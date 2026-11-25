"""Check (and optionally create) the link between your Meta app and your WhatsApp Business Account.

  python check_meta.py              -> shows the current link
  python check_meta.py --subscribe  -> creates the link
"""

import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv(".env")

version = os.getenv("WA_GRAPH_VERSION", "v23.0")
waba_id = os.getenv("WA_WABA_ID", "").strip()
token = os.getenv("WA_ACCESS_TOKEN", "").strip()
if not waba_id or not token:
    sys.exit("WA_WABA_ID or WA_ACCESS_TOKEN is missing in .env")

url = f"https://graph.facebook.com/{version}/{waba_id}/subscribed_apps"
headers = {"Authorization": f"Bearer {token}"}

if "--subscribe" in sys.argv:
    print("Subscribing:", requests.post(url, headers=headers, timeout=20).json())
print("Current link:", requests.get(url, headers=headers, timeout=20).json())