import streamlit as st
import speech_recognition as sr
import pandas as pd
from datetime import datetime, timedelta
import time
import hashlib
import json

# --- PERSISTENT CLOUD DATABASE (FIREBASE FIRESTORE) INITIALIZATION ---
import firebase_admin
from firebase_admin import credentials, firestore

@st.cache_resource
def get_db():
    if not firebase_admin._apps:
        try:
            if "firebase" in st.secrets:
                sec = st.secrets["firebase"]
                key_dict = json.loads(sec) if isinstance(sec, str) else dict(sec)
                cred = credentials.Certificate(key_dict)
                firebase_admin.initialize_app(cred)
            else:
                cred = credentials.Certificate("serviceAccountKey.json")
                firebase_admin.initialize_app(cred)
        except Exception as e:
            st.error(f"⚠️ Firebase Connection Error: {e}")
            return None
    return firestore.client()

db = get_db()

# --- HELPER FUNCTIONS FOR STORAGE ---
def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def get_vault_password(fridge_code):
    if db:
        try:
            doc = db.collection("vault_registry").document(fridge_code).get()
            if doc.exists:
                return doc.to_dict().get("password")
        except Exception:
            pass
    return None

def register_vault(fridge_code, hashed_pwd):
    if db:
        db.collection("vault_registry").document(fridge_code).set({"password": hashed_pwd})

def load_inventory(fridge_code):
    if db:
        try:
            doc = db.collection("vault_inventories").document(fridge_code).get()
            if doc.exists:
                return doc.to_dict().get("inventory", {})
        except Exception:
            pass
    return {}

def save_inventory(fridge_code, inventory_dict):
    if db:
        db.collection("vault_inventories").document(fridge_code).set({"inventory": inventory_dict})

# --- PAGE CONFIGURATION ---
st.set_page_config(
    page_title="FridgeInventory Panel",
    layout="wide"
)

# --- GROCERY SHELF LIFE DATABASE ---
EXPIRY_DATABASE = {
    # Dairy & Perishables
    "milk": (2, "Milk", "दूध"), "doodh": (2, "Milk", "दूध"),
    "paneer": (4, "Paneer", "पनीर"), "curd": (4, "Curd", "दही"), "dahi": (4, "Curd", "दही"),
    "bread": (4, "Bread", "ब्रेड"), "butter": (30, "Butter", "मक्खन"), "eggs": (14, "Eggs", "अंडे"),
    "anda": (14, "Eggs", "अंडे"), "cheese": (21, "Cheese", "चीज"), "lassi": (2, "Lassi", "लस्सी"),
    # Vegetables
    "tomato": (5, "Tomato", "टमाटर"), "tamatar": (5, "Tomato", "टमाटर"),
    "potato": (21, "Potato", "आलू"), "aloo": (21, "Potato", "आलू"),
    "onion": (21, "Onion", "प्याज"), "pyaaz": (21, "Onion", "प्याज"),
    "bhindi": (4, "Ladyfinger", "भिंडी"), "capsicum": (6, "Capsicum", "शिमला मिर्च"),
    "matar": (5, "Green Peas", "मटर"), "carrot": (10, "Carrot", "गाजर"),
    "spinach": (3, "Spinach", "पालक"), "palak": (3, "Spinach", "पालक"),
    "dhaniya": (2, "Coriander", "धनिया"), "garlic": (30, "Garlic", "लहसुन"),
    "ginger": (14, "Ginger", "अदरक"),
    # Fruits
    "banana": (4, "Banana", "केला"), "kela": (4, "Banana", "केला"),
    "apple": (14, "Apple", "सेब"), "seb": (14, "Apple", "सेब"),
    "mango": (5, "Mango", "आम"), "aam": (5, "Mango", "आम"),
    "dragonfruit": (7, "Dragonfruit", "ड्रैगन फ्रूट"), "grapes": (6, "Grapes", "अंगूर"),
    # Frozen & Packaged
    "potato bites": (180, "Potato Bites", "पोटाटो बाइट्स"), "frooti": (120, "Frooti", "फ्रूटी"),
    "maggi": (180, "Maggi", "मैगी"), "chips": (90, "Chips", "चिप्स")
}

