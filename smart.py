import streamlit as st
import speech_recognition as sr
import shelve
import pandas as pd
from datetime import datetime, timedelta
import time
import hashlib

# --- PASSWORD HASHING HELPER ---
def hash_password(password):
    """Securely hashes a password using SHA-256."""
    return hashlib.sha256(password.encode()).hexdigest()

# --- PAGE CONFIGURATION ---
st.set_page_config(
    page_title="FridgeInventory: Voice Kitchen Panel",
    layout="wide"
)

# --- ACCURATE REAL-WORLD GROCERY SHELF LIFE DATABASE ---
EXPIRY_DATABASE = {
    # --- 1. DAILY DAIRY & FRESH PERISHABLES (1-5 Days) ---
    "milk": (2, "Milk", "दूध"), "doodh": (2, "Milk", "दूध"), "toned milk": (3, "Milk", "दूध"),
    "paneer": (4, "Paneer", "पनीर"), "cottage cheese": (4, "Paneer", "पनीर"),
    "curd": (4, "Curd", "दही"), "dahi": (4, "Curd", "दही"), "greek yogurt": (5, "Greek Yogurt", "ग्रीक दही"),
    "lassi": (2, "Lassi", "लस्सी"), "chaas": (2, "Buttermilk", "छाछ"), "buttermilk": (2, "Buttermilk", "छाछ"),
    "bread": (4, "Bread", "ब्रेड"), "brown bread": (4, "Brown Bread", "ब्राउन ब्रेड"), "pav": (2, "Pav", "पाव"),
    "cream": (3, "Fresh Cream", "मलाई"), "malai": (3, "Fresh Cream", "मलाई"), "whipping cream": (5, "Whipping Cream", "व्हिपिंग क्रीम"),
    "tofu": (5, "Tofu", "टोफू"), "soy milk": (5, "Soy Milk", "सोया मिल्क"), "almond milk": (7, "Almond Milk", "बादाम दूध"),
    "oat milk": (7, "Oat Milk", "ओट मिल्क"), "yakult": (14, "Yakult", "याकुल्ट"), "eggs": (14, "Eggs", "अंडे"), "anda": (14, "Eggs", "अंडे"),

    # --- 2. LEAFY GREENS & HERBS (2-4 Days) ---
    "spinach": (3, "Spinach", "पालक"), "palak": (3, "Spinach", "पालक"),
    "coriander": (2, "Coriander", "धनिया"), "dhaniya": (2, "Coriander", "धनिया"),
    "methi": (3, "Fenugreek", "मेथी"), "pudina": (3, "Mint", "पुदीना"), "mint": (3, "Mint", "पुदीना"),
    "kale": (4, "Kale", "केल"), "lettuce": (3, "Lettuce", "सलाद पत्ता"), "parsley": (4, "Parsley", "पार्सले"),
    "basil": (3, "Basil", "तुलसी"), "rosemary": (7, "Rosemary", "रोज़मेरी"), "thyme": (7, "Thyme", "थाइम"),
    "curry leaves": (5, "Curry Leaves", "कढ़ी पत्ता"), "kadi patta": (5, "Curry Leaves", "कढ़ी पत्ता"),

    # --- 3. FRESH VEGETABLES (4-10 Days) ---
    "tomato": (5, "Tomato", "टमाटर"), "tamatar": (5, "Tomato", "टमाटर"), "cherry tomato": (7, "Cherry Tomato", "चेरी टमाटर"),
    "bhindi": (4, "Ladyfinger", "भिंडी"), "okra": (4, "Ladyfinger", "भिंडी"),
    "capsicum": (6, "Capsicum", "शिमला मिर्च"), "shimla mirch": (6, "Capsicum", "शिमला मिर्च"),
    "bell pepper": (6, "Bell Pepper", "रंगीन शिमला मिर्च"), "yellow pepper": (6, "Yellow Pepper", "पीली शिमला मिर्च"),
    "cauliflower": (5, "Cauliflower", "गोभी"), "gobi": (5, "Cauliflower", "गोभी"),
    "cabbage": (7, "Cabbage", "पत्ता गोभी"), "patta gobi": (7, "Cabbage", "पत्ता गोभी"),
    "peas": (5, "Green Peas", "मटर"), "matar": (5, "Green Peas", "मटर"),
    "carrot": (10, "Carrot", "गाजर"), "gajar": (10, "Carrot", "गाजर"),
    "cucumber": (5, "Cucumber", "खीरा"), "kheera": (5, "Cucumber", "खीरा"),
    "brinjal": (5, "Brinjal", "बैंगन"), "baingan": (5, "Brinjal", "बैंगन"), "eggplant": (5, "Brinjal", "बैंगन"),
    "broccoli": (5, "Broccoli", "ब्रोकोली"), "zucchini": (5, "Zucchini", "जुकिनी"), "asparagus": (4, "Asparagus", "शतावरी"),
    "celery": (7, "Celery", "सेलरी"), "avocado": (4, "Avocado", "एवोकैडो"), "mushroom": (3, "Mushroom", "मशरूम"),
    "sweet corn": (4, "Sweet Corn", "स्वीट कॉर्न"), "baby corn": (5, "Baby Corn", "बेबी कॉर्न"),
    "beetroot": (14, "Beetroot", "चुकंदर"), "radish": (7, "Radish", "मूली"), "mooli": (7, "Radish", "मूली"),
    "bitter gourd": (6, "Bitter Gourd", "करेला"), "karela": (6, "Bitter Gourd", "करेला"),
    "bottle gourd": (6, "Bottle Gourd", "लौकी"), "lauki": (6, "Bottle Gourd", "लौकी"),
    "ridge gourd": (5, "Ridge Gourd", "तोरई"), "turai": (5, "Ridge Gourd", "तोरई"),

    # --- 4. FRESH FRUITS (3-14 Days) ---
    "banana": (4, "Banana", "केला"), "kela": (4, "Banana", "केला"),
    "apple": (14, "Apple", "सेब"), "seb": (14, "Apple", "सेब"), "green apple": (14, "Green Apple", "हरा सेब"),
    "grapes": (6, "Grapes", "अंगूर"), "angoor": (6, "Grapes", "अंगूर"),
    "papaya": (4, "Papaya", "पपीता"), "papita": (4, "Papaya", "पपीता"),
    "mango": (5, "Mango", "आम"), "aam": (5, "Mango", "आम"), "alphonso": (5, "Alphonso Mango", "अल्फांसो आम"),
    "dragonfruit": (7, "Dragonfruit", "ड्रैगन फ्रूट"), "dragon fruit": (7, "Dragonfruit", "ड्रैगन फ्रूट"),
    "kiwi": (8, "Kiwi", "कीवी"), "blueberries": (7, "Blueberries", "ब्लूबेरी"), "strawberries": (3, "Strawberries", "स्ट्रॉबेरी"),
    "raspberries": (3, "Raspberries", "रास्पबेरी"), "blackberries": (4, "Blackberries", "ब्लैकबेरी"),
    "orange": (10, "Orange", "संतरा"), "santara": (10, "Orange", "संतरा"), "sweet lime": (8, "Mosambi", "मौसमी"), "mosambi": (8, "Mosambi", "मौसमी"),
    "pomegranate": (14, "Pomegranate", "अनार"), "anaar": (14, "Pomegranate", "अनार"),
    "watermelon": (7, "Watermelon", "तरबूज"), "tarbooz": (7, "Watermelon", "तरबूज"),
    "muskmelon": (5, "Muskmelon", "खरबूजा"), "kharbooja": (5, "Muskmelon", "खरबूजा"),
    "guava": (4, "Guava", "अमरूद"), "amrood": (4, "Guava", "अमरूद"),
    "passionfruit": (7, "Passion Fruit", "पैशन फ्रूट"), "passion fruit": (7, "Passion Fruit", "पैशन फ्रूट"),
    "durian": (5, "Durian", "ड्यूरियन"), "rambutan": (5, "Rambutan", "रामबुतान"), "mangosteen": (6, "Mangosteen", "मंगोस्टीन"),
    "plum": (5, "Plum", "आलूबुखारा"), "peach": (5, "Peach", "आड़ू"), "pear": (10, "Pear", "नाशपाती"),

    # --- 5. DURABLE ROOT VEGETABLES (14-30 Days) ---
    "potato": (21, "Potato", "आलू"), "aloo": (21, "Potato", "आलू"), "sweet potato": (21, "Sweet Potato", "शकरकंद"),
    "onion": (21, "Onion", "प्याज"), "pyaaz": (21, "Onion", "प्याज"), "spring onion": (5, "Spring Onion", "हरा प्याज"),
    "garlic": (30, "Garlic", "लहसुन"), "lahsun": (30, "Garlic", "लहसुन"), "lehsun": (30, "Garlic", "लहसुन"),
    "ginger": (14, "Ginger", "अदरक"), "adrak": (14, "Ginger", "अदरक"),
    "pumpkin": (21, "Pumpkin", "कद्दू"), "kaddu": (21, "Pumpkin", "कद्दू"),
    "chilli": (8, "Green Chilli", "हरी मिर्च"), "hari mirch": (8, "Green Chilli", "हरी मिर्च"),

    # --- 6. FROZEN SNACKS & READY-TO-COOK (180 Days Freezer Life) ---
    "potato bites": (180, "Potato Bites", "पोटाटो बाइट्स"), "potato pops": (180, "Potato Bites", "पोटाटो बाइट्स"),
    "smileys": (180, "McCain Smileys", "मक्केन स्माइलीज"), "french fries": (180, "French Fries", "फ्रेंच फ्राइज"),
    "mccain": (180, "McCain Snacks", "मक्केन स्नैक्स"), "hash browns": (180, "Hash Browns", "हैश ब्राउन्स"),
    "frozen momos": (120, "Frozen Momos", "फ्रोजन मोमोज"), "momos": (2, "Fresh Momos", "मोमोज"),
    "veg nuggets": (180, "Veg Nuggets", "वेज नगेट्स"), "cheese balls": (180, "Cheese Balls", "चीज बॉल्स"),
    "frozen peas": (180, "Frozen Peas", "फ्रोजन मटर"), "frozen corn": (180, "Frozen Corn", "फ्रोजन कॉर्न"),
    "spring rolls": (120, "Spring Rolls", "स्प्रिंग रोल्स"), "frozen paratha": (120, "Frozen Paratha", "फ्रोजन पराठा"),
    "aloo tikki": (180, "Aloo Tikki", "आलू टिक्की"), "patties": (2, "Fresh Patties", "पैटीज़"),

    # --- 7. AMBIENT SNACKS, BAKERY & PACKAGED FOODS (30-180 Days) ---
    "samosa": (1, "Samosa", "समोसा"), "kachori": (1, "Kachori", "कचौड़ी"), "dhokla": (2, "Dhokla", "ढोकला"),
    "bhujia": (90, "Aloo Bhujia", "आलू भुजिया"), "namkeen": (90, "Namkeen", "नमकीन"), "mathri": (60, "Mathri", "मठरी"),
    "chips": (90, "Potato Chips", "चिप्स"), "lays": (90, "Lays Chips", "लेज टिप्स"), "kurkure": (90, "Kurkure", "कुरकुरे"),
    "doritos": (120, "Doritos", "डोरिटोस"), "nachos": (120, "Nachos", "नाचोस"), "pringles": (180, "Pringles", "प्रिंगल्स"),
    "biscuits": (120, "Biscuits", "बिस्कुट"), "cookies": (90, "Cookies", "कुकीज"), "croissant": (2, "Croissant", "क्रोसांट"),
    "donut": (2, "Donut", "डोनट"), "muffin": (3, "Muffin", "मफिन"), "cake": (3, "Cake", "केक"), "pastry": (2, "Pastry", "पेस्ट्री"),
    "ramen": (180, "Instant Ramen", "रामेन"), "maggi": (180, "Maggi Noodles", "मैगी"), "yippee": (180, "Yippee Noodles", "यिप्पी"),
    "pasta": (365, "Dry Pasta", "पास्ता"), "spaghetti": (365, "Spaghetti", "स्पागेटी"), "macaroni": (365, "Macaroni", "मैकरोनी"),
    "popcorn": (120, "Popcorn", "पॉपकॉर्न"), "tacos": (90, "Taco Shells", "टाको शेल"),

    # --- 8. BEVERAGES & DRINKS (30-180 Days) ---
    "frooti": (120, "Frooti", "फ्रूटी"), "maaza": (120, "Maaza", "माज़ा"), "slice": (120, "Slice Mango", "स्लाइस"),
    "coke": (180, "Coca Cola", "कोका कोला"), "cocacola": (180, "Coca Cola", "कोका कोला"), "pepsi": (180, "Pepsi", "पेप्सी"),
    "sprite": (180, "Sprite", "स्प्राइट"), "thums up": (180, "Thums Up", "थम्स अप"), "7up": (180, "7Up", "सेवन अप"),
    "red bull": (365, "Red Bull", "रेड बुल"), "monster": (365, "Monster Energy", "मॉन्स्टर एनर्जी"),
    "kombucha": (30, "Kombucha", "कंबुचा"), "matcha": (180, "Matcha Tea", "माचा चाय"),
    "coconut water": (2, "Coconut Water", "नारियल पानी"), "nariyal pani": (2, "Coconut Water", "नारियल पानी"),
    "real juice": (14, "Real Juice", "रियल जूस"), "tropicana": (14, "Tropicana Juice", "ट्रॉपिकाना जूस"),
    "iced tea": (30, "Iced Tea", "आइस टी"), "cold coffee": (7, "Cold Coffee", "कोल्ड कॉफी"),

    # --- 9. CHEESE & SAUCES (15-180 Days) ---
    "cheese": (21, "Cheese Block", "चीज"), "processed cheese": (30, "Processed Cheese", "चीज"),
    "mozzarella": (15, "Mozzarella Cheese", "मोजरेला चीज"), "cheddar": (60, "Cheddar Cheese", "चेडर चीज"),
    "parmesan": (90, "Parmesan Cheese", "पारमेसन चीज"), "gouda": (45, "Gouda Cheese", "गौड़ा चीज"),
    "butter": (30, "Butter", "मक्खन"), "peanut butter": (180, "Peanut Butter", "पीनट बटर"),
    "mayonnaise": (60, "Mayonnaise", "मेयोनेज़"), "mayo": (60, "Mayonnaise", "मेयोनेज़"),
    "ketchup": (180, "Tomato Ketchup", "टोमैटो सॉस"), "sauce": (90, "Sauce", "सॉस"),
    "sriracha": (180, "Sriracha Sauce", "श्रीराचा सॉस"), "soy sauce": (365, "Soy Sauce", "सोया सॉस"),
    "kimchi": (30, "Kimchi", "किमची"), "hummus": (5, "Hummus", "हमुस"), "salsa": (14, "Salsa", "साल्सा"),
    "jam": (180, "Fruit Jam", "जैम"), "nutella": (180, "Nutella Spread", "न्यूटेला")
}

