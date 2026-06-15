"""
VinSolutions Follow-Up Tool  v14.0

v14.0 changes:
- Auto-update system: updater.py, background manifest check on launch
- UpdateWindow prompt: non-blocking, shows version + release notes
- Updater starts after license confirmed, before main window shown
- version.json manifest template included

v13.0 changes:
- License key system: HMAC-SHA256 offline validation, LicenseWindow UI
- license.py integrated: validate on launch, block expired/invalid keys
- LICENSE_FILE stored beside config.json (uses _get_app_dir)
- 30-day expiry warning shown in header
- Keygen tool (keygen.py) for generating customer keys

v12.0 changes:
- PyInstaller compatibility: _get_app_dir(), _get_playwright_browsers_path()
- CONFIG_FILE now uses _get_app_dir() so config saves beside the .exe
- PLAYWRIGHT_BROWSERS_PATH set before sync_playwright import when frozen
- `import sys` added

Audit history (all passes):
- Popup capture: page.expect_popup() reliably catches window.open()
- Text SMS: scroll to bottom, last visible textarea, verify typed text
- Email: banner img removed, message inserted in its place, all-frames search
- Subject: 3-strategy fill (selector, first-empty, brute-force) with full debug log
- Send button: CSS selectors + JS text-scan fallback across all frames
- process_lead: consistent page references, proper bring_to_front sequencing
- Removed duplicate time imports; _debug_frames only on first lead
- Text Send: removed dangerous catch-all button selector
- Removed unused imports and dead helper functions
- href CSS selector guarded against special characters
- panel_still_up expanded to include img-src + text label checks across all frames
- fr.keyboard for cross-frame keyboard events (fix focus bug in subject/text fill)
- Element-level el.press/el.type for subject fill (unambiguous focus targeting)
- requirements.txt: pinned major version bounds for playwright and openai
- README/bat files updated to match current v10.0 workflow (no login fields)
- panel_ready scans all sibling frames via Frame.page; strategy A verify loop
- panel_still_up multi-frame; Strategy B subject verify; Delete→Backspace
- popup timeout 15s; unicode name regex; fallback poll 30 iterations
- JS_CLICK_ICON closest() chain; sleep(3)→sleep(1.5); load_config try/except
- JS escape: &apos;→&#39;; stop_event check between text and email
"""

import tkinter as tk
from tkinter import scrolledtext, messagebox
import threading
import time
import json
import os
import sys
from datetime import datetime
from playwright.sync_api import sync_playwright

try:
    from openai import OpenAI as _OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

# ── PyInstaller compatibility ───────────────────────────────────────────────
# When bundled with PyInstaller, __file__ is inside a temp _MEIPASS folder.
# Config must be stored in a writable location next to the .exe instead.
def _get_app_dir() -> str:
    """Return the folder where config.json should live.

    - Bundled (.exe): same folder as the executable.
    - Development (python app.py): same folder as app.py.
    """
    if getattr(sys, 'frozen', False):
        # Running as PyInstaller bundle
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def _get_playwright_browsers_path():  # -> str | None
    """Tell Playwright where to find bundled browsers when frozen."""
    if getattr(sys, 'frozen', False):
        # Browsers are bundled under ms-playwright/ inside _MEIPASS
        candidate = os.path.join(sys._MEIPASS, 'ms-playwright')
        if os.path.isdir(candidate):
            return candidate
    return None

# Set Playwright browser path env var BEFORE importing sync_playwright
_pw_browsers = _get_playwright_browsers_path()
if _pw_browsers:
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', _pw_browsers)

# ── Config & License paths ──────────────────────────────────────────────────
CONFIG_FILE   = os.path.join(_get_app_dir(), "config.json")
LICENSE_FILE  = os.path.join(_get_app_dir(), "license.json")

# Import license module (bundled alongside app.py / in _MEIPASS when frozen)
try:
    import license as _lic
    _lic.set_license_file(LICENSE_FILE)
    LICENSE_AVAILABLE = True
except ImportError:
    LICENSE_AVAILABLE = False

# Import updater module
try:
    from updater import Updater as _Updater
    UPDATER_AVAILABLE = True
except ImportError:
    UPDATER_AVAILABLE = False

VINCONNECT_LOGIN = (
    "https://vinsolutions.app.coxautoinc.com/vinconnect/"
    "#/CarDashboard/Pages/LeadManagement/ActiveLeadsLayout.aspx"
    "?SelectedTab=t_ILM&leftpaneframe=ActiveLeads_NewLeadList.aspx&NoMenu=true"
)

# Runtime identity — loaded from config after setup screen
SALESPERSON_NAME = ""
DEALERSHIP_NAME  = ""

def make_default_message(name, dealership):
    n = name      or "your name"
    d = dealership or "our dealership"
    return (
        f"This is {n} from {d}, "
        "I want to check in and see if there is anything "
        "I can do for you today?"
    )

def make_tone_prompts(n, d):
    """Build AI tone prompts using the salesperson's name and dealership."""
    return {
        "Appointment Setter": (
            f"You are {n}, a salesperson at {d}.\n"
            "Your ONLY goal is to get this lead to commit to a specific appointment day and time.\n"
            "- Keep it under 3 sentences\n"
            "- Always end asking for a specific day/time: 'Are you free Tuesday or Wednesday?'\n"
            "- Sound natural, not scripted\n"
            "- Reference their vehicle interest if available\n"
            f"- Sign off: {n}\n"
            "Respond with ONLY the message text."
        ),
        "Aggressive Close": (
            f"You are {n} at {d}.\n"
            "Get this lead in the door TODAY or THIS WEEK with urgency.\n"
            "- Under 3 sentences\n"
            "- Create urgency: limited inventory, deal expiring, another buyer interested\n"
            "- Direct and confident, clear call to action\n"
            f"- Sign off: {n}\n"
            "Respond with ONLY the message text."
        ),
        "Soft Follow-Up": (
            f"You are {n} at {d}.\n"
            "Warm, low-pressure check-in to keep the relationship alive.\n"
            "- Under 3 sentences, friendly, zero pressure\n"
            "- Reference conversation history if available\n"
            "- Leave the door open without pushing\n"
            f"- Sign off: {n}\n"
            "Respond with ONLY the message text."
        ),
        "Value-First": (
            f"You are {n} at {d}.\n"
            "Lead with something useful before asking for anything.\n"
            "- Under 3 sentences\n"
            "- Open with value: new match, price drop, trade-in values up\n"
            "- Reference their vehicle interest if available\n"
            f"- Sign off: {n}\n"
            "Respond with ONLY the message text."
        ),
    }

TONE_OPTIONS = ["Appointment Setter", "Aggressive Close", "Soft Follow-Up", "Value-First"]
TONE_DESCRIPTIONS = {
    "Appointment Setter": "Laser focused on locking in a day and time.",
    "Aggressive Close":   "Urgency — limited inventory, push to get them in now.",
    "Soft Follow-Up":     "Warm, low-pressure. Just keeping the door open.",
    "Value-First":        "Lead with something useful before asking for anything.",
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "salesperson_name": "",
        "dealership_name":  "",
        "openai_key":       "",
        "message":          "",
        "mode":             "template",
        "tone":             "Soft Follow-Up",
        "setup_complete":   False,
    }

def is_setup_complete(cfg):
    return bool(
        cfg.get("setup_complete") and
        cfg.get("salesperson_name", "").strip() and
        cfg.get("dealership_name",  "").strip()
    )

def save_config(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
# AI
# ══════════════════════════════════════════════════════════════════════════════

def generate_ai_message(openai_key, lead_name, history, tone,
                        salesperson_name="", dealership_name=""):
    if not OPENAI_AVAILABLE:
        raise RuntimeError("Run: pip install openai")
    client = _OpenAI(api_key=openai_key)
    prompts = make_tone_prompts(
        salesperson_name or SALESPERSON_NAME,
        dealership_name  or DEALERSHIP_NAME,
    )
    system = prompts.get(tone, prompts["Soft Follow-Up"])
    user = (
        f"Lead name: {lead_name}\n\nConversation history:\n{history}\n\nWrite a follow-up."
        if history else
        f"Lead name: {lead_name}\n\nNo prior history. Write a first-contact follow-up."
    )
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": system},
                  {"role": "user",   "content": user}],
        max_tokens=200, temperature=0.75,
    )
    return resp.choices[0].message.content.strip()


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def scroll_into_view(page, element):
    """Scroll element into viewport using JS, then wait a moment."""
    try:
        page.evaluate("el => el.scrollIntoView({block:'center',inline:'center'})", element)
        time.sleep(0.3)
    except Exception:
        pass

def read_history(page):
    lines = []
    for sel in [
        ".message-item", ".sms-message", ".email-message",
        ".conversation-item", ".activity-item", ".timeline-item",
        "[class*='message']", "[class*='conversation']", ".note-text",
    ]:
        try:
            items = page.query_selector_all(sel)
            if items:
                for item in items[:20]:
                    txt = item.inner_text().strip()
                    if txt and len(txt) > 5:
                        lines.append(txt)
                if lines:
                    break
        except Exception:
            continue
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# BOT
# ══════════════════════════════════════════════════════════════════════════════

