"""
DealPulse — Gumroad Webhook Server v3
Ultra-hardened for Railway deployment.
"""

import os
import logging
import smtplib
import json
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout
)
log = logging.getLogger("dealpulse")

from flask import Flask, request, jsonify
app = Flask(__name__)

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASS", "")

log.info(f"Startup: GMAIL_ADDRESS={'SET' if GMAIL_ADDRESS else 'MISSING'}, GMAIL_APP_PASS={'SET' if GMAIL_APP_PASS else 'MISSING'}")

# ── License key generator ─────────────────────────────────────────────────────
import hashlib, struct, os as _os
from datetime import date, timedelta

_SECRET = b"1b15a2af42c2b816ab4dbb50057ab02e43439b96d2436cd3618a814ac08eb6a3"
_EPOCH  = date(2026, 1, 1)
_PLANS  = {"solo": 0, "team": 1, "dealership": 2}

def generate_key(plan="Solo"):
    try:
        expiry    = date.today() + timedelta(days=365)
        plan_byte = _PLANS.get(plan.lower(), 0)
        days      = (expiry - _EPOCH).days
        salt      = _os.urandom(3)
        import struct as _struct
        payload   = _struct.pack(">IB", days, plan_byte) + salt
        import hmac as _hmac
        mac       = _hmac.new(_SECRET, payload, hashlib.sha256).digest()[:4]
        raw       = (payload + mac).hex().upper()
        groups    = [raw[i:i+6] for i in range(0, 24, 6)]
        key       = "DP-" + "-".join(groups)
        log.info(f"Key generated: {key}")
        return key, expiry
    except Exception as e:
        log.error(f"Key generation failed: {e}")
        import traceback; traceback.print_exc()
        raise

def detect_plan(variant):
    v = (variant or "").lower()
    if "dealership" in v: return "Dealership"
    if "team" in v:       return "Team"
    return "Solo"

# ── Email ─────────────────────────────────────────────────────────────────────
def send_email(to_email, buyer_name, key, plan, expiry):
    if not GMAIL_ADDRESS or not GMAIL_APP_PASS:
        log.warning("Gmail credentials missing")
        return False
    try:
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        first = buyer_name.split()[0] if buyer_name else "there"
        body = f"""Hey {first},

Your DealPulse license key is ready.

YOUR LICENSE KEY:
{key}

Plan: {plan}
Expires: {expiry.strftime('%B %d, %Y')}

HOW TO ACTIVATE:
1. Extract the zip and run DealPulse_Installer.exe
2. Paste your license key when prompted
3. Enter your name + OpenAI API key on the setup screen
4. Log into VinSolutions in the built-in browser
5. Hit Start

GET YOUR FREE OPENAI API KEY:
https://platform.openai.com/api-keys

NEED HELP?
support@dealpulse.us | dealpulse.us

Every lead. Every follow-up. Every time.
— Jesse Boyd, DealPulse"""

        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Your DealPulse License Key is Ready"
        msg["From"]    = f"DealPulse <{GMAIL_ADDRESS}>"
        msg["To"]      = to_email
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASS)
            server.sendmail(GMAIL_ADDRESS, to_email, msg.as_string())
        log.info(f"Email sent to {to_email}")
        return True
    except Exception as e:
        log.error(f"Email failed: {e}")
        import traceback; traceback.print_exc()
        return False

# ── Sales log ─────────────────────────────────────────────────────────────────
def log_sale(name, email, plan, key):
    try:
        import csv
        path = "/tmp/sales_log.csv"
        exists = os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(["Date","Name","Email","Plan","Key"])
            w.writerow([date.today().isoformat(), name, email, plan, key])
    except Exception as e:
        log.error(f"Log write failed: {e}")

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status":    "DealPulse webhook server running",
        "version":   "3.0",
        "gmail_set": bool(GMAIL_ADDRESS),
        "pass_set":  bool(GMAIL_APP_PASS),
    })

@app.route("/gumroad-webhook", methods=["POST"])
def webhook():
    try:
        data        = request.form.to_dict()
        log.info(f"Webhook: {json.dumps(data)}")

        if data.get("refunded") == "true" or data.get("chargebacked") == "true":
            return jsonify({"status": "skipped"}), 200

        buyer_name  = data.get("full_name", "Customer")
        buyer_email = data.get("email", "")
        variant     = (data.get("variants[Tier]") or
                       data.get("variants[Version]") or
                       data.get("variants", "Solo"))

        if not buyer_email:
            return jsonify({"error": "no email"}), 400

        plan            = detect_plan(variant)
        key, expiry     = generate_key(plan)
        sent            = send_email(buyer_email, buyer_name, key, plan, expiry)
        log_sale(buyer_name, buyer_email, plan, key)

        return jsonify({"status": "ok", "sent": sent, "plan": plan, "key": key})

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log.error(f"CRASH:\n{tb}")
        return jsonify({"error": str(e), "traceback": tb}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