LANG_PACK = {
    "en": {
        "title": "FridgeInventory 🧊",
        "subtitle": "Secure multi-family kitchen inventory & live expiry alert tracking system.",
        "input_hdr": "Smart Control Panel", "tab_sim": "Simulation Box", "tab_voice": "Live Mic Voice Control",
        "tab_alerts": "🚨 Expiry Alerts",
        "placeholder": "e.g., 'I brought potato bites, frooti, dragonfruit, matar' or 'I used lassi'",
        "audio_err": "Audio format validation issue. Ensure clear spoken recording.",
        "ledger_hdr": "Active Kitchen Storage Dashboard", "col_item": "Grocery Item",
        "col_logged": "Date Logged", "col_target": "Expiry Target", "col_status": "Freshness Status",
        "empty_msg": "<b>System Status: Empty</b><br>The virtual ledger contains no active food records. Type or use the live voice button above to automatically populate your household tracking matrix.",
        "note_msg": "<b>NOTE:</b> Your <b>{items}</b> will spoil soon. Please plan to use your <b>{items}</b> today to optimize household utility before complete spoilage.",
        "status_expired": "EXPIRED ({days} Days Ago) -> Discard!", "status_today": "CRITICAL: EXPIRES TODAY",
        "status_tomorrow": "WARNING: EXPIRING TOMORROW", "status_fresh": "Fresh ({days} Days Remaining)"
    },
    "hi": {
        "title": "फ्रिज इन्वेंटरी (FridgeInventory) 🧊",
        "subtitle": "सुरक्षित पारिवारिक रसोई इन्वेंटरी और लाइव एक्सपायरी अलर्ट सिस्टम।",
        "input_hdr": "स्मार्ट कंट्रोल पैनल", "tab_sim": "सिमुलेशन बॉक्स", "tab_voice": "लाइव माइक वॉइस कंट्रोल",
        "tab_alerts": "🚨 एक्सपायरी अलर्ट",
        "placeholder": "जैसे, 'I brought potato bites, frooti, dragonfruit' या फिर 'I used lassi'",
        "audio_err": "ऑडियो फॉर्मेट में दिक्कत है। कृपया दोबारा स्पष्ट बोलें।",
        "ledger_hdr": "सक्रिय रसोई सामान डैशबोर्ड", "col_item": "रसोई का सामान",
        "col_logged": "लाने की तारीख", "col_target": "खराब होने की तारीख", "col_status": "ताजगी की स्थिति",
        "empty_msg": "<b>सिस्टम स्थिति: खाली</b><br>आपके डैशबोर्ड में अभी कोई सामान नहीं है। ऊपर दिए गए बॉक्स में टाइप करें या माइक बटन से बोलकर सामान जोड़ें।",
        "note_msg": "<b>सूचना:</b> आपका <b>{items}</b> जल्द ही खराब होने वाला है। कृपया भोजन की बर्बादी रोकने के लिए आज ही <b>{items}</b> का उपयोग करने की योजना बनाएं।",
        "status_expired": "खराब हो चुका है ({days} दिन पहले) -> फेंक दें!", "status_today": "आपातकालीन: आज ही खराब हो जाएगा",
        "status_tomorrow": "चेतावनी: कल खराब होने वाला है", "status_fresh": "बिल्कुल ताजा है ({days} दिन बचे हैं)"
    }
}

