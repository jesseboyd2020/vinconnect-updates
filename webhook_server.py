"""
DealPulse — Gumroad Webhook Server
====================================
Receives purchase notifications from Gumroad and automatically
emails a license key to the buyer within seconds.

Setup:
  1. pip install flask requests
  2. Set environment variables (see CONFIGURATION below)
  3. Deploy to Railway.app (free)
  4. Paste your Railway URL into Gumroad Settings > Webhooks

Environment Variables (set in Railway dashboard):
  GMAIL_ADDRESS     — your Gmail address (e.g. jesse@gmail.com)
  GMAIL_APP_PASS    — Gmail App Password (16 chars, no spaces)
  GUMROAD_SECRET   — your Gumroad webhook secret (optional but recommended)

Run locally for testing:
  python webhook_server.py
"""

import os
import hmac
import hashlib
import logging
import csv
import smtplib
import json
from datetime import date, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, request, jsonify

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dealpulse")

app = Flask(__name__)

# ── Configuration (from environment variables) ────────────────────────────────
GMAIL_ADDRESS   = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASS  = os.environ.get("GMAIL_APP_PASS", "")
GUMROAD_SECRET  = os.environ.get("GUMROAD_SECRET", "")
SALES_LOG       = os.path.join(os.path.dirname(__file__), "sales_log.csv")

# ── DealPulse License Engine (copy of license.py logic, self-contained) ──────
import struct, secrets as _secrets, re as _re
from hmac import new as _hmac_new

_SECRET_KEY = b"1b15a2af42c2b816ab4dbb50057ab02e43439b96d2436cd3618a814ac08eb6a3"
_EPOCH      = date(2026, 1, 1)
_PLANS      = {"Solo": 0, "Team": 1, "Dealership": 2}

def _generate_key(expiry_date: date, plan: str = "Solo") -> str:
    plan_byte = _PLANS.get(plan, 0)
    days      = (expiry_date - _EPOCH).days
    salt      = os.urandom(3)
    payload   = struct.pack(">IB", days, plan_byte) + salt
    mac       = _hmac_new(_SECRET_KEY, payload, hashlib.sha256).digest()[:4]
    raw       = payload + mac
    hex_str   = raw.hex().upper()
    groups    = [hex_str[i:i+6] for i in range(0, 24, 6)]
    return "DP-" + "-".join(groups)

# ── Plan detection from Gumroad variant name ──────────────────────────────────
def _detect_plan(variant_name: str) -> tuple:
    """Returns (plan_str, expiry_date) based on Gumroad variant purchased."""
    v = (variant_name or "").lower()
    if "dealership" in v:
        return "Dealership", date.today() + timedelta(days=365)
    elif "team" in v:
        return "Team", date.today() + timedelta(days=365)
    else:
        return "Solo", date.today() + timedelta(days=365)

# ── Email sender ──────────────────────────────────────────────────────────────
def _send_license_email(to_email: str, buyer_name: str, license_key: str, plan: str, expiry: date):
    """Send license key email to buyer via Gmail SMTP."""
    if not GMAIL_ADDRESS or not GMAIL_APP_PASS:
        log.warning("Gmail credentials not set — skipping email send")
        return False

    first_name = buyer_name.split()[0] if buyer_name else "there"

    subject = "Your DealPulse License Key is Ready"
    body = f"""Hey {first_name},

Your DealPulse license key is below — copy and paste it exactly when the app asks.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
YOUR LICENSE KEY:

{license_key}

Plan: {plan}
Expires: {expiry.strftime('%B %d, %Y')}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

HOW TO ACTIVATE:
1. Extract DealPulse_v1.0.zip and run DealPulse_Installer.exe
2. When DealPulse opens, paste your license key above
3. Complete the quick setup screen (your name + OpenAI API key)
4. Log into VinSolutions in the built-in browser
5. Hit Start — DealPulse does the rest

GET YOUR OPENAI API KEY (required for AI mode):
https://platform.openai.com/api-keys
Create a free account and generate a key — takes 2 minutes.

NEED HELP?
Email: support@dealpulse.us
Website: dealpulse.us

Thank you for choosing DealPulse.
Every lead. Every follow-up. Every time.

— Jesse Boyd
DealPulse / Rocky Mountain Auto Brokers
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"DealPulse <{GMAIL_ADDRESS}>"
    msg["To"]      = to_email
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASS)
            server.sendmail(GMAIL_ADDRESS, to_email, msg.as_string())
        log.info(f"License email sent to {to_email}")
        return True
    except Exception as e:
        log.error(f"Failed to send email to {to_email}: {e}")
        return False

# ── Sales log ─────────────────────────────────────────────────────────────────
def _log_sale(buyer_name: str, email: str, plan: str, key: str, variant: str):
    file_exists = os.path.exists(SALES_LOG)
    with open(SALES_LOG, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["Date", "Name", "Email", "Plan", "Variant", "LicenseKey"])
        writer.writerow([date.today().isoformat(), buyer_name, email, plan, variant, key])
    log.info(f"Sale logged: {buyer_name} ({email}) — {plan}")

# ── Webhook endpoint ───────────────────────────────────────────────────────────
@app.route("/gumroad-webhook", methods=["POST"])
def gumroad_webhook():
    data = request.form.to_dict()
    log.info(f"Webhook received: {json.dumps(data, indent=2)}")

    # Optional: verify Gumroad signature
    if GUMROAD_SECRET:
        sig = request.headers.get("X-Gumroad-Signature", "")
        expected = hmac.new(
            GUMROAD_SECRET.encode(),
            request.get_data(),
            hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            log.warning("Invalid Gumroad signature — rejected")
            return jsonify({"error": "invalid signature"}), 403

    # Only process completed sales
    if data.get("refunded") == "true" or data.get("chargebacked") == "true":
        log.info("Refund/chargeback — skipping")
        return jsonify({"status": "skipped"}), 200

    buyer_name  = data.get("full_name", "Customer")
    buyer_email = data.get("email", "")
    variant     = data.get("variants[Tier]") or data.get("variants[Version]") or data.get("variants", "Solo")

    if not buyer_email:
        log.error("No buyer email in webhook payload")
        return jsonify({"error": "no email"}), 400

    # Generate license key
    plan, expiry = _detect_plan(variant)
    license_key  = _generate_key(expiry, plan)

    log.info(f"Generated {plan} key for {buyer_email}: {license_key}")

    # Send email
    sent = _send_license_email(buyer_email, buyer_name, license_key, plan, expiry)

    # Log the sale
    _log_sale(buyer_name, buyer_email, plan, license_key, variant)

    return jsonify({
        "status":  "ok",
        "sent":    sent,
        "plan":    plan,
        "key":     license_key,
    }), 200

# ── Health check ──────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "DealPulse webhook server running"}), 200

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info(f"Starting DealPulse webhook server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
