#!/usr/bin/env python3
"""
FridgeInventory - Production-Ready Single-File FastAPI & Uvicorn Application
===========================================================================
Designed for household food inventory management, zero-waste cooking, and 
automated expiry date tracking. Optimized for smooth deployment on Render.

Features:
- Natural Language Command Engine: Recognizes both adding ("brought 2kg palak", "paneer")
  and consuming/removing ("i ate lays", "used milk", "khaya paneer", "clear all").
- Multi-Tenant Family Fridge System: Private fridges with PIN and 30-day session cookies.
- Bilingual UI (English & Hindi) with instant toggle and localStorage persistence.
- Modern, clean aesthetic with minimal emojis and crisp SVG iconography.
- AI Zero-Waste Rescue Recipe Generator with interactive close/dismiss controls.
- Dark & Light Mode toggle with seamless theme switching.
- Google GenAI (google-genai SDK) structured JSON output with fallback model hierarchy.
- Firebase Firestore initialization with automatic credential discovery.
"""

from __future__ import annotations

import os
import sys
import json
import logging
import re
import uuid
import secrets
import hashlib
import hmac
import glob
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

# Set up Indian Standard Time (IST) timezone
try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    IST = timezone(timedelta(hours=5, minutes=30))

# Configure production logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("fridge_inventory")

def _load_env_file():
    """Load key-value pairs from .env if present without external dependencies."""
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.isfile(env_file):
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("\"'")
                    if k and k not in os.environ:
                        os.environ[k] = v
            logger.info("Loaded environment variables from local .env file.")
        except Exception as e:
            logger.warning(f"Error reading .env file: {e}")

_load_env_file()

# ============================================================================
# 1. COMMAND PARSING CONSTANTS & SECURITY CONFIGURATION
# ============================================================================

FRIDGE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,29}$")
PIN_ITERATIONS = 200_000
SESSION_COOKIE_NAME = "fi_session_v2"
SESSION_DAYS = 30

WORD_RE = re.compile(r"[a-z\u0900-\u097f]+")

ADD_WORDS = {
    "brought", "bring", "bought", "buy", "got", "get", "added", "add",
    "purchased", "खरीदा", "खरीदी", "खरीदे", "लाया", "लाई", "लाए", "लिया",
}

TAKE_WORDS = {
    "used", "use", "ate", "eat", "eaten", "cooked", "finished", "finish",
    "consumed", "consume", "remove", "removed", "delete", "deleted",
    "इस्तेमाल", "खाया", "खाई", "खाए", "खत्म", "पिया", "पी", "हटाया", "हटाओ",
}

WASTE_WORDS = {
    "threw", "throw", "thrown", "discarded", "discard", "dumped", "dump",
    "trashed", "wasted", "waste", "spoiled", "फेंका", "फेंकी", "फेंके", "फेंक", "सड़",
}

ALL_WORDS = {"all", "everything", "whole", "सब", "सारा", "सारी", "पूरा", "पूरी"}

CLEAR_PHRASES = (
    "clear fridge", "clear storage", "clear all", "empty fridge",
    "used everything", "ate everything", "cooked everything",
    "finished everything", "सब खत्म", "खाली करो", "फ्रिज खाली",
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

def hash_pin(pin: str, salt: str) -> str:
    """PBKDF2 HMAC SHA-256 PIN hashing with per-fridge salt."""
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), PIN_ITERATIONS).hex()

def _session_token_hash(token: str) -> str:
    """SHA-256 hash for secure token lookup in database."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def detect_action(tokens: List[str]) -> str:
    """Detect whether natural language command indicates add, consume, or waste."""
    tokset = set(tokens)
    if tokset & WASTE_WORDS:
        return "waste"
    if tokset & TAKE_WORDS:
        return "take"
    if tokset & ADD_WORDS:
        return "add"
    return "add"


# ============================================================================
# 2. THIRD-PARTY & SDK IMPORTS (WITH SAFE FALLBACKS)
# ============================================================================

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
import uvicorn

# Firebase Admin SDK
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    HAVE_FIREBASE = True
except ImportError:
    HAVE_FIREBASE = False
    firebase_admin = None
    credentials = None
    firestore = None
    logger.warning("firebase-admin is not installed. To use Firestore, install 'firebase-admin'.")

# Google GenAI SDK (google-genai)
try:
    from google import genai
    from google.genai import types
    HAVE_GENAI = True
except ImportError:
    HAVE_GENAI = False
    genai = None
    types = None
    logger.warning("google-genai is not installed. To use Gemini AI, install 'google-genai'.")

# Optional Local Database (database.py)
try:
    from database import CHEESE_DATE_REQUIRED_ITEMS, EXPIRY_DATABASE
    logger.info("Loaded local food shelf-life lookup database (database.py).")
except Exception:
    EXPIRY_DATABASE = {}
    CHEESE_DATE_REQUIRED_ITEMS = set()
    logger.info("Running without optional local database.py dictionary.")


# ============================================================================
# 3. FIREBASE FIRESTORE INITIALIZATION
# ============================================================================

_firestore_client: Optional[Any] = None
_firestore_init_attempted: bool = False
_firestore_error_msg: str = ""

def init_firestore() -> Tuple[Optional[Any], str]:
    """Initialize Firebase Firestore client using environment variables."""
    global _firestore_client, _firestore_init_attempted, _firestore_error_msg
    if _firestore_init_attempted:
        return _firestore_client, _firestore_error_msg

    _firestore_init_attempted = True

    if not HAVE_FIREBASE:
        _firestore_error_msg = "firebase-admin library not installed."
        logger.warning(_firestore_error_msg)
        return None, _firestore_error_msg

    try:
        if firebase_admin._apps:
            _firestore_client = firestore.client()
            logger.info("Connected to existing Firebase Admin application.")
            return _firestore_client, ""

        raw_env_creds = (
            os.getenv("FIREBASE_CREDENTIALS_JSON")
            or os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
            or os.getenv("FIREBASE_CONFIG")
        )
        google_app_creds = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

        cert_dict = None

        if raw_env_creds:
            raw_str = raw_env_creds.strip()
            if raw_str.startswith("{"):
                try:
                    cert_dict = json.loads(raw_str)
                except Exception as json_err:
                    logger.error(f"Failed to parse FIREBASE_CREDENTIALS_JSON string: {json_err}")
            elif os.path.isfile(raw_str):
                with open(raw_str, "r", encoding="utf-8") as f:
                    cert_dict = json.load(f)
            else:
                import base64
                try:
                    decoded = base64.b64decode(raw_str).decode("utf-8")
                    cert_dict = json.loads(decoded)
                except Exception:
                    pass

        if cert_dict and isinstance(cert_dict, dict):
            if "private_key" in cert_dict and isinstance(cert_dict["private_key"], str):
                cert_dict["private_key"] = cert_dict["private_key"].replace("\\n", "\n")
            cred = credentials.Certificate(cert_dict)
            firebase_admin.initialize_app(cred)
            _firestore_client = firestore.client()
            logger.info("Firebase Firestore successfully initialized via environment credentials.")
            return _firestore_client, ""

        if google_app_creds and os.path.isfile(google_app_creds):
            cred = credentials.Certificate(google_app_creds)
            firebase_admin.initialize_app(cred)
            _firestore_client = firestore.client()
            logger.info("Firebase Firestore initialized via GOOGLE_APPLICATION_CREDENTIALS path.")
            return _firestore_client, ""

        # Check for local Firebase certificate files without altering original filenames
        local_candidates = [
            "fidgeinventory-firebase-adminsdk-fbsvc-1263762e2a.json",
            "firebase_credentials.json",
        ]
        local_candidates.extend(glob.glob("*firebase*.json"))
        local_candidates.extend(glob.glob("*adminsdk*.json"))

        for cand in local_candidates:
            if cand and os.path.isfile(cand):
                try:
                    cred = credentials.Certificate(cand)
                    firebase_admin.initialize_app(cred)
                    _firestore_client = firestore.client()
                    logger.info(f"Firebase Firestore successfully initialized via local certificate: {cand}")
                    return _firestore_client, ""
                except Exception as cand_exc:
                    logger.warning(f"Could not load Firebase credentials from {cand}: {cand_exc}")

        _firestore_error_msg = "FIREBASE_CREDENTIALS_JSON not provided or invalid. Operating in local in-memory storage mode."
        logger.warning(_firestore_error_msg)
        return None, _firestore_error_msg

    except Exception as exc:
        _firestore_error_msg = f"Firebase initialization failed: {exc}"
        logger.exception(_firestore_error_msg)
        return None, _firestore_error_msg


# ============================================================================
# 4. GOOGLE GENAI CLIENT CONFIGURATION
# ============================================================================

_gemini_client: Optional[Any] = None

GEMINI_MODELS = [
    os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-2.5-pro",
    "gemini-3.5-flash-lite",
]

def get_gemini_client() -> Optional[Any]:
    """Retrieve or initialize singleton Google GenAI client."""
    global _gemini_client
    if _gemini_client is not None:
        return _gemini_client

    if not HAVE_GENAI:
        return None

    api_key = (
        os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
        or os.getenv("GENAI_API_KEY")
    )
    if not api_key:
        return None

    try:
        _gemini_client = genai.Client(api_key=api_key.strip())
        logger.info("Google GenAI client successfully initialized.")
        return _gemini_client
    except Exception as exc:
        logger.exception(f"Failed to initialize Google GenAI client: {exc}")
        return None


# ============================================================================
# 5. PYDANTIC SCHEMAS & DATA MODELS
# ============================================================================

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


class FoodClassificationVerdict(BaseModel):
    is_food: bool = Field(description="True if this is an edible kitchen food, drink, grocery, produce, or cooked dish.")
    corrected_item: str = Field(default="", description="The item the user meant, spelled correctly in English.")
    name_en: str = Field(default="", description="Clean, standardized English food name. Empty if not food.")
    name_hi: str = Field(default="", description="Clean Hindi name in Devanagari script. Empty if not food.")
    category: FoodCategory = Field(default=FoodCategory.NOT_FOOD, description="Culinary category.")
    shelf_life_days: int = Field(default=0, description="Estimated shelf life in days in an Indian kitchen/fridge. 0 if expiry date required or not food.")
    requires_expiry_date: bool = Field(default=False, description="True for branded, factory-packaged, sealed, or commercial dairy products where printed label date is required.")
    storage_tip: str = Field(default="", description="One practical tip for storing this item properly in an Indian kitchen.")


class ClassifyRequest(BaseModel):
    item: Optional[str] = Field(default=None, description="Food phrase or grocery item to classify")
    phrase: Optional[str] = Field(default=None, description="Alternative field for food phrase")

    def get_query(self) -> str:
        return (self.item or self.phrase or "").strip()


class ClassifyResponse(BaseModel):
    is_food: bool
    name_en: str
    name_hi: str
    category: str
    shelf_life_days: Optional[int]
    requires_expiry_date: bool
    storage_tip: str = ""
    message: Optional[str] = None
    analyzed_by: str = "Gemini AI"


class CommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=300, description="Natural language user command")


class RescueRequest(BaseModel):
    items: List[str] = Field(default_factory=list, description="List of expiring or leftover food ingredients")


class RescueRecipe(BaseModel):
    recipe_name: str = Field(description="Appetizing, authentic Indian recipe title in English")
    recipe_name_hi: str = Field(description="Recipe title in Hindi (Devanagari script)")
    prep_time_minutes: int = Field(default=20, description="Total preparation and cooking time in minutes")
    ingredients_used: List[str] = Field(default_factory=list, description="Expiring ingredients incorporated into the dish")
    pantry_items_needed: List[str] = Field(default_factory=list, description="Common Indian pantry spices/staples needed")
    steps: List[str] = Field(default_factory=list, description="Clear, numbered step-by-step cooking instructions")
    chef_tip: str = Field(default="", description="Pro Indian home chef tip for zero-waste or flavor enhancement ('दादी माँ का नुस्खा')")


class InventoryItemCreate(BaseModel):
    name_en: str
    name_hi: str = ""
    category: str = "vegetable"
    shelf_life_days: Optional[int] = 5
    requires_expiry_date: bool = False
    expiry_date: Optional[str] = None  # Format: YYYY-MM-DD
    storage_tip: str = ""


class InventoryItem(BaseModel):
    id: str
    name_en: str
    name_hi: str
    category: str
    added_date: str
    expiry_date: str
    days_left: int
    is_expired: bool
    is_expiring_soon: bool
    requires_expiry_date: bool
    storage_tip: str


class AuthLoginRequest(BaseModel):
    fridge_id: str
    pin: str


class AuthCreateRequest(BaseModel):
    fridge_id: str
    pin: str


# ============================================================================
# 6. STORAGE & MULTI-TENANT FRIDGE SESSIONS
# ============================================================================

_IN_MEMORY_FRIDGES: Dict[str, dict] = {}
_IN_MEMORY_SESSIONS: Dict[str, dict] = {}
_IN_MEMORY_INVENTORIES: Dict[str, Dict[str, dict]] = {}

def _seed_demo_fridge():
    """Seed a default demo fridge ('myfridge' with PIN '1234') in local memory."""
    demo_fid = "myfridge"
    salt = secrets.token_hex(16)
    _IN_MEMORY_FRIDGES[demo_fid] = {
        "pin_salt": salt,
        "pin_hash": hash_pin("1234", salt),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    
    now = datetime.now(IST).date()
    _IN_MEMORY_INVENTORIES[demo_fid] = {
        "demo-1": {
            "id": "demo-1",
            "name_en": "Fresh Paneer",
            "name_hi": "ताज़ा पनीर",
            "category": "dairy",
            "added_date": (now - timedelta(days=2)).isoformat(),
            "expiry_date": (now + timedelta(days=1)).isoformat(),
            "requires_expiry_date": False,
            "storage_tip": "Keep immersed in cold water inside an airtight container; change water daily.",
        },
        "demo-2": {
            "id": "demo-2",
            "name_en": "Spinach (Palak)",
            "name_hi": "पालक",
            "category": "vegetable",
            "added_date": (now - timedelta(days=1)).isoformat(),
            "expiry_date": (now + timedelta(days=2)).isoformat(),
            "requires_expiry_date": False,
            "storage_tip": "Wrap in a dry paper towel or cotton bag. Do not wash until ready to cook.",
        },
        "demo-3": {
            "id": "demo-3",
            "name_en": "Tomatoes",
            "name_hi": "टमाटर",
            "category": "vegetable",
            "added_date": (now - timedelta(days=1)).isoformat(),
            "expiry_date": (now + timedelta(days=5)).isoformat(),
            "requires_expiry_date": False,
            "storage_tip": "Store at cool room temperature stem-down, or refrigerate if fully ripe.",
        },
        "demo-4": {
            "id": "demo-4",
            "name_en": "Lays Chips",
            "name_hi": "लेज चिप्स",
            "category": "snack_bakery",
            "added_date": (now - timedelta(days=3)).isoformat(),
            "expiry_date": (now + timedelta(days=45)).isoformat(),
            "requires_expiry_date": True,
            "storage_tip": "Seal packet tightly with a clip to prevent moisture absorption.",
        },
    }

_seed_demo_fridge()


def get_current_fridge_id(request: Request) -> Optional[str]:
    """Retrieve fridge_id from session cookie or Authorization header."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token or len(token) < 20:
        return None

    token_hash = _session_token_hash(token)
    db, _ = init_firestore()
    now_utc = datetime.now(timezone.utc)

    if db is not None:
        try:
            snap = db.collection("fridge_sessions").document(token_hash).get()
            if snap.exists:
                data = snap.to_dict() or {}
                if data.get("revoked"):
                    return None
                expires_at = data.get("expires_at")
                if expires_at:
                    if hasattr(expires_at, "tzinfo") and expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=timezone.utc)
                    if expires_at <= now_utc:
                        return None
                return data.get("fridge_id")
        except Exception as exc:
            logger.error(f"Error checking Firestore session: {exc}")

    if token_hash in _IN_MEMORY_SESSIONS:
        sess = _IN_MEMORY_SESSIONS[token_hash]
        if sess.get("revoked"):
            return None
        if sess.get("expires_at") and sess["expires_at"] <= now_utc:
            return None
        return sess.get("fridge_id")

    return None