# --- CUSTOM UI STYLES ---
def apply_custom_ui(is_recording=False):
    glow_style = ""
    if is_recording:
        glow_style = """
        .stApp {
            box-shadow: inset 0 0 50px 10px #3B82F6 !important;
            border: 4px solid #60A5FA !important;
            transition: all 0.3s ease-in-out;
        }
        """
    st.markdown(f"""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Fredoka+One&family=Lexend:wght@400;500;600;700&display=swap');
        .stApp {{ background-color: #0B0E14; font-family: 'Lexend', sans-serif; {glow_style} }}
        .cartoon-title {{ font-family: 'Fredoka One', cursive; font-size: 34px; color: #00F2FE; text-shadow: 2px 2px 0px #050505; margin: 0; }}
        .header-bubble {{ background: linear-gradient(135deg, #1E2638 0%, #111827 100%); padding: 24px; border-radius: 20px; border: 3px solid #3B82F6; box-shadow: 0px 8px 0px #1D4ED8; margin-bottom: 30px; }}
        .header-sub {{ color: #9CA3AF; font-size: 15px; font-weight: 500; margin-top: 6px; }}
        .bubble-empty {{ background-color: #0284C7; color: #FFFFFF; padding: 20px; border-radius: 18px; border: 3px solid #38BDF8; box-shadow: 0px 6px 0px #0369A1; font-size: 15px; line-height: 1.6; }}
        .bubble-alert-note {{ background-color: #701A75; color: #FDF4FF; padding: 22px; border-radius: 18px; border: 3px solid #F472B6; box-shadow: 0px 6px 0px #4A044E; font-size: 16px; line-height: 1.6; }}
        .modal-card {{ background: #1E2638; border: 3px solid #F472B6; padding: 24px; border-radius: 20px; box-shadow: 0px 8px 0px #BE185D; margin-bottom: 20px; }}
        .lang-block {{ margin-top: 10px; font-size: 15px; color: #E5E7EB; line-height: 1.5; }}
        div.stButton > button:first-child {{ font-family: 'Fredoka One', cursive !important; background-color: #EC4899 !important; color: white !important; border: 3px solid #F472B6 !important; box-shadow: 0px 5px 0px #BE185D !important; border-radius: 15px !important; font-size: 18px !important; transition: all 0.1s ease; }}
        div.stButton > button:first-child:active {{ transform: translateY(4px); box-shadow: 0px 1px 0px #BE185D !important; }}
        </style>
    """, unsafe_allow_html=True)

