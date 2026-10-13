from datetime import datetime, timedelta
import hashlib
import json
import time
import pandas as pd
import streamlit as st
import firebase_admin
from firebase_admin import credentials, firestore
import speech_recognition as sr
from database import EXPIRY_DATABASE
from gemini import get_ai_expiry_and_names
from help import render_expiry_helper

# ==========================================
# 1. PAGE CONFIG & PROFESSIONAL CARTOON CSS
# ==========================================
st.set_page_config(
    page_title="FridgeInventory",
    page_icon="FI",
    layout="centered",
    initial_sidebar_state="expanded",
)

cartoon_css = """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Fredoka+One&family=Lexend:wght@400;500;600&display=swap');
    
    #MainMenu {visibility: visible !important;}
    footer {visibility: hidden !important;}
    [data-testid="stDecoration"] {display: none !important;}
    .stAppToolbar {visibility: visible !important;}
    [data-testid="stHeaderActionElements"] {display: none !important;}
    
    button:focus, input:focus, select:focus, textarea:focus, [data-baseweb="input"]:focus-within {
        border-color: #EC4899 !important;
        box-shadow: 0 0 0 2px rgba(236, 72, 153, 0.3) !important;
    }
    
    [data-testid="stSidebar"] {
        background-color: #111827 !important;
        border-right: 3px solid #1F2937;
    }
    
    .stApp { background-color: #0B0E14; font-family: 'Lexend', sans-serif; color: #F3F4F6; }
    
    .cartoon-title { 
        font-family: 'Fredoka One', cursive; 
        font-size: 32px; 
        color: #00F2FE; 
        text-shadow: 2px 2px 0px #050505; 
        margin: 0; 
    }
    
    .header-bubble { 
        background: linear-gradient(135deg, #1E2638 0%, #111827 100%); 
        padding: 24px; 
        border-radius: 20px; 
        border: 3px solid #3B82F6; 
        box-shadow: 0px 8px 0px #1D4ED8; 
        margin-bottom: 25px; 
    }
    
    .header-sub { 
        color: #9CA3AF; 
        font-size: 15px; 
        font-weight: 500; 
        margin-top: 6px; 
    }
    
    .bubble-empty { 
        background-color: #0284C7; 
        color: #FFFFFF; 
        padding: 20px; 
        border-radius: 18px; 
        border: 3px solid #38BDF8; 
        box-shadow: 0px 6px 0px #0369A1; 
        font-size: 15px; 
        line-height: 1.6; 
    }
    
    .bubble-yellow-warning {
        background-color: #FEF08A;
        color: #713F12;
        padding: 24px;
        border-radius: 20px;
        border: 3px solid #FACC15;
        box-shadow: 0px 8px 0px #CA8A04;
        font-size: 18px;
        font-weight: 600;
        line-height: 1.5;
        margin-top: 20px;
        text-align: center;
    }

    .modal-overlay {
        background: linear-gradient(135deg, #451A03 0%, #7C2D12 100%);
        border: 4px solid #F87171;
        box-shadow: 0px 10px 0px #991B1B;
        padding: 25px;
        border-radius: 20px;
        margin-bottom: 25px;
        color: #FEF2F2;
    }
    
    div.stButton > button:first-child { 
        font-family: 'Fredoka One', cursive !important; 
        background-color: #EC4899 !important; 
        color: white !important; 
        border: 3px solid #F472B6 !important; 
        box-shadow: 0px 5px 0px #BE185D !important; 
        border-radius: 15px !important; 
        font-size: 18px !important; 
        transition: all 0.1s ease; 
    }
    
    div.stButton > button:first-child:active { 
        transform: translateY(4px); 
        box-shadow: 0px 1px 0px #BE185D !important; 
    }
    </style>
"""
st.markdown(cartoon_css, unsafe_allow_html=True)


