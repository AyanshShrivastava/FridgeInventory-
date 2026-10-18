"""FridgeInventory: a voice- and text-driven fridge tracker for Indian kitchens.

Run Streamlit UI:  streamlit run smart.py
Run FastAPI mode:  SERVICE_MODE=api uvicorn smart:api --host 0.0.0.0 --port 10000
                   OR: python smart.py api
Needs:             database.py (EXPIRY_DATABASE) next to this file,
                   and .streamlit/secrets.toml with [google] and [firebase] sections.
"""

import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import sys
import time
from datetime import date, datetime, timedelta
from enum import Enum
from functools import lru_cache
from html import escape as esc
from zoneinfo import ZoneInfo

import firebase_admin
import speech_recognition as sr
import streamlit as st
from firebase_admin import credentials, firestore
from google import genai
from google.api_core.exceptions import AlreadyExists
from google.genai import types
from pydantic import BaseModel, Field

try:
    import extra_streamlit_components as stx
except ImportError:
    stx = None

from database import CHEESE_DATE_REQUIRED_ITEMS, EXPIRY_DATABASE

# ==========================================
# 1. CONSTANTS, MODELS & VOCABULARY
# (Strictly NO st.* calls at import time so Streamlit 1.66 ASGI import succeeds!)
# ==========================================
API_MODE = (
    (len(sys.argv) > 1 and sys.argv[1].lower() == "api")
    or os.getenv("SERVICE_MODE", "").strip().lower() == "api"
)

log = logging.getLogger("fridgeinventory")
IST = ZoneInfo("Asia/Kolkata")
APP_URL = (
    os.getenv("APP_URL")
    or os.getenv("RENDER_EXTERNAL_URL")
    or "https://fridgeinventory.streamlit.app"
).rstrip("/")

# Keep the exact Gemini 3.x model IDs in the requested order.
SUPPORTED_GEMINI_MODELS = (
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
)
DEFAULT_MODEL = "gemini-3.5-flash-lite"
MODEL_FALLBACKS = list(SUPPORTED_GEMINI_MODELS)

PIN_ITERATIONS = 200_000
MAX_CMD_CHARS = 200
MAX_AI_LOOKUPS = 4          # unknown items per command (cost guard)
MAX_TRIES = 5               # wrong PINs before a short lockout
LOCK_SECONDS = 60
SESSION_COOKIE_NAME = "fi_session_v2"
SESSION_DAYS = 30
_cookie_manager = None

# Process-level singletons (avoids @st.cache_resource import-time warnings in Streamlit 1.66)
_FIRESTORE_CLIENT = None
_FIRESTORE_ERR = ""
_GEMINI_CLIENT = None

FRIDGE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,29}$")
WORD_RE = re.compile(r"[a-z\u0900-\u097f]+")
SPLIT_RE = re.compile(
    r"\s*(?:,|;|&|\+|\band\b|\bor\b|\balso\b|\bthen\b|\baur\b|और|तथा|फिर)\s*"
)

ADD_WORDS = {
    "brought", "bring", "bought", "buy", "got", "get", "added", "add",
    "purchased", "लाया", "लाई", "लाए", "लिया", "खरीदा", "खरीदी", "खरीदे",
}
TAKE_WORDS = {
    "used", "use", "ate", "eaten", "cooked", "finished", "consumed",
    "remove", "removed", "delete", "इस्तेमाल", "खाया", "खाई",
}
WASTE_WORDS = {
    "threw", "throw", "thrown", "discarded", "discard", "dumped",
    "trashed", "wasted", "spoiled", "फेंका", "फेंकी", "फेंके", "फेंक",
}
ALL_WORDS = {"all", "everything", "whole", "सब", "सारा", "सारी", "पूरा", "पूरी"}
CLEAR_PHRASES = (
    "clear fridge", "clear storage", "clear all", "empty fridge",
    "used everything", "ate everything", "cooked everything",
    "finished everything", "सब खत्म",
)
STOP_WORDS = ADD_WORDS | TAKE_WORDS | WASTE_WORDS | ALL_WORDS | {
    "and", "or", "also", "then", "aur", "और", "तथा", "फिर",
    "i", "we", "me", "my", "our", "the", "a", "an", "some", "of", "to", "from",
    "have", "has", "had", "in", "on", "it", "is", "was", "please", "just", "few",
    "little", "today", "yesterday", "kg", "kgs", "kilo", "kilos", "gm", "gms",
    "gram", "grams", "g", "l", "ltr", "ltrs", "litre", "litres", "liter", "liters",
    "ml", "pack", "packs", "packet", "packets", "piece", "pieces", "pc", "pcs", "dozen", "bunch",
    "का", "की", "के", "से", "ने", "को", "में", "पर", "एक", "मैं", "हम", "है", "आज",
    "किलो", "ग्राम", "लीटर", "पैकेट", "नग",
}