def show_center_modal(items_en, items_hi):
    st.markdown(f"""
        <div class="modal-card">
            <span style="color: #FCA5A5; font-size: 26px; font-weight: bold;">OH NO! / ओहो नहीं!</span>
            <div class="lang-block lang-en">
                <span style="color: #60A5FA; font-weight: bold;">English:</span><br>
                You had to throw away your <b>{items_en}</b>. Let's try to cook or consume items before their expiry window alerts trigger next time to minimize household waste.
            </div>
            <div class="lang-block lang-hi">
                <span style="color: #34D399; font-weight: bold;">हिंदी:</span><br>
                आपको अपना <b>{items_hi}</b> फेंकना पड़ा। घर के कचरे को कम करने के लिए अगली बार खराब होने की चेतावनी से पहले ही वस्तुओं का उपयोग करने का प्रयास करें।
            </div>
        </div>
    """, unsafe_allow_html=True)

# --- SESSION STATE INITIALIZATION ---
if "lang" not in st.session_state:
    st.session_state.lang = "en"
if "show_waste_modal" not in st.session_state:
    st.session_state.show_waste_modal = False
if "waste_modal_items" not in st.session_state:
    st.session_state.waste_modal_items = ""
if "waste_modal_items_hi" not in st.session_state:
    st.session_state.waste_modal_items_hi = ""