# ==========================================
# 2. FIRESTORE FIREWALL & SECURITY INITIALIZATION
# ==========================================
@st.cache_resource
def init_firebase():
  """Enforces secure cloud connection for multi-tenant household isolation."""
  try:
    if not firebase_admin._apps:
      if "firebase" in st.secrets:
        fb_secret = st.secrets["firebase"]
        if isinstance(fb_secret, str):
          cred_dict = json.loads(fb_secret)
        else:
          cred_dict = {k: v for k, v in fb_secret.items()}
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
      else:
        cred = credentials.Certificate("firebase_credentials.json")
        firebase_admin.initialize_app(cred)
    return firestore.client()
  except Exception as e:
    st.error(
        f"🔴 **Cloud Firewall Security Error:** Could not establish secure"
        f" connection to Firestore. Details: {e}"
    )
    return None


db = init_firebase()


# ==========================================
# 3. HELPER & SECURITY FUNCTIONS
# ==========================================
def hash_pin(pin: str) -> str:
  return hashlib.sha256(pin.encode()).hexdigest()


def get_md5_hash(text: str) -> str:
  return hashlib.md5(text.encode("utf-8")).hexdigest()


# ==========================================
# 4. BILINGUAL LANGUAGE PACKS
# ==========================================
LANG_PACK = {
    "en": {
        "title": "FridgeInventory: Voice Kitchen Panel",
        "subtitle": (
            "Zero-effort automated tracking infrastructure designed for Indian"
            " households."
        ),
        "input_hdr": "Smart Control Panel",
        "tab_sim": "Simulation Box",
        "tab_mic": "Live Voice Record",
        "tab_voice": "Upload Audio (.wav)",
        "placeholder": (
            "e.g., 'brought 2 kgs palak, milk and tomatoes' or 'used milk'"
        ),
        "audio_err": (
            "Audio format validation issue. Ensure it is a valid PCM recording."
        ),
        "ledger_hdr": "Active Kitchen Storage Dashboard",
        "col_item": "Grocery Item",
        "col_logged": "Date Logged",
        "col_target": "Expiry Target",
        "col_status": "Freshness Status",
        "empty_msg": (
            "<b>System Status: Empty</b><br>Your fridge contains no active food"
            " records. Type or record an audio update command inside the"
            " control panel above to automatically populate your household"
            " tracking matrix."
        ),
        "status_today": "CRITICAL: EXPIRES TODAY",
        "status_tomorrow": "WARNING: EXPIRING TOMORROW",
        "status_fresh": "Fresh ({days} Days Remaining)",
    },
    "hi": {
        "title": "फ्रिज इन्वेंटरी: वॉइस किचन पैनल",
        "subtitle": (
            "भारतीय घरों के लिए बनाया गया बिना किसी झंझट के ऑटोमैटिक सामान"
            " ट्रैकिंग सिस्टम।"
        ),
        "input_hdr": "स्मार्ट कंट्रोल पैनल",
        "tab_sim": "सिमुलेशन बॉक्स",
        "tab_mic": "लाइव वॉइस रिकॉर्ड",
        "tab_voice": "ऑडियो अपलोड (.wav)",
        "placeholder": (
            "जैसे, 'brought palak, milk and tomatoes' या 'used milk'"
        ),
        "audio_err": (
            "ऑडियो फॉर्मेट में दिक्कत है। कृपया सुनिश्चित करें कि यह एक साफ"
            " रिकॉर्डिंग है।"
        ),
        "ledger_hdr": "सक्रिय रसोई सामान डैशबोर्ड",
        "col_item": "रसोई का सामान",
        "col_logged": "लाने की तारीख",
        "col_target": "खराब होने की तारीख",
        "col_status": "ताजगी की स्थिति",
        "empty_msg": (
            "<b>सिस्टम स्थिति: खाली</b><br>आपके फ्रिज में अभी कोई सामान नहीं"
            " है। ऊपर दिए गए बॉक्स में सामान की जानकारी टाइप करें या रिकॉर्डिंग"
            " करें ताकि लिस्ट अपने आप बन सके।"
        ),
        "status_today": "आपातकालीन: आज ही खराब हो जाएगा",
        "status_tomorrow": "चेतावनी: कल खराब होने वाला है",
        "status_fresh": "बिल्कुल ताजा है ({days} दिन बचे हैं)",
    },
}