LANG_PACK = {
    "en": {
        "title": "FridgeInventory 🧊",
        "subtitle": "Multi-family kitchen inventory & live expiry alert tracking system.",
        "input_hdr": "Smart Control Panel", "tab_sim": "Simulation Box", "tab_voice": "Voice Control",
        "tab_alerts": "🚨 Expiry Alerts",
        "placeholder": "e.g., 'I brought potato bites, frooti, dragonfruit, matar' or 'I used lassi'",
        "audio_err": "Audio validation error. Please try speaking clearly.",
        "ledger_hdr": "Active Kitchen Dashboard", "col_item": "Grocery Item",
        "col_logged": "Date Logged", "col_target": "Expiry Target", "col_status": "Status",
        "empty_msg": "<b>System Status: Empty</b><br>No food items logged in your vault.",
        "note_msg": "<b>ALERT:</b> Your <b>{items}</b> will spoil soon. Plan to use them today!",
        "status_expired": "EXPIRED ({days} Days Ago)", "status_today": "CRITICAL: EXPIRES TODAY",
        "status_tomorrow": "WARNING: EXPIRING TOMORROW", "status_fresh": "Fresh ({days} Days Left)"
    },
    "hi": {
        "title": "फ्रिज इन्वेंटरी 🧊",
        "subtitle": "सुरक्षित पारिवारिक रसोई इन्वेंटरी और लाइव एक्सपायरी अलर्ट सिस्टम।",
        "input_hdr": "स्मार्ट कंट्रोल पैनल", "tab_sim": "सिमुलेशन बॉक्स", "tab_voice": "वॉइस कंट्रोल",
        "tab_alerts": "🚨 एक्सपायरी अलर्ट",
        "placeholder": "जैसे, 'I brought potato bites, frooti, dragonfruit' या 'I used lassi'",
        "audio_err": "ऑडियो त्रुटि। कृपया दोबारा स्पष्ट बोलें।",
        "ledger_hdr": "सक्रिय रसोई सामान डैशबोर्ड", "col_item": "रसोई का सामान",
        "col_logged": "लाने की तारीख", "col_target": "खराब होने की तारीख", "col_status": "स्थिति",
        "empty_msg": "<b>सिस्टम स्थिति: खाली</b><br>आपके वॉल्ट में अभी कोई सामान नहीं है।",
        "note_msg": "<b>सूचना:</b> आपका <b>{items}</b> जल्द ही खराब होने वाला है!",
        "status_expired": "खराब हो चुका है ({days} दिन पहले)", "status_today": "आपातकालीन: आज ही खराब होगा",
        "status_tomorrow": "चेतावनी: कल खराब होने वाला है", "status_fresh": "ताजा ({days} दिन बचे हैं)"
    }
}

# --- FORCED HIGH-CONTRAST DARK THEME & FIXED BUTTON STYLING ---
def apply_custom_ui(is_recording=False):
    glow_style = "box-shadow: inset 0 0 40px #3B82F6;" if is_recording else ""
    st.markdown(f"""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Fredoka+One&family=Lexend:wght@400;500;600;700&display=swap');
        .stApp {{ background-color: #0F172A !important; font-family: 'Lexend', sans-serif; color: #F8FAFC !important; {glow_style} }}
        h1, h2, h3, h4, h5, h6, p, label, div {{ color: #F8FAFC !important; }}
        .cartoon-title {{ font-family: 'Fredoka One', cursive; font-size: 34px; color: #60A5FA; text-shadow: 2px 2px 0px #1E3A8A; margin: 0; }}
        .header-bubble {{ background: #1E293B; padding: 20px; border-radius: 16px; border: 2px solid #3B82F6; margin-bottom: 20px; }}
        .header-sub {{ color: #94A3B8 !important; font-size: 15px; margin-top: 4px; }}
        .bubble-empty {{ background-color: #0C4A6E; color: #E0F2FE !important; padding: 18px; border-radius: 14px; border: 2px solid #0284C7; font-size: 15px; }}
        .bubble-alert-note {{ background-color: #581C87; color: #F3E8FF !important; padding: 18px; border-radius: 14px; border: 2px solid #A855F7; font-size: 15px; margin-top: 15px; }}
        
        /* FORCE ALL BUTTONS TO HAVE PERMANENT HIGH CONTRAST (NO BLACK BUTTONS) */
        div.stButton > button {{
            font-family: 'Fredoka One', cursive !important;
            background-color: #EC4899 !important;
            color: #FFFFFF !important;
            border: 2px solid #DB2777 !important;
            border-radius: 12px !important;
            font-size: 16px !important;
        }}
        div.stButton > button:hover {{
            background-color: #DB2777 !important;
            color: #FFFFFF !important;
            border: 2px solid #BE185D !important;
        }}
        
        input {{ background-color: #1E293B !important; color: #FFFFFF !important; border: 1px solid #475569 !important; border-radius: 8px !important; }}
        </style>
    """, unsafe_allow_html=True)

# --- SESSION STATE INITIALIZATION ---
if "lang" not in st.session_state:
    st.session_state.lang = "en"
if "logged_in_family" not in st.session_state:
    st.session_state.logged_in_family = None