if "waste_modal_time" not in st.session_state:
    st.session_state.waste_modal_time = 0.0
if "logged_in_family" not in st.session_state:
    st.session_state.logged_in_family = None
if "show_pwd_in_top" not in st.session_state:
    st.session_state.show_pwd_in_top = False

if st.session_state.show_waste_modal:
    if time.time() - st.session_state.waste_modal_time > 10:
        st.session_state.show_waste_modal = False
        st.rerun()

# --- SIDEBAR: SECURE FAMILY VAULT MANAGEMENT ---
st.sidebar.markdown("### 🔐 Secure Family Vault")

with st.sidebar.expander("ℹ️ How Vaults Work"):
    st.markdown("""
    * **Create Vault:** Set up a brand new family code with a secure password.
    * **Login Vault:** Enter your existing family code and password to access your family's live shared inventory.
    """)

auth_mode = st.sidebar.radio("Choose Action:", ["Log Into Vault", "Create New Vault"])

family_input_code = st.sidebar.text_input("Fridge / Family Code", placeholder="e.g., sharma_kitchen").strip().lower()
family_input_pass = st.sidebar.text_input("Vault Password / PIN", type="password", placeholder="e.g., Tiger-99!").strip()

master_registry_db = "fridge_vault_registry_db"

if auth_mode == "Create New Vault":
    if st.sidebar.button("🚀 Create Family Vault", use_container_width=True):
        if not family_input_code or not family_input_pass:
            st.sidebar.error("⚠️ Please fill in both Fridge Name and Password.")
        else:
            with shelve.open(master_registry_db, writeback=True) as reg:
                if family_input_code in reg:
                    st.sidebar.error(f"❌ Fridge name '{family_input_code}' is already taken! Please choose a different name.")
                else:
                    # Save hashed password securely
                    reg[family_input_code] = hash_password(family_input_pass)
                    st.session_state.logged_in_family = family_input_code
                    st.sidebar.success("🎉 Vault created and logged in successfully!")
                    st.rerun()