# ==========================================
# 5. SESSION STATE INITIALIZATION & RESET
# ==========================================
if "authenticated" not in st.session_state:
  if "fridge" in st.query_params:
    st.session_state.authenticated = True
    st.session_state.fridge_id = st.query_params["fridge"]
  else:
    st.session_state.authenticated = False
    st.session_state.fridge_id = ""

if "lang" not in st.session_state:
  st.session_state.lang = "en"
if "last_processed_hash" not in st.session_state:
  st.session_state.last_processed_hash = ""
if "show_waste_modal" not in st.session_state:
  st.session_state.show_waste_modal = False
if "waste_modal_items" not in st.session_state:
  st.session_state.waste_modal_items = ""
if "waste_modal_items_hi" not in st.session_state:
  st.session_state.waste_modal_items_hi = ""
if "waste_modal_time" not in st.session_state:
  st.session_state.waste_modal_time = 0.0

if st.session_state.get("clear_next", False):
  st.session_state["cmd_input"] = ""
  if "mic_input" in st.session_state:
    del st.session_state["mic_input"]
  st.session_state["clear_next"] = False

if st.session_state.show_waste_modal:
  if time.time() - st.session_state.waste_modal_time > 10:
    st.session_state.show_waste_modal = False
    st.rerun()

# ==========================================
# 6. SIDEBAR FRIDGE ACCESS & CONTROLS
# ==========================================
with st.sidebar:
  st.title("Fridge Security")
  st.markdown(
      "<p style='color: #9CA3AF; font-size: 13px; line-height: 1.4;'>A"
      " <b>Fridge</b> is a secure, isolated cloud namespace for your household"
      " inventory. Protected by a private PIN so different families keep"
      " their grocery data completely separate.</p>",
      unsafe_allow_html=True,
  )
  st.markdown("---")

  if st.session_state.authenticated:
    st.success(f"Logged into Fridge:\n**{st.session_state.fridge_id.upper()}**")
    st.markdown("---")

    st.subheader("Language Switch")
    btn_label = (
        "Switch to HINDI"
        if st.session_state.lang == "en"
        else "Switch to ENGLISH"
    )
    if st.button(btn_label, use_container_width=True):
      st.session_state.lang = "hi" if st.session_state.lang == "en" else "en"
      st.rerun()

    st.markdown("---")
    st.subheader("Add to Home Screen")
    st.link_button(
        "Open App Bookmark",
        "https://fridgeinventory.streamlit.app",
        use_container_width=True,
    )

    st.markdown("---")
    if st.button("Logout / Lock Fridge", use_container_width=True):
      st.session_state.authenticated = False
      st.session_state.fridge_id = ""
      st.session_state.last_processed_hash = ""
      if "fridge" in st.query_params:
        del st.query_params["fridge"]
      st.rerun()

  else:
    fridge_tab1, fridge_tab2 = st.tabs(["Login Fridge", "Create Fridge"])

    with fridge_tab1:
      st.subheader("Access Active Fridge")
      login_f_id = (
          st.text_input("Fridge ID", placeholder="e.g., sharma_kitchen")
          .strip()
          .lower()
      )
      login_f_pass = st.text_input(
          "Fridge PIN", type="password", key="login_pass"
      )
      if st.button("Unlock Fridge", use_container_width=True):
        if not login_f_id or not login_f_pass:
          st.error("Please enter both Fridge ID and PIN.")
        else:
          hashed = hash_pin(login_f_pass)
          if db:
            doc_ref = db.collection("fridges").document(login_f_id)
            doc = doc_ref.get()
            if doc.exists:
              if doc.to_dict().get("pin_hash") == hashed:
                st.session_state.authenticated = True
                st.session_state.fridge_id = login_f_id
                st.query_params["fridge"] = login_f_id
                st.rerun()
              else:
                st.error("Access Denied: Incorrect Fridge PIN.")
            else:
              st.error("Fridge not found. Create it in the next tab.")
          else:
            st.error("Database connection unavailable.")

    with fridge_tab2:
      st.subheader("Setup New Fridge")
      create_f_id = (
          st.text_input(
              "New Fridge ID", placeholder="e.g., gupta_kitchen", key="create_id"
          )
          .strip()
          .lower()
      )
      create_f_pass = st.text_input(
          "Set Master PIN", type="password", key="create_pass"
      )
      if st.button("Create New Fridge", use_container_width=True):
        if not create_f_id or not create_f_pass:
          st.error("Please provide a Fridge ID and PIN.")
        else:
          hashed = hash_pin(create_f_pass)
          if db:
            doc_ref = db.collection("fridges").document(create_f_id)
            if doc_ref.get().exists:
              st.error("Fridge ID already exists. Choose another name or Login.")
            else:
              doc_ref.set({"pin_hash": hashed, "created_at": datetime.now()})
              st.session_state.authenticated = True
              st.session_state.fridge_id = create_f_id
              st.query_params["fridge"] = create_f_id
              st.success("New secure Fridge created successfully.")
              st.rerun()
          else:
            st.error("Database connection unavailable.")