class VinSolutionsBot:

    def __init__(self, message, mode, openai_key, tone,
                 log_fn, stop_event, ready_event):
        self.message     = message
        self.mode        = mode
        self.openai_key  = openai_key
        self.tone        = tone
        self.log         = log_fn
        self.stop_event  = stop_event
        self.ready_event = ready_event
        self.results     = []

    # ── Wait for Jesse ─────────────────────────────────────────────────────────
    def wait_for_ready(self, page):
        self.log("Opening VinConnect...")
        page.goto(VINCONNECT_LOGIN, wait_until="domcontentloaded", timeout=30000)
        self.log("")
        self.log("=" * 58)
        self.log("  Log in if prompted.")
        self.log("  Navigate to your leads page.")
        self.log("  When the leads are visible, click")
        self.log("  [ I AM LOGGED IN ] in this app.")
        self.log("  The tool scrolls automatically — no need")
        self.log("  to have the full page visible first.")
        self.log("=" * 58)
        self.log("")
        deadline = time.time() + 600
        while time.time() < deadline:
            if self.ready_event.is_set():
                self.log("Ready — starting run...")
                time.sleep(1)
                return True
            if self.stop_event.is_set():
                return False
            time.sleep(1)
        raise RuntimeError("Timed out waiting for login confirmation.")

    # ── Find leads table ────────────────────────────────────────────────────────
    def find_leads_frame(self, page):
        """
        VinConnect My Leads: use JS to find name links by POSITION.
        Only grabs links in the LEFT half of the page, BELOW the nav bar (y>120).
        Ignores all nav/header/dashboard items completely.
        Returns (frame_or_page, list_of_{text,href}_dicts)
        """

        JS_FIND_LEADS = r"""
        () => {
            const NAV = new Set([
                'dashboard','leads','tasks','calendar','customers','marketing',
                'insights','settings','links','all','new','hot','recent',
                'sold log','add customer','showroom visit log','unmatched inbox',
                'communications','view all','user type','status/source',
                'details','save','cancel','search','filter','status','source',
                'crm','type','view','none','unknown','active',
                'customer','age','updated','contacted','active leads','new leads',
                'jesse boyd','rocky mountain auto brokers','cox automotive',
                'vinsolutions','my dashboard','today','appts','search customers'
            ]);

            function looksLikeName(txt) {
                if (!txt) return false;
                txt = txt.trim();
                if (txt.length < 4 || txt.length > 60) return false;
                if (NAV.has(txt.toLowerCase())) return false;
                var words = txt.split(/\s+/);
                if (words.length < 2) return false;
                if (!/^[A-Za-zÀ-ÖØ-öø-ÿ\s\-\']+$/.test(txt)) return false;
                return true;
            }

            var allLinks = Array.from(document.querySelectorAll('a'));
            var results = [];
            var seen = {};

            allLinks.forEach(function(a) {
                try {
                    var rect = a.getBoundingClientRect();
                    // Must be BELOW the nav bar
                    if (rect.top < 120) return;
                    // Must be visible (has width/height)
                    if (rect.width < 1 || rect.height < 1) return;
                    // Must be in the LEFT portion of the page (not the dashboard panel)
                    if (rect.left > window.innerWidth * 0.52) return;
                    var txt = (a.innerText || '').trim();
                    if (!looksLikeName(txt)) return;
                    if (seen[txt]) return;
                    seen[txt] = true;
                    results.push({ text: txt, href: a.getAttribute('href') || '' });
                } catch(e) {}
            });

            return results;
        }
        """

        def get_lead_data(ctx):
            try:
                results = ctx.evaluate(JS_FIND_LEADS)
                seen = set()
                out = []
                for r in (results or []):
                    name = (r.get('text') or '').strip()
                    if name and name not in seen:
                        seen.add(name)
                        out.append(r)
                return out
            except Exception as e:
                self.log(f"  [JS error] {e}")
                return []

        # Try main page first
        data = get_lead_data(page)
        if data:
            self.log(f"  Found {len(data)} lead names on main page")
            return page, data

        # Try every iframe
        for i, frame in enumerate(page.frames):
            if frame == page.main_frame:
                continue
            try:
                data = get_lead_data(frame)
                if data:
                    self.log(f"  Found {len(data)} lead names in frame[{i}]")
                    return frame, data
            except Exception:
                continue

        return None, []

    def snapshot_leads(self, leads_frame, links):
        """
        links is a list of {text, href} dicts from JS evaluation.
        Scrolls down to collect off-screen leads, then returns full list.
        """
        leads = []
        seen  = set()

        NAV = {
            'dashboard','leads','tasks','calendar','customers','marketing',
            'insights','settings','links','all','new','hot','recent',
            'sold log','add customer','showroom visit log','unmatched inbox',
            'communications','view all','user type','status/source',
            'details','save','cancel','search','filter','status','source',
            'crm','type','view','none','unknown','active',
            'customer','age','updated','contacted','active leads','new leads',
            'jesse boyd','rocky mountain auto brokers','cox automotive',
            'vinsolutions','my dashboard','today','appts','search customers',
        }

        JS_SCROLL_LEADS = r"""
        () => {
            const NAV = new Set([
                'dashboard','leads','tasks','calendar','customers','marketing',
                'insights','settings','links','all','new','hot','recent',
                'sold log','add customer','showroom visit log','unmatched inbox',
                'communications','view all','user type','status/source',
                'details','save','cancel','search','filter','status','source',
                'crm','type','view','none','unknown','active',
                'customer','age','updated','contacted','active leads','new leads',
                'jesse boyd','rocky mountain auto brokers','cox automotive',
                'vinsolutions','my dashboard','today','appts','search customers'
            ]);

            function looksLikeName(txt) {
                if (!txt) return false;
                txt = txt.trim();
                if (txt.length < 4 || txt.length > 60) return false;
                if (NAV.has(txt.toLowerCase())) return false;
                var words = txt.split(/\s+/);
                if (words.length < 2) return false;
                if (!/^[A-Za-zÀ-ÖØ-öø-ÿ\s\-\']+$/.test(txt)) return false;
                return true;
            }

            window.scrollBy(0, 500);

            var allLinks = Array.from(document.querySelectorAll('a'));
            var results = [];
            var seen = {};
            allLinks.forEach(function(a) {
                try {
                    var rect = a.getBoundingClientRect();
                    if (rect.top < 120) return;
                    if (rect.width < 1 || rect.height < 1) return;
                    if (rect.left > window.innerWidth * 0.52) return;
                    var txt = (a.innerText || '').trim();
                    if (!looksLikeName(txt)) return;
                    if (seen[txt]) return;
                    seen[txt] = true;
                    results.push({ text: txt, href: a.getAttribute('href') || '' });
                } catch(e) {}
            });
            return results;
        }
        """

        # Collect initial links
        for item in links:
            name = (item.get('text') or '').strip()
            href = item.get('href') or ''
            if name and name not in seen and name.lower() not in NAV:
                seen.add(name)
                leads.append({'name': name, 'href': href})

        # Scroll down up to 30 times to reveal more leads.
        # Use continue (not break) on exception — JS context errors are often
        # transient (frame navigating) and shouldn't abort the whole scroll.
        _scroll_errors = 0
        for _ in range(30):
            prev = len(leads)
            try:
                more = leads_frame.evaluate(JS_SCROLL_LEADS)
                _scroll_errors = 0  # reset on success
                for item in (more or []):
                    name = (item.get('text') or '').strip()
                    href = item.get('href') or ''
                    if name and name not in seen and name.lower() not in NAV:
                        seen.add(name)
                        leads.append({'name': name, 'href': href})
            except Exception:
                _scroll_errors += 1
                if _scroll_errors >= 3:
                    break  # 3 consecutive errors — frame is gone
                time.sleep(0.5)
                continue
            time.sleep(0.4)
            if len(leads) == prev:
                break

        # Scroll back to top
        try:
            leads_frame.evaluate("window.scrollTo(0,0)")
            time.sleep(0.3)
        except Exception:
            pass

        return leads

    def open_profile(self, ctx, leads_frame, lead):
        """
        Click the lead name — VinConnect opens a RIGHT-SIDE PANEL on the same page.
        No new tab is created. We wait for the panel to appear, then return the page
        (same leads_page) since everything is in-page.
        """
        name = lead["name"]
        href = lead["href"]
        try:
            # Re-find the link
            link = None
            if href and '"' not in href and "'" not in href:
                try:
                    link = leads_frame.query_selector(f"a[href='{href}']")
                except Exception:
                    pass
            if not link:
                all_links = leads_frame.query_selector_all("a")
                for lk in all_links:
                    try:
                        if lk.inner_text().strip() == name:
                            link = lk
                            break
                    except Exception:
                        continue

            if not link:
                self.log(f"  [!] Could not re-find link for {name}")
                return None

            scroll_into_view(leads_frame, link)
            time.sleep(0.3)
            link.click()

            # Wait for the right-side profile panel to load
            # VinConnect slides it in — wait up to 6 seconds for action icons to appear
            self.log("  Waiting for profile panel to open...")
            panel_ready = False
            # Build a list of contexts to check: leads_frame + all sibling frames
            # Icons live in a child iframe — querySelectorAll doesn't cross boundaries,
            # so we must check each frame individually.
            try:
                _parent_page = leads_frame.page  # Frame.page → parent Page
            except Exception:
                _parent_page = leads_frame       # Already a Page
            _check_frames = [_parent_page] + list(_parent_page.frames)

            JS_PANEL_CHECK = """
            () => {
                const all = Array.from(document.querySelectorAll('a,button,span,div,li'));
                for (const el of all) {
                    const txt = (el.innerText || el.textContent || '').trim().toLowerCase();
                    if ((txt === 'email' || txt === 'message' || txt === 'text' ||
                         txt === 'call' || txt === 'note') &&
                        el.getBoundingClientRect().width > 0) {
                        return true;
                    }
                }
                const imgs = Array.from(document.querySelectorAll('img'));
                for (const img of imgs) {
                    const src = (img.getAttribute('src') || '').toLowerCase();
                    if (src.includes('email') || src.includes('mail') ||
                        src.includes('text') || src.includes('sms') ||
                        src.includes('opted') || src.includes('call')) {
                        return true;
                    }
                }
                return false;
            }
            """
            for _ in range(12):
                time.sleep(0.5)
                for _ctx in _check_frames:
                    try:
                        result = _ctx.evaluate(JS_PANEL_CHECK)
                        if result:
                            panel_ready = True
                            break
                    except Exception:
                        continue
                if panel_ready:
                    break

            if panel_ready:
                self.log("  Profile panel ready")
            else:
                self.log("  Profile panel may not have loaded — continuing anyway")

            time.sleep(1)
            # Return the same page — profile is a panel ON this page
            return leads_frame

        except Exception as e:
            self.log(f"  [open_profile error] {e}")
            return None

    # ── Debug: dump frame content around icons ───────────────────────────────────
    def _debug_frames(self, page):
        """Log what each frame contains so we can find the icon elements."""
        JS_DUMP = """
        () => {
            const results = [];
            // Find all <a> and <td> elements that have onclick or href
            const els = Array.from(document.querySelectorAll('a, td, button, input, img'));
            els.forEach(el => {
                try {
                    const txt = (el.innerText || el.textContent || el.value || '').trim().substring(0, 40);
                    const src = el.getAttribute('src') || '';
                    const href = el.getAttribute('href') || '';
                    const onclick = el.getAttribute('onclick') || '';
                    const title = el.getAttribute('title') || '';
                    const alt = el.getAttribute('alt') || '';
                    if (txt || src || onclick) {
                        results.push(el.tagName + '|txt:' + txt + '|src:' + src + '|onclick:' + onclick.substring(0,60) + '|title:' + title + '|alt:' + alt);
                    }
                } catch(e) {}
            });
            return results.slice(0, 60);
        }
        """
        self.log("  === FRAME DEBUG ===")
        all_frames = [page] + list(page.frames)
        for i, fr in enumerate(all_frames):
            try:
                url = fr.url if hasattr(fr, 'url') else '?'
                items = fr.evaluate(JS_DUMP)
                if items:
                    self.log(f"  [frame {i}] {url[:50]}")
                    for item in items[:20]:
                        self.log(f"    {item}")
            except Exception as e:
                self.log(f"  [frame {i}] error: {e}")
        self.log("  === END DEBUG ===")

    # ── Click icon and capture the popup Playwright-style ───────────────────────
    def _click_icon_get_popup(self, browser_ctx, page, labels):
        """
        VinConnect icons call top.OpenWindow(url, ...) which is a window.open() call.
        Playwright CAN intercept these via page.expect_popup().
        We click the icon inside expect_popup() so Playwright captures the new window
        before it has a chance to escape as a native OS popup.
        """
        # The JS that clicks the icon by img-src keyword or label text
        JS_CLICK_ICON = """
        (keywords) => {
            // Try to click by img src keyword
            const imgs = Array.from(document.querySelectorAll('img'));
            for (const img of imgs) {
                const src = (img.getAttribute('src') || '').toLowerCase();
                if (keywords.some(k => src.includes(k.toLowerCase()))) {
                    // Walk up the DOM to find the clickable anchor
                    const a = img.closest('a') ||
                              img.closest('td[onclick]') ||
                              img.closest('tr[onclick]') ||
                              img.closest('[onclick]');
                    if (a) {
                        a.click();
                        return 'img:' + src;
                    }
                }
            }
            // Try to click by label text (case-insensitive)
            const els = Array.from(document.querySelectorAll('a, button, td, span'));
            for (const el of els) {
                const txt = (el.innerText || el.textContent || '').trim().toLowerCase();
                for (const lbl of keywords) {
                    if (txt === lbl.toLowerCase()) {
                        el.click();
                        return 'txt:' + txt;
                    }
                }
            }
            return null;
        }
        """

        # Map labels to img-src keywords
        img_keywords = {
            'email':   ['email', 'mail'],
            'text':    ['text', 'sms', 'opted'],
            'sms':     ['text', 'sms', 'opted'],
            'message': ['text', 'sms', 'opted'],
        }
        kw = []
        for lbl in labels:
            kw += img_keywords.get(lbl.lower(), [lbl.lower()])
        kw = list(dict.fromkeys(kw))  # deduplicate

        all_frames = [page] + list(page.frames)

        # Strategy 1: use page.expect_popup() to catch window.open() calls.
        # expect_popup() MUST be on the main page object (not a frame).
        # We click via JS inside every frame while the popup listener is active.
        #
        # Pre-check: verify the icon exists in at least one frame before
        # entering expect_popup, so we don't waste 15s hanging if it's absent.
        JS_ICON_EXISTS = """
        (keywords) => {
            const imgs = Array.from(document.querySelectorAll('img'));
            for (const img of imgs) {
                const src = (img.getAttribute('src') || '').toLowerCase();
                if (keywords.some(k => src.includes(k.toLowerCase()))) return true;
            }
            const els = Array.from(document.querySelectorAll('a, button, td, span'));
            for (const el of els) {
                const txt = (el.innerText || el.textContent || '').trim().toLowerCase();
                if (keywords.some(k => txt === k.toLowerCase())) return true;
            }
            return false;
        }
        """
        icon_present = False
        for _fr in all_frames:
            try:
                if _fr.evaluate(JS_ICON_EXISTS, kw):
                    icon_present = True
                    break
            except Exception:
                continue

        if not icon_present:
            self.log(f"    [!] Icon not found in any frame for {labels} — skipping popup wait")
            return None

        try:
            self.log(f"    Waiting for popup from icon click...")
            with page.expect_popup(timeout=15000) as popup_info:
                # Click the icon in every frame until one works
                clicked = False
                for fr in all_frames:
                    try:
                        result = fr.evaluate(JS_CLICK_ICON, kw)
                        if result:
                            self.log(f"    Clicked: {result}")
                            clicked = True
                            break
                    except Exception:
                        continue
                if not clicked:
                    self.log(f"    [!] Icon not found in any frame for {labels}")

            popup = popup_info.value
            try:
                popup.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            time.sleep(2)
            self.log(f"    Popup captured: {popup.url[:80]}")
            return popup

        except Exception as e:
            self.log(f"    [expect_popup failed] {e}")
            # Fall through to strategy 2

        # Strategy 2: fallback — click all frames hoping Playwright catches it
        self.log(f"    Trying fallback popup capture for {labels}...")
        pages_before = set(id(p) for p in browser_ctx.pages)

        for fr in all_frames:
            try:
                result = fr.evaluate(JS_CLICK_ICON, kw)
                if result:
                    self.log(f"    Fallback clicked: {result}")
                    break
            except Exception:
                continue

        # Poll for new page (catches some popups)
        for _ in range(30):
            time.sleep(0.5)
            new_pages = [p for p in browser_ctx.pages if id(p) not in pages_before]
            if new_pages:
                popup = new_pages[-1]
                try:
                    popup.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                time.sleep(2)
                self.log(f"    Fallback popup: {popup.url[:80]}")
                return popup

        self.log(f"    [!] No popup captured for {labels}")
        return None

    # ── Open text popup ──────────────────────────────────────────────────────────
    def open_text_tab(self, ctx, page):
        self.log("    Looking for Text icon...")
        popup = self._click_icon_get_popup(ctx, page, ["Text", "SMS", "Message"])
        if popup:
            self.log("    Text popup opened")
        return popup, False

    # ── Open email popup ─────────────────────────────────────────────────────────
    def open_email_tab(self, ctx, page):
        self.log("    Looking for Email icon...")
        popup = self._click_icon_get_popup(ctx, page, ["Email"])
        if popup:
            self.log("    Email popup opened")
        return popup, False

    # ── Send in compose popup ────────────────────────────────────────────────────
    def send_in_tab(self, tab, msg, kind):
        """
        VinConnect popup window handler.

        EMAIL popup (sendemail.aspx):
          - From / To already filled
          - Subject: text input (empty)
          - Body: rich-text iframe with contenteditable body
          - Send: input[value='Send'] top right

        TEXT popup (sendtext.aspx):
          - Existing SMS thread with blue bubbles
          - Reply textarea at the BOTTOM of the page
          - Send button to the RIGHT of the textarea
        """
        if tab is None:
            return False
        # Guard: popup may have been closed by VinConnect before we even start
        try:
            if tab.is_closed():
                self.log("    [!] Popup was already closed before send")
                return False
        except Exception:
            pass
        try:
            # Brief wait for popup to begin rendering; wait_for_selector below handles the rest
            time.sleep(1.5)

            # Log popup URL for debugging
            try:
                self.log(f"    Popup URL: {tab.url[:100]}")
            except Exception:
                pass

            # Dump all inputs visible in the popup
            try:
                page_inputs = tab.evaluate("""
                () => {
                    const els = Array.from(document.querySelectorAll(
                        'input, textarea, [contenteditable]'));
                    return els.slice(0, 20).map(el => ({
                        tag: el.tagName,
                        type: el.getAttribute('type') || '',
                        id: el.id || '',
                        name: el.getAttribute('name') || '',
                        ph: el.getAttribute('placeholder') || '',
                        vis: el.offsetParent !== null,
                        val: (el.value || el.innerText || '').substring(0, 30),
                    }));
                }
                """)
                for inp in (page_inputs or []):
                    self.log(f"    input: {inp}")
            except Exception as dbg_e:
                self.log(f"    [debug inputs] {dbg_e}")

            # Log frames
            try:
                self.log(f"    popup frames: {len(tab.frames)}")
                for i, fr in enumerate(tab.frames):
                    self.log(f"      frame[{i}]: {fr.url[:60]}")
            except Exception:
                pass

            all_frames = [tab] + list(tab.frames)

            if kind == "email":
                # ── EMAIL ───────────────────────────────────────────────────
                subject = "Help"

                # Wait for inputs to appear
                try:
                    tab.wait_for_selector("input, textarea", timeout=8000)
                except Exception:
                    pass
                time.sleep(1)

                # 1. Fill Subject — dump every input in every frame so we know exactly
                #    what is on the page, then try every strategy to fill it.
                subj_filled = False

                # Dump all inputs across all frames for debug
                for fi, fr in enumerate(all_frames):
                    try:
                        info = fr.evaluate("""
                        () => Array.from(document.querySelectorAll('input,textarea')).map(el => ({
                            tag: el.tagName,
                            type: el.type || '',
                            id: el.id || '',
                            name: el.name || '',
                            ph: el.placeholder || '',
                            cls: el.className || '',
                            vis: el.offsetParent !== null,
                            val: (el.value || '').substring(0, 30),
                            ro: el.readOnly,
                            dis: el.disabled,
                        }))
                        """)
                        if info:
                            self.log(f"    [frame {fi}] {len(info)} inputs:")
                            for inp in info:
                                self.log(f"      {inp}")
                    except Exception as e:
                        self.log(f"    [frame {fi} dump err] {e}")

                # Strategy A: by name/id/placeholder (case-insensitive)
                subj_selectors = [
                    "input[name='subject']", "input[name='Subject']",
                    "input[id='subject']",   "input[id='Subject']",
                    "input[id*='subject' i]", "input[name*='subject' i]",
                    "input[placeholder*='subject' i]",
                    "input[class*='subject' i]",
                ]
                for fr in all_frames:
                    for sel in subj_selectors:
                        try:
                            el = fr.query_selector(sel)
                            if el:
                                scroll_into_view(fr, el)
                                el.click()
                                time.sleep(0.2)
                                # Use element-level press/type so focus is unambiguous
                                # regardless of whether el is in a child frame
                                el.press("Control+a")
                                el.type(subject)
                                time.sleep(0.15)
                                # Quick verify
                                try:
                                    filled_val = el.input_value()
                                    if subject.lower() not in filled_val.lower():
                                        self.log(f"    [!] Strategy A typed but val={repr(filled_val[:20])}")
                                        continue
                                except Exception:
                                    pass
                                subj_filled = True
                                self.log(f"    Subject filled by selector ({sel})")
                                break
                        except Exception:
                            continue
                    if subj_filled:
                        break

                # Strategy B: positional — grab ALL visible non-readonly inputs,
                # skip From/To (they're pre-filled), pick first EMPTY one
                if not subj_filled:
                    for fi, fr in enumerate(all_frames):
                        try:
                            all_inputs = fr.query_selector_all(
                                "input[type='text'], input[type=''], input:not([type])"
                            )
                            visible = []
                            for inp in all_inputs:
                                try:
                                    if inp.is_visible() and not inp.is_disabled():
                                        val = inp.input_value()
                                        # is_editable() is the safe check —
                                        # get_attribute("readonly") misses bare readonly attrs
                                        try:
                                            editable = inp.is_editable()
                                        except Exception:
                                            editable = True
                                        visible.append((inp, val, editable))
                                except Exception:
                                    pass
                            self.log(f"    [frame {fi}] {len(visible)} visible inputs:")
                            for inp_el, val, editable in visible:
                                self.log(f"      val={repr(val[:30])} editable={editable}")
                            # Subject is typically empty (From/To are pre-filled)
                            for inp_el, val, editable in visible:
                                if not val.strip() and editable:
                                    scroll_into_view(fr, inp_el)
                                    inp_el.click()
                                    time.sleep(0.2)
                                    # Element-level press/type — correct focus regardless of frame
                                    inp_el.press("Control+a")
                                    inp_el.type(subject)
                                    time.sleep(0.15)
                                    # Verify it stuck
                                    try:
                                        chk = inp_el.input_value()
                                        if subject.lower() not in chk.lower():
                                            self.log(f"    [!] Strategy B typed but val={repr(chk[:20])}")
                                            continue
                                    except Exception:
                                        pass
                                    subj_filled = True
                                    self.log("    Subject filled (first empty input)")
                                    break
                            if subj_filled:
                                break
                        except Exception as e:
                            self.log(f"    [subj frame {fi}] {e}")
                            continue

                # Strategy C: last resort — use fill() on every input until one sticks
                if not subj_filled:
                    for fi, fr in enumerate(all_frames):
                        try:
                            all_inputs = fr.query_selector_all("input")
                            for inp_el in all_inputs:
                                try:
                                    if inp_el.is_visible() and not inp_el.is_disabled():
                                        try:
                                            editable = inp_el.is_editable()
                                        except Exception:
                                            editable = True
                                        if not editable:
                                            continue
                                        inp_el.fill(subject)
                                        time.sleep(0.1)
                                        val = inp_el.input_value()
                                        if val.strip() == subject.strip():
                                            subj_filled = True
                                            self.log(f"    Subject filled (brute force frame {fi})")
                                            break
                                except Exception:
                                    continue
                            if subj_filled:
                                break
                        except Exception:
                            continue

                if not subj_filled:
                    self.log("    [!] Subject STILL not filled — check log above for input details")

                time.sleep(0.4)

                # 2. Fill body (rich-text iframe)
                body_filled = False

                # Pass A: contenteditable iframe body — APPEND (keep existing content)
                for fr in all_frames:
                    if fr is tab or fr == tab.main_frame:
                        continue
                    try:
                        editable = fr.evaluate("""
                            () => {
                                if (!document.body) return false;
                                return document.body.isContentEditable ||
                                       document.designMode === 'on' ||
                                       document.body.getAttribute('contenteditable') === 'true';
                            }
                        """)
                        if editable:
                            escaped = msg.replace("'", "&#39;").replace("\n", "<br>")
                            # Remove the banner image(s), insert message in their place
                            # (at the top), keep all existing text below untouched
                            fr.evaluate("""
                                (txt) => {
                                    const imgs = Array.from(document.querySelectorAll('img'));
                                    if (imgs.length > 0) {
                                        // Replace the first img (banner) with our message
                                        const p = document.createElement('p');
                                        p.innerHTML = txt;
                                        imgs[0].parentNode.insertBefore(p, imgs[0]);
                                        // Remove all img tags (banner)
                                        imgs.forEach(img => img.remove());
                                    } else {
                                        // No banner found — prepend to top of body
                                        const p = document.createElement('p');
                                        p.innerHTML = txt;
                                        document.body.insertBefore(p, document.body.firstChild);
                                    }
                                    // Move cursor to end
                                    const range = document.createRange();
                                    range.selectNodeContents(document.body);
                                    range.collapse(false);
                                    const sel = window.getSelection();
                                    if (sel) { sel.removeAllRanges(); sel.addRange(range); }
                                }
                            """, escaped)
                            body_filled = True
                            self.log("    Email body: replaced banner with message at top")
                            break
                    except Exception:
                        continue

                # Pass B: click into iframe body at end, then type (keeps existing content)
                if not body_filled:
                    for fr in all_frames:
                        if fr is tab or fr == tab.main_frame:
                            continue
                        try:
                            body_el = fr.query_selector("body")
                            if body_el:
                                body_el.click()
                                time.sleep(0.3)
                                # Go to end, add message (leave existing text intact)
                                # Use fr.keyboard so events go to the correct frame
                                fr.keyboard.press("Control+End")
                                fr.keyboard.press("Enter")
                                fr.keyboard.type(msg)
                                body_filled = True
                                self.log("    Email body appended (iframe keyboard fallback)")
                                break
                        except Exception:
                            continue

                # Pass C: contenteditable div or textarea on main frame
                if not body_filled:
                    for sel in [
                        "div[contenteditable='true']",
                        "textarea",
                        "[class*='editor' i]",
                        "[id*='editor' i]",
                        "[class*='body' i]",
                    ]:
                        try:
                            el = tab.query_selector(sel)
                            if el and el.is_visible():
                                scroll_into_view(tab, el)
                                el.click()
                                time.sleep(0.2)
                                # Element-level for consistency
                                el.press("Control+End")
                                el.press("Enter")
                                el.type(msg)
                                body_filled = True
                                self.log(f"    Email body appended (main {sel})")
                                break
                        except Exception:
                            continue

                if not body_filled:
                    self.log("    [!] Could not fill email body — still attempting Send")
                    # Do NOT return False here — subject may be filled and
                    # the body might already contain text from a prior draft.
                    # Attempting Send is always worth trying.

                time.sleep(0.5)

                # 3. Click Send — dump all clickable elements first for debug
                for fi, fr in enumerate(all_frames):
                    try:
                        btns = fr.evaluate("""
                        () => Array.from(document.querySelectorAll(
                            'input[type=submit],input[type=button],button,a[onclick]'
                        )).map(el => ({
                            tag: el.tagName,
                            type: el.type || '',
                            id: el.id || '',
                            name: el.name || '',
                            val: (el.value || el.innerText || '').substring(0,30),
                            vis: el.offsetParent !== null,
                        }))
                        """)
                        if btns:
                            self.log(f"    [frame {fi}] buttons/inputs:")
                            for b in btns:
                                self.log(f"      {b}")
                    except Exception:
                        pass

                sent = False
                send_selectors = [
                    "input[value='Send']", "input[value=' Send']",
                    "input[value='send']", "input[value='SEND']",
                    "button:has-text('Send')", "a:has-text('Send')",
                    "[id*='btnSend' i]", "[id*='send' i]",
                    "[name*='send' i]",
                    "input[type='submit']", "button[type='submit']",
                    # NOTE: 'input[type=button]' intentionally omitted —
                    # it's a catch-all that could click Cancel; JS fallback below is safer
                ]
                for fi, fr in enumerate(all_frames):
                    for sel in send_selectors:
                        try:
                            el = fr.query_selector(sel)
                            if el and el.is_visible():
                                scroll_into_view(fr, el)
                                el.click()
                                sent = True
                                self.log(f"    Email sent (frame {fi}, {sel})")
                                time.sleep(2)
                                break
                        except Exception:
                            continue
                    if sent:
                        break

                # Last resort: JS click any element whose text is "Send"
                if not sent:
                    for fi, fr in enumerate(all_frames):
                        try:
                            clicked = fr.evaluate("""
                            () => {
                                const all = Array.from(document.querySelectorAll(
                                    'input,button,a,span,td,div'));
                                for (const el of all) {
                                    const txt = (el.value||el.innerText||'').trim();
                                    if (txt.toLowerCase() === 'send' && el.offsetParent !== null) {
                                        el.click();
                                        return 'clicked:' + el.tagName + ':' + txt;
                                    }
                                }
                                return null;
                            }
                            """)
                            if clicked:
                                sent = True
                                self.log(f"    Email sent via JS ({clicked}) frame {fi}")
                                time.sleep(2)
                                break
                        except Exception:
                            continue

                if not sent:
                    self.log("    [!] Email Send button NOT found — check button dump above")
                return sent

            else:
                # ── TEXT (SMS) popup ────────────────────────────────────────
                # Existing thread view. Reply box is at the BOTTOM.

                # Wait for textarea to appear
                try:
                    tab.wait_for_selector(
                        "textarea, input[type='text'], div[contenteditable='true']",
                        timeout=8000
                    )
                except Exception:
                    pass

                # Scroll every frame to bottom to expose reply box
                for fr in all_frames:
                    try:
                        fr.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    except Exception:
                        pass
                time.sleep(0.5)

                filled = False
                filled_frame = None

                text_selectors = [
                    "textarea",
                    "div[contenteditable='true']",
                    "input[type='text'][id*='message' i]",
                    "input[type='text'][id*='body' i]",
                    "input[type='text'][id*='sms' i]",
                    "input[type='text'][placeholder*='message' i]",
                    "input[type='text'][placeholder*='type' i]",
                    "input[type='text']",
                ]

                for fr in all_frames:
                    for sel in text_selectors:
                        try:
                            # Get all matching elements, pick last visible one
                            # (thread reply box is always at the bottom / last)
                            els = fr.query_selector_all(sel)
                            target = None
                            for el in reversed(els):
                                try:
                                    if el.is_visible():
                                        target = el
                                        break
                                except Exception:
                                    continue
                            if not target:
                                continue

                            scroll_into_view(fr, target)
                            time.sleep(0.3)
                            target.click()
                            time.sleep(0.3)

                            # Clear and type — use element-level methods so focus
                            # is always on the correct element regardless of frame
                            target.press("Control+a")
                            target.press("Backspace")
                            time.sleep(0.1)
                            target.type(msg)
                            time.sleep(0.2)

                            # Verify text was entered
                            try:
                                typed_val = fr.evaluate(
                                    "el => el.value || el.innerText || ''", target)
                            except Exception:
                                typed_val = "(check failed)"
                            self.log(f'    Text typed ({sel}): "{str(typed_val)[:50]}"')

                            if typed_val and str(typed_val).strip():
                                filled = True
                                filled_frame = fr
                                break
                            else:
                                self.log("    [!] Value empty after typing — trying next")
                        except Exception as e:
                            self.log(f"    [text fill {sel}] {e}")
                            continue
                    if filled:
                        break

                if not filled:
                    self.log("    [!] Text compose box not found")
                    try:
                        html = tab.evaluate("document.body.innerHTML.substring(0, 500)")
                        self.log(f"    body: {html}")
                    except Exception:
                        pass
                    return False

                time.sleep(0.4)

                # Click Send button
                sent = False
                send_selectors_text = [
                    "input[value='Send']", "input[value=' Send']",
                    "input[value='SEND']", "input[value='send']",
                    "button:has-text('Send')",
                    "a:has-text('Send')",
                    "[id*='btnSend' i]", "[id*='send' i]",
                    "input[type='submit']",
                    "button[type='submit']",
                ]
                # Search filled_frame first, then others
                search_order = (
                    ([filled_frame] if filled_frame else []) +
                    [f for f in all_frames if f is not filled_frame]
                )
                for fr in search_order:
                    for sel in send_selectors_text:
                        try:
                            els = fr.query_selector_all(sel)
                            for el in els:
                                try:
                                    if el.is_visible():
                                        scroll_into_view(fr, el)
                                        el.click()
                                        sent = True
                                        self.log(f"    Text sent ({sel})")
                                        time.sleep(2)
                                        break
                                except Exception:
                                    continue
                            if sent:
                                break
                        except Exception:
                            continue
                    if sent:
                        break

                # Last resort: JS click any visible element with text "Send"
                if not sent:
                    for fi, fr in enumerate(all_frames):
                        try:
                            clicked = fr.evaluate("""
                            () => {
                                const all = Array.from(document.querySelectorAll(
                                    'input,button,a,span,td,div'));
                                for (const el of all) {
                                    const txt = (el.value||el.innerText||'').trim();
                                    if (txt.toLowerCase() === 'send' && el.offsetParent !== null) {
                                        el.click();
                                        return 'clicked:' + el.tagName + ':' + txt;
                                    }
                                }
                                return null;
                            }
                            """)
                            if clicked:
                                sent = True
                                self.log(f"    Text sent via JS ({clicked}) frame {fi}")
                                time.sleep(2)
                                break
                        except Exception:
                            continue

                if not sent:
                    self.log("    [!] Text Send button not found")
                return sent

        except Exception as e:
            self.log(f"    [send error] {e}")
            return False

    # ── Process one lead ────────────────────────────────────────────────────────
    def process_lead(self, ctx, leads_page, leads_frame, lead, base_msg):
        name = lead["name"]
        self.log(f"  Opening profile: {name}")

        # open_profile clicks the name → right-side panel opens on same page
        # returns the same frame (leads_frame) since everything is in-page
        profile_frame = self.open_profile(ctx, leads_frame, lead)
        if not profile_frame:
            self.log(f"  [skip] Could not open profile for {name}")
            return "skipped", "skipped"

        final_msg = base_msg
        if self.mode == "ai":
            history = read_history(leads_page)
            self.log(f"  {'History found' if history else 'No history'}")
            try:
                final_msg = generate_ai_message(
                    self.openai_key, name, history, self.tone)
                _preview = final_msg[:70] + ('...' if len(final_msg) > 70 else '')
                self.log(f"  AI: \"{_preview}\"")
            except Exception as e:
                self.log(f"  [AI error — using template message]: {e}")

        # DEBUG: dump frames on first lead only to keep log clean
        if lead.get("_debug_first", False):
            self.log("  Scanning page frames for icons...")
            self._debug_frames(leads_page)

        # TEXT
        self.log("  Sending text...")
        text_compose, text_inline = self.open_text_tab(ctx, leads_page)
        text_ok = self.send_in_tab(text_compose, final_msg, "text")
        self.log(f"  Text: {'sent' if text_ok else 'failed'}")
        if text_compose and not text_inline:
            try:
                text_compose.close()
            except Exception:
                pass
            time.sleep(1.5)
        # Bring leads page back and wait for profile panel to still be active
        try:
            leads_page.bring_to_front()
            time.sleep(2)
        except Exception:
            pass
        # Re-verify profile panel is still showing icons before opening email
        # Must check ALL frames — icons live in a child iframe
        _still_frames = [leads_page] + list(leads_page.frames)
        panel_still_up = False
        JS_STILL_CHECK = """
        () => {
            // Check img src (icon images)
            const imgs = Array.from(document.querySelectorAll('img'));
            if (imgs.some(img => {
                const s = (img.getAttribute('src')||'').toLowerCase();
                return s.includes('email') || s.includes('mail') ||
                       s.includes('text') || s.includes('sms') || s.includes('opted');
            })) return true;
            // Also check text labels in case icons are text-only
            const els = Array.from(document.querySelectorAll('a,button,span,div,li'));
            return els.some(el => {
                const txt = (el.innerText || el.textContent || '').trim().toLowerCase();
                return (txt === 'email' || txt === 'text' || txt === 'message' ||
                        txt === 'call' || txt === 'note') &&
                       el.getBoundingClientRect().width > 0;
            });
        }
        """
        for _ in range(8):
            for _sf in _still_frames:
                try:
                    if _sf.evaluate(JS_STILL_CHECK):
                        panel_still_up = True
                        break
                except Exception:
                    continue
            if panel_still_up:
                break
            time.sleep(0.5)
        if not panel_still_up:
            self.log("  [!] Profile panel closed after text — re-opening profile for email")
            leads_page.bring_to_front()
            time.sleep(1)
            profile_frame2 = self.open_profile(ctx, leads_frame, lead)
            if profile_frame2:
                self.log("  Profile re-opened for email")
                time.sleep(1)

        # Check stop before starting email
        if self.stop_event.is_set():
            self.log("  [stopped before email]")
            return (
                "sent" if text_ok else "skipped",
                "skipped",
            )

        # EMAIL
        self.log("  Sending email...")
        email_compose, email_inline = self.open_email_tab(ctx, leads_page)
        email_ok = self.send_in_tab(email_compose, final_msg, "email")
        self.log(f"  Email: {'sent' if email_ok else 'failed'}")
        if email_compose and not email_inline:
            try:
                email_compose.close()
            except Exception:
                pass
            time.sleep(1.0)   # let popup fully close before bringing leads_page forward
        # Always settle before moving to the next lead
        try:
            leads_page.bring_to_front()
            time.sleep(2)
        except Exception:
            pass

        return (
            "sent" if text_ok  else "skipped",
            "sent" if email_ok else "skipped",
        )

    # ── Next page ───────────────────────────────────────────────────────────────
    def go_to_next_page(self, leads_page):
        next_selectors = [
            "a:has-text('Next')", "button:has-text('Next')",
            "[title='Next Page']", "[title='Next']",
            "[aria-label='Next Page']", "[aria-label='Next']",
            "a.next", "li.next a", ".pagination-next", ".pager-next a",
            "a[title='Next Page']", "input[title='Next Page']",
            "td[title='Next Page']", "a[id*='Next']", "input[id*='Next']",
            # SSRS report viewer buttons
            "input[id$='_Next']", "a[id$='_Next']",
        ]

        for ctx in ([leads_page] + list(leads_page.frames)):
            for sel in next_selectors:
                try:
                    el = ctx.query_selector(sel)
                    if el and el.is_visible():
                        disabled = el.get_attribute("disabled")
                        cls = el.get_attribute("class") or ""
                        # disabled attr may be bare (returns '') or 'true'/'disabled'
                        # use 'is not None' so even bare disabled= '' is caught
                        if disabled is not None or "disabled" in cls.lower():
                            continue
                        scroll_into_view(ctx, el)
                        el.click()
                        time.sleep(3)
                        try:
                            leads_page.wait_for_load_state("networkidle", timeout=20000)
                        except Exception:
                            pass
                        time.sleep(2)
                        self.log("  Moved to next page.")
                        return True
                except Exception:
                    continue
        return False

    # ── Main ────────────────────────────────────────────────────────────────────
    def run(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=False,
                slow_mo=80,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--start-maximized",
                ],
            )
            context = browser.new_context(
                viewport=None,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
            )
            context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
            leads_page = context.new_page()

            try:
                ok = self.wait_for_ready(leads_page)
                if not ok or self.stop_event.is_set():
                    return

                # Wait for the page to fully settle after login confirmation
                self.log("Waiting for page to stabilize...")
                time.sleep(4)

                # Verify we are still on VinConnect (not redirected to SSO)
                for _check in range(10):
                    cur_url = leads_page.url
                    if "coxautoinc.com/vinconnect" in cur_url or "vinsolutions" in cur_url.lower():
                        break
                    self.log(f"  Page redirected to: {cur_url}")
                    self.log("  Waiting for VinConnect to finish loading...")
                    time.sleep(3)
                else:
                    self.log("[!] Page never returned to VinConnect.")
                    self.log("    Please close and reopen the app, log in again,")
                    self.log("    navigate to your leads page, then click I AM LOGGED IN.")
                    return

                # Extra settle time
                try:
                    leads_page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                time.sleep(2)

                mode_label = (f"AI — {self.tone}" if self.mode == "ai"
                              else "Fixed Template")
                page_num = 1

                while True:
                    if self.stop_event.is_set():
                        self.log("\n[stopped]")
                        break

                    self.log(f"\n{'='*54}")
                    self.log(f"  PAGE {page_num}")
                    self.log(f"{'='*54}")

                    # Scroll to top of leads page before scanning
                    try:
                        leads_page.evaluate("window.scrollTo(0,0)")
                        time.sleep(0.5)
                    except Exception:
                        pass

                    # Retry find_leads_frame up to 4 times in case of JS context errors
                    leads_frame, rows = None, []
                    for attempt in range(4):
                        try:
                            leads_frame, rows = self.find_leads_frame(leads_page)
                            if rows:
                                break
                        except Exception as _e:
                            self.log(f"  [retry {attempt+1}] {_e}")
                        if attempt < 3:
                            self.log(f"  Page settling... retrying in 3s")
                            time.sleep(3)
                            try:
                                leads_page.wait_for_load_state("networkidle", timeout=5000)
                            except Exception:
                                pass

                    if not rows:
                        cur_url = leads_page.url
                        self.log("[!] No leads found on this page.")
                        if "authorize" in cur_url or "login" in cur_url or "logon" in cur_url:
                            self.log("    VinConnect redirected to a login page.")
                            self.log("    Close the app, reopen, log in, navigate")
                            self.log("    to leads, then click I AM LOGGED IN.")
                        else:
                            self.log(f"    Current URL: {cur_url}")
                            self.log("    Make sure you are on your My Leads page.")
                        break

                    leads = self.snapshot_leads(leads_frame, rows)
                    if not leads:
                        self.log("[!] Table found but no lead names readable.")
                        break

                    self.log(f"Found {len(leads)} lead(s)  |  {mode_label}\n")

                    for i, lead in enumerate(leads, 1):
                        if self.stop_event.is_set():
                            self.log("\n[stopped]")
                            break

                        name = lead["name"]
                        self.log(f"\n[{i}/{len(leads)}] {name}")
                        if i == 1 and page_num == 1:
                            lead["_debug_first"] = True

                        base_msg = (
                            self.message.replace("[Name]", name.split()[0])
                            if self.mode == "template"
                            else self.message
                        )

                        t_status, e_status = self.process_lead(
                            context, leads_page, leads_frame, lead, base_msg
                        )
                        self.results.append({
                            "name": name, "text": t_status,
                            "email": e_status, "page": page_num,
                        })
                        time.sleep(1)

                    if self.stop_event.is_set():
                        break

                    self.log(f"\nPage {page_num} complete. Checking for next page...")
                    if not self.go_to_next_page(leads_page):
                        self.log("No more pages. All done.")
                        break
                    page_num += 1

                # Summary
                self.log("\n" + "=" * 54)
                self.log("FINAL SUMMARY")
                self.log("=" * 54)
                for r in self.results:
                    self.log(
                        f"  [p{r['page']}] {r['name']:<24}  "
                        f"Text:{r['text']:<8}  Email:{r['email']}"
                    )
                self.log(f"\nDone. {len(self.results)} lead(s) across {page_num} page(s).")

            except RuntimeError as e:
                self.log(f"\n[ERROR] {e}")
            except Exception as e:
                self.log(f"\n[ERROR] {e}")
            finally:
                time.sleep(2)
                try:
                    browser.close()
                except Exception:
                    pass




