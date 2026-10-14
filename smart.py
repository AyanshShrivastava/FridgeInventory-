"""FridgeInventory: a voice- and text-driven fridge tracker for Indian kitchens.

Run:    streamlit run smart.py
Needs:  database.py (EXPIRY_DATABASE) next to this file,
        and .streamlit/secrets.toml with [google] and [firebase] sections.
"""

import hashlib
import hmac
import io
import json
import logging
import re
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from html import escape as esc
from zoneinfo import ZoneInfo

import extra_streamlit_components as stx
import firebase_admin
import speech_recognition as sr
import streamlit as st
from firebase_admin import credentials, firestore
from google import genai
from google.api_core.exceptions import AlreadyExists
from google.genai import types
from pydantic import BaseModel, Field

from database import EXPIRY_DATABASE

# ==========================================
# 1. CONFIG, STATE, VOCABULARY & PERSISTENCE
# ==========================================
st.set_page_config(
    page_title="FridgeInventory",
    page_icon="🧊",
    layout="centered",
    initial_sidebar_state="expanded",
)

log = logging.getLogger("fridgeinventory")
IST = ZoneInfo("Asia/Kolkata")
APP_URL = "https://fridgeinventory.streamlit.app"
PIN_ITERATIONS = 200_000
MAX_CMD_CHARS = 200
MAX_AI_LOOKUPS = 4          # unknown items per command (cost guard)
MAX_TRIES = 5               # wrong PINs before a short lockout
LOCK_SECONDS = 60
FRIDGE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,29}$")
WORD_RE = re.compile(r"[a-z\u0900-\u097f]+")
SPLIT_RE = re.compile(
    r"\s*(?:,|;|&|\+|\band\b|\bor\b|\balso\b|\bthen\b|\baur\b|और|तथा|फिर)\s*"
)

for _key, _val in {
    "authenticated": False,
    "fridge_id": "",
    "lang": "en",
    "recipe": None,
    "login_fails": 0,
    "lock_until": 0.0,
}.items():
    st.session_state.setdefault(_key, _val)

# Initialize Cookie Manager for clean, hidden session persistence
@st.cache_resource(show_spinner=False)
def get_cookie_manager():
    return stx.CookieManager(key="smartrasoi_cookie_mgr")

cookie_manager = get_cookie_manager()

# Secure Cookie Session Restoration:
def verify_and_restore_session():
    if st.session_state.get("authenticated"):
        return

    cookies = cookie_manager.cookies
    if cookies is None:
        return  # Still loading cookies from browser client

    c_fridge = cookies.get("smartrasoi_fridge")
    c_token = cookies.get("smartrasoi_token")

    if c_fridge and c_token:
        fid = str(c_fridge).strip().lower()
        try:
            if db is not None:
                doc_ref = db.collection("fridges").document(fid)
                snap = doc_ref.get()
                if snap.exists:
                    data = snap.to_dict() or {}
                    if hmac.compare_digest(data.get("active_session_token", ""), c_token):
                        st.session_state.authenticated = True
                        st.session_state.fridge_id = fid
                        return
        except Exception:
            pass
        st.session_state.unauthorized_cookie_attempt = fid

verify_and_restore_session()

ADD_WORDS = {"brought", "bring", "bought", "buy", "got", "get", "added", "add",
             "purchased", "लाया", "लाई", "लाए", "लिया", "खरीदा", "खरीदी", "खरीदे"}
TAKE_WORDS = {"used", "use", "ate", "eaten", "cooked", "finished", "consumed",
              "remove", "removed", "delete", "इस्तेमाल", "खाया", "खाई"}
WASTE_WORDS = {"threw", "throw", "thrown", "discarded", "discard", "dumped",
               "trashed", "wasted", "spoiled", "फेंका", "फेंकी", "फेंके", "फेंक"}
ALL_WORDS = {"all", "everything", "whole", "सब", "सारा", "सारी", "पूरा", "पूरी"}
CLEAR_PHRASES = ("clear fridge", "clear storage", "clear all", "empty fridge",
                 "used everything", "ate everything", "cooked everything",
                 "finished everything", "सब खत्म")