else:
    if st.sidebar.button("🔑 Login to Vault", use_container_width=True):
        if not family_input_code or not family_input_pass:
            st.sidebar.error("⚠️ Please enter your Fridge Name and Password.")
        else:
            with shelve.open(master_registry_db, writeback=True) as reg:
                if family_input_code not in reg:
                    st.sidebar.error("❌ Fridge Name not found. Create it first!")
                elif reg[family_input_code] != hash_password(family_input_pass):
                    st.sidebar.error("⛔ Incorrect password for this fridge.")
                else:
                    st.session_state.logged_in_family = family_input_code
                    st.sidebar.success("✅ Logged into family vault successfully!")
                    st.rerun()

# Check if authenticated
if not st.session_state.logged_in_family:
    st.warning("🔒 **Please log into or create your Family Vault in the left sidebar to access FridgeInventory.**")
    st.stop()

# Active isolated database for this specific family vault
db_filename = f"fridge_vault_{st.session_state.logged_in_family}_db"

st.sidebar.markdown("---")
st.sidebar.success(f"🏡 Active Vault: `{st.session_state.logged_in_family}`")
if st.sidebar.button("🚪 Logout Vault", use_container_width=True):
    st.session_state.logged_in_family = None
    st.rerun()

# --- TOP HEADER BAR: LANGUAGE SWITCHER & TOP-RIGHT VAULT STATUS BADGE ---
col_space, col_lang_btn, col_vault_badge = [2.2, 0.9, 0.9]