def get_fridge_inventory(fridge_id: str) -> List[InventoryItem]:
    """Retrieve all inventory items belonging to a specific fridge."""
    db, _ = init_firestore()
    today = datetime.now(IST).date()
    items = []

    if db is not None:
        try:
            docs = db.collection("fridges").document(fridge_id).collection("inventory").stream()
            for doc in docs:
                d = doc.to_dict()
                d["id"] = doc.id
                items.append(d)
        except Exception as exc:
            logger.error(f"Failed to fetch inventory from Firestore for {fridge_id}: {exc}")
            items = list(_IN_MEMORY_INVENTORIES.get(fridge_id, {}).values())
    else:
        items = list(_IN_MEMORY_INVENTORIES.get(fridge_id, {}).values())

    results: List[InventoryItem] = []
    for item in items:
        try:
            exp_date = datetime.strptime(item["expiry_date"], "%Y-%m-%d").date()
            days_left = (exp_date - today).days
        except Exception:
            days_left = 0
            exp_date = today

        results.append(
            InventoryItem(
                id=str(item.get("id", uuid.uuid4().hex[:8])),
                name_en=item.get("name_en", "Food Item"),
                name_hi=item.get("name_hi", ""),
                category=item.get("category", "vegetable"),
                added_date=item.get("added_date", today.isoformat()),
                expiry_date=item.get("expiry_date", exp_date.isoformat()),
                days_left=days_left,
                is_expired=days_left < 0,
                is_expiring_soon=(0 <= days_left <= 2),
                requires_expiry_date=bool(item.get("requires_expiry_date", False)),
                storage_tip=item.get("storage_tip", ""),
            )
        )

    results.sort(key=lambda x: x.days_left)
    return results


def save_fridge_inventory_item(fridge_id: str, item_data: InventoryItemCreate) -> InventoryItem:
    """Save an item to a specific fridge's inventory."""
    db, _ = init_firestore()
    today = datetime.now(IST).date()

    if item_data.expiry_date:
        try:
            exp_date = datetime.strptime(item_data.expiry_date.strip(), "%Y-%m-%d").date()
        except Exception:
            days = item_data.shelf_life_days if (item_data.shelf_life_days and item_data.shelf_life_days > 0) else 5
            exp_date = today + timedelta(days=days)
    else:
        days = item_data.shelf_life_days if (item_data.shelf_life_days and item_data.shelf_life_days > 0) else 5
        exp_date = today + timedelta(days=days)

    doc_id = uuid.uuid4().hex[:10]
    payload = {
        "id": doc_id,
        "name_en": item_data.name_en.strip(),
        "name_hi": item_data.name_hi.strip(),
        "category": item_data.category.strip(),
        "added_date": today.isoformat(),
        "expiry_date": exp_date.isoformat(),
        "requires_expiry_date": item_data.requires_expiry_date,
        "storage_tip": item_data.storage_tip.strip(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    if db is not None:
        try:
            db.collection("fridges").document(fridge_id).collection("inventory").document(doc_id).set(payload)
        except Exception as exc:
            logger.error(f"Error writing item to Firestore: {exc}")
            if fridge_id not in _IN_MEMORY_INVENTORIES:
                _IN_MEMORY_INVENTORIES[fridge_id] = {}
            _IN_MEMORY_INVENTORIES[fridge_id][doc_id] = payload
    else:
        if fridge_id not in _IN_MEMORY_INVENTORIES:
            _IN_MEMORY_INVENTORIES[fridge_id] = {}
        _IN_MEMORY_INVENTORIES[fridge_id][doc_id] = payload

    days_left = (exp_date - today).days
    return InventoryItem(
        id=doc_id,
        name_en=payload["name_en"],
        name_hi=payload["name_hi"],
        category=payload["category"],
        added_date=payload["added_date"],
        expiry_date=payload["expiry_date"],
        days_left=days_left,
        is_expired=days_left < 0,
        is_expiring_soon=(0 <= days_left <= 2),
        requires_expiry_date=payload["requires_expiry_date"],
        storage_tip=payload["storage_tip"],
    )


def remove_fridge_inventory_item(fridge_id: str, item_id: str) -> bool:
    """Remove item from a specific fridge's inventory."""
    db, _ = init_firestore()
    success = False

    if db is not None:
        try:
            db.collection("fridges").document(fridge_id).collection("inventory").document(item_id).delete()
            success = True
        except Exception as exc:
            logger.error(f"Error deleting item {item_id} from Firestore: {exc}")

    if fridge_id in _IN_MEMORY_INVENTORIES and item_id in _IN_MEMORY_INVENTORIES[fridge_id]:
        del _IN_MEMORY_INVENTORIES[fridge_id][item_id]
        success = True

    return success


# ============================================================================
# 7. LOCAL DATABASE & HEURISTIC LOOKUP
# ============================================================================

PACKAGED_SNACK_WORDS = {
    "bites", "bite", "chips", "chip", "crisps", "crisp", "fries", "fry",
    "nugget", "nuggets", "patty", "patties", "sticks", "stick", "roll", "rolls",
    "burger", "burgers", "pizza", "pizzas", "puff", "puffs", "snack", "snacks",
    "biscuit", "biscuits", "cookie", "cookies", "frozen", "packaged", "packet",
    "wafers", "wafer", "namkeen", "bhujia", "popcorn", "nachos", "rings", "wedges",
}

def lookup_local_database(phrase: str) -> Optional[Tuple[int, str, str, bool]]:
    """Check EXPIRY_DATABASE for exact zero-latency local match.
    Does NOT greedily match single words from multi-word phrases to avoid corrupting packaged items.
    """
    if not EXPIRY_DATABASE:
        return None

    clean = phrase.strip().lower()
    if clean in EXPIRY_DATABASE:
        days, en, hi = EXPIRY_DATABASE[clean]
        return days, en, hi, (clean in CHEESE_DATE_REQUIRED_ITEMS)

    norm = re.sub(r"[^a-zA-Z\u0900-\u097f]+", " ", clean).strip()
    if norm in EXPIRY_DATABASE:
        days, en, hi = EXPIRY_DATABASE[norm]
        return days, en, hi, (norm in CHEESE_DATE_REQUIRED_ITEMS)

    return None


# ============================================================================
# 8. AI CLASSIFICATION & RECIPE RESCUE CORE
# ============================================================================

CLASSIFY_PROMPT_TEMPLATE = """You are an expert culinary AI for a household kitchen and fridge inventory app ("FridgeInventory").
Analyze the food input phrase: "{query}"

Classification Rules:
1. Multi-word packaged, processed, or frozen items (CRITICAL):
   - If the user types a prepared dish, packaged product, or frozen snack (e.g. 'potato bites', 'banana chips', 'garlic bread', 'onion rings', 'french fries', 'cheese sticks', 'veggie fingers', 'potato wedges', 'maggi noodles', 'lays chips'), classify the ENTIRE phrase as the item.
   - NEVER truncate, simplify, or reduce a compound food product to its raw agricultural ingredient (e.g. NEVER classify 'potato bites' as raw 'Potato', or 'banana chips' as 'Banana', or 'garlic bread' as 'Garlic').
   - For processed/packaged snacks or frozen goods: set category=snack_bakery or packaged_grocery, requires_expiry_date=true, and shelf_life_days=0.
2. Spelling & Slang:
   - Correct common speech-to-text slips or Hinglish transliterations (e.g. 'tamatar' -> 'Tomato', 'panir'/'paneer' -> 'Paneer', 'palak' -> 'Spinach', 'dahi' -> 'Curd / Dahi', 'bhindee' -> 'Okra (Bhindi)', 'amul makkhan' -> 'Amul Butter', 'lays' -> 'Lays Chips', 'chawal' -> 'Cooked Rice').
3. is_food check:
   - Return is_food=true ONLY for edible/drinkable food, produce, dairy, meat/fish/poultry, eggs, grains, lentils, flours, spices, snacks, beverages, and cooked meals.
   - Return is_food=false for objects, electronics, medicines, cleaners, cosmetics, people, vehicles, furniture, or non-food words.
4. If is_food is false:
   - category=not_food, shelf_life_days=0, requires_expiry_date=false, name_en="", name_hi="", storage_tip="".
5. Expiry Date Requirement (requires_expiry_date):
   - Set requires_expiry_date=true for factory-packaged, sealed, branded items (e.g. packaged milk cartons, cheese blocks/slices, butter packs, sliced bread, biscuits, ketchup bottles, chips, snacks, frozen items).
   - Set requires_expiry_date=false for loose fresh fruits, fresh vegetables, fresh unsealed bakery items, raw whole grains/pulses, or freshly cooked home meals.
6. Shelf Life:
   - For fresh produce or cooked home dishes (requires_expiry_date=false), provide a realistic estimated shelf life in days in an Indian refrigerator (e.g., Spinach: 3 days, Okra: 4 days, Tomatoes: 6 days, Cooked Dal: 3 days).
   - For items with requires_expiry_date=true, set shelf_life_days=0 since the user must enter the printed package date.
7. Provide name_en (English), name_hi (Hindi in Devanagari script), category, and a practical storage_tip for Indian climates.
"""

def classify_food(phrase: str) -> ClassifyResponse:
    """Classify food phrase using Google GenAI with fallback to local database heuristic."""
    clean_phrase = phrase.strip()
    if not clean_phrase:
        raise HTTPException(status_code=400, detail="Food item phrase cannot be empty.")

    client = get_gemini_client()

    # 1. Prioritize Google GenAI (Gemini)
    if client is not None and types is not None:
        for model_name in GEMINI_MODELS:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=CLASSIFY_PROMPT_TEMPLATE.format(query=clean_phrase),
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=FoodClassificationVerdict,
                        temperature=0.1,
                    ),
                )
                raw_text = (response.text or "").strip()
                verdict = FoodClassificationVerdict.model_validate_json(raw_text)

                if not verdict.is_food or verdict.category == FoodCategory.NOT_FOOD or not verdict.name_en:
                    return ClassifyResponse(
                        is_food=False,
                        name_en="",
                        name_hi="",
                        category="not_food",
                        shelf_life_days=None,
                        requires_expiry_date=False,
                        storage_tip="",
                        message=f"'{clean_phrase}' does not appear to be a food or grocery item.",
                        analyzed_by="Gemini AI",
                    )

                return ClassifyResponse(
                    is_food=True,
                    name_en=verdict.name_en,
                    name_hi=verdict.name_hi or verdict.name_en,
                    category=verdict.category.value,
                    shelf_life_days=None if verdict.requires_expiry_date else verdict.shelf_life_days,
                    requires_expiry_date=verdict.requires_expiry_date,
                    storage_tip=verdict.storage_tip or "Keep refrigerated or in a cool dry space.",
                    analyzed_by="Gemini AI",
                )
            except Exception as exc:
                logger.warning(f"Gemini model {model_name} failed on '{clean_phrase}': {exc}")
                continue

    # 2. Check for multi-word packaged snacks heuristic (e.g. "potato bites", "banana chips")
    tokens = set(clean_phrase.lower().split())
    if tokens & PACKAGED_SNACK_WORDS:
        return ClassifyResponse(
            is_food=True,
            name_en=clean_phrase.title(),
            name_hi=clean_phrase,
            category="snack_bakery",
            shelf_life_days=None,
            requires_expiry_date=True,
            storage_tip="Store in freezer or cool dry place. Check printed package date.",
            message="Packaged / frozen snack detected. Printed expiry date required.",
            analyzed_by="Smart Classifier",
        )

    # 3. Exact match from local database
    local_match = lookup_local_database(clean_phrase)
    if local_match:
        days, en, hi, needs_date = local_match
        cat = "vegetable"
        lower_en = en.lower()
        if any(w in lower_en for w in ["milk", "curd", "paneer", "cheese", "butter", "ghee", "cream"]):
            cat = "dairy"
        elif any(w in lower_en for w in ["apple", "banana", "mango", "orange", "lemon", "grape"]):
            cat = "fruit"
        elif any(w in lower_en for w in ["dal", "lentil", "rice", "atta", "wheat", "flour", "besan"]):
            cat = "grain_pulse_flour"

        return ClassifyResponse(
            is_food=True,
            name_en=en,
            name_hi=hi,
            category=cat,
            shelf_life_days=None if needs_date else days,
            requires_expiry_date=needs_date,
            storage_tip="Store in a cool dry place or refrigerate as appropriate.",
            message="Classified via FridgeInventory database.",
            analyzed_by="Food Knowledge Base",
        )

    return ClassifyResponse(
        is_food=True,
        name_en=clean_phrase.title(),
        name_hi=clean_phrase,
        category="vegetable",
        shelf_life_days=4,
        requires_expiry_date=False,
        storage_tip="Refrigerate in a clean container to maximize freshness.",
        message="Classified using standard kitchen estimate.",
        analyzed_by="Default Heuristic",
    )


RESCUE_PROMPT_TEMPLATE = """You are an expert home cook and zero-waste kitchen master for 'FridgeInventory'.
A household has these specific expiring items or leftovers that MUST be used today to prevent food waste:
{ingredients}

Task:
Create one authentic, delicious, and easy everyday Indian recipe (curry, dry sabzi, paratha, dal tadka, pulao, or snacks) that incorporates these expiring items.
Pair with basic Indian pantry spices available in every home (turmeric, red chilli powder, cumin, mustard seeds, garam masala, salt, oil).

Output Format Requirements:
- recipe_name in English
- recipe_name_hi in Hindi (Devanagari)
- prep_time_minutes (realistic integer, 15 to 35 mins)
- ingredients_used (list)
- pantry_items_needed (list of common kitchen spices/staples)
- steps (list of clear step-by-step cooking steps)
- chef_tip (clever Indian cooking tip or zero-waste flavor boost)
"""