STOP_WORDS = ADD_WORDS | TAKE_WORDS | WASTE_WORDS | ALL_WORDS | {
    "and", "or", "also", "then", "aur", "और", "तथा", "फिर",
    "i", "we", "me", "my", "our", "the", "a", "an", "some", "of", "to", "from",
    "have", "has", "had", "in", "on", "it", "is", "was", "please", "just", "few",
    "little", "kg", "kgs", "kilo", "kilos", "gm", "gms", "gram", "grams", "g",
    "l", "ltr", "ltrs", "litre", "litres", "liter", "liters", "ml", "pack", "packs",
    "packet", "packets", "piece", "pieces", "pc", "pcs", "dozen",
    "का", "की", "के", "से", "ने", "को", "में", "पर", "एक", "मैं", "हम", "है",
    "किलो", "ग्राम", "लीटर", "पैकेट", "नग",
}

# ==========================================
# 2. LANGUAGE PACKS
# ==========================================
T = {
    "en": {
        "subtitle": "Tell it what you bought or used. It keeps your kitchen fresh and waste-free.",
        "tab_text": "Type", "tab_voice": "Record", "tab_upload": "Upload WAV",
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
        "ai_busy": "AI service is busy or not configured. Nothing was saved for: {names}.",
        "too_many": "Too many unknown items at once. Nothing was saved for: {names}. Send fewer items.",
        "added": "Added: {names}", "used": "Used up: {names}", "wasted": "Discarded: {names}",
        "missing": "Not found in your fridge: {names}",
        "rejected": "Not a food item: {names}. Check the spelling and try again.",
        "cleared": "Fridge cleared. Everything is marked as consumed.",
        "nothing": "Could not understand that. Try: \"brought milk and tomatoes\".",
        "table_hdr": "Active storage", "col_item": "Item", "col_added": "Added",
        "col_expiry": "Expires", "col_status": "Freshness",
        "st_today": "Expires today", "st_tomorrow": "Expires tomorrow",
        "st_fresh": "{days} days left",
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
        "stats_items": "Items", "stats_soon": "Expiring soon",
        "home_hdr": "Add to home screen",
        "home_note": "A website can't install itself. Open the link in Chrome (Android) or Safari (iPhone), then follow the steps below.",
        "open_link": "Open fridge link", "copy_link": "Copy the link below (tap the copy icon)",
        "android": "**Android (Chrome):** tap ⋮ → **Add to Home screen** (or **Install app**).",
        "iphone": "**iPhone (Safari):** tap the Share icon ⎋ → **Add to Home Screen**.",
        "secure": "PINs are stored as salted hashes. Each fridge is secured by secure browser cookies.",
        "auth_err": "Session invalid or expired. Please log in again.",
    },
    "hi": {
        "subtitle": "बताइए क्या लाए या इस्तेमाल किया। यह आपकी रसोई को ताज़ा और बर्बादी-मुक्त रखेगा।",
        "tab_text": "टाइप करें", "tab_voice": "रिकॉर्ड करें", "tab_upload": "WAV अपलोड करें",
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
        "st_fresh": "{days} दिन बाकी",
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
        "stats_items": "सामान", "stats_soon": "जल्द खराब",
        "home_hdr": "होम स्क्रीन पर जोड़ें",
        "home_note": "वेबसाइट खुद को इंस्टॉल नहीं कर सकती। लिंक Chrome (Android) या Safari (iPhone) में खोलें, फिर नीचे दिए स्टेप्स देखें।",
        "open_link": "फ्रिज लिंक खोलें", "copy_link": "नीचे का लिंक कॉपी करें (कॉपी आइकन दबाएँ)",
        "android": "**Android (Chrome):** ⋮ दबाएँ → **Add to Home screen** (या **Install app**)।",
        "iphone": "**iPhone (Safari):** Share ⎋ दबाएँ → **Add to Home Screen**。",
        "secure": "PIN सुरक्षित हैश में सेव होता है। ब्राउज़र कुकीज़ द्वारा सुरक्षित।",
        "auth_err": "सत्र अमान्य या समाप्त हो गया है। कृपया फिर से लॉग इन करें।",
    },
}

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Fredoka+One&family=Lexend:wght@400;500;600&display=swap');