# ══════════════════════════════════════════════════════════════════════════════
# UPDATE WINDOW
# ══════════════════════════════════════════════════════════════════════════════

class UpdateWindow(tk.Toplevel):
    """
    Non-blocking update notification shown when a new version is available.
    User can choose to update now or skip.
    apply_fn() downloads files and relaunches the app.
    """
    NAVY  = "#0d1b2a"
    DARK  = "#1a2535"
    GOLD  = "#c9a84c"
    GREEN = "#1a6b3a"
    GRAY  = "#607080"

    def __init__(self, master, version, release_notes, apply_fn):
        super().__init__(master)
        self._apply_fn = apply_fn
        self.title("VinConnect Update Available")
        self.geometry("480x300")
        self.resizable(False, False)
        self.configure(bg=self.DARK)
        self.grab_set()
        self._build(version, release_notes)
        self.lift()

    def _build(self, version, release_notes):
        # Header
        hdr = tk.Frame(self, bg=self.NAVY, pady=14)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Update Available",
                 font=("Helvetica", 14, "bold"),
                 bg=self.NAVY, fg=self.GOLD).pack()
        tk.Label(hdr, text=f"VinConnect v{version} is ready to install",
                 font=("Helvetica", 9), bg=self.NAVY, fg=self.GRAY).pack(pady=(2, 0))

        body = tk.Frame(self, bg=self.DARK, padx=24, pady=18)
        body.pack(fill="both", expand=True)

        tk.Label(body, text="What's new:",
                 font=("Helvetica", 9, "bold"),
                 bg=self.DARK, fg=self.GOLD).pack(anchor="w")

        tk.Label(body, text=release_notes,
                 font=("Helvetica", 9), bg=self.DARK, fg="white",
                 wraplength=420, justify="left").pack(anchor="w", pady=(4, 16))

        tk.Label(body,
                 text="The app will restart automatically after updating. Takes about 10 seconds.",
                 font=("Helvetica", 8), bg=self.DARK, fg=self.GRAY,
                 wraplength=420, justify="left").pack(anchor="w", pady=(0, 12))

        btn_row = tk.Frame(body, bg=self.DARK)
        btn_row.pack(fill="x")

        tk.Button(
            btn_row, text="Update Now",
            command=self._do_update,
            bg=self.GREEN, fg="white",
            font=("Helvetica", 11, "bold"),
            relief="flat", padx=16, pady=8,
            cursor="hand2",
            activebackground="#27ae60", activeforeground="white",
        ).pack(side="left", padx=(0, 10))

        tk.Button(
            btn_row, text="Skip This Update",
            command=self.destroy,
            bg="#2a3545", fg=self.GRAY,
            font=("Helvetica", 10),
            relief="flat", padx=12, pady=8,
            cursor="hand2",
            activebackground="#2a3545", activeforeground="white",
        ).pack(side="left")

    def _do_update(self):
        # Swap button for a progress label
        for w in self.winfo_children():
            w.destroy()
        prog = tk.Frame(self, bg=self.DARK, padx=24, pady=40)
        prog.pack(fill="both", expand=True)
        tk.Label(prog, text="Downloading update...",
                 font=("Helvetica", 12, "bold"),
                 bg=self.DARK, fg=self.GOLD).pack()
        tk.Label(prog, text="The app will restart in a moment.",
                 font=("Helvetica", 9), bg=self.DARK, fg=self.GRAY).pack(pady=(8, 0))
        self.update()
        # Run download in background thread, relaunch happens inside apply_fn
        threading.Thread(target=self._apply_fn, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# LICENSE WINDOW
# ══════════════════════════════════════════════════════════════════════════════

class LicenseWindow(tk.Toplevel):
    """
    Shown on launch when no valid license key is found.
    User enters their key; on success, on_valid(plan, expiry) is called.
    If license module is unavailable, on_valid is called immediately (dev mode).
    """
    NAVY  = "#0d1b2a"
    DARK  = "#1a2535"
    GOLD  = "#c9a84c"
    GREEN = "#1a6b3a"
    RED   = "#b03020"
    GRAY  = "#607080"

    def __init__(self, master, on_valid):
        super().__init__(master)
        self._on_valid = on_valid
        self.title("VinConnect — License Activation")
        self.geometry("520x420")
        self.resizable(False, False)
        self.configure(bg=self.DARK)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.grab_set()   # Modal
        self._build()
        self.lift()
        self.focus_force()

    def _on_close(self):
        """Closing the license window without a valid key exits the app."""
        self.master.destroy()

    def _lbl(self, parent, text, size=10, bold=False, color=None):
        return tk.Label(
            parent, text=text,
            bg=self.DARK,
            fg=color or "white",
            font=("Helvetica", size, "bold" if bold else "normal"),
            justify="left",
        )

    def _build(self):
        # Header
        hdr = tk.Frame(self, bg=self.NAVY, pady=18)
        hdr.pack(fill="x")
        tk.Label(hdr, text="VinConnect Follow-Up Tool",
                 font=("Helvetica", 15, "bold"),
                 bg=self.NAVY, fg=self.GOLD).pack()
        tk.Label(hdr, text="License Activation",
                 font=("Helvetica", 10),
                 bg=self.NAVY, fg=self.GRAY).pack(pady=(2, 0))

        body = tk.Frame(self, bg=self.DARK, padx=30, pady=20)
        body.pack(fill="both", expand=True)

        self._lbl(body,
            "Enter your VinConnect license key below.",
            size=10).pack(anchor="w", pady=(0, 4))
        self._lbl(body,
            "Keys look like:  VINC-A1B2C3-D4E5F6-G7H8I9-J0K1L2",
            size=8, color=self.GRAY).pack(anchor="w", pady=(0, 12))

        self._lbl(body, "License Key", bold=True, color=self.GOLD).pack(anchor="w")
        self.key_var = tk.StringVar()
        key_entry = tk.Entry(
            body, textvariable=self.key_var,
            width=44,
            bg=self.NAVY, fg="white",
            insertbackground="white", relief="flat",
            font=("Courier", 11),
            highlightbackground=self.GOLD, highlightthickness=1,
        )
        key_entry.pack(fill="x", pady=(4, 4))
        key_entry.bind("<Return>", lambda e: self._activate())
        key_entry.focus_set()

        # Status label (shows error or success)
        self.status_var = tk.StringVar()
        self.status_lbl = tk.Label(
            body, textvariable=self.status_var,
            bg=self.DARK, fg=self.RED,
            font=("Helvetica", 9), wraplength=440, justify="left",
        )
        self.status_lbl.pack(anchor="w", pady=(0, 12))

        tk.Button(
            body, text="ACTIVATE",
            command=self._activate,
            bg=self.GREEN, fg="white",
            font=("Helvetica", 12, "bold"),
            relief="flat", padx=20, pady=10,
            cursor="hand2",
            activebackground="#27ae60",
            activeforeground="white",
        ).pack(fill="x")

        # Footer
        footer = tk.Frame(self, bg=self.NAVY, pady=10)
        footer.pack(fill="x", side="bottom")
        tk.Label(
            footer,
            text="Need a license key? Visit  vinconnect.io  or contact your sales rep.",
            bg=self.NAVY, fg=self.GRAY,
            font=("Helvetica", 8),
        ).pack()

    def _activate(self):
        key = self.key_var.get().strip()
        if not key:
            self.status_var.set("Please enter your license key.")
            return

        if not LICENSE_AVAILABLE:
            # Dev mode — license module missing, skip check
            self.status_var.set("")
            self.destroy()
            self._on_valid("Dev", None)
            return

        result = _lic.validate_key(key)
        if result["valid"]:
            _lic.save_key(key)
            self.status_lbl.config(fg="#27ae60")
            expiry_str = result["expiry"].strftime("%B %d, %Y")
            self.status_var.set(
                f"Activated!  Plan: {result['plan']}  |  Expires: {expiry_str}"
            )
            self.after(1200, lambda: (
                self.destroy(),
                self._on_valid(result["plan"], result["expiry"]),
            ))
        else:
            self.status_lbl.config(fg=self.RED)
            self.status_var.set(result["error"] or "Invalid license key.")


# ══════════════════════════════════════════════════════════════════════════════
# SETUP WINDOW
# ══════════════════════════════════════════════════════════════════════════════

class SetupWindow(tk.Toplevel):
    """
    First-launch setup screen.
    Collects salesperson name + dealership name, saves to config,
    then opens the main App window.
    """
    NAVY  = "#0d1b2a"
    DARK  = "#1a2535"
    GOLD  = "#c9a84c"
    GRAY  = "#607080"
    GREEN = "#1a6b3a"

    def __init__(self, parent, cfg, on_complete):
        super().__init__(parent)
        self.title("VinSolutions Follow-Up Tool — First-Time Setup")
        self.geometry("520x440")
        self.resizable(False, False)
        self.configure(bg=self.DARK)
        self.grab_set()   # modal
        self._cfg = cfg
        self._on_complete = on_complete
        self._build()
        self.update_idletasks()
        x = (self.winfo_screenwidth()  - 520) // 2
        y = (self.winfo_screenheight() - 440) // 2
        self.geometry(f"520x440+{x}+{y}")

    def _lbl(self, parent, text, bold=False, size=10, color=None):
        return tk.Label(parent, text=text,
                        bg=self.DARK,
                        fg=color or "white",
                        font=("Helvetica", size, "bold" if bold else "normal"))

    def _entry(self, parent, width=42):
        return tk.Entry(parent, width=width,
                        bg=self.NAVY, fg="white",
                        insertbackground="white", relief="flat",
                        font=("Helvetica", 11),
                        highlightbackground=self.GOLD,
                        highlightthickness=1)

    def _build(self):
        hdr = tk.Frame(self, bg=self.NAVY, pady=18)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Welcome to VinSolutions Follow-Up Tool",
                 font=("Helvetica", 14, "bold"),
                 bg=self.NAVY, fg=self.GOLD).pack()
        tk.Label(hdr, text="Let's set up your account  —  takes 30 seconds",
                 font=("Helvetica", 9),
                 bg=self.NAVY, fg=self.GRAY).pack(pady=(4, 0))

        body = tk.Frame(self, bg=self.DARK, padx=36, pady=24)
        body.pack(fill="both", expand=True)

        self._lbl(body, "Your First Name", bold=True, color=self.GOLD).pack(anchor="w")
        self._lbl(body, "The name you use when texting / emailing leads",
                  size=8, color=self.GRAY).pack(anchor="w", pady=(0, 4))
        self.name_entry = self._entry(body)
        self.name_entry.pack(anchor="w", pady=(0, 16))
        self.name_entry.insert(0, self._cfg.get("salesperson_name", ""))

        self._lbl(body, "Dealership Name", bold=True, color=self.GOLD).pack(anchor="w")
        self._lbl(body, "Full dealership name as it should appear in messages",
                  size=8, color=self.GRAY).pack(anchor="w", pady=(0, 4))
        self.dealer_entry = self._entry(body)
        self.dealer_entry.pack(anchor="w", pady=(0, 8))
        self.dealer_entry.insert(0, self._cfg.get("dealership_name", ""))

        self._lbl(body, "Message preview:", size=8, color=self.GRAY).pack(anchor="w", pady=(8, 2))
        self.preview_var = tk.StringVar()
        tk.Label(body, textvariable=self.preview_var,
                 bg="#162840", fg="#7dc87d",
                 font=("Helvetica", 9), wraplength=440,
                 justify="left", padx=10, pady=8).pack(fill="x", pady=(0, 16))
        self._update_preview()

        self.name_entry.bind("<KeyRelease>",   lambda e: self._update_preview())
        self.dealer_entry.bind("<KeyRelease>", lambda e: self._update_preview())

        tk.Button(body, text="SAVE & CONTINUE",
                  command=self._save,
                  bg=self.GREEN, fg="white",
                  font=("Helvetica", 12, "bold"),
                  relief="flat", padx=20, pady=10,
                  cursor="hand2",
                  activebackground="#27ae60",
                  activeforeground="white").pack(fill="x")

    def _update_preview(self):
        n = self.name_entry.get().strip()   or "[Your Name]"
        d = self.dealer_entry.get().strip() or "[Your Dealership]"
        self.preview_var.set(make_default_message(n, d))

    def _save(self):
        n = self.name_entry.get().strip()
        d = self.dealer_entry.get().strip()
        if not n:
            messagebox.showerror("Missing", "Please enter your first name.", parent=self)
            return
        if not d:
            messagebox.showerror("Missing", "Please enter your dealership name.", parent=self)
            return
        self._cfg["salesperson_name"] = n
        self._cfg["dealership_name"]  = d
        self._cfg["setup_complete"]   = True
        if not self._cfg.get("message", "").strip():
            self._cfg["message"] = make_default_message(n, d)
        save_config(self._cfg)
        self.destroy()
        self._on_complete(self._cfg)