def generate_rescue_recipe(items: List[str]) -> RescueRecipe:
    """Generate an Indian zero-waste rescue recipe using Gemini GenAI with graceful culinary fallback."""
    clean_items = [str(i).strip() for i in items if str(i).strip()]
    if not clean_items:
        raise HTTPException(status_code=400, detail="Provide at least one expiring food item.")

    client = get_gemini_client()
    if client is not None and types is not None:
        for model_name in GEMINI_MODELS:
            try:
                formatted_list = ", ".join(clean_items)
                response = client.models.generate_content(
                    model=model_name,
                    contents=RESCUE_PROMPT_TEMPLATE.format(ingredients=formatted_list),
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=RescueRecipe,
                        temperature=0.3,
                    ),
                )
                raw_text = (response.text or "").strip()
                recipe = RescueRecipe.model_validate_json(raw_text)
                return recipe
            except Exception as exc:
                logger.warning(f"Rescue recipe generation failed on {model_name}: {exc}")
                continue

    primary = clean_items[0].title()
    all_items_str = ", ".join(clean_items)
    return RescueRecipe(
        recipe_name=f"Homestyle Rasoi Masala Bhurji / Stir-Fry ({primary})",
        recipe_name_hi=f"झटपट मसाला {primary} भुर्जी",
        prep_time_minutes=20,
        ingredients_used=clean_items,
        pantry_items_needed=[
            "1-2 tbsp Cooking Mustard or Sunflower Oil",
            "1/2 tsp Cumin seeds (Jeera)",
            "1/4 tsp Turmeric powder (Haldi)",
            "1/2 tsp Red chilli powder (Lal Mirch)",
            "1/2 tsp Coriander powder (Dhaniya)",
            "Salt to taste",
            "1 green chilli & fresh coriander leaves (finely chopped)",
        ],
        steps=[
            f"Thoroughly inspect and wash the expiring ingredients ({all_items_str}). Cut into uniform small pieces.",
            "Heat 1.5 tablespoons of oil in a kadai or pan over medium flame. Add cumin seeds and let them crackle.",
            f"Add any firm vegetables or {primary} first and sauté for 4-5 minutes until tender.",
            "Lower the heat and add turmeric powder, red chilli powder, coriander powder, and salt. Toss well so spices coat the ingredients evenly.",
            "Add softer ingredients (such as paneer, boiled potatoes, greens, or cooked rice) and cook for another 2-3 minutes.",
            "Turn off the heat, garnish with fresh chopped coriander and a squeeze of fresh lemon juice. Serve hot with rotis, parathas, or toast.",
        ],
        chef_tip="To revive vegetables that have lost their crispness, soak them in chilled water for 10 minutes before chopping. A pinch of amchur (dry mango powder) or chaat masala balances flavors beautifully!",
    )


# ============================================================================
# 9. FASTAPI APPLICATION SETUP
# ============================================================================

app = FastAPI(
    title="FridgeInventory API",
    description="Intelligent Household Fridge & Pantry Inventory Management System with Gemini AI",
    version="2.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================================
# 10. UNIFIED NATURAL LANGUAGE COMMAND API
# ============================================================================

@app.post("/api/command", summary="Process natural language voice/text commands")
def api_process_command(payload: CommandRequest, request: Request, fridge: Optional[str] = None):
    """
    Intelligently processes voice or typed commands:
    - Adding food: 'bhindee 500g', 'brought 2kg tomatoes', 'amul paneer'
    - Consuming/eating: 'i ate lays', 'used milk', 'khaya paneer', 'discarded bread'
    - Clearing fridge: 'clear all', 'empty fridge'
    """
    raw_cmd = payload.command.strip()
    if not raw_cmd:
        raise HTTPException(status_code=400, detail="Command cannot be empty.")

    fid = get_current_fridge_id(request) or (fridge.strip().lower() if fridge else "myfridge")
    inv = get_fridge_inventory(fid)
    cmd_lower = raw_cmd.lower()

    # 1. Clear check
    if any(p in cmd_lower for p in CLEAR_PHRASES):
        for item in inv:
            remove_fridge_inventory_item(fid, item.id)
        return {
            "action": "clear",
            "message_en": "All items cleared from your fridge.",
            "message_hi": "फ्रिज पूरी तरह से साफ़ कर दिया गया है।",
        }

    # 2. Tokenize & detect intent
    tokens = WORD_RE.findall(cmd_lower)
    if not tokens:
        raise HTTPException(status_code=400, detail="Could not understand command.")

    action = detect_action(tokens)

    # 3. Handle Take / Consume / Waste command (e.g. "i ate lays", "used milk", "khaya paneer")
    if action in ("take", "waste"):
        # Strip action & stop words to find the target food phrase
        target_words = [w for w in tokens if w not in STOP_WORDS]
        target_phrase = " ".join(target_words).strip()
        if not target_phrase:
            # Fallback to non-action tokens if everything got stripped
            target_words = [w for w in tokens if w not in TAKE_WORDS and w not in WASTE_WORDS]
            target_phrase = " ".join(target_words).strip()

        if not target_phrase:
            return {
                "action": "unknown",
                "message_en": "Please specify which item you consumed or removed.",
                "message_hi": "कृपया बताएं कि आपने कौन सा सामान खाया या हटाया।",
            }

        # Match against current inventory
        matched_item = None
        for it in inv:
            name_en_lower = it.name_en.lower()
            name_hi_lower = it.name_hi.lower()
            # Direct or substring match
            if (target_phrase in name_en_lower or name_en_lower in target_phrase or
                (name_hi_lower and (target_phrase in name_hi_lower or name_hi_lower in target_phrase))):
                matched_item = it
                break

        # Also check word-by-word if target_phrase has multiple words (e.g. "lays chips")
        if not matched_item:
            for it in inv:
                for tw in target_words:
                    if len(tw) >= 3 and (tw in it.name_en.lower() or tw in it.name_hi.lower()):
                        matched_item = it
                        break
                if matched_item:
                    break

        if matched_item:
            remove_fridge_inventory_item(fid, matched_item.id)
            verb_en = "Consumed" if action == "take" else "Discarded"
            verb_hi = "इस्तेमाल हुआ" if action == "take" else "फेंका गया"
            return {
                "action": "removed",
                "item_id": matched_item.id,
                "name_en": matched_item.name_en,
                "name_hi": matched_item.name_hi,
                "message_en": f"{verb_en}: {matched_item.name_en}.",
                "message_hi": f"{verb_hi}: {matched_item.name_hi or matched_item.name_en}।",
            }
        else:
            return {
                "action": "not_found",
                "target": target_phrase,
                "message_en": f"'{target_phrase}' was not found in your fridge inventory.",
                "message_hi": f"'{target_phrase}' आपके फ्रिज में नहीं मिला।",
            }

    # 4. Handle Add / Classify command
    # Strip addition command keywords (e.g., 'brought 2kg palak' -> '2kg palak')
    food_tokens = [w for w in tokens if w not in ADD_WORDS]
    food_phrase = " ".join(food_tokens).strip() or raw_cmd

    classification = classify_food(food_phrase)
    return {
        "action": "classify",
        "food_phrase": food_phrase,
        "classification": classification.model_dump(),
    }


# ============================================================================
# 11. AUTH & FRIDGE MANAGEMENT REST APIS
# ============================================================================

@app.get("/api/auth/me", summary="Check current fridge authentication status")
def api_auth_me(request: Request):
    """Inspects cookie to verify whether user is logged into a family fridge."""
    fid = get_current_fridge_id(request)
    if fid:
        return {"authenticated": True, "fridge_id": fid}
    return {"authenticated": False, "fridge_id": None}


@app.post("/api/auth/login", summary="Login to existing family fridge")
def api_auth_login(payload: AuthLoginRequest, response: Response):
    """Authenticate with Fridge ID and PIN; sets 30-day persistent cookie."""
    fid = payload.fridge_id.strip().lower()
    pin = payload.pin.strip()

    if not fid or not pin:
        raise HTTPException(status_code=400, detail="Fridge ID and PIN are required.")

    db, _ = init_firestore()
    authenticated = False

    if db is not None:
        try:
            doc = db.collection("fridges").document(fid).get()
            if not doc.exists:
                raise HTTPException(status_code=404, detail=f"Fridge '{fid}' not found. Check spelling or create a new fridge.")
            fdata = doc.to_dict() or {}
            salt = fdata.get("pin_salt")
            stored_hash = fdata.get("pin_hash", "")
            if salt:
                authenticated = hmac.compare_digest(stored_hash, hash_pin(pin, salt))
            else:
                legacy = hashlib.sha256(pin.encode()).hexdigest()
                authenticated = hmac.compare_digest(stored_hash, legacy)
                if authenticated:
                    new_salt = secrets.token_hex(16)
                    doc.reference.update({"pin_salt": new_salt, "pin_hash": hash_pin(pin, new_salt)})
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(f"Firestore login error: {exc}")
            raise HTTPException(status_code=500, detail="Database error during login.")
    else:
        if fid not in _IN_MEMORY_FRIDGES:
            raise HTTPException(status_code=404, detail=f"Fridge '{fid}' not found in local memory.")
        fdata = _IN_MEMORY_FRIDGES[fid]
        authenticated = hmac.compare_digest(fdata["pin_hash"], hash_pin(pin, fdata["pin_salt"]))

    if not authenticated:
        raise HTTPException(status_code=401, detail="Incorrect PIN for this fridge. Please try again.")

    token = secrets.token_urlsafe(48)
    token_hash = _session_token_hash(token)
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)

    if db is not None:
        try:
            db.collection("fridge_sessions").document(token_hash).set({
                "fridge_id": fid,
                "created_at": firestore.SERVER_TIMESTAMP,
                "expires_at": expires_at,
                "revoked": False,
            })
        except Exception as exc:
            logger.error(f"Failed to record session in Firestore: {exc}")
            _IN_MEMORY_SESSIONS[token_hash] = {"fridge_id": fid, "expires_at": expires_at, "revoked": False}
    else:
        _IN_MEMORY_SESSIONS[token_hash] = {"fridge_id": fid, "expires_at": expires_at, "revoked": False}

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return {"ok": True, "fridge_id": fid, "message": f"Successfully logged into fridge '{fid}'."}


@app.post("/api/auth/create", summary="Create a new family fridge")
def api_auth_create(payload: AuthCreateRequest, response: Response):
    """Creates a new fridge entry with a hashed family PIN and sets session cookie."""
    fid = payload.fridge_id.strip().lower()
    pin = payload.pin.strip()

    if not fid or not pin:
        raise HTTPException(status_code=400, detail="Fridge ID and PIN are required.")
    if not FRIDGE_RE.fullmatch(fid):
        raise HTTPException(
            status_code=422,
            detail="Fridge ID must be 3-30 lowercase characters (letters, numbers, hyphens, underscores). Example: 'sharma-rasoi'.",
        )
    if len(pin) < 4:
        raise HTTPException(status_code=422, detail="PIN must be at least 4 digits long for security.")

    db, _ = init_firestore()
    salt = secrets.token_hex(16)
    p_hash = hash_pin(pin, salt)

    if db is not None:
        try:
            doc_ref = db.collection("fridges").document(fid)
            if doc_ref.get().exists:
                raise HTTPException(status_code=409, detail=f"Fridge '{fid}' already exists. Choose a different ID or login.")
            doc_ref.set({
                "pin_salt": salt,
                "pin_hash": p_hash,
                "created_at": firestore.SERVER_TIMESTAMP,
            })
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(f"Firestore create fridge error: {exc}")
            raise HTTPException(status_code=500, detail="Database error creating fridge.")
    else:
        if fid in _IN_MEMORY_FRIDGES:
            raise HTTPException(status_code=409, detail=f"Fridge '{fid}' already exists in local memory.")
        _IN_MEMORY_FRIDGES[fid] = {"pin_salt": salt, "pin_hash": p_hash}

    token = secrets.token_urlsafe(48)
    token_hash = _session_token_hash(token)
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)

    if db is not None:
        try:
            db.collection("fridge_sessions").document(token_hash).set({
                "fridge_id": fid,
                "created_at": firestore.SERVER_TIMESTAMP,
                "expires_at": expires_at,
                "revoked": False,
            })
        except Exception as exc:
            logger.error(f"Failed to record session in Firestore: {exc}")
            _IN_MEMORY_SESSIONS[token_hash] = {"fridge_id": fid, "expires_at": expires_at, "revoked": False}
    else:
        _IN_MEMORY_SESSIONS[token_hash] = {"fridge_id": fid, "expires_at": expires_at, "revoked": False}

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return {"ok": True, "fridge_id": fid, "message": f"Fridge '{fid}' successfully created!"}