with col_lang_btn:
    btn_label = "🇮🇳 हिन्दी" if st.session_state.lang == "en" else "🇬🇧 English"
    if st.button(btn_label, use_container_width=True, key="top_lang_switch"):
        st.session_state.lang = "hi" if st.session_state.lang == "en" else "en"
        st.rerun()

with col_vault_badge:
    with st.popover(f"🛡️ {st.session_state.logged_in_family}", use_container_width=True):
        st.markdown("### 🔐 Active Vault")
        st.markdown(f"**Fridge Name:** `{st.session_state.logged_in_family}`")
        st.markdown("**Password:** `•••••••• (Secured Hash)`")
        st.markdown("---")
        if st.button("🚪 Logout", use_container_width=True, key="top_modal_logout"):
            st.session_state.logged_in_family = None
            st.rerun()

current_lang = st.session_state.lang
T = LANG_PACK[current_lang]

if st.session_state.show_waste_modal:
    c_left, c_close = st.columns([3.2, 0.8])
    with c_close:
        if st.button("❌ CLOSE ALERT", use_container_width=True, key="close_waste_modal_btn"):
            st.session_state.show_waste_modal = False
            st.rerun()
    show_center_modal(st.session_state.waste_modal_items, st.session_state.waste_modal_items_hi)

st.markdown(f'<div class="header-bubble"><div class="cartoon-title">{T["title"]}</div><div class="header-sub">{T["subtitle"]}</div></div>', unsafe_allow_html=True)

# --- TABS INCLUDING LIVE EXPIRY ALERTS CENTER ---
tab1, tab2, tab3 = st.tabs([T['tab_sim'], T['tab_voice'], T['tab_alerts']])
voice_text = ""
is_currently_recording = False

with tab1:
    st.markdown(f"### {T['input_hdr']}")
    text_input = st.text_input("Simulation Input", placeholder=T['placeholder'], label_visibility="collapsed").lower()

with tab2:
    st.markdown(f"### {T['input_hdr']}")
    st.markdown("🎙️ **Click record to speak your kitchen update (e.g., 'I brought potato bites and frooti today'):**")
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
                st.toast(f'🗣️ Transcribed Command: "{voice_text}"', icon="🎙️")
        except Exception as e:
            st.error(T['audio_err'])

with tab3:
    st.markdown("### 🚨 Live Expiry Warning Center")
    st.info("Review urgent items in your fridge that require immediate cooking or usage today/tomorrow.")

apply_custom_ui(is_recording=is_currently_recording)

final_command = text_input if 'text_input' in locals() and text_input else voice_text