# ══════════════════════════════════════════════════════════════════════════════
# GUI
# ══════════════════════════════════════════════════════════════════════════════

class App(tk.Tk):
    NAVY  = "#0d1b2a"
    DARK  = "#1a2535"
    GOLD  = "#c9a84c"
    GREEN = "#1a6b3a"
    RED   = "#b03020"
    GRAY  = "#607080"

    def __init__(self):
        super().__init__()
        self.withdraw()   # hide until license + setup confirmed
        self.title("VinSolutions Follow-Up Tool")
        self.geometry("840x840")
        self.resizable(True, True)
        self.configure(bg=self.DARK)

        self._stop_event  = threading.Event()
        self._ready_event = threading.Event()
        self._thread      = None
        self._cfg         = load_config()
        self._license_plan   = ""
        self._license_expiry = None
        self._mode_var    = tk.StringVar(value=self._cfg.get("mode", "template"))
        self._tone_var    = tk.StringVar(value=self._cfg.get("tone", "Soft Follow-Up"))

        # ── Step 1: License check ─────────────────────────────────────────────
        self._check_license_then_setup()

    # ── License flow ──────────────────────────────────────────────────────────

    def _check_license_then_setup(self):
        """Validate saved license key; show LicenseWindow if missing/expired."""
        if not LICENSE_AVAILABLE:
            # Dev mode — no license.py, skip check
            self._after_license("Dev", None)
            return

        result = _lic.check_saved_license()
        if result["valid"]:
            self._after_license(result["plan"], result["expiry"])
        else:
            # No valid key — show activation window
            LicenseWindow(self, self._after_license)

    def _after_license(self, plan, expiry):
        """Called after license is confirmed valid. Move on to setup check."""
        self._license_plan   = plan
        self._license_expiry = expiry
        # ── Step 2: Kick off background update check ────────────────────────────
        if UPDATER_AVAILABLE:
            _Updater(
                on_update_available=self._on_update_available,
                tk_root=self,
            ).start()
        # ── Step 3: Setup check ───────────────────────────────────────────────
        if is_setup_complete(self._cfg):
            self._apply_identity(self._cfg)
            self._show_main()
        else:
            SetupWindow(self, self._cfg, self._on_setup_complete)

    def _on_update_available(self, version, release_notes, apply_fn):
        """Called on main thread when updater finds a newer version."""
        UpdateWindow(self, version, release_notes, apply_fn)

    def _on_setup_complete(self, cfg):
        """Called by SetupWindow after dealer saves their info."""
        self._cfg = cfg
        self._apply_identity(cfg)
        self._show_main()

    def _apply_identity(self, cfg):
        """Set global name/dealership vars used by the bot and AI prompts."""
        global SALESPERSON_NAME, DEALERSHIP_NAME
        SALESPERSON_NAME = cfg.get("salesperson_name", "").strip()
        DEALERSHIP_NAME  = cfg.get("dealership_name",  "").strip()

    def _show_main(self):
        """Build and show the main window after setup is confirmed."""
        self._mode_var = tk.StringVar(value=self._cfg.get("mode", "template"))
        self._tone_var = tk.StringVar(value=self._cfg.get("tone", "Soft Follow-Up"))
        self._build_ui()
        self._load_cfg()
        self._on_mode_change()
        self.deiconify()

    def _entry(self, parent, width=62, show=None):
        e = tk.Entry(parent, width=width, bg=self.NAVY, fg="white",
                     insertbackground="white", relief="flat",
                     font=("Helvetica", 10),
                     highlightbackground=self.GOLD, highlightthickness=1)
        if show:
            e.config(show=show)
        return e

    def _divider(self, parent, row):
        tk.Frame(parent, bg=self.GOLD, height=1).grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=8)

    def _build_ui(self):
        hdr = tk.Frame(self, bg=self.NAVY, pady=14)
        hdr.pack(fill="x")
        tk.Label(hdr, text="VinConnect Follow-Up Tool  v14.0",
                 font=("Helvetica", 16, "bold"),
                 bg=self.NAVY, fg=self.GOLD).pack()
        # Show the dealer's own name — set dynamically from setup
        subtitle = f"{DEALERSHIP_NAME}  —  {SALESPERSON_NAME}" if DEALERSHIP_NAME else ""
        tk.Label(hdr, text=subtitle,
                 font=("Helvetica", 10), bg=self.NAVY, fg=self.GRAY).pack()
        # License plan + expiry warning
        if self._license_plan and self._license_plan != "Dev":
            from datetime import date as _date
            expiry = self._license_expiry
            if expiry:
                days_left = (expiry - _date.today()).days
                if days_left <= 30:
                    warn_color = "#e74c3c" if days_left <= 7 else "#e67e22"
                    warn_text  = (
                        f"License expires in {days_left} day{'s' if days_left != 1 else ''}  "
                        f"({expiry.strftime('%b %d')}) — renew at vinconnect.io"
                    )
                    tk.Label(hdr, text=warn_text,
                             font=("Helvetica", 8, "bold"),
                             bg=self.NAVY, fg=warn_color).pack(pady=(2, 0))
                else:
                    lic_text = f"Plan: {self._license_plan}  |  License valid until {expiry.strftime('%b %d, %Y')}"
                    tk.Label(hdr, text=lic_text,
                             font=("Helvetica", 8),
                             bg=self.NAVY, fg=self.GRAY).pack(pady=(2, 0))
        # Change Account link
        tk.Button(hdr, text="Change Account",
                  command=self._change_account,
                  bg=self.NAVY, fg=self.GRAY,
                  font=("Helvetica", 8), relief="flat",
                  cursor="hand2",
                  activebackground=self.NAVY,
                  activeforeground=self.GOLD).pack(pady=(2, 0))

        body = tk.Frame(self, bg=self.DARK, padx=22, pady=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        row = 0

        notice = tk.Frame(body, bg="#162840", padx=12, pady=12)
        notice.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0,12))
        row += 1
        tk.Label(notice,
                 text=(
                     "HOW TO USE:\n"
                     "1.  Click START — browser opens on VinConnect.\n"
                     "2.  Log in if prompted, then navigate to your leads page.\n"
                     "3.  Once leads are visible (partial is fine), click  [ I AM LOGGED IN ].\n"
                     "    The tool scrolls through everything automatically."
                 ),
                 bg="#162840", fg="#7dc87d",
                 font=("Helvetica", 9), wraplength=720, justify="left").pack(anchor="w")

        tk.Label(body, text="FOLLOW-UP MODE", bg=self.DARK, fg=self.GOLD,
                 font=("Helvetica", 9, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0,8))
        row += 1
        mf = tk.Frame(body, bg=self.DARK)
        mf.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0,6))
        row += 1
        for val, label in [
            ("template", "Fixed Template  (free — same message to everyone)"),
            ("ai",       "AI Personalized  (reads history, writes custom message per lead)"),
        ]:
            tk.Radiobutton(mf, text=label, variable=self._mode_var, value=val,
                           command=self._on_mode_change,
                           bg=self.DARK, fg="white", selectcolor=self.NAVY,
                           activebackground=self.DARK, activeforeground="white",
                           font=("Helvetica", 10)).pack(anchor="w", pady=2)

        self.key_frame = tk.Frame(body, bg=self.DARK)
        self.key_frame.grid(row=row, column=0, columnspan=2, sticky="ew")
        row += 1
        self.key_frame.columnconfigure(1, weight=1)
        tk.Label(self.key_frame, text="OpenAI Key:", bg=self.DARK, fg=self.GOLD,
                 font=("Helvetica", 10, "bold")).grid(row=0, column=0, sticky="w", pady=3)
        self.key_entry = self._entry(self.key_frame, show="*")
        self.key_entry.grid(row=0, column=1, sticky="ew", pady=3)
        tk.Label(self.key_frame, text="platform.openai.com  |  ~$0.01-0.05 per lead",
                 bg=self.DARK, fg=self.GRAY,
                 font=("Helvetica", 8)).grid(row=1, column=1, sticky="w")

        self.tone_frame = tk.Frame(body, bg=self.DARK)
        self.tone_frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8,4))
        row += 1
        self.tone_frame.columnconfigure(0, weight=1)
        self.tone_frame.columnconfigure(1, weight=1)
        tk.Label(self.tone_frame, text="AI TONE / STYLE", bg=self.DARK, fg=self.GOLD,
                 font=("Helvetica", 9, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0,8))
        tone_colors = {
            "Appointment Setter": "#1a5276",
            "Aggressive Close":   "#6e1a0d",
            "Soft Follow-Up":     "#1a6b3a",
            "Value-First":        "#4a3000",
        }
        tone_tags = {
            "Appointment Setter": "[Appt]",
            "Aggressive Close":   "[Close]",
            "Soft Follow-Up":     "[Soft]",
            "Value-First":        "[Value]",
        }
        for (r, c), tone in zip([(1,0),(1,1),(2,0),(2,1)], TONE_OPTIONS):
            card = tk.Frame(self.tone_frame, bg=tone_colors[tone], padx=10, pady=8)
            card.grid(row=r, column=c, sticky="ew", padx=4, pady=4)
            tk.Radiobutton(card, text=f"{tone_tags[tone]}  {tone}",
                           variable=self._tone_var, value=tone,
                           bg=tone_colors[tone], fg="white",
                           selectcolor=tone_colors[tone],
                           activebackground=tone_colors[tone],
                           activeforeground="white",
                           font=("Helvetica", 10, "bold")).pack(anchor="w")
            tk.Label(card, text=TONE_DESCRIPTIONS[tone],
                     bg=tone_colors[tone], fg="#ccddcc",
                     font=("Helvetica", 8), wraplength=310,
                     justify="left").pack(anchor="w", pady=(4,0))

        self._divider(body, row); row += 1

        self.msg_section = tk.Frame(body, bg=self.DARK)
        self.msg_section.grid(row=row, column=0, columnspan=2, sticky="ew")
        row += 1
        tk.Label(self.msg_section, text="FOLLOW-UP MESSAGE",
                 bg=self.DARK, fg=self.GOLD,
                 font=("Helvetica", 9, "bold")).pack(anchor="w", pady=(0,6))
        self.msg_text = tk.Text(self.msg_section, height=4,
                                bg=self.NAVY, fg="white",
                                insertbackground="white", relief="flat",
                                font=("Helvetica", 10),
                                highlightbackground=self.GOLD,
                                highlightthickness=1, wrap="word")
        self.msg_text.pack(fill="x", pady=3)
        tk.Label(self.msg_section,
                 text="Use [Name] to auto-insert the lead's first name",
                 bg=self.DARK, fg=self.GRAY,
                 font=("Helvetica", 8)).pack(anchor="w")

        btn_frame = tk.Frame(self, bg=self.DARK, padx=22, pady=8)
        btn_frame.pack(fill="x")

        self.run_btn = tk.Button(
            btn_frame, text="START",
            command=self._start_run, bg=self.GREEN, fg="white",
            font=("Helvetica", 12, "bold"), relief="flat",
            padx=24, pady=10, cursor="hand2",
            activebackground="#27ae60", activeforeground="white")
        self.run_btn.pack(side="left", padx=(0,8))

        self.login_btn = tk.Button(
            btn_frame, text="I AM LOGGED IN",
            command=self._confirm_login,
            bg="#b8860b", fg="white",
            font=("Helvetica", 11, "bold"), relief="flat",
            padx=20, pady=10, cursor="hand2",
            activebackground="#daa520", activeforeground="white",
            state="disabled")
        self.login_btn.pack(side="left", padx=(0,8))

        self.stop_btn = tk.Button(
            btn_frame, text="STOP",
            command=self._stop_run, bg=self.RED, fg="white",
            font=("Helvetica", 11, "bold"), relief="flat",
            padx=20, pady=10, cursor="hand2", state="disabled",
            activebackground="#e74c3c", activeforeground="white")
        self.stop_btn.pack(side="left", padx=(0,8))

        tk.Button(btn_frame, text="Save Settings",
                  command=self._save_settings,
                  bg=self.DARK, fg=self.GOLD,
                  font=("Helvetica", 9), relief="flat",
                  padx=10, pady=10, cursor="hand2").pack(side="right")

        log_frame = tk.Frame(self, bg=self.DARK, padx=22, pady=4)
        log_frame.pack(fill="both", expand=True)
        tk.Label(log_frame, text="LOG", bg=self.DARK, fg=self.GOLD,
                 font=("Helvetica", 9, "bold")).pack(anchor="w")
        self.log_box = scrolledtext.ScrolledText(
            log_frame, height=10, bg="#0a1520", fg="#a0c8a0",
            font=("Courier", 9), relief="flat", state="disabled",
            wrap="word", highlightbackground="#334455", highlightthickness=1)
        self.log_box.pack(fill="both", expand=True)

    def _on_mode_change(self):
        if self._mode_var.get() == "ai":
            self.key_frame.grid()
            self.tone_frame.grid()
            self.msg_section.grid_remove()
        else:
            self.key_frame.grid_remove()
            self.tone_frame.grid_remove()
            self.msg_section.grid()

    def _load_cfg(self):
        self.key_entry.insert(0, self._cfg.get("openai_key", ""))
        default_msg = make_default_message(
            self._cfg.get("salesperson_name", ""),
            self._cfg.get("dealership_name",  ""),
        )
        self.msg_text.insert("1.0", self._cfg.get("message", "") or default_msg)
        self._tone_var.set(self._cfg.get("tone", "Soft Follow-Up"))

    def _save_settings(self):
        self._cfg.update({
            "openai_key": self.key_entry.get().strip(),
            "message":    self.msg_text.get("1.0", "end").strip(),
            "mode":       self._mode_var.get(),
            "tone":       self._tone_var.get(),
        })
        save_config(self._cfg)
        messagebox.showinfo("Saved", "Settings saved.")

    def _change_account(self):
        """Re-open setup screen to update name / dealership."""
        SetupWindow(self, self._cfg, self._on_account_changed)

    def _on_account_changed(self, cfg):
        self._cfg = cfg
        self._apply_identity(cfg)
        # Rebuild the whole UI so header subtitle reflects new name
        for widget in self.winfo_children():
            widget.destroy()
        self._show_main()

    def _log(self, msg):
        """Thread-safe log — always dispatched on the Tk main thread."""
        ts   = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}]  {msg}\n"
        def _write():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", line)
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.after(0, _write)

    def _confirm_login(self):
        self._ready_event.set()
        self.login_btn.config(state="disabled", text="Logged In")
        self._log("Confirmed — reading leads...")

    def _start_run(self):
        mode    = self._mode_var.get()
        tone    = self._tone_var.get()
        message = self.msg_text.get("1.0", "end").strip()
        api_key = self.key_entry.get().strip()

        if mode == "template" and not message:
            messagebox.showerror("Missing", "Please enter a follow-up message.")
            return
        if mode == "ai" and not api_key:
            messagebox.showerror("Missing OpenAI Key",
                "AI mode needs an OpenAI API key.\nGet one at platform.openai.com")
            return

        self._stop_event.clear()
        self._ready_event.clear()
        self.run_btn.config(state="disabled")
        self.login_btn.config(state="normal", text="I AM LOGGED IN")
        self.stop_btn.config(state="normal")
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

        label = f"AI — {tone}" if mode == "ai" else "Fixed Template"
        self._log(f"Starting — Mode: {label}")
        self._log("-" * 50)

        bot = VinSolutionsBot(
            message=message, mode=mode, openai_key=api_key, tone=tone,
            log_fn=self._log, stop_event=self._stop_event,
            ready_event=self._ready_event,
        )

        def run_thread():
            bot.run()
            self.after(0, self._run_finished)

        self._thread = threading.Thread(target=run_thread, daemon=True)
        self._thread.start()

    def _stop_run(self):
        self._stop_event.set()
        self._ready_event.set()
        self._log("Stopping...")
        self.stop_btn.config(state="disabled")

    def _run_finished(self):
        self.run_btn.config(state="normal")
        self.login_btn.config(state="disabled")
        self.stop_btn.config(state="disabled")
        self._log("\nRun complete. Browser closed.")


if __name__ == "__main__":
    App().mainloop()
