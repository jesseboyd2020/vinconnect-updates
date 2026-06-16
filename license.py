"""
DealPulse License System  v1.0
================================
Offline HMAC-SHA256 license validation.

Key format:   DP-XXXX-XXXX-XXXX-XXXX
              (4 groups of 4 hex chars = 16 bytes payload + 4 bytes HMAC checksum)

Payload (8 bytes = 16 hex chars, split across groups 1-2):
  Bytes 0-3  : expiry as days since 2026-01-01 (uint32, big-endian)
  Byte  4    : plan tier  (0=Solo, 1=Team, 2=Dealership)
  Bytes 5-7  : random salt (3 bytes)

HMAC (4 bytes = 8 hex chars, split across groups 3-4):
  HMAC-SHA256(SECRET_KEY, payload_bytes)[:4]

The SECRET_KEY is compiled into the app — customers cannot forge keys
without it. Keep it private.

IMPORTANT: Change SECRET_KEY before distributing to customers.
           Once you sell a key, do NOT change SECRET_KEY or all existing
           keys will stop working.
"""

import hmac
import hashlib
import struct
import os
import json
import re
from datetime import date, timedelta

# ── Secret key — CHANGE THIS before distributing ─────────────────────────────
# Must be kept private. Use a long random string (32+ chars).
# Generate one with: python -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY = b"1b15a2af42c2b816ab4dbb50057ab02e43439b96d2436cd3618a814ac08eb6a3"

# ── Constants ─────────────────────────────────────────────────────────────────
EPOCH = date(2026, 1, 1)          # Day-0 for expiry encoding
MAX_DAYS = 65535                  # ~179 years — plenty of headroom

PLANS = {0: "Solo", 1: "Team", 2: "Dealership"}
PLAN_NAMES = {v: k for k, v in PLANS.items()}

LICENSE_FILE = None  # Set at runtime by app.py via set_license_file()

def set_license_file(path: str):
    """Called by app.py to tell license.py where to store the activated key."""
    global LICENSE_FILE
    LICENSE_FILE = path


# ── Key generation (run on your machine, never shipped to customers) ──────────

def generate_key(expiry_date: date, plan: str = "Solo") -> str:
    """Generate a license key for a customer.

    Args:
        expiry_date: The date the license expires (e.g. date(2027, 6, 1))
        plan: "Solo", "Team", or "Dealership"

    Returns:
        Key string like  DP-A1B2-C3D4-E5F6-7890
    """
    plan_byte = PLAN_NAMES.get(plan, 0)
    days = (expiry_date - EPOCH).days
    if days < 0:
        raise ValueError("Expiry date must be after 2026-01-01")
    if days > MAX_DAYS:
        raise ValueError("Expiry date too far in the future")

    salt = os.urandom(3)
    payload = struct.pack(">IB", days, plan_byte) + salt   # 8 bytes

    mac = hmac.new(SECRET_KEY, payload, hashlib.sha256).digest()[:4]

    raw = payload + mac   # 12 bytes = 24 hex chars
    hex_str = raw.hex().upper()

    # Format: DP-XXXX-XXXX-XXXX-XXXX  (split 24 hex into 4 groups of 6 — wait)
    # Actually split 24 chars into 4 groups of 6:
    #   Group 1: chars 0-5   (payload bytes 0-2)
    #   Group 2: chars 6-11  (payload bytes 3-5)
    #   Group 3: chars 12-17 (payload bytes 6-7 + mac bytes 0-1)
    #   Group 4: chars 18-23 (mac bytes 2-5)
    groups = [hex_str[i:i+6] for i in range(0, 24, 6)]
    return "DP-" + "-".join(groups)


def _parse_key(key: str):
    """Strip formatting and return raw 24-char hex string, or raise ValueError."""
    clean = key.strip().upper().replace(" ", "").replace("-", "")
    # Remove leading VINC prefix if present
    if clean.startswith("DP"):
        clean = clean[2:]
    if not re.fullmatch(r"[0-9A-F]{24}", clean):
        raise ValueError(f"Key must be 24 hex characters (got {len(clean)}): {clean!r}")
    return clean


def validate_key(key: str):
    """Validate a license key.

    Returns:
        dict with keys: valid (bool), plan (str), expiry (date), days_left (int),
                        expired (bool), error (str or None)
    """
    result = {
        "valid":     False,
        "plan":      "",
        "expiry":    None,
        "days_left": 0,
        "expired":   False,
        "error":     None,
    }
    try:
        hex_str = _parse_key(key)
        raw = bytes.fromhex(hex_str)
        payload = raw[:8]
        given_mac = raw[8:12]

        # Verify HMAC
        expected_mac = hmac.new(SECRET_KEY, payload, hashlib.sha256).digest()[:4]
        if not hmac.compare_digest(given_mac, expected_mac):
            result["error"] = "Invalid license key."
            return result

        # Decode payload
        days, plan_byte = struct.unpack(">IB", payload[:5])
        expiry = EPOCH + timedelta(days=days)
        today  = date.today()
        days_left = (expiry - today).days

        result["plan"]      = PLANS.get(plan_byte, "Unknown")
        result["expiry"]    = expiry
        result["days_left"] = days_left
        result["expired"]   = days_left < 0

        if days_left < 0:
            result["error"] = (
                f"License expired on {expiry.strftime('%B %d, %Y')}. "
                "Please renew at dealpulse.io"
            )
        else:
            result["valid"] = True

    except ValueError as e:
        result["error"] = f"Invalid key format. {e}"
    except Exception as e:
        result["error"] = f"License check failed: {e}"

    return result


# ── Persistent activation ─────────────────────────────────────────────────────

def load_saved_key() -> str:
    """Load previously activated key from license.json, or return ''."""
    if not LICENSE_FILE or not os.path.exists(LICENSE_FILE):
        return ""
    try:
        with open(LICENSE_FILE) as f:
            data = json.load(f)
        return data.get("license_key", "")
    except Exception:
        return ""


def save_key(key: str):
    """Persist an activated key to license.json."""
    if not LICENSE_FILE:
        return
    with open(LICENSE_FILE, "w") as f:
        json.dump({"license_key": key}, f, indent=2)


def check_saved_license():
    """Load and validate the saved key. Returns validate_key() result dict."""
    key = load_saved_key()
    if not key:
        return {"valid": False, "error": "No license key found.", "plan": "",
                "expiry": None, "days_left": 0, "expired": False}
    return validate_key(key)