@app.post("/api/auth/logout", summary="Logout and revoke browser session")
def api_auth_logout(request: Request, response: Response):
    """Revokes session and deletes the session cookie."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        token_hash = _session_token_hash(token)
        db, _ = init_firestore()
        if db is not None:
            try:
                db.collection("fridge_sessions").document(token_hash).set({"revoked": True}, merge=True)
            except Exception:
                pass
        if token_hash in _IN_MEMORY_SESSIONS:
            _IN_MEMORY_SESSIONS[token_hash]["revoked"] = True

    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return {"ok": True, "message": "Successfully logged out."}


# ============================================================================
# 12. REST API ENDPOINTS (INVENTORY & AI)
# ============================================================================

@app.get("/health", summary="Health and Service Status Check")
def health_check():
    """Returns application status, time in IST, and configuration flags."""
    db, db_err = init_firestore()
    gemini_ready = get_gemini_client() is not None
    now_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z")
    
    return {
        "status": "healthy",
        "service": "FridgeInventory",
        "timestamp_ist": now_ist,
        "gemini_configured": gemini_ready,
        "firebase_configured": db is not None,
        "firestore_status": "connected" if db is not None else db_err,
        "local_database_cached_items": len(EXPIRY_DATABASE),
    }


@app.post("/classify", response_model=ClassifyResponse, summary="Classify Food & Shelf Life")
def api_classify(payload: ClassifyRequest):
    """Classify a food phrase directly."""
    query = payload.get_query()
    if not query:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Item phrase cannot be empty. Please provide 'item' or 'phrase'.",
        )
    return classify_food(query)


@app.post("/rescue", response_model=RescueRecipe, summary="Generate Zero-Waste Rescue Recipe")
def api_rescue(payload: RescueRequest):
    """Generate an Indian rescue recipe for expiring ingredients."""
    if not payload.items:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Please provide at least one expiring food item in the 'items' list.",
        )
    return generate_rescue_recipe(payload.items)


@app.get("/api/inventory", response_model=List[InventoryItem], summary="Get All Inventory Items")
def api_get_inventory(request: Request, fridge: Optional[str] = None):
    """Retrieve all tracked items for the active fridge."""
    fid = get_current_fridge_id(request) or (fridge.strip().lower() if fridge else "myfridge")
    return get_fridge_inventory(fid)


@app.post("/api/inventory", response_model=InventoryItem, summary="Add Item to Inventory")
def api_add_inventory(item: InventoryItemCreate, request: Request, fridge: Optional[str] = None):
    """Add a verified food item into the active fridge inventory."""
    fid = get_current_fridge_id(request) or (fridge.strip().lower() if fridge else "myfridge")
    return save_fridge_inventory_item(fid, item)


@app.delete("/api/inventory/{item_id}", summary="Delete / Consume Inventory Item")
def api_delete_inventory(item_id: str, request: Request, fridge: Optional[str] = None):
    """Remove an item from the active fridge inventory."""
    fid = get_current_fridge_id(request) or (fridge.strip().lower() if fridge else "myfridge")
    removed = remove_fridge_inventory_item(fid, item_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not found.")
    return {"ok": True, "message": f"Item {item_id} removed successfully."}


# ============================================================================
# 13. EMBEDDED FRONTEND UI (FASTAPI HTMLRESPONSE)
# ============================================================================

HTML_CONTENT = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>FridgeInventory</title>
  <!-- Tailwind CSS CDN -->
  <script src="https://cdn.tailwindcss.com"></script>
  <!-- Google Fonts: Inter, Poppins, Noto Sans Devanagari -->
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Noto+Sans+Devanagari:wght@400;600;700&display=swap" rel="stylesheet">
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          fontFamily: {
            sans: ['Inter', 'Noto Sans Devanagari', 'sans-serif'],
          },
          colors: {
            brand: { 50: '#fffbeb', 100: '#fef3c7', 500: '#f59e0b', 600: '#d97706', 700: '#b45309' },
            leaf: { 50: '#ecfdf5', 100: '#d1fae5', 500: '#10b981', 600: '#059669', 700: '#047857' }
          }
        }
      }
    };
    if (localStorage.getItem('rasoi_theme') === 'dark' || (!('rasoi_theme' in localStorage) && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
      document.documentElement.classList.add('dark');
    } else {
      document.documentElement.classList.remove('dark');
    }
  </script>
</head>
<body class="bg-stone-50 dark:bg-stone-950 text-stone-800 dark:text-stone-100 min-h-screen flex flex-col antialiased selection:bg-brand-500 selection:text-white transition-colors duration-150">

  <!-- TOP APP HEADER -->
  <header class="bg-white dark:bg-stone-900 border-b border-stone-200 dark:border-stone-800 sticky top-0 z-40 transition-colors">
    <div class="max-w-6xl mx-auto px-4 py-3 flex items-center justify-between gap-3">
      
      <!-- Brand Logo & Title -->
      <div class="flex items-center gap-3">
        <div class="w-10 h-10 rounded-xl bg-gradient-to-tr from-brand-600 to-amber-500 flex items-center justify-center text-white shadow-sm">
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"></path></svg>
        </div>
        <div>
          <div class="flex items-center gap-2">
            <h1 class="font-bold text-lg sm:text-xl tracking-tight text-stone-900 dark:text-white" data-i18n="app_title">FridgeInventory</h1>
            <span class="text-[11px] bg-stone-100 dark:bg-stone-800 text-stone-600 dark:text-stone-400 font-semibold px-2 py-0.5 rounded-full">v2.1</span>
          </div>
          <p class="text-xs text-stone-500 dark:text-stone-400 hidden sm:block" data-i18n="app_subtitle">Smart Food Inventory & Zero-Waste Kitchen</p>
        </div>
      </div>

      <!-- Controls: Language Toggle, Theme Toggle & Fridge Session -->
      <div class="flex items-center gap-2 sm:gap-2.5 text-xs">
        
        <!-- Language Toggle Button -->
        <button id="lang-toggle-btn" class="px-2.5 py-1.5 rounded-xl border border-stone-200 dark:border-stone-700 bg-stone-100 dark:bg-stone-800 hover:bg-stone-200 dark:hover:bg-stone-700 text-stone-700 dark:text-stone-300 font-medium transition flex items-center gap-1.5" title="Switch Language">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 5h12M9 3v2m1.048 9.5A18.022 18.022 0 016.412 9m6.088 9h7M11 21l5-10 5 10M12.751 5C11.783 10.77 8.07 15.61 3 18.129"></path></svg>
          <span id="lang-label">हिन्दी</span>
        </button>

        <!-- Theme Toggle Button -->
        <button id="theme-toggle-btn" class="p-2 rounded-xl border border-stone-200 dark:border-stone-700 bg-stone-100 dark:bg-stone-800 hover:bg-stone-200 dark:hover:bg-stone-700 text-stone-700 dark:text-stone-300 transition" title="Toggle Theme">
          <svg id="theme-icon-sun" class="w-4 h-4 hidden" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z"></path></svg>
          <svg id="theme-icon-moon" class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z"></path></svg>
        </button>

        <!-- Authenticated Fridge State -->
        <div id="auth-logged-in" class="hidden flex items-center gap-2">
          <div class="px-3 py-1.5 rounded-xl bg-amber-50 dark:bg-stone-800 border border-amber-200 dark:border-stone-700 text-brand-700 dark:text-brand-400 font-bold flex items-center gap-1.5 shadow-sm">
            <svg class="w-3.5 h-3.5 text-brand-600" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"></path></svg>
            <span id="header-fridge-name">MY-FRIDGE</span>
          </div>
          <button id="logout-btn" class="px-2.5 py-1.5 rounded-xl bg-stone-100 dark:bg-stone-800 hover:bg-rose-50 dark:hover:bg-rose-950/60 hover:text-rose-600 text-stone-600 dark:text-stone-300 font-medium border border-stone-200 dark:border-stone-700 transition" title="Logout" data-i18n="logout_btn">
            Logout
          </button>
        </div>

        <!-- Logged Out State -->
        <div id="auth-logged-out" class="flex items-center gap-2">
          <button id="open-auth-btn" class="px-3.5 py-1.5 rounded-xl bg-stone-900 dark:bg-white hover:bg-stone-800 dark:hover:bg-stone-100 text-white dark:text-stone-900 font-semibold shadow-sm transition flex items-center gap-1.5" data-i18n="login_create_btn">
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 16l-4-4m0 0l4-4m-4 4h14m-5 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h7a3 3 0 013 3v1"></path></svg>
            <span>Login</span>
          </button>
        </div>
      </div>
    </div>
  </header>

  <!-- MAIN CONTAINER -->
  <main class="flex-grow max-w-6xl w-full mx-auto px-4 py-6 space-y-6">

    <!-- LOGGED OUT WELCOME BUBBLE / CARD (SHOWN WHEN NOT LOGGED IN) -->
    <div id="logged-out-bubble" class="max-w-2xl mx-auto my-6 sm:my-10 p-7 sm:p-10 bg-white dark:bg-stone-900 rounded-3xl border border-stone-200/90 dark:border-stone-800 shadow-xl text-center space-y-6 transition-all">
      <div class="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-gradient-to-tr from-brand-600 to-amber-500 text-white shadow-md mx-auto">
        <svg class="w-8 h-8" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"></path></svg>
      </div>

      <div>
        <h2 class="text-2xl sm:text-3xl font-extrabold tracking-tight text-stone-900 dark:text-white" data-i18n="bubble_title">
          Welcome to FridgeInventory
        </h2>
        <p class="text-xs sm:text-sm text-stone-600 dark:text-stone-300 mt-2 max-w-lg mx-auto leading-relaxed" data-i18n="bubble_desc">
          Your smart household kitchen companion for effortless inventory tracking, expiry prevention, and AI-powered zero-waste cooking.
        </p>
      </div>

      <!-- Feature Highlight Cards -->
      <div class="grid grid-cols-1 sm:grid-cols-3 gap-3.5 text-left pt-1">
        <div class="p-4 rounded-2xl bg-stone-50 dark:bg-stone-800/60 border border-stone-200/70 dark:border-stone-700/60">
          <div class="text-xl mb-1.5">📦</div>
          <h3 class="font-bold text-xs sm:text-sm text-stone-900 dark:text-white" data-i18n="feat_track_title">Smart Food Tracking</h3>
          <p class="text-xs text-stone-500 dark:text-stone-400 mt-1" data-i18n="feat_track_desc">Keep real-time tabs on fresh produce, dairy, groceries, and cooked meals.</p>
        </div>
        <div class="p-4 rounded-2xl bg-stone-50 dark:bg-stone-800/60 border border-stone-200/70 dark:border-stone-700/60">
          <div class="text-xl mb-1.5">⏳</div>
          <h3 class="font-bold text-xs sm:text-sm text-stone-900 dark:text-white" data-i18n="feat_expiry_title">Expiry Prevention</h3>
          <p class="text-xs text-stone-500 dark:text-stone-400 mt-1" data-i18n="feat_expiry_desc">Automatic shelf-life estimates and urgent alerts before your food spoils.</p>
        </div>
        <div class="p-4 rounded-2xl bg-stone-50 dark:bg-stone-800/60 border border-stone-200/70 dark:border-stone-700/60">
          <div class="text-xl mb-1.5">✨</div>
          <h3 class="font-bold text-xs sm:text-sm text-stone-900 dark:text-white" data-i18n="feat_ai_title">AI Rescue Chef</h3>
          <p class="text-xs text-stone-500 dark:text-stone-400 mt-1" data-i18n="feat_ai_desc">Turn expiring ingredients into homestyle recipes with one click.</p>
        </div>
      </div>

      <!-- Call to Action Buttons -->
      <div class="pt-2 flex flex-col sm:flex-row items-center justify-center gap-3">
        <button id="bubble-login-btn" class="w-full sm:w-auto px-7 py-3 rounded-2xl bg-brand-600 hover:bg-brand-700 text-white font-bold text-sm shadow-md transition-all transform active:scale-95 flex items-center justify-center gap-2">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 16l-4-4m0 0l4-4m-4 4h14m-5 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h7a3 3 0 013 3v1"></path></svg>
          <span data-i18n="bubble_login_btn">Login to Fridge</span>
        </button>
        <button id="bubble-create-btn" class="w-full sm:w-auto px-7 py-3 rounded-2xl border border-stone-300 dark:border-stone-700 hover:bg-stone-100 dark:hover:bg-stone-800 text-stone-800 dark:text-stone-200 font-bold text-sm transition-all transform active:scale-95 flex items-center justify-center gap-2">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"></path></svg>
          <span data-i18n="bubble_create_btn">Create a New Fridge</span>
        </button>
      </div>

      <!-- Demo Credentials Helper -->
      <p class="text-xs text-stone-400 dark:text-stone-500 pt-1" data-i18n="bubble_demo_hint">
        Demo Access: Fridge ID: <strong class="text-brand-600 dark:text-brand-400">myfridge</strong> | PIN: <strong class="text-brand-600 dark:text-brand-400">1234</strong>
      </p>
    </div>

    <!-- MAIN APP DASHBOARD (SHOWN ONLY WHEN LOGGED IN) -->
    <div id="app-dashboard" class="space-y-6 hidden">

      <!-- METRIC STATS STRIP -->
      <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 sm:gap-4">
      <div class="bg-white dark:bg-stone-900 p-4 rounded-2xl border border-stone-200/80 dark:border-stone-800 shadow-sm flex items-center gap-3.5 transition-colors">
        <div class="w-10 h-10 rounded-xl bg-stone-100 dark:bg-stone-800 text-stone-700 dark:text-stone-300 flex items-center justify-center">
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"></path></svg>
        </div>
        <div>
          <div class="text-2xl font-bold text-stone-900 dark:text-white" id="stat-total">0</div>
          <div class="text-xs font-medium text-stone-500 dark:text-stone-400" data-i18n="stat_total">Total Items</div>
        </div>
      </div>
      <div class="bg-white dark:bg-stone-900 p-4 rounded-2xl border border-stone-200/80 dark:border-stone-800 shadow-sm flex items-center gap-3.5 transition-colors">
        <div class="w-10 h-10 rounded-xl bg-leaf-50 dark:bg-stone-800 text-leaf-600 flex items-center justify-center">
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
        </div>
        <div>
          <div class="text-2xl font-bold text-leaf-600" id="stat-fresh">0</div>
          <div class="text-xs font-medium text-stone-500 dark:text-stone-400" data-i18n="stat_fresh">Fresh (3+ Days)</div>
        </div>
      </div>
      <div class="bg-white dark:bg-stone-900 p-4 rounded-2xl border border-stone-200/80 dark:border-stone-800 shadow-sm flex items-center gap-3.5 transition-colors">
        <div class="w-10 h-10 rounded-xl bg-amber-50 dark:bg-stone-800 text-brand-600 flex items-center justify-center">
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"></path></svg>
        </div>
        <div>
          <div class="text-2xl font-bold text-brand-600" id="stat-urgent">0</div>
          <div class="text-xs font-medium text-stone-500 dark:text-stone-400" data-i18n="stat_urgent">Expiring Soon</div>
        </div>
      </div>
      <div class="bg-white dark:bg-stone-900 p-4 rounded-2xl border border-stone-200/80 dark:border-stone-800 shadow-sm flex items-center gap-3.5 transition-colors">
        <div class="w-10 h-10 rounded-xl bg-rose-50 dark:bg-stone-800 text-rose-600 flex items-center justify-center">
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
        </div>
        <div>
          <div class="text-2xl font-bold text-rose-600" id="stat-expired">0</div>
          <div class="text-xs font-medium text-stone-500 dark:text-stone-400" data-i18n="stat_expired">Expired</div>
        </div>
      </div>
    </div>

    <!-- NATURAL LANGUAGE VOICE & TEXT COMMAND BAR -->
    <section class="bg-white dark:bg-stone-900 rounded-3xl p-5 sm:p-6 border border-stone-200/80 dark:border-stone-800 shadow-sm transition-colors">
      <div class="max-w-2xl">
        <h2 class="text-base sm:text-lg font-bold text-stone-900 dark:text-white flex items-center gap-2">
          <span data-i18n="cmd_heading">Smart Voice & Text Control</span>
        </h2>
        <p class="text-xs text-stone-500 dark:text-stone-400 mt-1 mb-4" data-i18n="cmd_sub">
          Tell it what you brought or consumed. Example: "brought 2kg palak", "i ate lays", "used milk", "khaya paneer", "clear all".
        </p>
      </div>

      <form id="command-form" class="flex flex-col sm:flex-row gap-2.5">
        <div class="relative flex-grow">
          <input 
            type="text" 
            id="cmd-input" 
            placeholder="Type or speak (e.g. 'bought 2kg palak', 'i ate lays', 'used milk')..." 
            class="w-full pl-4 pr-12 py-3 rounded-2xl border border-stone-200 dark:border-stone-700 focus:outline-none focus:ring-2 focus:ring-brand-500 text-sm text-stone-800 dark:text-stone-100 bg-stone-50/50 dark:bg-stone-800"
            required
          />
          <button 
            type="button" 
            id="mic-btn" 
            title="Speak command"
            class="absolute right-2 top-2 p-1.5 rounded-xl text-stone-400 hover:text-brand-600 hover:bg-stone-100 dark:hover:bg-stone-700 transition-colors"
          >
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 003-3V5a3 3 0 00-6 0v6a3 3 0 003 3z"></path>
            </svg>
          </button>
        </div>
        <button 
          type="submit" 
          id="cmd-submit-btn"
          class="px-6 py-3 bg-brand-600 hover:bg-brand-700 text-white font-semibold rounded-2xl shadow-sm text-sm flex items-center justify-center gap-2 transition-all transform active:scale-95"
        >
          <span data-i18n="cmd_btn">Process Command</span>
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>
        </button>
      </form>

      <!-- SUGGESTION CHIPS -->
      <div class="mt-3.5 flex items-center gap-1.5 flex-wrap text-xs text-stone-500 dark:text-stone-400">
        <span class="font-medium" data-i18n="suggestions_label">Quick Try:</span>
        <button type="button" class="chip hover:bg-stone-200 dark:hover:bg-stone-700 bg-stone-100 dark:bg-stone-800 text-stone-700 dark:text-stone-300 px-2.5 py-1 rounded-lg">पालक (Palak)</button>
        <button type="button" class="chip hover:bg-stone-200 dark:hover:bg-stone-700 bg-stone-100 dark:bg-stone-800 text-stone-700 dark:text-stone-300 px-2.5 py-1 rounded-lg">पनीर (Paneer)</button>
        <button type="button" class="chip hover:bg-stone-200 dark:hover:bg-stone-700 bg-stone-100 dark:bg-stone-800 text-stone-700 dark:text-stone-300 px-2.5 py-1 rounded-lg">टमाटर (Tomato)</button>
        <button type="button" class="chip hover:bg-stone-200 dark:hover:bg-stone-700 bg-stone-100 dark:bg-stone-800 text-stone-700 dark:text-stone-300 px-2.5 py-1 rounded-lg">i ate lays</button>
        <button type="button" class="chip hover:bg-stone-200 dark:hover:bg-stone-700 bg-stone-100 dark:bg-stone-800 text-stone-700 dark:text-stone-300 px-2.5 py-1 rounded-lg">used milk</button>
      </div>
    </section>

    <!-- TAB NAVIGATION BAR -->
    <div class="flex items-center justify-between border-b border-stone-200 dark:border-stone-800">
      <nav class="flex space-x-6 text-sm font-semibold">
        <button id="tab-inventory-btn" class="tab-btn py-3 border-b-2 border-brand-600 text-brand-600 dark:text-brand-400 flex items-center gap-2">
          <span data-i18n="tab_inventory">Fridge Inventory</span>
          <span id="tab-inventory-count" class="bg-stone-100 dark:bg-stone-800 text-stone-700 dark:text-stone-300 text-xs px-2 py-0.5 rounded-full font-bold">0</span>
        </button>
        <button id="tab-rescue-btn" class="tab-btn py-3 border-b-2 border-transparent text-stone-500 dark:text-stone-400 hover:text-stone-800 dark:hover:text-stone-200 flex items-center gap-2">
          <span data-i18n="tab_rescue">Rescue Chef</span>
          <span class="bg-brand-50 dark:bg-brand-950 text-brand-700 dark:text-brand-300 text-[11px] px-2 py-0.5 rounded-full font-bold">AI Zero-Waste</span>
        </button>
        <button id="tab-test-btn" class="tab-btn py-3 border-b-2 border-transparent text-stone-500 dark:text-stone-400 hover:text-stone-800 dark:hover:text-stone-200 flex items-center gap-2">
          <span data-i18n="tab_scanner">Food Scanner</span>
        </button>
      </nav>

      <!-- FILTER CONTROLS -->
      <div id="filter-controls" class="hidden sm:flex items-center gap-1.5 text-xs">
        <button class="filter-btn active bg-stone-900 dark:bg-white text-white dark:text-stone-900 px-3 py-1 rounded-full font-medium" data-filter="all" data-i18n="filter_all">All</button>
        <button class="filter-btn bg-stone-100 dark:bg-stone-800 text-stone-600 dark:text-stone-300 hover:bg-stone-200 px-3 py-1 rounded-full font-medium" data-filter="urgent" data-i18n="filter_urgent">Urgent</button>
        <button class="filter-btn bg-stone-100 dark:bg-stone-800 text-stone-600 dark:text-stone-300 hover:bg-stone-200 px-3 py-1 rounded-full font-medium" data-filter="fresh" data-i18n="filter_fresh">Fresh</button>
        <button class="filter-btn bg-stone-100 dark:bg-stone-800 text-stone-600 dark:text-stone-300 hover:bg-stone-200 px-3 py-1 rounded-full font-medium" data-filter="expired" data-i18n="filter_expired">Expired</button>
      </div>
    </div>

    <!-- TAB 1: INVENTORY SECTION -->
    <section id="section-inventory" class="tab-section space-y-4">
      <!-- MULTI-SELECT RESCUE BAR -->
      <div id="rescue-action-bar" class="hidden bg-stone-900 dark:bg-stone-800 text-white p-3.5 rounded-2xl flex flex-col sm:flex-row items-center justify-between gap-3 shadow-md">
        <div class="flex items-center gap-2 text-xs sm:text-sm font-medium">
          <span>Selected <b id="selected-count" class="text-brand-400">0</b> expiring items for recipe rescue</span>
        </div>
        <button id="trigger-rescue-btn" class="px-4 py-2 bg-brand-600 hover:bg-brand-500 font-bold rounded-xl text-xs sm:text-sm shadow transition">
          Generate Rescue Recipe ✨
        </button>
      </div>

      <!-- INVENTORY GRID -->
      <div id="inventory-grid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        <!-- Populated via JS -->
      </div>

      <!-- EMPTY STATE -->
      <div id="empty-state" class="hidden text-center py-16 bg-white dark:bg-stone-900 rounded-3xl border border-dashed border-stone-200 dark:border-stone-800 p-6 transition-colors">
        <h3 class="text-base font-bold text-stone-800 dark:text-stone-200" data-i18n="empty_title">Your Fridge is Empty</h3>
        <p class="text-xs text-stone-500 dark:text-stone-400 mt-1 max-w-sm mx-auto" data-i18n="empty_desc">
          Add fresh produce, groceries, or meals using the input above or your microphone.
        </p>
      </div>
    </section>

    <!-- TAB 2: RASOI RESCUE RECIPE SECTION -->
    <section id="section-rescue" class="tab-section hidden space-y-6">
      <div class="bg-white dark:bg-stone-900 rounded-3xl p-6 border border-stone-200/80 dark:border-stone-800 shadow-sm transition-colors">
        <div class="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 border-b border-stone-100 dark:border-stone-800 pb-5">
          <div>
            <h2 class="text-lg font-bold text-stone-900 dark:text-white flex items-center gap-2">
              <span data-i18n="rescue_title">Rasoi Rescue Chef</span>
            </h2>
            <p class="text-xs text-stone-500 dark:text-stone-400 mt-1" data-i18n="rescue_desc">
              Select expiring ingredients from your fridge or specify custom leftovers to create an authentic Indian home dish.
            </p>
          </div>
          <button id="generate-recipe-btn" class="px-5 py-2.5 bg-brand-600 hover:bg-brand-700 text-white font-semibold rounded-xl text-xs sm:text-sm shadow transition flex items-center gap-2">
            <span data-i18n="rescue_run_btn">Generate Rescue Recipe</span>
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg>
          </button>
        </div>

        <!-- CUSTOM INGREDIENTS INPUT -->
        <div class="mt-4">
          <label class="block text-xs font-semibold text-stone-700 dark:text-stone-300 mb-1" data-i18n="custom_rescue_label">
            Custom Ingredients / Leftovers to Rescue (Comma-separated):
          </label>
          <input 
            type="text" 
            id="rescue-items-input" 
            placeholder="e.g. leftover boiled potatoes, sour curd, coriander stems, capsicum..."
            class="w-full px-4 py-2.5 rounded-xl border border-stone-200 dark:border-stone-700 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 bg-stone-50 dark:bg-stone-800 text-stone-800 dark:text-stone-100"
          />
        </div>

        <!-- RECIPE RESULT CARD -->
        <div id="recipe-container" class="mt-6 hidden space-y-6">
          <div class="bg-stone-50 dark:bg-stone-800/60 border border-stone-200 dark:border-stone-700 rounded-2xl p-6 relative">
            
            <!-- Close / Dismiss Recipe Button -->
            <button id="close-recipe-btn" class="absolute top-4 right-4 px-3 py-1.5 rounded-xl border border-stone-200 dark:border-stone-700 hover:bg-stone-200 dark:hover:bg-stone-700 text-stone-600 dark:text-stone-300 text-xs font-semibold flex items-center gap-1.5 transition">
              <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
              <span data-i18n="close_recipe_btn">Close Recipe</span>
            </button>

            <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-stone-200/60 dark:border-stone-700 pb-4 pr-24">
              <div>
                <span class="text-xs font-bold text-brand-600 dark:text-brand-400 uppercase tracking-wider" data-i18n="recipe_card_tag">Zero-Waste Indian Recipe</span>
                <h3 id="recipe-name" class="text-xl sm:text-2xl font-bold text-stone-900 dark:text-white"></h3>
                <h4 id="recipe-name-hi" class="text-base font-semibold text-brand-600 dark:text-brand-400"></h4>
              </div>
              <div class="flex items-center gap-2 mt-2 sm:mt-0">
                <span id="recipe-time" class="bg-white dark:bg-stone-900 border border-stone-200 dark:border-stone-700 text-stone-700 dark:text-stone-300 text-xs font-semibold px-3 py-1.5 rounded-full">
                  ⏱️ 20 mins
                </span>
                <button id="copy-recipe-btn" class="p-2 bg-white dark:bg-stone-900 hover:bg-stone-100 border border-stone-200 dark:border-stone-700 rounded-xl text-stone-600 dark:text-stone-300 text-xs font-medium" title="Copy">
                  📋 Copy
                </button>
              </div>
            </div>

            <!-- INGREDIENTS LISTS -->
            <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mt-4">
              <div class="bg-white dark:bg-stone-900 p-4 rounded-xl border border-stone-200/80 dark:border-stone-700">
                <h5 class="text-xs font-bold text-stone-700 dark:text-stone-300 uppercase mb-2" data-i18n="rescued_items_hdr">
                  Rescued Ingredients
                </h5>
                <ul id="recipe-rescued-list" class="text-xs text-stone-600 dark:text-stone-400 space-y-1.5 list-disc list-inside"></ul>
              </div>
              <div class="bg-white dark:bg-stone-900 p-4 rounded-xl border border-stone-200/80 dark:border-stone-700">
                <h5 class="text-xs font-bold text-stone-700 dark:text-stone-300 uppercase mb-2" data-i18n="pantry_items_hdr">
                  Pantry Spices & Staples
                </h5>
                <ul id="recipe-pantry-list" class="text-xs text-stone-600 dark:text-stone-400 space-y-1.5 list-disc list-inside"></ul>
              </div>
            </div>

            <!-- STEP BY STEP -->
            <div class="mt-5">
              <h5 class="text-xs font-bold text-stone-800 dark:text-stone-200 uppercase tracking-wider mb-3" data-i18n="instructions_hdr">
                Cooking Instructions:
              </h5>
              <ol id="recipe-steps-list" class="space-y-3 text-xs sm:text-sm text-stone-700 dark:text-stone-300 leading-relaxed"></ol>
            </div>

            <!-- CHEF TIP -->
            <div class="mt-5 p-3.5 bg-brand-50 dark:bg-stone-800 rounded-xl border border-brand-200 dark:border-stone-700">
              <h6 class="text-xs font-bold text-brand-900 dark:text-brand-300" data-i18n="chef_tip_hdr">Chef's Zero-Waste Tip:</h6>
              <p id="recipe-tip" class="text-xs text-stone-700 dark:text-stone-300 mt-0.5"></p>
            </div>
          </div>
        </div>

        <!-- LOADING SKELETON -->
        <div id="recipe-loader" class="hidden text-center py-12">
          <div class="inline-block animate-spin text-2xl mb-2">⏳</div>
          <p class="text-sm font-semibold text-stone-800 dark:text-stone-200" data-i18n="recipe_loading">Consulting Indian Culinary Intelligence...</p>
        </div>
      </div>
    </section>

    <!-- TAB 3: STANDALONE FOOD SCANNER SECTION -->
    <section id="section-test" class="tab-section hidden space-y-4">
      <div class="bg-white dark:bg-stone-900 rounded-3xl p-6 border border-stone-200 dark:border-stone-800 shadow-sm max-w-2xl mx-auto transition-colors">
        <h2 class="text-lg font-bold text-stone-900 dark:text-white mb-1" data-i18n="scanner_title">AI Food Inspector & Storage Advisor</h2>
        <p class="text-xs text-stone-500 dark:text-stone-400 mb-4" data-i18n="scanner_desc">
          Test any Indian food name, brand, or dish to inspect its culinary category, Hindi name, standard shelf life, and whether package expiry is mandatory.
        </p>

        <div class="flex gap-2">
          <input 
            type="text" 
            id="scanner-input" 
            placeholder="Type any food (e.g. Gajar, Haldiram Bhujia, Amul Butter)..." 
            class="flex-grow px-4 py-2.5 rounded-xl border border-stone-200 dark:border-stone-700 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 bg-stone-50 dark:bg-stone-800 text-stone-800 dark:text-stone-100"
          />
          <button 
            type="button" 
            id="scanner-btn" 
            class="px-5 py-2.5 bg-stone-900 dark:bg-white text-white dark:text-stone-900 font-semibold rounded-xl text-sm transition"
            data-i18n="scanner_inspect_btn"
          >
            Inspect
          </button>
        </div>

        <div id="scanner-result" class="mt-5 hidden"></div>
      </div>
    </section>

    </div> <!-- End of #app-dashboard -->

  </main>

  <!-- FRIDGE AUTH MODAL -->
  <div id="auth-modal" class="fixed inset-0 bg-stone-900/50 backdrop-blur-sm z-50 flex items-center justify-center p-4 hidden">
    <div class="bg-white dark:bg-stone-900 rounded-3xl max-w-md w-full p-6 shadow-2xl border border-stone-100 dark:border-stone-800 text-stone-800 dark:text-stone-100">
      <div class="flex items-center justify-between border-b border-stone-100 dark:border-stone-800 pb-3">
        <h3 class="font-bold text-base" data-i18n="auth_modal_title">Fridge Login & Setup</h3>
        <button id="close-auth-modal-btn" class="text-stone-400 hover:text-stone-600 text-xl font-bold">&times;</button>
      </div>

      <div class="flex border-b border-stone-100 dark:border-stone-800 mt-4 text-xs font-bold">
        <button id="auth-tab-login" class="flex-1 py-2 text-brand-600 dark:text-brand-400 border-b-2 border-brand-600 text-center" data-i18n="auth_login_tab">
          Login to Fridge
        </button>
        <button id="auth-tab-create" class="flex-1 py-2 text-stone-400 hover:text-stone-600 dark:hover:text-stone-200 border-b-2 border-transparent text-center" data-i18n="auth_create_tab">
          Create New Fridge
        </button>
      </div>

      <!-- FORM: LOGIN -->
      <form id="login-form" class="mt-4 space-y-3.5">
        <div>
          <label class="block text-xs font-semibold mb-1" data-i18n="auth_fid_label">Fridge ID:</label>
          <input 
            type="text" 
            id="login-fid" 
            placeholder="e.g. sharma-rasoi or myfridge" 
            class="w-full px-4 py-2.5 rounded-xl border border-stone-200 dark:border-stone-700 bg-stone-50 dark:bg-stone-800 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500"
            required
          />
        </div>
        <div>
          <label class="block text-xs font-semibold mb-1" data-i18n="auth_pin_label">Family PIN:</label>
          <input 
            type="password" 
            id="login-pin" 
            placeholder="Enter fridge PIN..." 
            class="w-full px-4 py-2.5 rounded-xl border border-stone-200 dark:border-stone-700 bg-stone-50 dark:bg-stone-800 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500"
            required
          />
        </div>
        <button type="submit" class="w-full py-2.5 bg-brand-600 hover:bg-brand-700 text-white font-semibold rounded-xl text-sm shadow transition" data-i18n="auth_login_submit">
          Open Fridge
        </button>
        <p class="text-[11px] text-stone-500 dark:text-stone-400 text-center">
          Demo: <b class="text-brand-600">myfridge</b> | PIN: <b class="text-brand-600">1234</b>
        </p>
      </form>

      <!-- FORM: CREATE NEW FRIDGE -->
      <form id="create-form" class="mt-4 space-y-3.5 hidden">
        <div>
          <label class="block text-xs font-semibold mb-1" data-i18n="auth_create_fid_label">Choose Fridge ID:</label>
          <input 
            type="text" 
            id="create-fid" 
            placeholder="e.g. gupta-home (3-30 letters/numbers)" 
            class="w-full px-4 py-2.5 rounded-xl border border-stone-200 dark:border-stone-700 bg-stone-50 dark:bg-stone-800 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500"
            required
          />
        </div>
        <div>
          <label class="block text-xs font-semibold mb-1" data-i18n="auth_create_pin_label">Set Family PIN (min 4 digits):</label>
          <input 
            type="password" 
            id="create-pin" 
            placeholder="e.g. 5678" 
            class="w-full px-4 py-2.5 rounded-xl border border-stone-200 dark:border-stone-700 bg-stone-50 dark:bg-stone-800 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500"
            required
          />
        </div>
        <button type="submit" class="w-full py-2.5 bg-leaf-600 hover:bg-leaf-700 text-white font-semibold rounded-xl text-sm shadow transition" data-i18n="auth_create_submit">
          Create & Save Fridge
        </button>
      </form>
    </div>
  </div>

  <!-- CLASSIFICATION VERIFICATION MODAL -->
  <div id="classify-modal" class="fixed inset-0 bg-stone-900/40 backdrop-blur-sm z-50 flex items-center justify-center p-4 hidden">
    <div class="bg-white dark:bg-stone-900 rounded-3xl max-w-md w-full p-6 shadow-2xl border border-stone-100 dark:border-stone-800 text-stone-800 dark:text-stone-100">
      <div class="flex items-center justify-between border-b border-stone-100 dark:border-stone-800 pb-3">
        <h3 class="font-bold text-base" data-i18n="modal_confirm_title">Confirm Rasoi Entry</h3>
        <button id="close-modal-btn" class="text-stone-400 hover:text-stone-600 text-xl font-bold">&times;</button>
      </div>

      <div class="mt-4 space-y-4">
        <div class="p-3.5 bg-stone-50 dark:bg-stone-800 rounded-2xl border border-stone-200 dark:border-stone-700 flex items-center justify-between">
          <div>
            <h4 id="modal-name-en" class="font-bold text-base leading-tight"></h4>
            <div id="modal-name-hi" class="text-xs font-semibold text-brand-600 dark:text-brand-400"></div>
          </div>
          <span id="modal-category-badge" class="text-[11px] font-bold uppercase px-2.5 py-1 rounded-md bg-white dark:bg-stone-900 border border-stone-200 dark:border-stone-700 text-stone-700 dark:text-stone-300"></span>
        </div>

        <div id="modal-packaged-alert" class="p-3 bg-amber-50 dark:bg-amber-950/40 rounded-xl border border-amber-300 dark:border-amber-800 text-xs text-amber-900 dark:text-amber-200 hidden">
          <div class="font-bold mb-0.5" data-i18n="modal_packaged_warn">Printed Expiry Date Required</div>
          <span data-i18n="modal_packaged_desc">Packaged or dairy item detected. Please check and enter the printed label date.</span>
        </div>

        <div>
          <label class="block text-xs font-semibold mb-1" id="modal-date-label">Expiry Date:</label>
          <input 
            type="date" 
            id="modal-date-input" 
            class="w-full px-4 py-2.5 rounded-xl border border-stone-200 dark:border-stone-700 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 bg-stone-50 dark:bg-stone-800 text-stone-800 dark:text-stone-100"
          />
          <p id="modal-date-help" class="text-[11px] text-stone-500 dark:text-stone-400 mt-1"></p>
        </div>

        <div class="p-3 bg-leaf-50 dark:bg-stone-800 rounded-xl border border-leaf-100 dark:border-stone-700 text-xs text-leaf-900 dark:text-leaf-300">
          <div class="font-bold mb-0.5" data-i18n="modal_tip_label">Kitchen Storage Tip:</div>
          <div id="modal-storage-tip"></div>
        </div>

        <div class="flex items-center gap-2 pt-2">
          <button id="modal-cancel-btn" class="w-1/2 py-2.5 rounded-xl border border-stone-200 dark:border-stone-700 text-stone-600 dark:text-stone-300 text-sm font-semibold hover:bg-stone-50 dark:hover:bg-stone-800" data-i18n="modal_cancel">
            Cancel
          </button>
          <button id="modal-confirm-btn" class="w-1/2 py-2.5 rounded-xl bg-brand-600 hover:bg-brand-700 text-white text-sm font-semibold shadow-sm" data-i18n="modal_save">
            Save to Fridge
          </button>
        </div>
      </div>
    </div>
  </div>

  <!-- TOAST NOTIFICATION CONTAINER -->
  <div id="toast-container" class="fixed bottom-4 right-4 z-50 flex flex-col gap-2 pointer-events-none"></div>

  <!-- JAVASCRIPT APPLICATION LOGIC -->
  <script>
    // State
    let inventory = [];
    let currentFilter = 'all';
    let currentFridge = null;
    let currentLang = localStorage.getItem('rasoi_lang') || 'en';
    let selectedItemsForRescue = new Set();
    let pendingModalItem = null;

    // Bilingual Text Dictionary
    const i18n = {
      en: {
        app_title: "FridgeInventory",
        app_subtitle: "Smart Food Inventory & Zero-Waste Kitchen",
        lang_btn: "हिन्दी",
        logout_btn: "Logout",
        login_create_btn: "Login",
        bubble_title: "Welcome to FridgeInventory",
        bubble_desc: "Your smart household kitchen companion for effortless inventory tracking, expiry prevention, and AI-powered zero-waste cooking.",
        bubble_login_btn: "Login to Fridge",
        bubble_create_btn: "Create a New Fridge",
        bubble_demo_hint: "Demo Access: Fridge ID: myfridge | PIN: 1234",
        feat_track_title: "Smart Food Tracking",
        feat_track_desc: "Keep real-time tabs on fresh produce, dairy, groceries, and cooked meals.",
        feat_expiry_title: "Expiry Prevention",
        feat_expiry_desc: "Automatic shelf-life estimates and urgent alerts before your food spoils.",
        feat_ai_title: "AI Rescue Chef",
        feat_ai_desc: "Turn expiring ingredients into homestyle recipes with one click.",
        stat_total: "Total Items",
        stat_fresh: "Fresh (3+ Days)",
        stat_urgent: "Expiring Soon",
        stat_expired: "Expired",
        cmd_heading: "Smart Voice & Text Control",
        cmd_sub: "Tell it what you brought or consumed. Example: 'brought 2kg palak', 'i ate lays', 'used milk', 'clear all'.",
        cmd_placeholder: "Type or speak (e.g. 'bought 2kg palak', 'i ate lays', 'used milk')...",
        cmd_btn: "Process Command",
        suggestions_label: "Quick Try:",
        tab_inventory: "Fridge Inventory",
        tab_rescue: "Rescue Chef",
        tab_scanner: "Food Scanner",
        filter_all: "All",
        filter_urgent: "Urgent",
        filter_fresh: "Fresh",
        filter_expired: "Expired",
        empty_title: "Your Fridge is Empty",
        empty_desc: "Add fresh produce, groceries, or meals using the input above or your microphone.",
        rescue_title: "Zero-Waste Rescue Chef",
        rescue_desc: "Select expiring ingredients from your fridge or specify custom leftovers to create an authentic homestyle dish.",
        rescue_run_btn: "Generate Rescue Recipe",
        custom_rescue_label: "Custom Ingredients / Leftovers to Rescue (Comma-separated):",
        close_recipe_btn: "Close Recipe",
        recipe_card_tag: "Zero-Waste Homestyle Recipe",
        rescued_items_hdr: "Rescued Ingredients",
        pantry_items_hdr: "Pantry Spices & Staples",
        instructions_hdr: "Cooking Instructions:",
        chef_tip_hdr: "Chef's Zero-Waste Tip:",
        recipe_loading: "Consulting Culinary Intelligence...",
        scanner_title: "AI Food Inspector & Storage Advisor",
        scanner_desc: "Test any food name, brand, snack, or dish to inspect its culinary category, Hindi name, standard shelf life, and whether package expiry is mandatory.",
        scanner_inspect_btn: "Inspect",
        auth_modal_title: "Fridge Login & Setup",
        auth_login_tab: "Login to Fridge",
        auth_create_tab: "Create New Fridge",
        auth_fid_label: "Fridge ID:",
        auth_pin_label: "Family PIN:",
        auth_login_submit: "Open Fridge",
        auth_create_fid_label: "Choose Fridge ID:",
        auth_create_pin_label: "Set Family PIN (min 4 digits):",
        auth_create_submit: "Create & Save Fridge",
        modal_confirm_title: "Confirm Fridge Entry",
        modal_packaged_warn: "Printed Expiry Date Required",
        modal_packaged_desc: "Packaged or dairy item detected. Please check and enter the printed label date.",
        modal_tip_label: "Kitchen Storage Tip:",
        modal_cancel: "Cancel",
        modal_save: "Save to Fridge"
      },
      hi: {
        app_title: "FridgeInventory",
        app_subtitle: "स्मार्ट फूड इन्वेंटरी और ज़ीरो-वेस्ट किचन",
        lang_btn: "English",
        logout_btn: "लॉगआउट",
        login_create_btn: "लॉग इन",
        bubble_title: "FridgeInventory में आपका स्वागत है",
        bubble_desc: "इन्वेंटरी ट्रैकिंग, एक्सपायरी अलर्ट और ज़ीरो-वेस्ट कुकिंग के लिए आपका स्मार्ट साथी।",
        bubble_login_btn: "फ्रिज में लॉग इन करें",
        bubble_create_btn: "नया फ्रिज बनाएँ",
        bubble_demo_hint: "डेमो एक्सेस: फ्रिज ID: myfridge | पिन: 1234",
        feat_track_title: "स्मार्ट फूड ट्रैकिंग",
        feat_track_desc: "ताज़ी सब्ज़ियों, डेयरी और किराना सामान पर नज़र रखें।",
        feat_expiry_title: "एक्सपायरी अलर्ट",
        feat_expiry_desc: "सामान खराब होने से पहले स्वचालित शेल्फ-लाइफ अनुमान और चेतावनी।",
        feat_ai_title: "AI रेस्क्यू शेफ़",
        feat_ai_desc: "समाप्त होने वाले सामान से स्वादिष्ट होमस्टाइल रेसिपी बनाएँ।",
        stat_total: "कुल सामान",
        stat_fresh: "ताज़ा (3+ दिन)",
        stat_urgent: "जल्द समाप्त",
        stat_expired: "समय समाप्त",
        cmd_heading: "स्मार्ट आवाज़ और टेक्स्ट नियंत्रण",
        cmd_sub: "बताएं क्या लाए या खाया। जैसे: '2 किलो पालक लाया', 'i ate lays', 'दूध इस्तेमाल किया', 'सब खत्म'।",
        cmd_placeholder: "टाइप करें या बोलें (जैसे: '2 किलो पालक लाया', 'i ate lays', 'दूध पिया')...",
        cmd_btn: "कमांड चलाएँ",
        suggestions_label: "त्वरित प्रयास:",
        tab_inventory: "फ्रिज सामग्री",
        tab_rescue: "रेस्क्यू शेफ़",
        tab_scanner: "खाद्य परीक्षक",
        filter_all: "सभी",
        filter_urgent: "जल्द समाप्त",
        filter_fresh: "ताज़ा",
        filter_expired: "समय समाप्त",
        empty_title: "आपका फ्रिज खाली है",
        empty_desc: "ऊपर दिए गए इनपुट या माइक्रोफ़ोन का उपयोग करके ताज़ी सब्ज़ियाँ, किराना या खाना जोड़ें।",
        rescue_title: "रेस्क्यू शेफ़",
        rescue_desc: "बर्बादी रोकने के लिए अपने फ्रिज से समाप्त होने वाले सामान चुनें और स्वादिष्ट होमस्टाइल रेसिपी बनाएँ।",
        rescue_run_btn: "रेस्क्यू रेसिपी बनाएँ",
        custom_rescue_label: "रेस्क्यू के लिए बचे हुए सामान (अल्पविराम से अलग करें):",
        close_recipe_btn: "रेसिपी बंद करें",
        recipe_card_tag: "जीरो-वेस्ट होमस्टाइल रेसिपी",
        rescued_items_hdr: "बचाई गई सामग्री",
        pantry_items_hdr: "रसोई के मसाले और आवश्यक सामान",
        instructions_hdr: "पकाने की विधि:",
        chef_tip_hdr: "दादी माँ का नुस्खा (जीरो-वेस्ट टिप):",
        recipe_loading: "रेसिपी तैयार की जा रही है...",
        scanner_title: "खाद्य परीक्षक एवं भंडारण गाइड",
        scanner_desc: "किसी भी खाद्य पदार्थ की शेल्फ-लाइफ, श्रेणी और पैकेज एक्सपायरी जाँचें।",
        scanner_inspect_btn: "परीक्षण करें",
        auth_modal_title: "फ्रिज लॉग इन और सेटअप",
        auth_login_tab: "फ्रिज खोलें",
        auth_create_tab: "नया फ्रिज बनाएँ",
        auth_fid_label: "फ्रिज ID:",
        auth_pin_label: "फैमिली PIN:",
        auth_login_submit: "फ्रिज खोलें",
        auth_create_fid_label: "फ्रिज ID चुनें:",
        auth_create_pin_label: "फैमिली PIN सेट करें (कम से कम 4 अंक):",
        auth_create_submit: "फ्रिज बनाएँ और जोड़ें",
        modal_confirm_title: "सामान की पुष्टि करें",
        modal_packaged_warn: "पैकेट की एक्सपायरी आवश्यक है",
        modal_packaged_desc: "पैकेज्ड सामान। कृपया पैकेट पर छपी एक्सपायरी तारीख दर्ज करें।",
        modal_tip_label: "रसोई भंडारण सुझाव:",
        modal_cancel: "रद्द करें",
        modal_save: "फ्रिज में सेव करें"
      }
    };

    function applyLanguage(lang) {
      currentLang = lang;
      localStorage.setItem('rasoi_lang', lang);
      const texts = i18n[lang] || i18n.en;

      document.querySelectorAll('[data-i18n]').forEach(el => {
        const key = el.getAttribute('data-i18n');
        if (texts[key]) el.textContent = texts[key];
      });

      document.getElementById('cmd-input').placeholder = texts.cmd_placeholder;
      document.getElementById('lang-label').textContent = texts.lang_btn;
    }

    document.getElementById('lang-toggle-btn').addEventListener('click', () => {
      applyLanguage(currentLang === 'en' ? 'hi' : 'en');
      renderInventory();
    });

    // Theme Management
    function initTheme() {
      const isDark = document.documentElement.classList.contains('dark');
      document.getElementById('theme-icon-sun').classList.toggle('hidden', !isDark);
      document.getElementById('theme-icon-moon').classList.toggle('hidden', isDark);
    }

    function toggleTheme() {
      if (document.documentElement.classList.contains('dark')) {
        document.documentElement.classList.remove('dark');
        localStorage.setItem('rasoi_theme', 'light');
      } else {
        document.documentElement.classList.add('dark');
        localStorage.setItem('rasoi_theme', 'dark');
      }
      initTheme();
    }

    document.getElementById('theme-toggle-btn').addEventListener('click', toggleTheme);

    // Toast Notifications
    function showToast(message, type = 'info') {
      const el = document.createElement('div');
      el.className = `pointer-events-auto px-4 py-2.5 rounded-xl shadow-lg text-xs font-semibold flex items-center gap-2 transition-all transform translate-y-2 opacity-0 ${
        type === 'success' ? 'bg-leaf-600 text-white' :
        type === 'error' ? 'bg-rose-600 text-white' :
        type === 'warning' ? 'bg-brand-600 text-white' : 'bg-stone-800 text-white'
      }`;
      el.textContent = message;
      document.getElementById('toast-container').appendChild(el);
      requestAnimationFrame(() => el.classList.remove('translate-y-2', 'opacity-0'));
      setTimeout(() => {
        el.classList.add('opacity-0', 'translate-y-2');
        setTimeout(() => el.remove(), 300);
      }, 3500);
    }

    // Auth & Fridge Management
    async function checkAuth() {
      try {
        const res = await fetch('/api/auth/me');
        const data = await res.json();
        if (data.authenticated && data.fridge_id) {
          setAuthenticatedState(data.fridge_id);
        } else {
          setLoggedOutState();
        }
      } catch (e) {
        setLoggedOutState();
      }
    }

    function setAuthenticatedState(fid) {
      currentFridge = fid;
      document.getElementById('auth-logged-in').classList.remove('hidden');
      document.getElementById('auth-logged-out').classList.add('hidden');
      document.getElementById('header-fridge-name').textContent = fid.toUpperCase();
      document.getElementById('logged-out-bubble').classList.add('hidden');
      document.getElementById('app-dashboard').classList.remove('hidden');
      loadInventory();
    }

    function setLoggedOutState() {
      currentFridge = null;
      document.getElementById('auth-logged-in').classList.add('hidden');
      document.getElementById('auth-logged-out').classList.remove('hidden');
      document.getElementById('logged-out-bubble').classList.remove('hidden');
      document.getElementById('app-dashboard').classList.add('hidden');
      inventory = [];
    }

    const authModal = document.getElementById('auth-modal');
    document.getElementById('open-auth-btn').addEventListener('click', () => {
      authTabLogin.click();
      authModal.classList.remove('hidden');
    });
    document.getElementById('close-auth-modal-btn').addEventListener('click', () => authModal.classList.add('hidden'));

    document.getElementById('bubble-login-btn').addEventListener('click', () => {
      authTabLogin.click();
      authModal.classList.remove('hidden');
    });

    document.getElementById('bubble-create-btn').addEventListener('click', () => {
      authTabCreate.click();
      authModal.classList.remove('hidden');
    });

    const authTabLogin = document.getElementById('auth-tab-login');
    const authTabCreate = document.getElementById('auth-tab-create');
    const loginForm = document.getElementById('login-form');
    const createForm = document.getElementById('create-form');

    authTabLogin.addEventListener('click', () => {
      authTabLogin.className = 'flex-1 py-2 text-brand-600 dark:text-brand-400 border-b-2 border-brand-600 text-center font-bold';
      authTabCreate.className = 'flex-1 py-2 text-stone-400 hover:text-stone-600 dark:hover:text-stone-200 border-b-2 border-transparent text-center';
      loginForm.classList.remove('hidden');
      createForm.classList.add('hidden');
    });

    authTabCreate.addEventListener('click', () => {
      authTabCreate.className = 'flex-1 py-2 text-leaf-600 dark:text-leaf-400 border-b-2 border-leaf-600 text-center font-bold';
      authTabLogin.className = 'flex-1 py-2 text-stone-400 hover:text-stone-600 dark:hover:text-stone-200 border-b-2 border-transparent text-center';
      createForm.classList.remove('hidden');
      loginForm.classList.add('hidden');
    });

    loginForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fid = document.getElementById('login-fid').value.trim();
      const pin = document.getElementById('login-pin').value.trim();
      try {
        const res = await fetch('/api/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ fridge_id: fid, pin: pin })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Login failed');
        authModal.classList.add('hidden');
        setAuthenticatedState(data.fridge_id);
        showToast(data.message, 'success');
      } catch (err) {
        showToast(err.message, 'error');
      }
    });

    createForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fid = document.getElementById('create-fid').value.trim();
      const pin = document.getElementById('create-pin').value.trim();
      try {
        const res = await fetch('/api/auth/create', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ fridge_id: fid, pin: pin })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Fridge creation failed');
        authModal.classList.add('hidden');
        setAuthenticatedState(data.fridge_id);
        showToast(data.message, 'success');
      } catch (err) {
        showToast(err.message, 'error');
      }
    });

    document.getElementById('logout-btn').addEventListener('click', async () => {
      try {
        await fetch('/api/auth/logout', { method: 'POST' });
        setLoggedOutState();
        showToast(currentLang === 'hi' ? 'लॉगआउट सफल' : 'Logged out', 'info');
      } catch (e) {
        setLoggedOutState();
      }
    });

    // Load Inventory Items
    async function loadInventory() {
      try {
        const url = currentFridge ? `/api/inventory?fridge=${encodeURIComponent(currentFridge)}` : '/api/inventory';
        const res = await fetch(url);
        if (!res.ok) throw new Error('Failed to load inventory');
        inventory = await res.json();
        renderInventory();
        updateStats();
      } catch (err) {
        showToast('Could not load inventory', 'error');
      }
    }

    function updateStats() {
      const total = inventory.length;
      const fresh = inventory.filter(i => i.days_left >= 3).length;
      const urgent = inventory.filter(i => i.days_left >= 0 && i.days_left <= 2).length;
      const expired = inventory.filter(i => i.days_left < 0).length;

      document.getElementById('stat-total').textContent = total;
      document.getElementById('stat-fresh').textContent = fresh;
      document.getElementById('stat-urgent').textContent = urgent;
      document.getElementById('stat-expired').textContent = expired;
      document.getElementById('tab-inventory-count').textContent = total;
    }

    function renderInventory() {
      const grid = document.getElementById('inventory-grid');
      const empty = document.getElementById('empty-state');
      grid.innerHTML = '';
      
      const filtered = inventory.filter(item => {
        if (currentFilter === 'urgent') return item.days_left >= 0 && item.days_left <= 2;
        if (currentFilter === 'fresh') return item.days_left >= 3;
        if (currentFilter === 'expired') return item.days_left < 0;
        return true;
      });

      if (filtered.length === 0) {
        empty.classList.remove('hidden');
      } else {
        empty.classList.add('hidden');
      }

      filtered.forEach(item => {
        let badgeClass = 'bg-leaf-50 dark:bg-stone-800 text-leaf-700 dark:text-leaf-300 border-leaf-200 dark:border-stone-700';
        let statusText = `${item.days_left}d left`;
        
        if (item.days_left < 0) {
          badgeClass = 'bg-rose-50 dark:bg-stone-800 text-rose-700 dark:text-rose-300 border-rose-200 dark:border-stone-700';
          statusText = currentLang === 'hi' ? `अवधि समाप्त (${Math.abs(item.days_left)} दिन पहले)` : `Expired ${Math.abs(item.days_left)}d ago`;
        } else if (item.days_left === 0) {
          badgeClass = 'bg-amber-100 dark:bg-stone-800 text-brand-800 dark:text-brand-300 border-amber-300 dark:border-stone-700 font-bold';
          statusText = currentLang === 'hi' ? 'आज समाप्त' : 'Expires Today';
        } else if (item.days_left <= 2) {
          badgeClass = 'bg-amber-50 dark:bg-stone-800 text-brand-700 dark:text-brand-300 border-amber-200 dark:border-stone-700 font-semibold';
          statusText = currentLang === 'hi' ? `${item.days_left} दिन बाकी` : `${item.days_left}d left`;
        } else {
          statusText = currentLang === 'hi' ? `${item.days_left} दिन बाकी` : `${item.days_left}d left`;
        }

        const isChecked = selectedItemsForRescue.has(item.name_en);
        const displayName = (currentLang === 'hi' && item.name_hi) ? item.name_hi : item.name_en;
        const subName = (currentLang === 'hi' && item.name_hi) ? item.name_en : item.name_hi;

        const card = document.createElement('div');
        card.className = `bg-white dark:bg-stone-900 rounded-2xl p-4 border border-stone-200/80 dark:border-stone-800 shadow-sm hover:shadow-md transition-all flex flex-col justify-between ${
          item.days_left < 0 ? 'opacity-85' : ''
        }`;
        card.innerHTML = `
          <div>
            <div class="flex items-start justify-between gap-2">
              <div>
                <h4 class="font-bold text-stone-900 dark:text-white text-sm leading-tight">${displayName}</h4>
                ${subName ? `<p class="text-xs text-stone-500 dark:text-stone-400 mt-0.5">${subName}</p>` : ''}
              </div>
              <span class="text-[11px] px-2 py-0.5 rounded-full border ${badgeClass}">
                ${statusText}
              </span>
            </div>

            <div class="mt-3 text-[11px] text-stone-500 dark:text-stone-400 space-y-1">
              <div>Expiry: <span class="font-medium text-stone-700 dark:text-stone-300">${item.expiry_date}</span></div>
              ${item.storage_tip ? `<div class="text-stone-600 dark:text-stone-300 bg-stone-50 dark:bg-stone-800 p-2 rounded-lg border border-stone-100 dark:border-stone-700 mt-2">${item.storage_tip}</div>` : ''}
            </div>
          </div>

          <div class="mt-4 pt-3 border-t border-stone-100 dark:border-stone-800 flex items-center justify-between">
            <label class="flex items-center gap-1.5 text-xs text-stone-600 dark:text-stone-300 cursor-pointer select-none">
              <input type="checkbox" class="rescue-check rounded text-brand-600 focus:ring-brand-500" data-name="${item.name_en}" ${isChecked ? 'checked' : ''}>
              <span>${currentLang === 'hi' ? 'रेस्क्यू में जोड़ें' : 'Select for Rescue'}</span>
            </label>
            <button class="delete-btn text-xs text-stone-400 hover:text-rose-600 font-medium transition" data-id="${item.id}">
              ${currentLang === 'hi' ? 'हटाएँ' : 'Clear'}
            </button>
          </div>
        `;
        grid.appendChild(card);
      });

      document.querySelectorAll('.rescue-check').forEach(box => {
        box.addEventListener('change', (e) => {
          const name = e.target.dataset.name;
          if (e.target.checked) selectedItemsForRescue.add(name);
          else selectedItemsForRescue.delete(name);
          updateRescueBar();
        });
      });

      document.querySelectorAll('.delete-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
          await deleteItem(e.target.dataset.id);
        });
      });
    }

    function updateRescueBar() {
      const count = selectedItemsForRescue.size;
      document.getElementById('selected-count').textContent = count;
      document.getElementById('rescue-action-bar').classList.toggle('hidden', count === 0);
    }

    async function deleteItem(id) {
      try {
        const url = currentFridge ? `/api/inventory/${id}?fridge=${encodeURIComponent(currentFridge)}` : `/api/inventory/${id}`;
        const res = await fetch(url, { method: 'DELETE' });
        if (!res.ok) throw new Error('Delete failed');
        inventory = inventory.filter(i => i.id !== id);
        renderInventory();
        updateStats();
        showToast(currentLang === 'hi' ? 'सामान हटा दिया गया' : 'Item removed', 'info');
      } catch (err) {
        showToast('Failed to delete item', 'error');
      }
    }

    // Modal Handling for Adding Classified Food
    const modal = document.getElementById('classify-modal');
    const modalNameEn = document.getElementById('modal-name-en');
    const modalNameHi = document.getElementById('modal-name-hi');
    const modalCatBadge = document.getElementById('modal-category-badge');
    const modalPackagedAlert = document.getElementById('modal-packaged-alert');
    const modalDateInput = document.getElementById('modal-date-input');
    const modalDateHelp = document.getElementById('modal-date-help');
    const modalStorageTip = document.getElementById('modal-storage-tip');

    function openModalWithClassification(result) {
      pendingModalItem = result;
      modalNameEn.textContent = result.name_en;
      modalNameHi.textContent = result.name_hi;
      modalCatBadge.textContent = result.category.replace(/_/g, ' ');
      modalStorageTip.textContent = result.storage_tip || 'Keep in a cool dry spot or refrigerated.';

      const today = new Date();
      let defaultDate = new Date();

      if (result.requires_expiry_date) {
        modalPackagedAlert.classList.remove('hidden');
        modalDateHelp.textContent = currentLang === 'hi' ? 'पैकेट पर दी गई एक्सपायरी तारीख दर्ज करें।' : 'Check the printed date on package.';
        defaultDate.setDate(today.getDate() + 7);
      } else {
        modalPackagedAlert.classList.add('hidden');
        const days = (result.shelf_life_days && result.shelf_life_days > 0) ? result.shelf_life_days : 4;
        defaultDate.setDate(today.getDate() + days);
        modalDateHelp.textContent = currentLang === 'hi' ? `अनुमानित ${days} दिन की शेल्फ लाइफ।` : `Estimated ${days} days shelf life.`;
      }

      modalDateInput.value = defaultDate.toISOString().split('T')[0];
      modal.classList.remove('hidden');
    }

    document.getElementById('close-modal-btn').addEventListener('click', () => modal.classList.add('hidden'));
    document.getElementById('modal-cancel-btn').addEventListener('click', () => modal.classList.add('hidden'));

    document.getElementById('modal-confirm-btn').addEventListener('click', async () => {
      if (!pendingModalItem) return;
      const expiry = modalDateInput.value;

      try {
        const payload = {
          name_en: pendingModalItem.name_en,
          name_hi: pendingModalItem.name_hi,
          category: pendingModalItem.category,
          shelf_life_days: pendingModalItem.shelf_life_days,
          requires_expiry_date: pendingModalItem.requires_expiry_date,
          expiry_date: expiry,
          storage_tip: pendingModalItem.storage_tip
        };

        const url = currentFridge ? `/api/inventory?fridge=${encodeURIComponent(currentFridge)}` : '/api/inventory';
        const res = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });

        if (!res.ok) throw new Error('Could not add item');
        const saved = await res.json();
        inventory.unshift(saved);
        renderInventory();
        updateStats();
        modal.classList.add('hidden');
        document.getElementById('cmd-input').value = '';
        showToast(currentLang === 'hi' ? `${saved.name_hi || saved.name_en} जोड़ा गया!` : `Added ${saved.name_en}!`, 'success');
      } catch (err) {
        showToast('Error saving item to inventory', 'error');
      }
    });

    // UNIFIED NATURAL LANGUAGE COMMAND FORM
    const commandForm = document.getElementById('command-form');
    const cmdInput = document.getElementById('cmd-input');
    const cmdSubmitBtn = document.getElementById('cmd-submit-btn');

    commandForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const text = cmdInput.value.trim();
      if (!text) return;

      cmdSubmitBtn.disabled = true;
      cmdSubmitBtn.innerHTML = '<span>Processing...</span>';

      try {
        const url = currentFridge ? `/api/command?fridge=${encodeURIComponent(currentFridge)}` : '/api/command';
        const res = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ command: text })
        });

        if (!res.ok) {
          const errData = await res.json();
          throw new Error(errData.detail || 'Command processing failed');
        }

        const data = await res.json();

        // 1. Handled Clear action
        if (data.action === 'clear') {
          inventory = [];
          renderInventory();
          updateStats();
          cmdInput.value = '';
          showToast(currentLang === 'hi' ? data.message_hi : data.message_en, 'success');
        }
        // 2. Handled Take / Consume action (e.g. "i ate lays")
        else if (data.action === 'removed') {
          inventory = inventory.filter(i => i.id !== data.item_id);
          renderInventory();
          updateStats();
          cmdInput.value = '';
          showToast(currentLang === 'hi' ? data.message_hi : data.message_en, 'success');
        }
        // 3. Item not found for consumption
        else if (data.action === 'not_found') {
          showToast(currentLang === 'hi' ? data.message_hi : data.message_en, 'warning');
        }
        // 4. Handled Add / Classify action
        else if (data.action === 'classify') {
          const c = data.classification;
          if (!c.is_food) {
            showToast(c.message || 'Not recognized as a food item.', 'warning');
          } else {
            openModalWithClassification(c);
          }
        }
      } catch (err) {
        showToast(err.message, 'error');
      } finally {
        cmdSubmitBtn.disabled = false;
        cmdSubmitBtn.innerHTML = `<span>${i18n[currentLang].cmd_btn}</span><svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>`;
      }
    });

    // Web Speech API Voice Input
    const micBtn = document.getElementById('mic-btn');
    if ('webkitSpeechRecognition' in window || 'SpeechRecognition' in window) {
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      const recognition = new SpeechRecognition();
      recognition.continuous = false;
      recognition.interimResults = false;
      recognition.lang = currentLang === 'hi' ? 'hi-IN' : 'en-IN';

      micBtn.addEventListener('click', () => {
        try {
          recognition.lang = currentLang === 'hi' ? 'hi-IN' : 'en-IN';
          recognition.start();
          micBtn.classList.add('text-rose-600', 'animate-pulse');
          showToast(currentLang === 'hi' ? 'सुन रहा हूँ... बोलिए' : 'Listening... Speak now', 'info');
        } catch (err) {
          recognition.stop();
        }
      });

      recognition.onresult = (e) => {
        const transcript = e.results[0][0].transcript;
        cmdInput.value = transcript;
        micBtn.classList.remove('text-rose-600', 'animate-pulse');
        commandForm.dispatchEvent(new Event('submit'));
      };

      recognition.onerror = () => micBtn.classList.remove('text-rose-600', 'animate-pulse');
      recognition.onend = () => micBtn.classList.remove('text-rose-600', 'animate-pulse');
    } else {
      micBtn.style.display = 'none';
    }

    // Quick Chips
    document.querySelectorAll('.chip').forEach(chip => {
      chip.addEventListener('click', () => {
        cmdInput.value = chip.textContent;
        commandForm.dispatchEvent(new Event('submit'));
      });
    });

    // Tab Navigation
    const tabInvBtn = document.getElementById('tab-inventory-btn');
    const tabRescBtn = document.getElementById('tab-rescue-btn');
    const tabTestBtn = document.getElementById('tab-test-btn');
    const secInv = document.getElementById('section-inventory');
    const secResc = document.getElementById('section-rescue');
    const secTest = document.getElementById('section-test');
    const filterControls = document.getElementById('filter-controls');

    function switchTab(activeBtn, activeSec) {
      [tabInvBtn, tabRescBtn, tabTestBtn].forEach(b => {
        b.className = 'tab-btn py-3 border-b-2 border-transparent text-stone-500 dark:text-stone-400 hover:text-stone-800 dark:hover:text-stone-200 flex items-center gap-2';
      });
      [secInv, secResc, secTest].forEach(s => s.classList.add('hidden'));

      activeBtn.className = 'tab-btn py-3 border-b-2 border-brand-600 text-brand-600 dark:text-brand-400 flex items-center gap-2 font-bold';
      activeSec.classList.remove('hidden');

      filterControls.classList.toggle('hidden', activeSec !== secInv);
    }

    tabInvBtn.addEventListener('click', () => switchTab(tabInvBtn, secInv));
    tabRescBtn.addEventListener('click', () => switchTab(tabRescBtn, secResc));
    tabTestBtn.addEventListener('click', () => switchTab(tabTestBtn, secTest));

    document.getElementById('trigger-rescue-btn').addEventListener('click', () => {
      switchTab(tabRescBtn, secResc);
      const items = Array.from(selectedItemsForRescue);
      document.getElementById('rescue-items-input').value = items.join(', ');
      triggerRecipeGeneration(items);
    });

    document.querySelectorAll('.filter-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        document.querySelectorAll('.filter-btn').forEach(b => {
          b.className = 'filter-btn bg-stone-100 dark:bg-stone-800 text-stone-600 dark:text-stone-300 hover:bg-stone-200 px-3 py-1 rounded-full font-medium';
        });
        e.target.className = 'filter-btn active bg-stone-900 dark:bg-white text-white dark:text-stone-900 px-3 py-1 rounded-full font-medium';
        currentFilter = e.target.dataset.filter;
        renderInventory();
      });
    });

    // Rasoi Rescue Generation & Close Button
    const generateRecipeBtn = document.getElementById('generate-recipe-btn');
    const rescueInput = document.getElementById('rescue-items-input');
    const recipeContainer = document.getElementById('recipe-container');
    const recipeLoader = document.getElementById('recipe-loader');
    const closeRecipeBtn = document.getElementById('close-recipe-btn');

    closeRecipeBtn.addEventListener('click', () => {
      recipeContainer.classList.add('hidden');
    });

    async function triggerRecipeGeneration(itemsList) {
      if (!itemsList || itemsList.length === 0) {
        showToast(currentLang === 'hi' ? 'कृपया कम से कम एक सामान चुनें' : 'Please select at least one ingredient', 'warning');
        return;
      }

      recipeContainer.classList.add('hidden');
      recipeLoader.classList.remove('hidden');
      generateRecipeBtn.disabled = true;

      try {
        const res = await fetch('/rescue', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ items: itemsList })
        });

        if (!res.ok) {
          const err = await res.json();
          throw new Error(err.detail || 'Recipe generation failed');
        }

        const recipe = await res.json();
        renderRecipe(recipe);
      } catch (err) {
        showToast(err.message, 'error');
      } finally {
        recipeLoader.classList.add('hidden');
        generateRecipeBtn.disabled = false;
      }
    }

    function renderRecipe(recipe) {
      document.getElementById('recipe-name').textContent = recipe.recipe_name;
      document.getElementById('recipe-name-hi').textContent = recipe.recipe_name_hi;
      document.getElementById('recipe-time').textContent = `⏱️ ${recipe.prep_time_minutes} mins`;
      document.getElementById('recipe-tip').textContent = recipe.chef_tip;

      const rescuedList = document.getElementById('recipe-rescued-list');
      rescuedList.innerHTML = '';
      recipe.ingredients_used.forEach(i => {
        const li = document.createElement('li');
        li.textContent = i;
        rescuedList.appendChild(li);
      });

      const pantryList = document.getElementById('recipe-pantry-list');
      pantryList.innerHTML = '';
      recipe.pantry_items_needed.forEach(i => {
        const li = document.createElement('li');
        li.textContent = i;
        pantryList.appendChild(li);
      });

      const stepsList = document.getElementById('recipe-steps-list');
      stepsList.innerHTML = '';
      recipe.steps.forEach((step, idx) => {
        const li = document.createElement('li');
        li.className = 'flex items-start gap-3';
        li.innerHTML = `<span class="w-5 h-5 rounded-full bg-brand-600 text-white flex items-center justify-center text-xs font-bold shrink-0 mt-0.5">${idx + 1}</span><span>${step}</span>`;
        stepsList.appendChild(li);
      });

      recipeContainer.classList.remove('hidden');
    }

    generateRecipeBtn.addEventListener('click', () => {
      let raw = rescueInput.value.trim();
      let items = [];
      if (raw) {
        items = raw.split(',').map(s => s.trim()).filter(Boolean);
      } else {
        items = inventory.filter(i => i.days_left <= 2).map(i => i.name_en);
        if (items.length === 0 && inventory.length > 0) {
          items = inventory.slice(0, 3).map(i => i.name_en);
        }
      }
      triggerRecipeGeneration(items);
    });

    document.getElementById('copy-recipe-btn').addEventListener('click', () => {
      const name = document.getElementById('recipe-name').textContent;
      const nameHi = document.getElementById('recipe-name-hi').textContent;
      const tip = document.getElementById('recipe-tip').textContent;
      const text = `${name} (${nameHi})\n\nChef Tip: ${tip}`;
      navigator.clipboard.writeText(text).then(() => {
        showToast(currentLang === 'hi' ? 'रेसिपी कॉपी की गई!' : 'Recipe copied!', 'success');
      });
    });

    // Scanner Tab Logic
    const scannerInput = document.getElementById('scanner-input');
    const scannerBtn = document.getElementById('scanner-btn');
    const scannerResult = document.getElementById('scanner-result');

    scannerBtn.addEventListener('click', async () => {
      const val = scannerInput.value.trim();
      if (!val) return;
      scannerBtn.disabled = true;
      scannerBtn.textContent = 'Scanning...';

      try {
        const res = await fetch('/classify', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ item: val })
        });
        const data = await res.json();
        
        scannerResult.classList.remove('hidden');
        if (!data.is_food) {
          scannerResult.innerHTML = `
            <div class="p-5 bg-rose-50 dark:bg-stone-800 border border-rose-200 dark:border-rose-900 rounded-2xl text-rose-800 dark:text-rose-200 text-xs">
              <div class="flex items-start justify-between gap-3">
                <div>
                  <b class="text-sm">Not recognized as food</b>
                  <p class="mt-1">${data.message || 'Please check spelling or enter a food item.'}</p>
                </div>
                <button type="button" id="close-scanned-item-btn" class="shrink-0 px-2.5 py-1.5 rounded-xl border border-rose-200 dark:border-rose-800 hover:bg-rose-100 dark:hover:bg-stone-700 text-rose-600 dark:text-rose-300 text-xs font-semibold flex items-center gap-1 transition" title="Dismiss Item">
                  <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
                  <span>Close</span>
                </button>
              </div>
            </div>
          `;
        } else {
          scannerResult.innerHTML = `
            <div class="p-5 bg-stone-50 dark:bg-stone-800 border border-stone-200 dark:border-stone-700 rounded-2xl space-y-3.5 relative">
              <div class="flex items-start justify-between gap-3">
                <div>
                  <div class="flex items-center gap-2">
                    <h4 class="font-bold text-base text-stone-900 dark:text-white">${data.name_en}</h4>
                    <span class="text-[10px] font-bold px-2 py-0.5 rounded-full ${data.analyzed_by && data.analyzed_by.includes('Gemini') ? 'bg-purple-100 text-purple-700 dark:bg-purple-950 dark:text-purple-300 border border-purple-200 dark:border-purple-800' : 'bg-brand-50 text-brand-700 dark:bg-brand-950 dark:text-brand-300 border border-brand-200 dark:border-brand-800'}">
                      ${data.analyzed_by || 'Verified'}
                    </span>
                  </div>
                  <div class="text-xs font-semibold text-brand-600 dark:text-brand-400 mt-0.5">${data.name_hi}</div>
                </div>
                <div class="flex items-center gap-2">
                  <span class="text-xs font-bold px-2.5 py-1 rounded-lg bg-white dark:bg-stone-900 border border-stone-200 dark:border-stone-700 uppercase">
                    ${data.category.replace(/_/g, ' ')}
                  </span>
                  <button type="button" id="close-scanned-item-btn" class="p-1.5 rounded-xl border border-stone-200 dark:border-stone-700 hover:bg-stone-200 dark:hover:bg-stone-700 text-stone-600 dark:text-stone-300 text-xs font-semibold flex items-center gap-1 transition" title="Dismiss Item">
                    <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
                    <span>Close</span>
                  </button>
                </div>
              </div>
              <div class="text-xs text-stone-600 dark:text-stone-300 space-y-1">
                <div><b>Printed Expiry Date Required:</b> ${data.requires_expiry_date ? '<span class="text-rose-600 font-bold">YES</span>' : '<span class="text-leaf-600 font-bold">NO</span>'}</div>
                <div><b>Estimated Shelf Life:</b> ${data.requires_expiry_date ? 'Check package date' : (data.shelf_life_days + ' Days')}</div>
              </div>
              <div class="p-3 bg-white dark:bg-stone-900 rounded-xl border border-stone-200 dark:border-stone-700 text-xs text-stone-700 dark:text-stone-300">
                <b>Storage Advice:</b> ${data.storage_tip || 'Refrigerate in a clean container.'}
              </div>
            </div>
          `;
        }

        const closeBtn = document.getElementById('close-scanned-item-btn');
        if (closeBtn) {
          closeBtn.addEventListener('click', () => {
            scannerResult.classList.add('hidden');
            scannerResult.innerHTML = '';
            scannerInput.value = '';
          });
        }
      } catch (err) {
        showToast('Inspection failed', 'error');
      } finally {
        scannerBtn.disabled = false;
        scannerBtn.textContent = i18n[currentLang].scanner_inspect_btn;
      }
    });

    // Initialize on page load
    window.addEventListener('DOMContentLoaded', () => {
      initTheme();
      applyLanguage(currentLang);
      checkAuth();
    });
  </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse, summary="Serve FridgeInventory Frontend UI")
def serve_ui():
    """Returns the single-page application dashboard for FridgeInventory."""
    return HTMLResponse(content=HTML_CONTENT, status_code=200)


# ============================================================================
# 14. PRODUCTION EXECUTION ENTRYPOINT
# ============================================================================

if __name__ == "__main__":
    server_port = int(os.getenv("PORT", 10000))
    server_host = os.getenv("HOST", "0.0.0.0")

    logger.info("=" * 70)
    logger.info("Starting FridgeInventory Production Server")
    logger.info(f"Binding strictly to http://{server_host}:{server_port}")
    logger.info(f"Target Environment: Render / Production ASGI")
    logger.info("=" * 70)

    init_firestore()

    uvicorn.run(
        app,
        host=server_host,
        port=server_port,
        log_level="info",
        access_log=True,
    )