# --- SHELF DATABASE PERSISTENCE LOGIC ---
if final_command:
    with shelve.open(db_filename, writeback=True) as db:
        if 'inventory' not in db: db['inventory'] = {}
        inventory = db['inventory']
        today = datetime.now().date()

        if any(w in final_command for w in ["used everything", "cooked everything", "ate everything", "clear fridge"]):
            db['inventory'] = {}
            st.toast("Cleared storage matrix: Marked all items as consumed!")
            st.balloons()
            st.rerun()
        elif any(w in final_command for w in ["threw", "discarded", "thrown away", "dumped"]):
            wasted_en, wasted_hi = [], []
            for trigger_word, (days, name_en, name_hi) in EXPIRY_DATABASE.items():
                if trigger_word in final_command and (name_en in inventory or name_hi in inventory):
                    if name_en in inventory: del inventory[name_en]
                    if name_hi in inventory: del inventory[name_hi]
                    wasted_en.append(name_en)
                    wasted_hi.append(name_hi)
            if wasted_en:
                st.session_state.waste_modal_items = ', '.join(list(set(wasted_en)))
                st.session_state.waste_modal_items_hi = ', '.join(list(set(wasted_hi)))
                st.session_state.waste_modal_time = time.time()
                st.session_state.show_waste_modal = True
                st.toast("Food waste parameter recorded.")
                st.rerun()
        elif any(w in final_command for w in ["used", "ate", "cooked", "forgot", "remove", "delete"]):
            removed_items = []
            for trigger_word, (days, name_en, name_hi) in EXPIRY_DATABASE.items():
                target_name = name_en if current_lang == "en" else name_hi
                if trigger_word in final_command and (name_en in inventory or name_hi in inventory):
                    if name_en in inventory: del inventory[name_en]
                    if name_hi in inventory: del inventory[name_hi]
                    removed_items.append(target_name)
            if removed_items:
                st.toast(f"Removed: {', '.join(removed_items)}")
                st.balloons()
        elif any(w in final_command for w in ["brought", "bought", "added", "add", "market", "came"]):
            added_items = []
            for trigger_word, (days, name_en, name_hi) in EXPIRY_DATABASE.items():
                if trigger_word in final_command:
                    target_name = name_en if current_lang == "en" else name_hi
                    if target_name not in added_items:
                        expiry_date = today + timedelta(days=days)
                        inventory[target_name] = {"date_brought": today.strftime("%Y-%m-%d"), "expiry_date": expiry_date.strftime("%Y-%m-%d"), "core_id": name_en}
                        added_items.append(target_name)
            if added_items: st.toast(f"Logged: {', '.join(added_items)}")

st.markdown("<br>", unsafe_allow_html=True)
st.markdown(f"### {T['ledger_hdr']}")

with shelve.open(db_filename, writeback=True) as db:
    inventory_data = db.get('inventory', {})
    synchronized_data = {}
    for item, details in list(inventory_data.items()):
        core_ref = details.get("core_id", "")
        for trigger_word, (days, name_en, name_hi) in EXPIRY_DATABASE.items():
            if core_ref == name_en:
                synchronized_data[name_en if current_lang == "en" else name_hi] = details
                break
    db['inventory'] = synchronized_data

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
    
    # Render main inventory table
    st.dataframe(pd.DataFrame(display_rows), use_container_width=True, hide_index=True)
    
    # Render live Expiry Alerts inside Tab 3
    with tab3:
        if urgent_alerts_list:
            st.warning("⚠️ **Immediate Action Required: The following items are expiring today or tomorrow!**")
            st.dataframe(pd.DataFrame(urgent_alerts_list), use_container_width=True, hide_index=True)
        else:
            st.success("✅ **All clear!** No items are expiring immediately.")

    urgent_items = [r[T['col_item']] for r in display_rows if any(k in r[T['col_status']] for k in ["EXPIRED", "CRITICAL", "WARNING", "खराब", "आपातकालीन", "चेतावनी"])]
    if urgent_items:
        st.markdown(f'<div class="bubble-alert-note">{T["note_msg"].format(items=", ".join(urgent_items))}</div>', unsafe_allow_html=True)
else:
    st.markdown(f'<div class="bubble-empty">{T["empty_msg"]}</div>', unsafe_allow_html=True)