# ==========================================
# 2. LANGUAGE PACKS
# ==========================================
T = {
    "en": {
        "subtitle": "Tell it what you bought or used. It keeps your kitchen fresh and waste-free.",
        "tab_text": "⌨️ Type", "tab_voice": "🎙️ Record", "tab_upload": "📁 Upload WAV",
        "cmd_label": "Command",
        "cmd_ph": "e.g. brought 2 kg palak, milk and tomatoes  |  used milk  |  threw bread",
        "submit": "Run command",
        "rec_label": "Record your command", "upl_label": "Upload a .wav file",
        "heard": "Heard:", "light_lbl": "Light notice",
        "ai_wait": "Checking with AI...",
        "rescue_wait": "Making your rescue recipe...",
        "audio_fmt": "Could not read this audio. Use a clear WAV recording.",
        "audio_net": "Speech service is unreachable. Check your internet connection.",
        "audio_unclear": "Could not understand the audio. Please speak clearly.",
        "no_db": "Database is unavailable. Check your Firebase secrets.",
        "ai_busy": "AI service is busy. Nothing was saved for: {names}.",
        "too_many": "Too many unknown items at once. Nothing was saved for: {names}. Send fewer items.",
        "added": "Added: {names}", "used": "Used up: {names}", "wasted": "Discarded: {names}",
        "missing": "Not found in your fridge: {names}",
        "rejected": "Not a food item: {names}. Check the spelling and try again.",
        "cleared": "Fridge cleared. Everything is marked as consumed.",
        "nothing": "Could not understand that. Try: \"brought milk and tomatoes\".",
        "table_hdr": "Active storage", "col_item": "Item", "col_added": "Added",
        "col_expiry": "Expires", "col_status": "Freshness",
        "st_today": "Expires today", "st_tomorrow": "Expires tomorrow",
        "st_soon": "{days} days left", "st_fresh": "{days} days left",
        "empty": "Your fridge is empty. Add something above to start tracking.",
        "expired_msg": "Past expiry date: {names}",
        "expired_btn": "Remove expired items",
        "rescue_msg": "Use these before they go bad: {names}",
        "rescue_btn": "Cook a rescue recipe",
        "rescue_none": "Nothing expires in the next day. Great job!",
        "rescue_fail": "Could not create a recipe right now. Try again shortly.",
        "ing": "Ingredients", "steps": "Steps",
        "login_tab": "Login", "create_tab": "Create a new fridge", "fid": "Fridge ID",
        "fid_ph": "e.g. sharma_kitchen", "pin": "PIN",
        "login_btn": "Unlock fridge", "create_btn": "Create fridge",
        "bad_id": "Use 3 to 30 letters, numbers, _ or -.",
        "need_both": "Enter both a Fridge ID and a PIN.",
        "pin_short": "PIN must be at least 4 characters.",
        "no_fridge": "No fridge with that ID.", "wrong_pin": "Wrong PIN.",
        "exists": "That Fridge ID is taken. Choose another.",
        "locked": "Too many wrong attempts. Wait {s} seconds.",
        "lock_info": "Log in or create a fridge in the sidebar to start.",
        "lock_btn": "Lock fridge", "lang_btn": "हिंदी में देखें",
        "stats_items": "Items", "stats_soon": "Expiring soon", "stats_expired": "Expired",
        "home_hdr": "Add to home screen",
        "home_note": "A website can't install itself. Open the link in Chrome (Android) or Safari (iPhone), then follow the steps below.",
        "open_link": "Open fridge link", "copy_link": "Copy the link below (tap the copy icon)",
        "android": "**Android (Chrome):** tap ⋮ → **Add to Home screen** (or **Install app**).",
        "iphone": "**iPhone (Safari):** tap the Share icon ⎋ → **Add to Home Screen**.",
        "secure": "PINs are stored as salted hashes. Each fridge is isolated by its ID.",
        "skip_btn": "Cancel / Skip item",
    },
    "hi": {
        "subtitle": "बताइए क्या लाए या इस्तेमाल किया। यह आपकी रसोई को ताज़ा और बर्बादी-मुक्त रखेगा।",
        "tab_text": "⌨️ टाइप करें", "tab_voice": "🎙️ रिकॉर्ड करें", "tab_upload": "📁 WAV अपलोड करें",
        "cmd_label": "कमांड",
        "cmd_ph": "जैसे: 2 किलो पालक, दूध और टमाटर लाया  |  दूध इस्तेमाल किया",
        "submit": "कमांड चलाएँ",
        "rec_label": "अपना कमांड रिकॉर्ड करें", "upl_label": ".wav फ़ाइल अपलोड करें",
        "heard": "सुना गया:", "light_lbl": "हल्का नोटिस",
        "ai_wait": "AI से जाँच हो रही है...",
        "rescue_wait": "रेस्क्यू रेसिपी बन रही है...",
        "audio_fmt": "यह ऑडियो पढ़ा नहीं जा सका। साफ WAV रिकॉर्डिंग दें।",
        "audio_net": "स्पीच सेवा उपलब्ध नहीं है। इंटरनेट जाँचें।",
        "audio_unclear": "ऑडियो समझ नहीं आया। कृपया साफ बोलें।",
        "no_db": "डेटाबेस उपलब्ध नहीं है। Firebase सेटिंग जाँचें।",
        "ai_busy": "AI सेवा व्यस्त है या सेट नहीं है। इनके लिए कुछ सेव नहीं हुआ: {names}।",
        "too_many": "एक बार में बहुत सारे अज्ञात सामान। इनके लिए कुछ सेव नहीं हुआ: {names}। कम सामान लिखें।",
        "added": "जोड़ा गया: {names}", "used": "इस्तेमाल हुआ: {names}",
        "wasted": "फेंका गया: {names}", "missing": "फ्रिज में नहीं मिला: {names}",
        "rejected": "यह खाने का सामान नहीं है: {names}। स्पेलिंग जाँचें।",
        "cleared": "फ्रिज साफ़ कर दिया गया है।",
        "nothing": "यह समझ नहीं आया। उदाहरण: \"दूध और टमाटर लाया\"।",
        "table_hdr": "सक्रिय स्टोरेज", "col_item": "सामान", "col_added": "लाने की तारीख",
        "col_expiry": "खराब होगा", "col_status": "ताज़गी",
        "st_today": "आज खराब होगा", "st_tomorrow": "कल खराब होगा",
        "st_soon": "{days} दिन बाकी", "st_fresh": "{days} दिन बाकी",
        "empty": "आपका फ्रिज खाली है। ऊपर से कुछ जोड़ें।",
        "expired_msg": "समय सीमा समाप्त: {names}",
        "expired_btn": "खराब सामान हटाएँ",
        "rescue_msg": "इन्हें जल्दी इस्तेमाल करें: {names}",
        "rescue_btn": "रेस्क्यू रेसिपी बनाएँ",
        "rescue_none": "अगले एक दिन में कुछ भी खराब नहीं होगा। बढ़िया!",
        "rescue_fail": "अभी रेसिपी नहीं बन सकी। थोड़ी देर बाद कोशिश करें।",
        "ing": "सामग्री", "steps": "तरीका",
        "login_tab": "लॉगिन", "create_tab": "नया फ्रिज बनाएँ", "fid": "फ्रिज ID",
        "fid_ph": "जैसे: sharma_kitchen", "pin": "PIN",
        "login_btn": "फ्रिज खोलें", "create_btn": "फ्रिज बनाएँ",
        "bad_id": "3 से 30 अक्षर, नंबर, _ या - का उपयोग करें।",
        "need_both": "फ्रिज ID और PIN दोनों डालें।",
        "pin_short": "PIN कम से कम 4 अक्षर का हो।",
        "no_fridge": "इस ID का कोई फ्रिज नहीं है।", "wrong_pin": "गलत PIN।",
        "exists": "यह ID पहले से है। कोई और चुनें।",
        "locked": "बहुत अधिक गलत प्रयास। {s} सेकंड रुकें।",
        "lock_info": "साइडबार में लॉगिन करें या नया फ्रिज बनाएँ।",
        "lock_btn": "फ्रिज लॉक करें", "lang_btn": "View in English",
        "stats_items": "सामान", "stats_soon": "जल्द खराब", "stats_expired": "समय समाप्त",
        "home_hdr": "होम स्क्रीन पर जोड़ें",
        "home_note": "वेबसाइट खुद को इंस्टॉल नहीं कर सकती। लिंक Chrome (Android) या Safari (iPhone) में खोलें, फिर नीचे दिए स्टेप्स देखें।",
        "open_link": "फ्रिज लिंक खोलें", "copy_link": "नीचे का लिंक कॉपी करें (कॉपी आइकन दबाएँ)",
        "android": "**Android (Chrome):** ⋮ दबाएँ → **Add to Home screen** (या **Install app**)।",
        "iphone": "**iPhone (Safari):** Share ⎋ दबाएँ → **Add to Home Screen**।",
        "secure": "PIN सुरक्षित हैश में सेव होता है। हर फ्रिज अलग रहता है।",
        "skip_btn": "छोड़ें / रद्द करें",
    },
}