# ==========================================
# 7. MAIN DASHBOARD (LOGGED IN STATE)
# ==========================================
if not st.session_state.authenticated:
  st.title("FridgeInventory")
  st.markdown(
      '<div class="bubble-yellow-warning">Please log in or create a fridge'
      " using the sidebar tabs on the left to unlock your active kitchen"
      " inventory panel.</div>",
      unsafe_allow_html=True,
  )

else:
  current_lang = st.session_state.lang
  T = LANG_PACK[current_lang]

  if st.session_state.show_waste_modal:
    c_left, c_close = st.columns([3.2, 0.8])
    with c_close:
      if st.button(
          "CLOSE ALERT", use_container_width=True, key="close_waste_modal_btn"
      ):
        st.session_state.show_waste_modal = False
        st.rerun()

    st.markdown(
        f"""
        <div class="modal-overlay">
            <span style="font-size: 24px; font-weight: bold;">NOTICE / सूचना</span><br><br>
            <span style="color: #60A5FA; font-weight: bold;">English:</span><br>
            You discarded: <b>{st.session_state.waste_modal_items}</b>. Plan ahead to use items before expiry next time.<br><br>
            <span style="color: #34D399; font-weight: bold;">हिंदी:</span><br>
            आपने हटाया: <b>{st.session_state.waste_modal_items_hi}</b>। अगली बार समय रहते इसका उपयोग करने का प्रयास करें।
        </div>
    """,
        unsafe_allow_html=True,
    )

  st.markdown(
      f'<div class="header-bubble"><div'
      f' class="cartoon-title">{T["title"]}</div><div'
      f' class="header-sub">{T["subtitle"]}</div></div>',
      unsafe_allow_html=True,
  )

  st.markdown(f"### {T['input_hdr']}")
  tab1, tab2, tab3 = st.tabs([T["tab_sim"], T["tab_mic"], T["tab_voice"]])
  voice_text = ""

  with tab1:
    text_input = st.text_input(
        T["input_hdr"],
        placeholder=T["placeholder"],
        label_visibility="collapsed",
        key="cmd_input",
    ).lower()

  with tab2:
    recorded_audio = st.audio_input(T["tab_mic"], key="mic_input")
    if recorded_audio is not None:
      recognizer = sr.Recognizer()
      try:
        with sr.AudioFile(recorded_audio) as source:
          audio_data = recognizer.record(source)
          voice_text = recognizer.recognize_google(audio_data).lower()
          st.success(f'Transcribed Audio Command: "{voice_text}"')
      except Exception:
        st.error(T["audio_err"])

  with tab3:
    uploaded_audio = st.file_uploader(
        T["tab_voice"], type=["wav"], label_visibility="collapsed"
    )
    if uploaded_audio is not None:
      recognizer = sr.Recognizer()
      try:
        with sr.AudioFile(uploaded_audio) as source:
          audio_data = recognizer.record(source)
          voice_text = recognizer.recognize_google(audio_data).lower()
          st.success(f'Transcribed Audio Command: "{voice_text}"')
      except Exception:
        st.error(T["audio_err"])

  final_command = text_input if text_input else voice_text

  if final_command and final_command.strip():
    current_hash = get_md5_hash(final_command.strip())

    if current_hash != st.session_state.last_processed_hash:
      st.session_state.last_processed_hash = current_hash
      today = datetime.now().date()

      def fetch_inventory():
        inv = {}
        if db:
          docs = (
              db.collection("fridges")
              .document(st.session_state.fridge_id)
              .collection("inventory")
              .stream()
          )
          for doc in docs:
            inv[doc.id] = doc.to_dict()
        return inv

      def save_item(item_key, payload):
        if db:
          (
              db.collection("fridges")
              .document(st.session_state.fridge_id)
              .collection("inventory")
              .document(item_key)
              .set(payload)
          )

      def delete_item(item_key):
        if db:
          (
              db.collection("fridges")
              .document(st.session_state.fridge_id)
              .collection("inventory")
              .document(item_key)
              .delete()
          )

      def clear_all_items():
        if db:
          docs = (
              db.collection("fridges")
              .document(st.session_state.fridge_id)
              .collection("inventory")
              .stream()
          )
          for doc in docs:
            doc.reference.delete()

      inventory = fetch_inventory()

      # 1. Clear All Command
      if any(
          w in final_command
          for w in [
              "used everything",
              "cooked everything",
              "ate everything",
              "clear fridge",
              "clear storage",
          ]
      ):
        clear_all_items()
        st.session_state.clear_next = True
        st.toast("Cleared storage matrix: Marked all items as consumed.")
        st.balloons()
        st.rerun()

      # 2. Food Waste / Discarded Command
      elif any(
          w in final_command
          for w in ["threw", "discarded", "thrown away", "dumped"]
      ):
        wasted_en, wasted_hi = [], []
        for trigger_word, (days, name_en, name_hi) in EXPIRY_DATABASE.items():
          if trigger_word in final_command:
            for doc_id, details in list(inventory.items()):
              if (
                  details.get("core_id") == name_en
                  or doc_id == name_en
                  or doc_id == name_hi
              ):
                delete_item(doc_id)
                wasted_en.append(name_en)
                wasted_hi.append(name_hi)
        if wasted_en:
          st.session_state.waste_modal_items = ", ".join(list(set(wasted_en)))
          st.session_state.waste_modal_items_hi = ", ".join(
              list(set(wasted_hi))
          )
          st.session_state.waste_modal_time = time.time()
          st.session_state.show_waste_modal = True
          st.session_state.clear_next = True
          st.toast("Food waste parameter recorded.")
          st.rerun()
        else:
          st.warning(
              "No tracking matches found within active entries to discard."
          )

      # 3. Consumed / Used Command
      elif any(
          w in final_command
          for w in ["used", "ate", "cooked", "remove", "delete"]
      ):
        removed = []
        for trigger_word, (days, name_en, name_hi) in EXPIRY_DATABASE.items():
          if trigger_word in final_command:
            for doc_id, details in list(inventory.items()):
              if (
                  details.get("core_id") == name_en
                  or doc_id == name_en
                  or doc_id == name_hi
              ):
                delete_item(doc_id)
                removed.append(name_en if current_lang == "en" else name_hi)
        if removed:
          st.session_state.clear_next = True
          st.toast(f"Removed: {', '.join(set(removed))}")
          st.balloons()
          st.rerun()

      # 4. DEFAULT ACTION: ADD / BRING (Static Matrix + Gemini AI Fallback with Validation)
      else:
        added = []
        matched_any = False
        for trigger_word, (days, name_en, name_hi) in EXPIRY_DATABASE.items():
          if trigger_word in final_command:
            target_name = name_en if current_lang == "en" else name_hi
            expiry_date = today + timedelta(days=days)
            item_payload = {
                "date_brought": today.strftime("%Y-%m-%d"),
                "expiry_date": expiry_date.strftime("%Y-%m-%d"),
                "core_id": name_en,
            }
            save_item(target_name, item_payload)
            added.append(target_name)
            matched_any = True

        if not matched_any:
          words = final_command.split()
          action_words = [
              "brought",
              "bought",
              "added",
              "add",
              "market",
              "came",
              "i",
              "we",
              "today",
              "yesterday",
              "2",
              "kgs",
              "kg",
              "grams",
              "packet",
              "packets",
          ]
          clean_words = [
              w for w in words if w not in action_words and not w.isdigit()
          ]
          raw_item_query = (
              " ".join(clean_words) if clean_words else final_command
          )

          ai_days, ai_en, ai_hi = get_ai_expiry_and_names(raw_item_query)

          # Validate if Gemini identified a real food item (ai_days != -1)
          if ai_days == -1:
            st.error(
                "❌ **Validation Error:** The item you spoke is not a food"
                " item. Speak with more accuracy."
            )
          else:
            target_name = ai_en if current_lang == "en" else ai_hi
            expiry_date = today + timedelta(days=ai_days)
            item_payload = {
                "date_brought": today.strftime("%Y-%m-%d"),
                "expiry_date": expiry_date.strftime("%Y-%m-%d"),
                "core_id": ai_en,
            }
            save_item(target_name, item_payload)
            added.append(target_name)

        if added:
          st.session_state.clear_next = True
          st.toast(f"Logged: {', '.join(set(added))}")
          st.rerun()

  st.markdown("<br>", unsafe_allow_html=True)
  st.markdown(f"### {T['ledger_hdr']}")

  display_inventory = {}
  if db:
    docs = (
        db.collection("fridges")
        .document(st.session_state.fridge_id)
        .collection("inventory")
        .stream()
    )
    for doc in docs:
      display_inventory[doc.id] = doc.to_dict()

  inventory_items = []
  for doc_id, d in display_inventory.items():
    core_id = d.get("core_id", doc_id)
    display_name = core_id

    for trig, (d_val, n_en, n_hi) in EXPIRY_DATABASE.items():
      if core_id == n_en:
        display_name = n_en if current_lang == "en" else n_hi
        break

    inventory_items.append({
        "item": display_name,
        "date_brought": d.get("date_brought"),
        "expiry_date": d.get("expiry_date"),
    })

  if inventory_items:
    today = datetime.now().date()
    active_rows = []
    expired_items_list = []

    for item_info in inventory_items:
      expiry_dt = datetime.strptime(
          item_info["expiry_date"], "%Y-%m-%d"
      ).date()
      days_left = (expiry_dt - today).days

      if days_left < 0:
        expired_items_list.append(item_info["item"])
      else:
        if days_left == 0:
          status_text = T["status_today"]
        elif days_left == 1:
          status_text = T["status_tomorrow"]
        else:
          status_text = T["status_fresh"].format(days=days_left)

        active_rows.append({
            T["col_item"]: item_info["item"],
            T["col_logged"]: item_info["date_brought"],
            T["col_target"]: item_info["expiry_date"],
            T["col_status"]: status_text,
        })

    if expired_items_list:
      exp_str = ", ".join(expired_items_list)
      exp_banner_msg = (
          f"<b>Oh no! Expired Items Detected:</b> <b>{exp_str}</b> have"
          " expired and been removed from active storage. Please discard"
          " them safely."
          if current_lang == "en"
          else f"<b>अरे नहीं! सामान खराब हो गया है:</b> <b>{exp_str}</b> की"
          " समय सीमा समाप्त हो गई है और इसे एक्टिव स्टोरेज से हटा दिया गया है।"
      )
      st.markdown(
          f'<div'
          ' style="background-color: #7F1D1D; color: #FEE2E2; padding: 20px;'
          ' border-radius: 18px; border: 3px solid #EF4444; box-shadow: 0px'
          ' 6px 0px #991B1B; font-size: 16px; margin-bottom: 20px;"'
          f'>{exp_banner_msg}</div>',
          unsafe_allow_html=True,
      )

    if active_rows:
      st.dataframe(
          pd.DataFrame(active_rows), use_container_width=True, hide_index=True
      )
      render_expiry_helper(active_rows, T, current_lang)
    else:
      if not expired_items_list:
        st.markdown(
            f'<div class="bubble-empty">{T["empty_msg"]}</div>',
            unsafe_allow_html=True,
        )
  else:
    st.markdown(
        f'<div class="bubble-empty">{T["empty_msg"]}</div>',
        unsafe_allow_html=True,
    )
