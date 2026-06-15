"""
VinConnect Auto-Updater  v1.0
==============================
Checks for new versions on launch and updates in-place — no action needed
from the customer.

How it works:
1. On every launch, a background thread fetches VERSION_MANIFEST_URL
2. If the remote version > current APP_VERSION, user is prompted (optional)
3. On approval, new files are downloaded and the app relaunches automatically

Hosting the manifest (free, using GitHub):
  1. Create a public GitHub repo named  vinconnect-updates  (or any name)
  2. Add the files listed in the manifest to that repo
  3. Set VERSION_MANIFEST_URL to the raw URL of version.json in that repo
  4. To push an update:
       a. Commit the new app.py / license.py to the repo
       b. Edit version.json — bump "version" to e.g. "1.1.0"
       c. All customers get notified on next launch automatically

Manifest format (version.json hosted on GitHub):
  {
    "version": "1.0.0",
    "release_notes": "Fixed email sending on certain VinSolutions accounts.",
    "files": {
      "app.py":     "https://raw.githubusercontent.com/YOU/REPO/main/app.py",
      "license.py": "https://raw.githubusercontent.com/YOU/REPO/main/license.py",
      "updater.py": "https://raw.githubusercontent.com/YOU/REPO/main/updater.py"
    },
    "min_python": "3.9"
  }

IMPORTANT: Set VERSION_MANIFEST_URL below to your actual GitHub raw URL.
           Leave it as the placeholder and updates will be silently skipped.
"""

import os
import sys
import json
import shutil
import tempfile
import threading
import subprocess
import urllib.request
import urllib.error
from datetime import datetime

# ── Configuration ─────────────────────────────────────────────────────────────

APP_VERSION = "1.0.0"  # Current version — bump this with every release

# URL of your version.json hosted on GitHub (raw content URL)
# Example: "https://raw.githubusercontent.com/yourname/vinconnect-updates/main/version.json"
VERSION_MANIFEST_URL = "https://raw.githubusercontent.com/YOURNAME/vinconnect-updates/main/version.json"

# How long to wait for the manifest fetch before giving up (seconds)
FETCH_TIMEOUT = 8

# ── Version comparison ────────────────────────────────────────────────────────

def _parse_version(v: str):
    """Convert '1.2.3' → (1, 2, 3) for comparison."""
    try:
        return tuple(int(x) for x in str(v).strip().split("."))
    except Exception:
        return (0, 0, 0)

def is_newer(remote_ver: str, local_ver: str) -> bool:
    return _parse_version(remote_ver) > _parse_version(local_ver)

# ── Manifest fetch ────────────────────────────────────────────────────────────

def fetch_manifest(url: str, timeout: int = FETCH_TIMEOUT) -> dict | None:
    """Download and parse the remote version manifest. Returns None on failure."""
    if "YOURNAME" in url:
        return None   # Placeholder URL — skip silently
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "VinConnect-Updater/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw)
    except (urllib.error.URLError, json.JSONDecodeError, Exception):
        return None   # Network down or bad manifest — fail silently

# ── File download ─────────────────────────────────────────────────────────────

def download_file(url: str, dest_path: str, timeout: int = 30) -> bool:
    """Download url to a temp file, then atomically replace dest_path.
    Returns True on success."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "VinConnect-Updater/1.0"})
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(dest_path),
            prefix=".vc_update_",
            suffix=".tmp"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                with os.fdopen(tmp_fd, "wb") as f:
                    shutil.copyfileobj(resp, f)
            # Atomic replace
            os.replace(tmp_path, dest_path)
            return True
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise
    except Exception:
        return False

# ── Relaunch ──────────────────────────────────────────────────────────────────

def relaunch():
    """Restart the application after an update."""
    if getattr(sys, "frozen", False):
        # Running as .exe
        subprocess.Popen([sys.executable])
    else:
        # Running as Python source
        subprocess.Popen([sys.executable] + sys.argv)
    sys.exit(0)

# ── Main update flow ──────────────────────────────────────────────────────────

class Updater:
    """
    Background update checker. Integrates with the Tk app via a callback.

    Usage in App.__init__:
        from updater import Updater
        Updater(on_update_available=self._on_update_available).start()

    The on_update_available callback is called on the main thread (via Tk.after)
    when an update is found. It receives (version, release_notes, apply_fn).
    Call apply_fn() to download and relaunch; ignore it to skip.
    """

    def __init__(self, on_update_available=None, tk_root=None):
        self._callback  = on_update_available
        self._tk_root   = tk_root
        self._thread    = None

    def start(self, tk_root=None):
        """Start the background check. Pass tk_root to enable main-thread callback."""
        if tk_root:
            self._tk_root = tk_root
        self._thread = threading.Thread(target=self._check, daemon=True)
        self._thread.start()

    def _check(self):
        manifest = fetch_manifest(VERSION_MANIFEST_URL)
        if not manifest:
            return
        remote_ver = manifest.get("version", "0.0.0")
        if not is_newer(remote_ver, APP_VERSION):
            return   # Already up to date

        release_notes = manifest.get("release_notes", "Bug fixes and improvements.")
        files = manifest.get("files", {})

        def apply_update():
            self._do_update(files)

        if self._callback and self._tk_root:
            # Schedule callback on Tk main thread
            self._tk_root.after(0, lambda: self._callback(remote_ver, release_notes, apply_update))
        elif self._callback:
            self._callback(remote_ver, release_notes, apply_update)

    def _do_update(self, files: dict):
        """Download all updated files and relaunch."""
        app_dir = os.path.dirname(os.path.abspath(
            sys.executable if getattr(sys, "frozen", False) else __file__
        ))

        success_count = 0
        for filename, url in files.items():
            dest = os.path.join(app_dir, filename)
            if download_file(url, dest):
                success_count += 1

        if success_count > 0:
            relaunch()

# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"VinConnect Updater  v{APP_VERSION}")
    print(f"Checking: {VERSION_MANIFEST_URL}")
    manifest = fetch_manifest(VERSION_MANIFEST_URL)
    if manifest is None:
        print("No manifest found (placeholder URL or network error).")
    else:
        rv = manifest.get("version", "?")
        print(f"Remote version: {rv}  |  Local: {APP_VERSION}")
        if is_newer(rv, APP_VERSION):
            print(f"Update available: {manifest.get('release_notes','')}")
        else:
            print("You are up to date.")