# ==========================================
# 3. BUBBLE-STYLED UI CSS
# ==========================================
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Fredoka+One&family=Lexend:wght@400;500;600;700&display=swap');
[data-testid="stDecoration"], footer, #MainMenu, [data-testid="stStatusWidget"] {
    display: none !important;
}
.stApp {
    background: radial-gradient(circle at top right, #131B2E 0%, #0B0E14 65%);
    font-family: 'Lexend', sans-serif;
    color: #F3F4F6;
}
[data-testid="stSidebar"] {
    background: #111827 !important;
    border-right: 1px solid #1F2937 !important;
}
[data-testid="stSidebarCollapsedControl"],
[data-testid="stSidebarCollapseButton"] {
    display: flex !important;
    visibility: visible !important;
    opacity: 1 !important;
    z-index: 100000 !important;
}
.hdr {
    background: linear-gradient(135deg, #1E2638 0%, #111827 100%);
    padding: 22px 24px;
    border-radius: 20px;
    border: 3px solid #3B82F6;
    box-shadow: 0 8px 0 #1D4ED8, 0 12px 20px rgba(0, 0, 0, 0.35);
    margin-bottom: 22px;
}
.hdr h1 {
    font-family: 'Fredoka One', cursive;
    font-size: 30px;
    color: #00F2FE;
    margin: 0;
    text-shadow: 2px 2px 0 #050505;
}
.hdr p {
    color: #9CA3AF;
    margin: 6px 0 0;
    font-size: 14px;
}
.kpi-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 12px;
    margin: 12px 0 18px;
}
.kpi-bubble {
    background: #161B26;
    border-radius: 18px;
    padding: 12px 14px;
    text-align: center;
    border: 3px solid #3B82F6;
    box-shadow: 0 6px 0 #1D4ED8;
}
.kpi-warn { border-color: #FACC15; box-shadow: 0 6px 0 #CA8A04; }
.kpi-exp  { border-color: #EF4444; box-shadow: 0 6px 0 #991B1B; }
.kpi-num {
    font-family: 'Fredoka One', cursive;
    font-size: 25px;
    line-height: 1.1;
}
.kpi-lbl {
    font-size: 11.5px;
    color: #9CA3AF;
    font-weight: 600;
    margin-top: 4px;
}
.bubble {
    border-radius: 18px;
    padding: 18px 20px;
    margin: 14px 0;
    line-height: 1.55;
}
.bubble-info {
    background: #0284C7;
    border: 3px solid #38BDF8;
    box-shadow: 0 6px 0 #0369A1;
    color: #fff;
}
.bubble-warn {
    background: #FEF08A;
    border: 3px solid #FACC15;
    box-shadow: 0 6px 0 #CA8A04;
    color: #713F12;
    font-weight: 600;
}
.bubble-danger {
    background: #7F1D1D;
    border: 3px solid #EF4444;
    box-shadow: 0 6px 0 #991B1B;
    color: #FEE2E2;
}
.bubble-rec {
    background: linear-gradient(135deg, #1E3A8A 0%, #0F172A 100%);
    border: 3px solid #3B82F6;
    box-shadow: 0 6px 0 #1D4ED8;
}
.heard-box {
    background: #0F2A44;
    color: #E0F2FE;
    border: 2px solid #38BDF8;
    box-shadow: 0 4px 0 #0369A1;
    border-radius: 14px;
    padding: 12px 16px;
    font-size: 15px;
    line-height: 1.5;
    margin: 6px 0 10px;
    word-break: break-word;
}
.heard-box.light {
    background: #F0F9FF;
    color: #0C4A6E;
    border-color: #0284C7;
    box-shadow: 0 4px 0 #BAE6FD;
}
table.inv {
    width: 100%;
    border-collapse: separate;
    border-spacing: 0 8px;
}
table.inv td {
    background: #161B26;
    padding: 11px 13px;
    font-size: 14px;
    border-top: 2px solid #1F2937;
    border-bottom: 3px solid #090D16;
}
table.inv td:first-child {
    border-radius: 12px 0 0 12px;
    border-left: 2px solid #1F2937;
}
table.inv td:last-child {
    border-radius: 0 12px 12px 0;
    border-right: 2px solid #1F2937;
}
.badge {
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    white-space: nowrap;
}
.b-today { background: #7F1D1D; color: #FCA5A5; border: 1.5px solid #EF4444; }
.b-tomorrow { background: #78350F; color: #FDE68A; border: 1.5px solid #F59E0B; }
.b-soon { background: #1E3A8A; color: #BFDBFE; border: 1.5px solid #3B82F6; }
.b-fresh { background: #064E3B; color: #6EE7B7; border: 1.5px solid #10B981; }
div.stButton > button,
div[data-testid="stFormSubmitButton"] > button {
    font-family: 'Fredoka One', cursive !important;
    background: #EC4899 !important;
    color: #fff !important;
    border: 3px solid #F472B6 !important;
    box-shadow: 0 5px 0 #BE185D !important;
    border-radius: 15px !important;
    font-size: 17px !important;
    transition: transform 0.1s ease !important;
}
div.stButton > button:active,
div[data-testid="stFormSubmitButton"] > button:active {
    transform: translateY(4px) !important;
    box-shadow: 0 1px 0 #BE185D !important;
}
</style>
"""


def init_session_state():
    for _key, _val in {
        "authenticated": False,
        "fridge_id": "",
        "lang": "en",
        "recipe": None,
        "login_fails": 0,
        "lock_until": 0.0,
        "pending_expiry": [],
        "expiry_form_error": "",
    }.items():
        st.session_state.setdefault(_key, _val)


def tr():
    return T[st.session_state.get("lang", "en")]


def pick(en, hi):
    return (hi or en) if st.session_state.get("lang", "en") == "hi" else en


def closable(inner_html, key, css):
    flag = f"dismissed_{key}"
    if st.session_state.get(flag):
        return
    c_text, c_close = st.columns([12, 1])
    c_text.markdown(f'<div class="{css}">{inner_html}</div>', unsafe_allow_html=True)
    if c_close.button("✕", key=f"x_{key}"):
        st.session_state[flag] = True
        st.rerun()


def short_hash(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:10]


def query_param(name):
    try:
        value = st.query_params.get(name, "")
        return value[0] if isinstance(value, list) else value
    except Exception:
        return ""


def _session_token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ==========================================
# 4. FIREBASE
# ==========================================
def init_firestore():
    """Return (client_or_None, error_text) using a process-level singleton."""
    global _FIRESTORE_CLIENT, _FIRESTORE_ERR
    if _FIRESTORE_CLIENT is not None:
        return _FIRESTORE_CLIENT, ""
    try:
        if not firebase_admin._apps:
            env_json = (
                os.getenv("FIREBASE_CREDENTIALS_JSON")
                or os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
            )
            credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
            info = None

            if env_json:
                info = json.loads(env_json)
            else:
                try:
                    if "firebase" in st.secrets:
                        fb = st.secrets["firebase"]
                        if isinstance(fb, str):
                            info = json.loads(fb)
                        elif "value" in fb:
                            info = json.loads(fb["value"])
                        else:
                            info = dict(fb)
                except Exception:
                    pass

            if info:
                if "private_key" in info and isinstance(info["private_key"], str):
                    info["private_key"] = info["private_key"].replace("\\n", "\n")
                cred = credentials.Certificate(info)
            elif credentials_path and os.path.isfile(credentials_path):
                cred = credentials.Certificate(credentials_path)
            elif os.path.isfile("firebase_credentials.json"):
                cred = credentials.Certificate("firebase_credentials.json")
            else:
                _FIRESTORE_ERR = "Firebase credentials not found in secrets or environment."
                return None, _FIRESTORE_ERR

            firebase_admin.initialize_app(cred)
        _FIRESTORE_CLIENT = firestore.client()
        _FIRESTORE_ERR = ""
        return _FIRESTORE_CLIENT, ""
    except Exception as exc:
        log.exception("Firebase initialisation failed")
        _FIRESTORE_ERR = str(exc)
        return None, _FIRESTORE_ERR


def get_db():
    return init_firestore()[0]


def hash_pin(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), PIN_ITERATIONS).hex()


def inventory(fridge_id=None):
    fid = fridge_id or st.session_state.get("fridge_id", "")
    return get_db().collection("fridges").document(fid).collection("inventory")


def load_items(fridge_id=None):
    db = get_db()
    fid = fridge_id or st.session_state.get("fridge_id", "")
    if db is None or not fid:
        return []
    if not API_MODE and not st.session_state.get("authenticated"):
        return []
    return [{"id": d.id, **(d.to_dict() or {})} for d in inventory(fid).stream()]


def clear_all(fridge_id=None):
    db = get_db()
    fid = fridge_id or st.session_state.get("fridge_id", "")
    if db is None or not fid:
        return
    for doc in inventory(fid).stream():
        doc.reference.delete()


# ==========================================
# 5. PERSISTENT BROWSER SESSION
# ==========================================
def issue_browser_session(fridge_id):
    db = get_db()
    if db is None or _cookie_manager is None:
        return False
    token = secrets.token_urlsafe(48)
    now = datetime.now(IST)
    expires = now + timedelta(days=SESSION_DAYS)
    try:
        db.collection("fridge_sessions").document(_session_token_hash(token)).set({
            "fridge_id": fridge_id,
            "created_at": firestore.SERVER_TIMESTAMP,
            "expires_at": expires,
            "revoked": False,
        })
        _cookie_manager.set(
            SESSION_COOKIE_NAME, token,
            expires_at=expires.replace(tzinfo=None),
            key=f"set_cookie_{short_hash(token)}",
        )
        st.session_state.session_token = token
        time.sleep(0.15)
        return True
    except Exception:
        log.exception("Could not create persistent browser session")
        return False


def revoke_browser_session(token=None):
    db = get_db()
    token = token or st.session_state.get("session_token", "")
    if token and db is not None:
        try:
            db.collection("fridge_sessions").document(_session_token_hash(token)).set({
                "revoked": True,
                "revoked_at": firestore.SERVER_TIMESTAMP,
            }, merge=True)
        except Exception:
            log.exception("Could not revoke browser session")
    if _cookie_manager is not None:
        try:
            _cookie_manager.delete(SESSION_COOKIE_NAME, key=f"del_cookie_{int(time.time())}")
            time.sleep(0.15)
        except Exception:
            log.exception("Could not clear browser session cookie")
    st.session_state.pop("session_token", None)


def restore_browser_session():
    db = get_db()
    if st.session_state.get("authenticated") or db is None or _cookie_manager is None:
        return
    try:
        cookies = _cookie_manager.get_all() or {}
        token = cookies.get(SESSION_COOKIE_NAME, "")
        if not isinstance(token, str) or len(token) < 40:
            return
        ref = db.collection("fridge_sessions").document(_session_token_hash(token))
        snap = ref.get()
        if not snap.exists:
            return
        session = snap.to_dict() or {}
        expiry = session.get("expires_at")
        if session.get("revoked") or not session.get("fridge_id") or not expiry:
            return
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=IST)
        if expiry <= datetime.now(IST):
            ref.set({"revoked": True, "expired_at": firestore.SERVER_TIMESTAMP}, merge=True)
            return
        fridge_id = str(session["fridge_id"])
        if not db.collection("fridges").document(fridge_id).get().exists:
            ref.set({"revoked": True}, merge=True)
            return
        st.session_state.authenticated = True
        st.session_state.fridge_id = fridge_id
        st.session_state.session_token = token
    except Exception:
        log.exception("Persistent login restore failed")


# ==========================================
# 6. GEMINI: MODEL & STRICT FOOD CLASSIFICATION
# ==========================================
class FoodCategory(str, Enum):
    FRUIT = "fruit"
    VEGETABLE = "vegetable"
    DAIRY = "dairy"
    MEAT_FISH_EGG = "meat_fish_egg"
    GRAIN_PULSE_FLOUR = "grain_pulse_flour"
    SPICE_CONDIMENT = "spice_condiment"
    BEVERAGE = "beverage"
    HEALTH_DRINK = "health_drink_powder"
    PACKAGED_GROCERY = "packaged_grocery"
    SNACK_BAKERY = "snack_bakery"
    OIL_SUGAR = "oil_sugar_fat"
    COOKED_DISH = "cooked_dish"
    NOT_FOOD = "not_food"


class FoodVerdict(BaseModel):
    corrected_item: str = Field(description="The item the user means, spelled correctly in English.")
    is_food: bool = Field(description="True only if people eat or drink this and keep it in a kitchen.")
    category: FoodCategory
    shelf_life_days: int = Field(description="Typical shelf life in days in an Indian kitchen. 0 if not food.")
    requires_expiry_date: bool = Field(description="True for branded, factory-packaged, sealed, or store-bought items whose printed expiry date should be entered; false for fresh produce and food prepared today.")
    name_en: str = Field(description="Clean English name. Empty if not food.")
    name_hi: str = Field(description="Hindi name in Devanagari. Empty if not food.")


class ExpiryDateVerdict(BaseModel):
    is_valid: bool = Field(description="True only if the supplied date is a plausible, correctly interpreted expiry date for the food item.")
    reason: str = Field(description="Short explanation if invalid; otherwise an empty or short confirmation.")


class EdibleCheck(BaseModel):
    edible: bool = Field(description="True only if the word names something a person eats or drinks.")


class RescueRecipe(BaseModel):
    recipe_name: str
    recipe_name_hi: str
    ingredients: list[str]
    steps: list[str]


CLASSIFY_PROMPT = """You classify ONE item for an Indian household fridge and pantry app.
The text may have typos or speech-to-text errors: "{item}"

Rules:
- Correct spelling only when the corrected word is clearly a real food, drink or brand
  (horlics -> Horlicks, tomto -> Tomato, panir -> Paneer, bournvita -> Bournvita).
- is_food is true ONLY for things people eat or drink: produce, dairy, meat, fish, eggs,
  grains, pulses, flour, spices, cooking oils, sugar, beverages, packaged groceries,
  health drink powders, snacks, cooked leftover dishes.
- is_food is false for everything else: buildings, vehicles, places, people, animals,
  electronics, clothing, furniture, tools, medicines, cleaning products, cosmetics, plants,
  abstract or random words.
- If you are not clearly sure it is food, it is NOT food. Never pick a food just because the input is vague.
- If is_food is false: category not_food, shelf_life_days 0, requires_expiry_date false, and names empty.
- If it is fresh fruit, fresh vegetables, raw fresh ingredients, or a dish clearly prepared today,
  set requires_expiry_date=false and estimate sensible storage life.
- If it is branded, factory-packaged, sealed, ready-made from a shop/restaurant, a packaged dairy
  product, or a product whose manufacture date is unknown, set requires_expiry_date=true. Never
  guess its expiry from a generic shelf-life estimate; ask the user to read the printed label.
- Cheese varieties bought in a packet/block, packaged sauces, snacks, biscuits, chips, cookies,
  instant foods, bottled drinks, and branded groceries require the printed expiry date.
- If the user says the dish was freshly cooked/prepared today, treat it as prepared today unless
  the wording indicates a sealed commercial product.
- Return the food category, an English name and a Devanagari Hindi name.
"""

VERIFY_PROMPT = """Does the word "{item}" name something a person eats or drinks?
Answer edible=true only for real food, drinks, ingredients or packaged grocery products.
Answer edible=false for buildings, vehicles, places, objects, electronics, clothing,
medicines, people, animals, and words that are not food.
"""

RECIPE_PROMPT = """You are an expert Indian home cook who prevents food waste.
These ingredients expire today or tomorrow and must be used now: {items}.
Write one practical Indian rescue recipe. If there are only one or two items, keep it simple
and add basic pantry spices (salt, turmeric, red chilli powder, oil, garlic).
Give a Hindi name in Devanagari. Keep each step short.
"""


def get_client():
    """Return process-cached Gemini client."""
    global _GEMINI_CLIENT
    if _GEMINI_CLIENT is not None:
        return _GEMINI_CLIENT
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        try:
            key = st.secrets["google"]["api_key"]
        except Exception:
            pass
    if not key:
        log.error("Gemini API key missing: set [google].api_key or GEMINI_API_KEY")
        return None
    try:
        _GEMINI_CLIENT = genai.Client(api_key=str(key).strip())
        return _GEMINI_CLIENT
    except Exception:
        log.exception("Could not initialise Gemini client")
        return None


def gemini_models():
    return list(MODEL_FALLBACKS)


def gemini_model():
    return list(MODEL_FALLBACKS)[0]


@lru_cache(maxsize=512)
def classify_food(phrase, model_name):
    """Classify a food with two checks; try models from lower cost to higher cost."""
    client = get_client()
    if client is None:
        raise RuntimeError("Gemini API key is missing")

    models = []
    allowed_models = set(SUPPORTED_GEMINI_MODELS)
    for name in [model_name, *gemini_models()]:
        if name in allowed_models and name not in models:
            models.append(name)
    if not models:
        models = [DEFAULT_MODEL]
    errors = []
    for model in models:
        try:
            first = client.models.generate_content(
                model=model,
                contents=CLASSIFY_PROMPT.format(item=phrase),
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=FoodVerdict,
                ),
            )
            verdict = FoodVerdict.model_validate_json(first.text or "")
            name_en = verdict.name_en.strip()
            if not verdict.is_food or verdict.category == FoodCategory.NOT_FOOD or not name_en:
                return None
            if not verdict.requires_expiry_date and not 1 <= verdict.shelf_life_days <= 730:
                return None
            if verdict.requires_expiry_date and verdict.shelf_life_days < 0:
                return None

            second = client.models.generate_content(
                model=model,
                contents=VERIFY_PROMPT.format(item=verdict.corrected_item.strip() or phrase),
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=EdibleCheck,
                ),
            )
            if not EdibleCheck.model_validate_json(second.text or "").edible:
                return None
            return (
                verdict.shelf_life_days,
                name_en,
                verdict.name_hi.strip() or name_en,
                verdict.category.value,
                verdict.requires_expiry_date,
            )
        except Exception as exc:
            errors.append(f"{model}: {type(exc).__name__}")
            log.warning("Gemini food classification failed on model %s", model, exc_info=True)
    raise RuntimeError("All configured Gemini models failed: " + "; ".join(errors))


def parse_expiry_date(raw_value):
    value = str(raw_value or "").strip()
    accepted = (
        (r"\d{1,2}/\d{1,2}/\d{4}", "%d/%m/%Y"),
        (r"\d{1,2}-\d{1,2}-\d{4}", "%d-%m-%Y"),
        (r"\d{1,2}\.\d{1,2}\.\d{4}", "%d.%m.%Y"),
        (r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d"),
    )
    for pattern, fmt in accepted:
        if not re.fullmatch(pattern, value):
            continue
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            return None
    return None


def validate_expiry_with_gemini(item_name, raw_value, parsed_date=None):
    client = get_client()
    if client is None:
        raise RuntimeError("Gemini API key is missing")
    today_iso = datetime.now(IST).date().isoformat()
    prompt = (
        f"Today's date is {today_iso}. "
        "Check an expiry date entered from a food package label. Do not invent or alter the date. "
        "Item: " + json.dumps(item_name) + ". User entered: " + json.dumps(raw_value) + ". "
        + ("Interpreted calendar date: " + parsed_date.isoformat() + ". " if parsed_date else "No valid calendar date could be parsed by the app. ")
        + "Return is_valid=false if the supplied string is not a clear date, the date is not a "
        "plausible calendar date, or the date is implausibly far in the future (more than 15 years). "
        "Do not reject a date merely because it is near; expiry dates may be close. Return JSON only."
    )
    errors = []
    for model in gemini_models():
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ExpiryDateVerdict,
                ),
            )
            verdict = ExpiryDateVerdict.model_validate_json(response.text or "")
            return verdict.is_valid, verdict.reason
        except Exception as exc:
            errors.append(f"{model}: {type(exc).__name__}")
            log.warning("Gemini expiry-date validation failed on model %s", model, exc_info=True)
    raise RuntimeError("Gemini date validation failed: " + "; ".join(errors))


def add_inventory_item(name_en, name_hi, expiry_date, added_date=None):
    today_value = added_date or datetime.now(IST).date()
    inventory().add({
        "name_en": name_en,
        "name_hi": name_hi,
        "core_id": name_en,
        "added": today_value.isoformat(),
        "expiry": expiry_date.isoformat(),
    })


def submit_pending_expiry(raw_date):
    pending = list(st.session_state.get("pending_expiry", []))
    if not pending:
        return ("warn", "There is no item waiting for an expiry date.")
    current = pending[0]
    parsed = parse_expiry_date(raw_date)
    if parsed is None:
        try:
            valid, reason = validate_expiry_with_gemini(current["name_en"], raw_date, None)
        except Exception:
            log.exception("Gemini expiry date validation failed for malformed input")
            return ("err", "Gemini could not verify that date right now. Please try again; nothing was added.")
        detail = (reason or "") if not valid else ""
        return ("err", "Enter a real date in DD/MM/YYYY format, for example 25/12/2026. " + detail)
    today_value = datetime.now(IST).date()
    if parsed < today_value:
        return ("err", "That expiry date has already passed. Check the package label and enter a future date.")
    try:
        valid, reason = validate_expiry_with_gemini(current["name_en"], raw_date, parsed)
    except Exception:
        log.exception("Gemini expiry date validation failed")
        return ("err", "Gemini could not verify that date right now. Please try again; nothing was added.")
    if not valid:
        return ("err", "Gemini could not verify that expiry date. " + (reason or "Check the printed date and try again."))
    add_inventory_item(current["name_en"], current["name_hi"], parsed)
    st.session_state.pending_expiry = pending[1:]
    st.session_state.expiry_form_error = ""
    return ("ok", f"Added {current['name_en']} with expiry {parsed.strftime('%d/%m/%Y')}.")


def render_expiry_form(t):
    pending = st.session_state.get("pending_expiry", [])
    if not pending:
        return
    item = pending[0]
    st.markdown(
        f'<div class="bubble bubble-warn"><b>Enter expiry date</b><br>'
        f'{esc(item["name_en"])} may have been manufactured before today. Read the date '
        f'printed on its package and enter it as <b>DD/MM/YYYY</b>. Gemini will check it '
        f'before the item is saved.</div>',
        unsafe_allow_html=True,
    )
    with st.form("expiry_date_form", clear_on_submit=True):
        expiry_value = st.text_input("Expiry date (DD/MM/YYYY)", placeholder="25/12/2026", key="expiry_date_value")
        submitted = st.form_submit_button("Check date and add item", use_container_width=True)
        if submitted:
            kind, message = submit_pending_expiry(expiry_value)
            if kind == "ok":
                st.toast(message, icon="✅")
                st.rerun()
            else:
                st.error(message)
    if st.button(t["skip_btn"], key="skip_pending_expiry", use_container_width=True):
        st.session_state.pending_expiry = pending[1:]
        st.rerun()


def generate_rescue(names):
    client = get_client()
    if client is None:
        raise RuntimeError("Gemini API key is missing")
    errors = []
    for model in gemini_models():
        try:
            resp = client.models.generate_content(
                model=model,
                contents=RECIPE_PROMPT.format(items=", ".join(names)),
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=RescueRecipe,
                ),
            )
            return RescueRecipe.model_validate_json(resp.text or "")
        except Exception as exc:
            errors.append(f"{model}: {type(exc).__name__}")
            log.warning("Gemini rescue recipe failed on model %s", model, exc_info=True)
    raise RuntimeError("All configured Gemini models failed: " + "; ".join(errors))


# ==========================================
# 7. SPEECH TO TEXT
# ==========================================
@lru_cache(maxsize=64)
def transcribe(raw):
    """Return (text, problem_key). problem_key is "" on success."""
    recognizer = sr.Recognizer()
    try:
        with sr.AudioFile(io.BytesIO(raw)) as source:
            audio = recognizer.record(source)
    except Exception:
        return "", "audio_fmt"
    for lang_code in ("en-IN", "hi-IN"):
        try:
            return recognizer.recognize_google(audio, language=lang_code).strip().lower(), ""
        except sr.UnknownValueError:
            continue
        except sr.RequestError:
            return "", "audio_net"
    return "", "audio_unclear"


# ==========================================
# 8. COMMAND PARSING & PROCESSING
# ==========================================
def detect_action(tokens):
    tokset = set(tokens)
    if tokset & WASTE_WORDS:
        return "waste"
    if tokset & TAKE_WORDS:
        return "take"
    if tokset & ADD_WORDS:
        return "add"
    return None


def _whole_phrase_in(trigger, phrase):
    return bool(re.search(rf"(?<![a-z\u0900-\u097f]){re.escape(trigger)}(?![a-z\u0900-\u097f])", phrase))


def matches_item(item, keys):
    names = {
        str(item.get("name_en", "")).lower(),
        str(item.get("name_hi", "")).lower(),
        str(item.get("core_id", "")).lower(),
    } - {""}
    for n in names:
        for k in keys:
            if k == n or k in n.split() or n in k.split() or (len(n) >= 4 and _whole_phrase_in(n, k)):
                return True
    return False


def _is_clear_command(cmd):
    normalized = " ".join(str(cmd or "").lower().split()).strip(" .!?")
    for phrase in CLEAR_PHRASES:
        if normalized in {phrase, f"please {phrase}", f"{phrase} please"}:
            return True
        if normalized in {f"i {phrase}", f"we {phrase}", f"i have {phrase}", f"we have {phrase}"}:
            return True
    return False


def process_command(raw, t):
    db = get_db()
    cmd = " ".join(raw.lower().split())[:MAX_CMD_CHARS]
    if not cmd:
        return []
    if db is None:
        return [("err", t["no_db"])]
    if _is_clear_command(cmd):
        clear_all()
        return [("ok", t["cleared"])]

    inv = load_items()
    today = datetime.now(IST).date()
    model_name = gemini_model()
    added, used, wasted, missing, rejected, ai_failed, too_many = [], [], [], [], [], [], []
    lookups = 0
    last_action = "add"

    for seg in SPLIT_RE.split(cmd):
        tokens = WORD_RE.findall(seg)
        if not tokens:
            continue
        action = detect_action(tokens) or last_action
        last_action = action
        phrase = " ".join(w for w in tokens if w not in STOP_WORDS)
        if not phrase:
            continue

        if action == "add":
            if lookups >= MAX_AI_LOOKUPS:
                too_many.append(phrase)
                continue
            lookups += 1
            try:
                with st.spinner(t["ai_wait"]):
                    result = classify_food(phrase, model_name)
            except Exception:
                log.exception("Gemini classification failed")
                ai_failed.append(phrase)
                continue
            if result is None:
                rejected.append(phrase)
                continue

            ai_days, name_en, name_hi, category, needs_date = result
            if needs_date:
                pending = list(st.session_state.get("pending_expiry", []))
                if not any(p.get("name_en", "").casefold() == name_en.casefold() for p in pending):
                    pending.append({"name_en": name_en, "name_hi": name_hi, "category": category})
                    st.session_state.pending_expiry = pending
                continue

            local_hits = [
                (len(trigger), days, local_en, local_hi)
                for trigger, (days, local_en, local_hi) in EXPIRY_DATABASE.items()
                if _whole_phrase_in(trigger, phrase) and trigger not in CHEESE_DATE_REQUIRED_ITEMS
            ]
            if local_hits:
                _, days, _, _ = max(local_hits, key=lambda hit: hit[0])
            else:
                days = ai_days
            add_inventory_item(name_en, name_hi, today + timedelta(days=days), today)
            added.append(pick(name_en, name_hi))

        else:
            keys = {phrase} | {
                name_en.lower()
                for trigger, (_, name_en, _) in EXPIRY_DATABASE.items()
                if _whole_phrase_in(trigger, phrase)
            }
            keys = {k for k in keys if len(k) >= 2}
            candidates = sorted(
                (it for it in inv if matches_item(it, keys)),
                key=lambda it: str(it.get("added", "")),
            )
            if not candidates:
                missing.append(phrase)
                continue
            targets = candidates if set(tokens) & ALL_WORDS else candidates[:1]
            for it in targets:
                inventory().document(it["id"]).delete()
                inv.remove(it)
                label = pick(it.get("name_en", ""), it.get("name_hi", ""))
                (wasted if action == "waste" else used).append(label)

    msgs = []
    if added:
        msgs.append(("ok", t["added"].format(names=", ".join(sorted(set(added))))))
    if used:
        msgs.append(("ok", t["used"].format(names=", ".join(sorted(set(used))))))
    if wasted:
        msgs.append(("warn", t["wasted"].format(names=", ".join(sorted(set(wasted))))))
    if missing:
        msgs.append(("warn", t["missing"].format(names=", ".join(missing))))
    if rejected:
        msgs.append(("toast_err", t["rejected"].format(names=", ".join(rejected))))
    if too_many:
        msgs.append(("err", t["too_many"].format(names=", ".join(too_many))))
    if ai_failed:
        msgs.append(("err", t["ai_busy"].format(names=", ".join(ai_failed))))
    if not (added or used or wasted or missing or rejected or too_many or ai_failed) and not st.session_state.get("pending_expiry"):
        msgs.append(("warn", t["nothing"]))
    return msgs


# ==========================================
# 9. AUTH & SIDEBAR
# ==========================================
def create_fridge(fid, pin):
    db = get_db()
    salt = secrets.token_hex(16)
    try:
        db.collection("fridges").document(fid).create({
            "pin_salt": salt,
            "pin_hash": hash_pin(pin, salt),
            "created_at": firestore.SERVER_TIMESTAMP,
        })
        return True, ""
    except AlreadyExists:
        return False, "exists"


def login_fridge(fid, pin):
    db = get_db()
    ref = db.collection("fridges").document(fid)
    snap = ref.get()
    if not snap.exists:
        return False, "no_fridge"
    data = snap.to_dict() or {}
    if data.get("pin_salt"):
        ok = hmac.compare_digest(data.get("pin_hash", ""), hash_pin(pin, data["pin_salt"]))
    else:
        legacy = hashlib.sha256(pin.encode()).hexdigest()
        ok = hmac.compare_digest(data.get("pin_hash", ""), legacy)
        if ok:
            new_salt = secrets.token_hex(16)
            ref.update({"pin_salt": new_salt, "pin_hash": hash_pin(pin, new_salt)})
    return (True, "") if ok else (False, "wrong_pin")


def _days_left(item):
    try:
        exp = date.fromisoformat(item.get("expiry") or item.get("expiry_date", ""))
    except (ValueError, TypeError):
        return None
    return (exp - datetime.now(IST).date()).days


def render_sidebar(t, items):
    db = get_db()
    with st.sidebar:
        st.markdown("## 🧊 FridgeInventory")
        st.caption(t["secure"])
        st.divider()

        if st.button(t["lang_btn"], use_container_width=True, key="lang_btn"):
            st.session_state.lang = "hi" if st.session_state.lang == "en" else "en"
            st.rerun()

        if not st.session_state.authenticated:
            st.subheader(t["login_tab"])
            fid_in = st.text_input(t["fid"], value=query_param("fridge"),
                                   placeholder=t["fid_ph"], key="login_fid")
            pin_in = st.text_input(t["pin"], type="password", key="login_pin")
            fid = fid_in.strip().lower()
            if st.button(t["login_btn"], use_container_width=True, key="login_go"):
                wait = int(st.session_state.lock_until - time.time())
                if wait > 0:
                    st.error(t["locked"].format(s=wait))
                elif not fid or not pin_in:
                    st.error(t["need_both"])
                elif not FRIDGE_RE.fullmatch(fid):
                    st.error(t["bad_id"])
                elif db is None:
                    st.error(t["no_db"])
                else:
                    ok, err = login_fridge(fid, pin_in)
                    if ok:
                        st.session_state.authenticated = True
                        st.session_state.fridge_id = fid
                        st.session_state.login_fails = 0
                        if not issue_browser_session(fid):
                            st.warning("Logged in for this visit, but persistent login could not be enabled.")
                        st.query_params["fridge"] = fid
                        st.rerun()
                    st.session_state.login_fails += 1
                    if st.session_state.login_fails >= MAX_TRIES:
                        st.session_state.lock_until = time.time() + LOCK_SECONDS
                        st.session_state.login_fails = 0
                    if err == "wrong_pin":
                        st.toast("Username or password is incorrect.", icon="⚠️")
                    else:
                        st.error(t[err])

            with st.expander(t["create_tab"]):
                new_fid = st.text_input(t["fid"], placeholder=t["fid_ph"], key="create_fid").strip().lower()
                new_pin = st.text_input(t["pin"], type="password", key="create_pin")
                if st.button(t["create_btn"], use_container_width=True, key="create_go"):
                    if not new_fid or not new_pin:
                        st.error(t["need_both"])
                    elif not FRIDGE_RE.fullmatch(new_fid):
                        st.error(t["bad_id"])
                    elif len(new_pin) < 4:
                        st.error(t["pin_short"])
                    elif db is None:
                        st.error(t["no_db"])
                    else:
                        ok, err = create_fridge(new_fid, new_pin)
                        if ok:
                            st.session_state.authenticated = True
                            st.session_state.fridge_id = new_fid
                            if not issue_browser_session(new_fid):
                                st.warning("Fridge created, but persistent login could not be enabled.")
                            st.query_params["fridge"] = new_fid
                            st.rerun()
                        st.error(t[err])
            return

        st.success(f"**{st.session_state.fridge_id.upper()}**")
        c1, c2 = st.columns(2)
        c1.metric(t["stats_items"], len(items))
        soon = sum(1 for it in items if _days_left(it) in (0, 1))
        c2.metric(t["stats_soon"], soon)

        st.divider()
        st.subheader(t["home_hdr"])
        st.caption(t["home_note"])
        link = f"{APP_URL}?fridge={st.session_state.fridge_id}"
        st.link_button(t["open_link"], link, use_container_width=True)
        st.caption(t["copy_link"])
        st.code(link, language=None)
        with st.expander(t["home_hdr"]):
            st.markdown(t["android"])
            st.markdown(t["iphone"])

        st.divider()
        if st.button(t["lock_btn"], use_container_width=True, key="logout_go"):
            revoke_browser_session()
            st.session_state.authenticated = False
            st.session_state.fridge_id = ""
            st.session_state.recipe = None
            st.session_state.pending_expiry = []
            if "fridge" in st.query_params:
                del st.query_params["fridge"]
            st.rerun()


# ==========================================
# 10. DASHBOARD
# ==========================================
def render_dashboard(t, items):
    today = datetime.now(IST).date()
    active, expired = [], []
    for it in items:
        try:
            exp = date.fromisoformat(it.get("expiry") or it.get("expiry_date", ""))
        except (ValueError, TypeError):
            continue
        name = pick(it.get("name_en") or it.get("core_id") or it["id"], it.get("name_hi", ""))
        row = {
            "id": it["id"],
            "name": name,
            "added": it.get("added") or it.get("date_brought", ""),
            "expiry": exp.isoformat(),
            "left": (exp - today).days,
        }
        (expired if row["left"] < 0 else active).append(row)

    soon_count = sum(1 for r in active if r["left"] <= 1)

    st.markdown(
        f"""
        <div class="kpi-grid">
            <div class="kpi-bubble">
                <div class="kpi-num" style="color:#00F2FE;">{len(active)}</div>
                <div class="kpi-lbl">{esc(t['stats_items'])}</div>
            </div>
            <div class="kpi-bubble kpi-warn">
                <div class="kpi-num" style="color:#FACC15;">{soon_count}</div>
                <div class="kpi-lbl">{esc(t['stats_soon'])}</div>
            </div>
            <div class="kpi-bubble kpi-exp">
                <div class="kpi-num" style="color:#FCA5A5;">{len(expired)}</div>
                <div class="kpi-lbl">{esc(t['stats_expired'])}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if expired:
        names = ", ".join(r["name"] for r in expired)
        closable(
            f"<b>⚠️</b> {esc(t['expired_msg'].format(names=names))}",
            f"expired_{short_hash(names)}",
            "bubble bubble-danger",
        )
        if st.button(t["expired_btn"], use_container_width=True, key="rm_expired"):
            for r in expired:
                inventory().document(r["id"]).delete()
            st.rerun()

    st.markdown(f"### {t['table_hdr']}")
    if not active:
        st.markdown(f'<div class="bubble bubble-info">{esc(t["empty"])}</div>', unsafe_allow_html=True)
        return

    active.sort(key=lambda r: r["left"])
    rows_html = []
    for r in active:
        if r["left"] == 0:
            badge = f'<span class="badge b-today">{esc(t["st_today"])}</span>'
        elif r["left"] == 1:
            badge = f'<span class="badge b-tomorrow">{esc(t["st_tomorrow"])}</span>'
        elif r["left"] <= 3:
            badge = f'<span class="badge b-soon">{esc(t["st_soon"].format(days=r["left"]))}</span>'
        else:
            badge = f'<span class="badge b-fresh">{esc(t["st_fresh"].format(days=r["left"]))}</span>'
        rows_html.append(
            f"<tr><td><b>{esc(r['name'])}</b></td><td>{esc(r['added'])}</td>"
            f"<td>{esc(r['expiry'])}</td><td>{badge}</td></tr>"
        )
    st.markdown(
        f"""<table class="inv"><thead><tr style="color:#9CA3AF;font-size:12px;text-align:left;">
        <th style="padding:0 12px;">{esc(t['col_item'])}</th>
        <th>{esc(t['col_added'])}</th><th>{esc(t['col_expiry'])}</th>
        <th>{esc(t['col_status'])}</th></tr></thead>
        <tbody>{''.join(rows_html)}</tbody></table>""",
        unsafe_allow_html=True,
    )

    urgent = [r["name"] for r in active if r["left"] <= 1]
    if not urgent:
        st.caption(t["rescue_none"])
        return

    urgent_key = "|".join(sorted(urgent))
    closable(
        esc(t["rescue_msg"].format(names=", ".join(urgent))),
        f"urgent_{short_hash(urgent_key)}",
        "bubble bubble-warn",
    )
    if st.button(t["rescue_btn"], use_container_width=True, key="rescue_go"):
        st.session_state.pop(f"dismissed_recipe_{short_hash(urgent_key)}", None)
        with st.spinner(t["rescue_wait"]):
            try:
                recipe = generate_rescue(urgent)
                st.session_state.recipe = {"key": urgent_key, "data": recipe.model_dump()}
            except Exception:
                log.exception("Rescue recipe failed")
                st.session_state.recipe = {"key": urgent_key, "data": None}

    rec = st.session_state.recipe
    if rec and rec["key"] == urgent_key:
        if rec["data"] is None:
            st.error(t["rescue_fail"])
        else:
            d = rec["data"]
            title = pick(d["recipe_name"], d["recipe_name_hi"])
            ing = "".join(f"<li>{esc(i)}</li>" for i in d["ingredients"])
            steps = "".join(f"<li>{esc(s)}</li>" for s in d["steps"])
            closable(
                f'<h4 style="margin:0 0 8px;">🍽️ {esc(title)}</h4>'
                f"<b>{esc(t['ing'])}:</b><ul>{ing}</ul>"
                f"<b>{esc(t['steps'])}:</b><ol>{steps}</ol>",
                f"recipe_{short_hash(urgent_key)}",
                "bubble bubble-rec",
            )


# ==========================================
# 11. MAIN PAGE
# ==========================================
def show_messages(msgs):
    for kind, text in msgs:
        if kind == "toast_err":
            try:
                st.toast(text, icon="⚠️", duration=11)
            except TypeError:
                st.toast(text, icon="⚠️")
        elif kind == "err":
            st.error(text)
        else:
            st.toast(text, icon="✅" if kind == "ok" else "⚠️")


def _voice_block(src, key, t):
    if src is None:
        return []
    raw = src.getvalue()
    audio_id = hashlib.md5(raw).hexdigest()[:12]
    text, problem = transcribe(raw)
    if problem:
        st.warning(t.get(problem, t["audio_unclear"]))
        return []

    msgs = []
    done_key = f"done_{key}"
    if st.session_state.get(done_key) != audio_id:
        st.session_state[done_key] = audio_id
        msgs = process_command(text, t)

    light = st.toggle(t["light_lbl"], key=f"light_{key}")
    css = "heard-box light" if light else "heard-box"
    closable(f'{esc(t["heard"])} "{esc(text)}"', f"heard_{key}_{audio_id}", css)
    return msgs


def main():
    global _cookie_manager
    st.set_page_config(
        page_title="FridgeInventory",
        page_icon="🧊",
        layout="centered",
        initial_sidebar_state="expanded",
    )
    init_session_state()
    st.markdown(CSS, unsafe_allow_html=True)

    db, db_error = init_firestore()

    if stx is not None:
        try:
            _cookie_manager = stx.CookieManager(key="fridgeinventory_cookie_manager")
            restore_browser_session()
        except Exception:
            _cookie_manager = None
            log.exception("Browser cookie manager failed to initialize")

    t = tr()
    st.markdown(
        f'<div class="hdr"><h1>🧊 FridgeInventory</h1><p>{esc(t["subtitle"])}</p></div>',
        unsafe_allow_html=True,
    )

    if db is None and db_error:
        st.error(f"{t['no_db']} ({db_error})")

    msgs = []
    if st.session_state.authenticated and db is not None:
        tab_text, tab_rec, tab_up = st.tabs([t["tab_text"], t["tab_voice"], t["tab_upload"]])

        with tab_text:
            if not st.session_state.get("pending_expiry"):
                with st.form("cmd_form", clear_on_submit=True):
                    cmd = st.text_input(t["cmd_label"], placeholder=t["cmd_ph"], label_visibility="collapsed")
                    if st.form_submit_button(t["submit"], use_container_width=True) and cmd.strip():
                        msgs += process_command(cmd, t)

        with tab_rec:
            src = st.audio_input(t["rec_label"], key="rec")
            msgs += _voice_block(src, "rec", t)

        with tab_up:
            src = st.file_uploader(t["upl_label"], type=["wav"], key="upl")
            msgs += _voice_block(src, "upl", t)

        render_expiry_form(t)
    else:
        st.markdown(f'<div class="bubble bubble-warn">{esc(t["lock_info"])}</div>', unsafe_allow_html=True)

    show_messages(msgs)

    items = load_items() if st.session_state.authenticated and db is not None else []
    if st.session_state.authenticated and db is not None:
        render_dashboard(t, items)
    render_sidebar(t, items)


# ==========================================
# 12. FASTAPI FACTORY (Only created when API_MODE is active)
# ==========================================
def create_api():
    from fastapi import FastAPI, File, HTTPException, UploadFile
    from fastapi.middleware.cors import CORSMiddleware

    fastapi_app = FastAPI(title="FridgeInventory API", version="1.0.0")
    fastapi_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    class ClassifyRequest(BaseModel):
        item: str = Field(min_length=1, max_length=200)

    class RescueRequest(BaseModel):
        items: list[str] = Field(min_length=1, max_length=30)

    @fastapi_app.get("/")
    @fastapi_app.get("/health")
    def api_health():
        return {
            "ok": True,
            "service": "FridgeInventory",
            "gemini_configured": get_client() is not None,
            "firebase_configured": get_db() is not None,
        }

    @fastapi_app.post("/classify")
    def api_classify(payload: ClassifyRequest):
        phrase = payload.item.strip()
        if not phrase:
            raise HTTPException(status_code=422, detail="Enter a food or grocery item.")
        try:
            result = classify_food(phrase, gemini_model())
        except Exception as exc:
            log.exception("API food classification failed")
            raise HTTPException(
                status_code=503,
                detail="Food recognition is temporarily unavailable. Check Gemini key, model access, quota, and logs.",
            ) from exc
        if result is None:
            return {"is_food": False, "item": phrase, "message": "This does not appear to be a food or grocery item."}
        days, name_en, name_hi, category, needs_date = result
        return {
            "is_food": True,
            "name_en": name_en,
            "name_hi": name_hi,
            "category": category,
            "requires_expiry_date": needs_date,
            "shelf_life_days": None if needs_date else days,
        }

    @fastapi_app.post("/rescue")
    def api_rescue(payload: RescueRequest):
        names = [str(item).strip()[:100] for item in payload.items if str(item).strip()]
        if not names:
            raise HTTPException(status_code=422, detail="Provide at least one ingredient.")
        try:
            recipe = generate_rescue(names)
            return recipe.model_dump()
        except Exception as exc:
            log.exception("API rescue recipe generation failed")
            raise HTTPException(
                status_code=503,
                detail="Rescue recipe generation is temporarily unavailable.",
            ) from exc

    @fastapi_app.post("/transcribe")
    async def api_transcribe(file: UploadFile = File(...)):
        if not file.filename or not file.filename.lower().endswith(".wav"):
            raise HTTPException(status_code=415, detail="Upload a WAV audio file.")
        max_audio_bytes = 15 * 1024 * 1024
        raw = await file.read(max_audio_bytes + 1)
        if not raw:
            raise HTTPException(status_code=400, detail="Audio file is empty.")
        if len(raw) > max_audio_bytes:
            raise HTTPException(status_code=413, detail="Audio must be no larger than 15 MB.")
        text_value, problem = transcribe(raw)
        if problem:
            raise HTTPException(
                status_code=422,
                detail={"problem": problem, "message": "Could not transcribe this audio. Use a clear WAV recording."},
            )
        return {"text": text_value}

    return fastapi_app


if API_MODE:
    api = create_api()

if __name__ == "__main__":
    if API_MODE:
        import uvicorn
        uvicorn.run(api, host="0.0.0.0", port=int(os.getenv("PORT", "10000")), reload=False)
    else:
        main()