if "current_raw_pwd" not in st.session_state:
    st.session_state.current_raw_pwd = ""

# --- SIDEBAR: SECURE FAMILY VAULT MANAGEMENT ---
st.sidebar.markdown("### 🔐 Secure Family Vault")

auth_mode = st.sidebar.radio("Choose Action:", ["Log Into Vault", "Create New Vault"])

family_input_code = st.sidebar.text_input("Fridge / Family Code", placeholder="e.g., sharma_kitchen").strip().lower()
# NATIVE PASSWORD FIELD WITH BUILT-IN EYE ICON
family_input_pass = st.sidebar.text_input("Vault Password / PIN", type="password", placeholder="Enter PIN").strip()

if auth_mode == "Create New Vault":
    if st.sidebar.button("🚀 Create Family Vault", use_container_width=True):
        if not family_input_code or not family_input_pass:
            st.sidebar.error("⚠️ Fill in both fields.")
        else:
            existing_pwd = get_vault_password(family_input_code)
            if existing_pwd:
                st.sidebar.error(f"❌ '{family_input_code}' is already taken!")
            else:
                register_vault(family_input_code, hash_password(family_input_pass))
                st.session_state.logged_in_family = family_input_code
                st.session_state.current_raw_pwd = family_input_pass
                st.sidebar.success("🎉 Vault created successfully!")
                st.rerun()
else:
    if st.sidebar.button("🔑 Login to Vault", use_container_width=True):
        if not family_input_code or not family_input_pass:
            st.sidebar.error("⚠️ Enter Fridge Name and Password.")
        else:
            stored_pwd = get_vault_password(family_input_code)
            if not stored_pwd:
                st.sidebar.error("❌ Vault not found.")
            elif stored_pwd != hash_password(family_input_pass):
                st.sidebar.error("⛔ Incorrect password.")
            else:
                st.session_state.logged_in_family = family_input_code
                st.session_state.current_raw_pwd = family_input_pass
                st.sidebar.success("✅ Logged in successfully!")
                st.rerun()

if not st.session_state.logged_in_family:
    st.warning("🔒 **Please log in or create a Family Vault in the left sidebar to proceed.**")
    st.stop()

st.sidebar.markdown("---")
st.sidebar.success(f"🏡 Active Vault: `{st.session_state.logged_in_family}`")
if st.sidebar.button("🚪 Logout Vault", use_container_width=True):
    st.session_state.logged_in_family = None
    st.session_state.current_raw_pwd = ""
    st.rerun()

# --- HEADER BAR & VAULT STATUS POPOVER WITH PASSWORD VIEW ---
col_space, col_lang_btn, col_vault_badge = st.columns([2.2, 0.9, 0.9])

with col_lang_btn:
    btn_label = "🇮🇳 हिन्दी" if st.session_state.lang == "en" else "🇬🇧 English"
    if st.button(btn_label, use_container_width=True, key="top_lang_switch"):
        st.session_state.lang = "hi" if st.session_state.lang == "en" else "en"
        st.rerun()

with col_vault_badge:
    with st.popover(f"🛡️ {st.session_state.logged_in_family}", use_container_width=True):
        st.markdown("### 🔐 Active Vault Details")
        st.markdown(f"**Fridge Name:** `{st.session_state.logged_in_family}`")
        show_pwd = st.checkbox("👁️ Show Vault Password")
        if show_pwd:
            st.code(st.session_state.current_raw_pwd if st.session_state.current_raw_pwd else "Password Encrypted", language="text")
        else:
            st.markdown("**Password:** `••••••••`")
        st.markdown("---")
        if st.button("🚪 Logout", use_container_width=True, key="top_modal_logout"):
            st.session_state.logged_in_family = None
            st.session_state.current_raw_pwd = ""
            st.rerun()

current_lang = st.session_state.lang
T = LANG_PACK[current_lang]

st.markdown(f'<div class="header-bubble"><div class="cartoon-title">{T["title"]}</div><div class="header-sub">{T["subtitle"]}</div></div>', unsafe_allow_html=True)

# --- TABS CONTROL PANEL ---
tab1, tab2, tab3 = st.tabs([T['tab_sim'], T['tab_voice'], T['tab_alerts']])
voice_text = ""
is_currently_recording = False

with tab1:
    st.markdown(f"### {T['input_hdr']}")
    text_input = st.text_input("Simulation Input", placeholder=T['placeholder'], label_visibility="collapsed").lower()

