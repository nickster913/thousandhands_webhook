"""
Thousand Hands WhatsApp webhook.
Receives WhatsApp Flow submissions and stores them in Postgres.

Railway env vars:
  DATABASE_URL     - set automatically when you add Postgres and reference it
  VERIFY_TOKEN     - any string; paste the same one into Meta's webhook settings
  APP_SECRET       - your Meta app secret (App settings > Basic)
  WA_TOKEN         - WhatsApp Cloud API access token
  WA_PHONE_ID      - your WhatsApp phone number ID
  NOTIFY_WA_ID     - your own WhatsApp number, e.g. 6591234567, to get pinged per lead
  FLOW_ID_RENOVATION, FLOW_ID_TUITION, FLOW_ID_HOME_SERVICES - flow IDs from WhatsApp Manager
Start command: gunicorn app:app
"""
import hashlib, hmac, json, logging, os

import psycopg
import requests
from flask import Flask, abort, request
from psycopg.types.json import Jsonb

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
GRAPH = "https://graph.facebook.com/v21.0"


def send_text(to, text):
    r = requests.post(
        f"{GRAPH}/{os.environ['WA_PHONE_ID']}/messages",
        headers={"Authorization": f"Bearer {os.environ['WA_TOKEN']}"},
        json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}},
        timeout=10,
    )
    if not r.ok:
        logging.error("send failed %s %s", r.status_code, r.text)


# Phrase in the landing page's pre-filled WhatsApp text -> (industry, env var holding the flow ID).
# Must match the wa.me ?text= on each live page.
FLOWS = {
    "renovation agent": ("renovation", "FLOW_ID_RENOVATION"),
    "tuition centre agent": ("tuition", "FLOW_ID_TUITION"),
    "missed-call recovery": ("home_services", "FLOW_ID_HOME_SERVICES"),
}

FALLBACK = ("Thanks for reaching out! Which best describes you: a renovation firm, "
            "a tuition centre, or a home-services company (aircon, plumbing, pest control, movers)?")

# Also route people who answer the fallback question in their own words.
KEYWORDS = {
    "renovation": ("renovation", "interior", "reno", "id firm"),
    "tuition": ("tuition", "enrichment", "centre", "center", "school"),
    "home_services": ("aircon", "plumb", "pest", "mover", "moving", "home service"),
}


def send_flow(to, industry, flow_id):
    r = requests.post(
        f"{GRAPH}/{os.environ['WA_PHONE_ID']}/messages",
        headers={"Authorization": f"Bearer {os.environ['WA_TOKEN']}"},
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "flow",
                "body": {"text": "Tell us about your business and we'll send your "
                                 "personalised demo within 24 hours."},
                "action": {
                    "name": "flow",
                    "parameters": {
                        "flow_message_version": "3",
                        "flow_id": flow_id,
                        "flow_token": industry,
                        "flow_cta": "Get my demo",
                        "flow_action": "navigate",
                        "flow_action_payload": {"screen": "SETUP"},
                    },
                },
            },
        },
        timeout=10,
    )
    if not r.ok:
        logging.error("flow send failed %s %s", r.status_code, r.text)


def route_text(wa_id, text):
    text = text.lower()
    industry = None
    for phrase, (ind, _) in FLOWS.items():
        if phrase in text:
            industry = ind
            break
    if industry is None:
        for ind, words in KEYWORDS.items():
            if any(w in text for w in words):
                industry = ind
                break
    if industry is None:
        send_text(wa_id, FALLBACK)
        return
    env = {ind: env for ind, env in FLOWS.values()}[industry]
    send_flow(wa_id, industry, os.environ[env])


def save_lead(wa_id, answers):
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute(
            """INSERT INTO leads (wa_id, industry, business_name, team_size, monthly_volume,
                                  biggest_leak, booking_system, consent, raw)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                wa_id,
                answers.get("industry"),
                answers.get("business_name"),
                answers.get("team_size"),
                answers.get("monthly_volume"),
                answers.get("biggest_leak"),
                answers.get("booking_system"),
                str(answers.get("consent")).lower() == "true",
                Jsonb(answers),
            ),
        )


def valid_signature(req):
    """Reject anything not signed by Meta with your app secret."""
    sig = req.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(
        os.environ["APP_SECRET"].encode(), req.get_data(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(sig, expected)


@app.get("/webhook")
def verify():
    # Meta calls this once when you save the webhook URL.
    if (request.args.get("hub.mode") == "subscribe"
            and request.args.get("hub.verify_token") == os.environ["VERIFY_TOKEN"]):
        return request.args.get("hub.challenge", ""), 200
    abort(403)


@app.post("/webhook")
def receive():
    if not valid_signature(request):
        abort(401)
    body = request.get_json(silent=True) or {}
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                try:
                    handle(msg)
                except Exception:
                    logging.exception("failed to handle message %s", msg.get("id"))
    return "ok", 200  # always 200 so Meta doesn't retry-storm


def handle(msg):
    interactive = msg.get("interactive", {})
    if msg.get("type") == "interactive" and interactive.get("type") == "nfm_reply":
        answers = json.loads(interactive["nfm_reply"]["response_json"])
        wa_id = msg["from"]
        save_lead(wa_id, answers)
        name = answers.get("business_name") or "there"
        send_text(wa_id, f"Thanks, {name}. We'll send your personalised demo within 24 hours.")
        if os.environ.get("NOTIFY_WA_ID"):
            send_text(os.environ["NOTIFY_WA_ID"],
                      f"New lead ({answers.get('industry')}): {name}, wa {wa_id}, "
                      f"leak: {answers.get('biggest_leak')}. Demo due in 24h.")
        return
    if msg.get("type") == "text":
        route_text(msg["from"], msg["text"]["body"])


@app.get("/")
def health():
    return "ok"