/* Hide Streamlit footers, deployment badges, and watermarks */
[data-testid="stDecoration"] {display: none !important;}
[data-testid="stFooter"] {display: none !important;}
footer {display: none !important;}
.viewerBadge_container__1QSob {display: none !important;}
.stDeployButton {display: none !important;}

/* Hide the status widget/toolbar badge but explicitly KEEP the 3-dot menu button functional */
[data-testid="stStatusWidget"] {display: none !important;}
#MainMenu {visibility: visible !important; display: block !important;}
header[data-testid="stHeader"] {
    background: transparent !important;
}
header[data-testid="stHeader"] > div:first-child {
    display: none !important; /* Hides deploy/hosting buttons */
}
#MainMenu {
    display: block !important;
    visibility: visible !important;
}

.stApp {background: #0B0E14; font-family: 'Lexend', sans-serif; color: #F3F4F6;}
[data-testid="stSidebar"] {background: #111827; border-right: 1px solid #1F2937;}
.hdr {background: linear-gradient(135deg, #1E2638 0%, #111827 100%); padding: 22px 24px;
      border-radius: 20px; border: 3px solid #3B82F6; box-shadow: 0 8px 0 #1D4ED8; margin-bottom: 22px;}
.hdr h1 {font-family: 'Fredoka One', cursive; font-size: 30px; color: #00F2FE; margin: 0;
         text-shadow: 2px 2px 0 #050505;}
.hdr p {color: #9CA3AF; margin: 6px 0 0; font-size: 14px;}
.bubble {border-radius: 18px; padding: 18px 20px; margin: 14px 0; line-height: 1.55;}
.bubble-info {background: #0284C7; border: 3px solid #38BDF8; box-shadow: 0 6px 0 #0369A1; color: #fff;}
.bubble-warn {background: #FEF08A; border: 3px solid #FACC15; box-shadow: 0 6px 0 #CA8A04;
              color: #713F12; font-weight: 600;}
.bubble-danger {background: #7F1D1D; border: 3px solid #EF4444; box-shadow: 0 6px 0 #991B1B; color: #FEE2E2;}
.bubble-rec {background: linear-gradient(135deg, #1E3A8A 0%, #0F172A 100%); border: 3px solid #3B82F6;
             box-shadow: 0 6px 0 #1D4ED8;}
.heard-box {background: #0F2A44; color: #E0F2FE; border: 2px solid #38BDF8; border-radius: 14px;
            padding: 12px 16px; font-size: 15px; line-height: 1.5; margin: 6px 0 10px; word-break: break-word;}
.heard-box.light {background: #F0F9FF; color: #0C4A6E; border-color: #0284C7; box-shadow: 0 4px 0 #BAE6FD;}
table.inv {width: 100%; border-collapse: separate; border-spacing: 0 8px;}
table.inv td {background: #161B26; padding: 10px 12px; font-size: 14px;}
table.inv td:first-child {border-radius: 12px 0 0 12px;}
table.inv td:last-child {border-radius: 0 12px 12px 0;}
.badge {padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; white-space: nowrap;}
.b-today {background: #7F1D1D; color: #FCA5A5;}
.b-tomorrow {background: #78350F; color: #FDE68A;}
.b-fresh {background: #064E3B; color: #6EE7B7;}
div.stButton > button {font-family: 'Fredoka One', cursive !important; background: #EC4899 !important;
  color: #fff !important; border: 3px solid #F472B6 !important; box-shadow: 0 5px 0 #BE185D !important;
  border-radius: 15px !important; font-size: 17px !important;}
div.stButton > button:active {transform: translateY(4px); box-shadow: 0 1px 0 #BE185D !important;}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


def tr():
    return T[st.session_state.lang]


def pick(en, hi):
    return (hi or en) if st.session_state.lang == "hi" else en


def closable(inner_html, key, css):
    """A notice with a ✕ button. Once closed it stays hidden until its key changes."""
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


# ==========================================
# 3. GEMINI: MODELS, FALLBACK & SCHEMAS
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
    name_en: str = Field(description="Clean English name. Empty if not food.")
    name_hi: str = Field(description="Hindi name in Devanagari. Empty if not food.")


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
- If is_food is false: category not_food, shelf_life_days 0, name_en and name_hi empty.
- If is_food is true: typical shelf life in days for an Indian kitchen (sealed goods last
  longer than fresh produce), an English name and a Devanagari Hindi name.
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


@st.cache_resource(show_spinner=False)
def get_client():
    try:
        raw_key = st.secrets["google"].get("api_key", "")
        api_key = str(raw_key).strip(" '\"\n\r")
        if not api_key:
            log.error("Gemini API key is missing or empty")
            return None
        import os
        os.environ["GEMINI_API_KEY"] = api_key
        return genai.Client()
    except Exception:
        log.exception("Gemini API initialization failed")
        return None


def _call_gemini_with_fallback(contents, config):
    """Strict ordered multi-tier fallback sequence:
    1. gemini-3.8-flash
    2. gemini-3.7
    3. gemini-3.6
    4. gemini-3.5-flash-lite
    """
    client = get_client()
    if client is None:
        raise RuntimeError("Gemini API key is missing")
    
    models_to_try = [
        "gemini-3.8-flash",
        "gemini-3.7",
        "gemini-3.6",
        "gemini-3.5-flash-lite",
    ]
    
    last_exc = None
    for mdl in models_to_try:
        try:
            return client.models.generate_content(
                model=mdl,
                contents=contents,
                config=config,
            )
        except Exception as exc:
            last_exc = exc
            log.warning(f"Model {mdl} unavailable, trying next tier... Error: {exc}")
            continue
    raise last_exc


@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def classify_food(phrase):
    """Return (days, name_en, name_hi) for real food, or None for non-food."""
    first = _call_gemini_with_fallback(
        contents=CLASSIFY_PROMPT.format(item=phrase),
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=FoodVerdict,
        ),
    )
    verdict = FoodVerdict.model_validate_json(first.text or "")
    name_en = verdict.name_en.strip()
    if not verdict.is_food or verdict.category == FoodCategory.NOT_FOOD or not name_en:
        return None
    if not 1 <= verdict.shelf_life_days <= 730:
        return None

    second = _call_gemini_with_fallback(
        contents=VERIFY_PROMPT.format(item=verdict.corrected_item.strip() or phrase),
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=EdibleCheck,
        ),
    )
    if not EdibleCheck.model_validate_json(second.text or "").edible:
        return None
    return verdict.shelf_life_days, name_en, verdict.name_hi.strip() or name_en


def generate_rescue(names):
    resp = _call_gemini_with_fallback(
        contents=RECIPE_PROMPT.format(items=", ".join(names)),
        config=types.GenerateContentConfig(
            temperature=0.6,
            response_mime_type="application/json",
            response_schema=RescueRecipe,
        ),
    )
    return RescueRecipe.model_validate_json(resp.text or "")


# ==========================================
# 4. SPEECH TO TEXT
# ==========================================
@st.cache_data(ttl=300, show_spinner=False)
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
# 5. FIREBASE
# ==========================================
@st.cache_resource(show_spinner=False)
def init_firestore():
    """Return (client_or_None, error_text). Connects once per server."""
    try:
        if not firebase_admin._apps:
            if "firebase" in st.secrets:
                fb = st.secrets["firebase"]
                if isinstance(fb, str):
                    info = json.loads(fb)
                elif "value" in fb:
                    info = json.loads(fb["value"])
                else:
                    info = dict(fb)
                cred = credentials.Certificate(info)
            else:
                cred = credentials.Certificate("firebase_credentials.json")
            firebase_admin.initialize_app(cred)
        return firestore.client(), ""
    except Exception as exc:
        log.exception("Firebase initialisation failed")
        return None, str(exc)


db, db_error = init_firestore()


def hash_pin(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), PIN_ITERATIONS).hex()


def inventory():
    return db.collection("fridges").document(st.session_state.fridge_id).collection("inventory")


def load_items():
    """Return a list of dicts with 'id' plus the stored fields."""
    if db is None or not st.session_state.authenticated:
        return []
    return [{"id": d.id, **(d.to_dict() or {})} for d in inventory().stream()]


def clear_all():
    if db is None:
        return
    for doc in inventory().stream():
        doc.reference.delete()


# ==========================================
# 6. COMMAND PARSING & PROCESSING
# ==========================================
def detect_action(tokens):
    """Return 'waste', 'take', 'add', or None if the words name no action."""
    tokset = set(tokens)
    if tokset & WASTE_WORDS:
        return "waste"
    if tokset & TAKE_WORDS:
        return "take"
    if tokset & ADD_WORDS:
        return "add"
    return None


def matches_item(item, keys):
    """Whole-word matching, so 'tea' does not match 'steak'."""
    names = {
        str(item.get("name_en", "")).lower(),
        str(item.get("name_hi", "")).lower(),
        str(item.get("core_id", "")).lower(),
    } - {""}
    for n in names:
        for k in keys:
            if k == n or k in n.split() or n in k.split() or (len(n) >= 4 and n in k):
                return True
    return False


def process_command(raw, t):
    """Apply a command to Firestore. Returns a list of (kind, message) for display."""
    cmd = " ".join(raw.lower().split())[:MAX_CMD_CHARS]
    if not cmd:
        return []
    if db is None:
        return [("err", t["no_db"])]
    if any(p in cmd for p in CLEAR_PHRASES):
        clear_all()
        return [("ok", t["cleared"])]

    inv = load_items()
    today = datetime.now(IST).date()
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
            matches = {}
            for trigger, (days, name_en, name_hi) in EXPIRY_DATABASE.items():
                if trigger in phrase:
                    matches.setdefault(name_en, (days, name_en, name_hi))
            if not matches:
                if lookups >= MAX_AI_LOOKUPS:
                    too_many.append(phrase)
                    continue
                lookups += 1
                try:
                    with st.spinner(t["ai_wait"]):
                        result = classify_food(phrase)
                except Exception:
                    log.exception("Gemini classification failed")
                    ai_failed.append(phrase)
                    continue
                if result is None:
                    rejected.append(phrase)
                    continue
                matches[result[1]] = result
            for days, name_en, name_hi in matches.values():
                inventory().add({
                    "name_en": name_en,
                    "name_hi": name_hi,
                    "core_id": name_en,
                    "added": today.isoformat(),
                    "expiry": (today + timedelta(days=days)).isoformat(),
                })
                added.append(pick(name_en, name_hi))

        else:  # take or waste
            keys = {phrase} | {
                name_en.lower()
                for trigger, (_, name_en, _) in EXPIRY_DATABASE.items()
                if trigger in phrase
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
        msgs.append(("err", t["rejected"].format(names=", ".join(rejected))))
    if too_many:
        msgs.append(("err", t["too_many"].format(names=", ".join(too_many))))
    if ai_failed:
        msgs.append(("err", t["ai_busy"].format(names=", ".join(ai_failed))))
    if not (added or used or wasted or missing or rejected or too_many or ai_failed):
        msgs.append(("warn", t["nothing"]))
    return msgs


# ==========================================
# 7. AUTH & SIDEBAR
# ==========================================
def create_fridge(fid, pin):
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
    with st.sidebar:
        st.markdown("## 🧊 FridgeInventory")
        st.caption(t["secure"])
        st.divider()

        if st.button(t["lang_btn"], use_container_width=True, key="lang_btn"):
            st.session_state.lang = "hi" if st.session_state.lang == "en" else "en"
            st.rerun()

        if not st.session_state.authenticated:
            if st.session_state.get("unauthorized_cookie_attempt"):
                st.error(t["auth_err"])

            st.subheader(t["login_tab"])
            fid_in = st.text_input(t["fid"], placeholder=t["fid_ph"], key="login_fid")
            pin_in = st.text_input(t["pin"], type="password", key="login_pin")
            fid = fid_in.strip().lower()
            if st.button(t["login_btn"], use_container_width=True, key="login_go"):
                wait = int(st.session_state.lock_until - time.time())
                if wait > 0:
                    st.error(t["locked"].format(s=wait))
                elif not fid or not pin_in:
                    st.error(t["need_both"])
                elif db is None:
                    st.error(t["no_db"])
                else:
                    ok, err = login_fridge(fid, pin_in)
                    if ok:
                        session_token = secrets.token_hex(16)
                        db.collection("fridges").document(fid).update({"active_session_token": session_token})

                        st.session_state.authenticated = True
                        st.session_state.fridge_id = fid
                        st.session_state.login_fails = 0
                        if "unauthorized_cookie_attempt" in st.session_state:
                            del st.session_state["unauthorized_cookie_attempt"]

                        # Secure Cookie storage (expires in 30 days)
                        cookie_manager.set("smartrasoi_fridge", fid, expires_at=datetime.now(timezone.utc) + timedelta(days=30))
                        cookie_manager.set("smartrasoi_token", session_token, expires_at=datetime.now(timezone.utc) + timedelta(days=30))
                        st.rerun()

                    st.session_state.login_fails += 1
                    if st.session_state.login_fails >= MAX_TRIES:
                        st.session_state.lock_until = time.time() + LOCK_SECONDS
                        st.session_state.login_fails = 0
                    st.error(t[err])

            with st.expander(t["create_tab"]):
                new_fid = st.text_input(t["fid"], placeholder=t["fid_ph"], key="create_fid").strip().lower()
                new_pin = st.text_input(t["pin"], type="password", key="create_pin")
                if st.button(t["create_btn"], use_container_width=True, key="create_go"):
                    if not new_fid or not new_pin:
                        st.error(t["need_both"])
                    elif not FRIDGE_RE.match(new_fid):
                        st.error(t["bad_id"])
                    elif len(new_pin) < 4:
                        st.error(t["pin_short"])
                    elif db is None:
                        st.error(t["no_db"])
                    else:
                        ok, err = create_fridge(new_fid, new_pin)
                        if ok:
                            session_token = secrets.token_hex(16)
                            db.collection("fridges").document(new_fid).update({"active_session_token": session_token})

                            st.session_state.authenticated = True
                            st.session_state.fridge_id = new_fid
                            if "unauthorized_cookie_attempt" in st.session_state:
                                del st.session_state["unauthorized_cookie_attempt"]

                            cookie_manager.set("smartrasoi_fridge", new_fid, expires_at=datetime.now(timezone.utc) + timedelta(days=30))
                            cookie_manager.set("smartrasoi_token", session_token, expires_at=datetime.now(timezone.utc) + timedelta(days=30))
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
            cookie_manager.delete("smartrasoi_fridge")
            cookie_manager.delete("smartrasoi_token")
            st.session_state.authenticated = False
            st.session_state.fridge_id = ""
            st.session_state.recipe = None
            st.rerun()


# ==========================================
# 8. DASHBOARD
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
            st.markdown(
                f'<div class="bubble bubble-rec">'
                f'<h4 style="margin:0 0 8px;">🍽️ {esc(title)}</h4>'
                f"<b>{esc(t['ing'])}:</b><ul>{ing}</ul>"
                f"<b>{esc(t['steps'])}:</b><ol>{steps}</ol>"
                f'</div>',
                unsafe_allow_html=True,
            )


# ==========================================
# 9. MAIN PAGE
# ==========================================
def show_messages(msgs):
    for kind, text in msgs:
        if kind == "err":
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
    t = tr()
    st.markdown(
        f'<div class="hdr"><h1>FridgeInventory</h1><p>{esc(t["subtitle"])}</p></div>',
        unsafe_allow_html=True,
    )

    if db is None and db_error:
        st.error(f"{t['no_db']} ({db_error})")

    msgs = []
    if st.session_state.authenticated and db is not None:
        tab_text, tab_rec, tab_up = st.tabs([t["tab_text"], t["tab_voice"], t["tab_upload"]])

        with tab_text:
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
    else:
        st.markdown(f'<div class="bubble bubble-warn">{esc(t["lock_info"])}</div>', unsafe_allow_html=True)

    show_messages(msgs)

    items = load_items()
    if st.session_state.authenticated and db is not None:
        render_dashboard(t, items)

    render_sidebar(t, items)


main()
