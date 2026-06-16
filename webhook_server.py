"""
DealPulse — Gumroad Webhook Server
====================================
Receives purchase notifications from Gumroad and automatically
emails a license key to the buyer within seconds.
"""

import os
import hmac
import hashlib
import logging
import csv
import smtplib
import json
import traceback
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
SALES_LOG       = "/tmp/sales_log.csv"

# ── DealPulse License Engine ──────────────────────────────────────────────────
import struct
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

def _detect_plan(variant_name: str) -> tuple:
    v = (variant_name or "").lower()
    if "dealership" in v:
        return "Dealership", date.today() + timedelta(days=365)
    elif "team" in v:
        return "Team", date.today() + timedelta(days=365)
    else:
        return "Solo", date.today() + timedelta(days=365)

def _send_license_email(to_email: str, buyer_name: str, license_key: str, plan: str, expiry: date):
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
1. Extract the zip and run DealPulse_Installer.exe
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

def _log_sale(buyer_name: str, email: str, plan: str, key: str, variant: str):
    file_exists = os.path.exists(SALES_LOG)
    try:
        with open(SALES_LOG, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Date", "Name", "Email", "Plan", "Variant", "LicenseKey"])
            writer.writerow([date.today().isoformat(), buyer_name, email, plan, variant, key])
        log.info(f"Sale logged: {buyer_name} ({email}) — {plan}")
    except Exception as e:
        log.error(f"Failed to write sales log: {e}")

# ── Webhook endpoint ───────────────────────────────────────────────────────────
@app.route("/gumroad-webhook", methods=["POST"])
def gumroad_webhook():
    try:
        data = request.form.to_dict()
        log.info(f"Webhook received: {json.dumps(data, indent=2)}")

        # Optional: verify Gumroad signature
        if GUMROAD_SECRET:
            sig      = request.headers.get("X-Gumroad-Signature", "")
            expected = hmac.new(
                GUMROAD_SECRET.encode(),
                request.get_data(),
                hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(sig, expected):
                log.warning("Invalid Gumroad signature — rejected")
                return jsonify({"error": "invalid signature"}), 403

        if data.get("refunded") == "true" or data.get("chargebacked") == "true":
            log.info("Refund/chargeback — skipping")
            return jsonify({"status": "skipped"}), 200

        buyer_name  = data.get("full_name", "Customer")
        buyer_email = data.get("email", "")
        variant     = data.get("variants[Tier]") or data.get("variants[Version]") or data.get("variants", "Solo")

        if not buyer_email:
            log.error("No buyer email in webhook payload")
            return jsonify({"error": "no email"}), 400

        plan, expiry = _detect_plan(variant)
        license_key  = _generate_key(expiry, plan)

        log.info(f"Generated {plan} key for {buyer_email}: {license_key}")

        sent = _send_license_email(buyer_email, buyer_name, license_key, plan, expiry)
        _log_sale(buyer_name, buyer_email, plan, license_key, variant)

        return jsonify({
            "status": "ok",
            "sent":   sent,
            "plan":   plan,
            "key":    license_key,
        }), 200

    except Exception as e:
        tb = traceback.format_exc()
        log.error(f"Webhook crashed:\n{tb}")
        return jsonify({"error": str(e), "traceback": tb}), 500

# ── Health check ──────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "DealPulse webhook server running",
        "gmail_set": bool(GMAIL_ADDRESS),
        "pass_set": bool(GMAIL_APP_PASS),
        "secret_set": bool(GUMROAD_SECRET),
    }), 200

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info(f"Starting DealPulse webhook server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