with tab2:
    st.markdown(f"### {T['input_hdr']}")
    st.markdown("🎙️ **Click record to speak your command:**")
    audio_data_input = st.audio_input("Record Voice Command")
    
    if audio_data_input is not None:
        is_currently_recording = True
        recognizer = sr.Recognizer()
        try:
            with open("temp_audio.wav", "wb") as f:
                f.write(audio_data_input.getbuffer())
            with sr.AudioFile("temp_audio.wav") as source:
                audio_file_record = recognizer.record(source)
                voice_text = recognizer.recognize_google(audio_file_record).lower()
                st.toast(f'🗣️ Voice Command: "{voice_text}"', icon="🎙️")
        except Exception:
            st.error(T['audio_err'])

with tab3:
    st.markdown("### 🚨 Live Expiry Warning Center")

apply_custom_ui(is_recording=is_currently_recording)
final_command = text_input if 'text_input' in locals() and text_input else voice_text

# --- DATABASE PERSISTENCE LOGIC ---
if final_command:
    inventory = load_inventory(st.session_state.logged_in_family)
    today = datetime.now().date()

    if any(w in final_command for w in ["used everything", "cooked everything", "ate everything", "clear fridge"]):
        save_inventory(st.session_state.logged_in_family, {})
        st.toast("Storage cleared!")
        st.rerun()
    elif any(w in final_command for w in ["used", "ate", "cooked", "remove", "delete", "threw"]):
        removed_items = []
        for trigger_word, (days, name_en, name_hi) in EXPIRY_DATABASE.items():
            target_name = name_en if current_lang == "en" else name_hi
            if trigger_word in final_command and (name_en in inventory or name_hi in inventory):
                if name_en in inventory: del inventory[name_en]
                if name_hi in inventory: del inventory[name_hi]
                removed_items.append(target_name)
        if removed_items:
            save_inventory(st.session_state.logged_in_family, inventory)
            st.toast(f"Removed: {', '.join(removed_items)}")
            st.rerun()
    elif any(w in final_command for w in ["brought", "bought", "added", "add", "market", "came"]):
        added_items = []
        for trigger_word, (days, name_en, name_hi) in EXPIRY_DATABASE.items():
            if trigger_word in final_command:
                target_name = name_en if current_lang == "en" else name_hi
                if target_name not in added_items:
                    expiry_date = today + timedelta(days=days)
                    inventory[target_name] = {"date_brought": today.strftime("%Y-%m-%d"), "expiry_date": expiry_date.strftime("%Y-%m-%d"), "core_id": name_en}
                    added_items.append(target_name)
        if added_items:
            save_inventory(st.session_state.logged_in_family, inventory)
            st.toast(f"Logged: {', '.join(added_items)}")
            st.rerun()

st.markdown("<br>", unsafe_allow_html=True)
st.markdown(f"### {T['ledger_hdr']}")

inventory_data = load_inventory(st.session_state.logged_in_family)
synchronized_data = {}
for item, details in list(inventory_data.items()):
    core_ref = details.get("core_id", "")
    for trigger_word, (days, name_en, name_hi) in EXPIRY_DATABASE.items():
        if core_ref == name_en:
            synchronized_data[name_en if current_lang == "en" else name_hi] = details
            break

if synchronized_data:
    today = datetime.now().date()
    display_rows = []
    urgent_alerts_list = []
    
    for item, details in synchronized_data.items():
        expiry_dt = datetime.strptime(details['expiry_date'], "%Y-%m-%d").date()
        days_left = (expiry_dt - today).days
        if days_left < 0: status_text = T['status_expired'].format(days=abs(days_left))
        elif days_left == 0: status_text = T['status_today']
        elif days_left == 1: status_text = T['status_tomorrow']
        else: status_text = T['status_fresh'].format(days=days_left)
        
        row_item = { T['col_item']: item, T['col_logged']: details['date_brought'], T['col_target']: details['expiry_date'], T['col_status']: status_text }
        display_rows.append(row_item)
        
        if days_left <= 1:
            urgent_alerts_list.append(row_item)
    
    st.dataframe(pd.DataFrame(display_rows), use_container_width=True, hide_index=True)
    
    with tab3:
        if urgent_alerts_list:
            st.warning("⚠️ **Items expiring today or tomorrow!**")
            st.dataframe(pd.DataFrame(urgent_alerts_list), use_container_width=True, hide_index=True)
        else:
            st.success("✅ **All clear! No urgent items.**")

    urgent_items = [r[T['col_item']] for r in display_rows if any(k in r[T['col_status']] for k in ["EXPIRED", "CRITICAL", "WARNING", "खराब", "आपातकालीन", "चेतावनी"])]
    if urgent_items:
        st.markdown(f'<div class="bubble-alert-note">{T["note_msg"].format(items=", ".join(urgent_items))}</div>', unsafe_allow_html=True)
else:
    st.markdown(f'<div class="bubble-empty">{T["empty_msg"]}</div>', unsafe_allow_html=True)
