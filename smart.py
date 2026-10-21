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
import json
import logging
import re
import uuid
import secrets
import hashlib
import hmac
import glob
import base64
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union
import difflib

# Multi-Country Global Timezone Support
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

DEFAULT_TIMEZONE = "UTC"

def resolve_tz(tz_identifier: Optional[str] = None) -> Union[timezone, Any]:
    """
    Resolves any IANA timezone from anywhere in the world:
    e.g. 'America/New_York', 'Europe/London', 'Asia/Tokyo', 'Europe/Paris',
         'Asia/Dubai', 'Australia/Sydney', 'America/Los_Angeles', 'Asia/Kolkata', 'UTC'.
    Falls back gracefully to UTC if unrecognized or invalid.
    """
    if tz_identifier:
        clean = tz_identifier.strip()
        try:
            return ZoneInfo(clean)
        except Exception:
            pass
    return timezone.utc

IST = resolve_tz("Asia/Kolkata")

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

FRIDGE_RE = re.compile(r"^[a-zA-Z0-9]{3,30}$")
PIN_RE = re.compile(r"^[a-zA-Z0-9]{4,30}$")
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
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn

# Firebase Admin SDK
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    HAVE_FIREBASE = True
except ImportError:
    HAVE_FIREBASE = False
    firebase_admin = None  # type: ignore[assignment]
    credentials = None  # type: ignore[assignment]
    firestore = None  # type: ignore[assignment]
    logger.warning("firebase-admin is not installed. To use Firestore, install 'firebase-admin'.")

# Google GenAI SDK (google-genai)
try:
    from google import genai
    from google.genai import types
    HAVE_GENAI = True
except ImportError:
    HAVE_GENAI = False
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]
    logger.warning("google-genai is not installed. To use Gemini AI, install 'google-genai'.")

# Web Push Notifications (pywebpush)
try:
    from pywebpush import webpush, WebPushException
    HAVE_WEBPUSH = True
except ImportError:
    HAVE_WEBPUSH = False
    webpush = None  # type: ignore[assignment]
    WebPushException = Exception  # type: ignore[assignment,misc]
    logger.warning("pywebpush is not installed. Web push notifications disabled.")

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
            or os.getenv("FIREBASE_CREDENTIALS")
            or os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
            or os.getenv("FIREBASE_SERVICE_ACCOUNT_KEY")
            or os.getenv("FIREBASE_CONFIG")
        )
        google_app_creds = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

        cert_dict = None

        if raw_env_creds:
            raw_str = raw_env_creds.strip()
            # If wrapped in outer quotation marks from dashboard paste
            if (raw_str.startswith('"') and raw_str.endswith('"')) or (raw_str.startswith("'") and raw_str.endswith("'")):
                raw_str = raw_str[1:-1].strip()

            # Direct JSON parse attempt
            try:
                cert_dict = json.loads(raw_str)
                if isinstance(cert_dict, str):
                    cert_dict = json.loads(cert_dict)
            except Exception as json_err:
                logger.debug(f"Direct JSON parse attempt: {json_err}")

            # File path attempt
            if not cert_dict and os.path.isfile(raw_str):
                try:
                    with open(raw_str, "r", encoding="utf-8") as f:
                        cert_dict = json.load(f)
                except Exception as file_err:
                    logger.debug(f"File credential read attempt: {file_err}")

            # Base64 decode attempt
            if not cert_dict:
                import base64
                try:
                    decoded = base64.b64decode(raw_str).decode("utf-8")
                    cert_dict = json.loads(decoded)
                    if isinstance(cert_dict, str):
                        cert_dict = json.loads(cert_dict)
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

def normalize_gemini_model_name(name: str) -> str:
    """Convert colloquial model names (e.g. '3.5 flash lite') to official Gemini API identifiers."""
    cleaned = name.strip().lower().replace(" ", "-").replace("_", "-")
    if not cleaned.startswith("gemini-"):
        cleaned = f"gemini-{cleaned}"
    return cleaned

_primary_model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_MODELS = list(dict.fromkeys([
    normalize_gemini_model_name(_primary_model),
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.6-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
]))

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
        logger.error(f"Failed to initialize Google GenAI client: {exc}")
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


class FoodItemVerdict(BaseModel):
    name_en: str = Field(default="", description="Clean, standardized English food name (e.g. 'Fresh Tomato', 'Lays Chips', 'Paneer Curry', 'Banana').")
    name_hi: str = Field(default="", description="Clean Hindi name in Devanagari script (e.g. 'ताज़ा टमाटर', 'लेज़ चिप्स', 'पनीर करी', 'केला').")
    category: FoodCategory = Field(default=FoodCategory.COOKED_DISH, description="Culinary category.")
    shelf_life_days: int = Field(default=2, description="Estimated shelf life days for fresh items/produce/cooked food. Set to 0 if packaged item requiring printed expiry date.")
    requires_expiry_date: bool = Field(default=False, description="Set to true ONLY for manufactured, packaged goods, sealed commercial snacks/drinks/cans that have printed expiry dates. Set to false for fresh produce, fruits, fresh milk, vegetables, cooked meals, dishes made before.")
    storage_tip: str = Field(default="", description="Practical household refrigerator storage advice.")


class MultiFoodClassificationVerdict(BaseModel):
    is_food: bool = Field(default=True, description="True if photo contains at least one food, grocery, produce, or kitchen item.")
    items: List[FoodItemVerdict] = Field(default_factory=list, description="List of all distinct food items recognized in the photo.")
    message: str = Field(default="", description="Brief description of what was identified.")


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


class PhotoScanRequest(BaseModel):
    image: str = Field(..., description="Base64 encoded data URI or raw base64 string of the food photo")
    mime_type: Optional[str] = Field(default="image/jpeg", description="MIME type of the photo (e.g. image/jpeg, image/png, image/webp)")
    fridge_id: Optional[str] = Field(default=None, description="Active fridge ID to auto-add fresh items to")
    timezone: Optional[str] = Field(default=None, description="Client timezone for freshness calculation")


class CommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=300, description="Natural language user command")


class RescueRequest(BaseModel):
    items: List[str] = Field(default_factory=list, description="List of expiring or leftover food ingredients")


class RescueRecipe(BaseModel):
    recipe_name: str = Field(description="Appetizing, authentic recipe title in English")
    recipe_name_hi: str = Field(description="Recipe title in Hindi (Devanagari script)")
    meal_type: str = Field(default="Homestyle Meal", description="Category: 'Food & Drink Combo', 'Savory Food', 'Beverage / Drink', or 'Homestyle Meal'")
    meal_type_hi: Optional[str] = Field(default="घर का भोजन", description="Meal type category in Hindi")
    pairing_notes: Optional[str] = Field(default="", description="Explanation of culinary pairing or rationale for separating drink and food items")
    pairing_notes_hi: Optional[str] = Field(default="", description="Explanation of culinary pairing or rationale in Hindi (Devanagari script)")
    prep_time_minutes: int = Field(default=20, description="Total preparation and cooking time in minutes")
    ingredients_used: List[str] = Field(default_factory=list, description="Expiring ingredients incorporated into the dish")
    ingredients_used_hi: List[str] = Field(default_factory=list, description="Expiring ingredients in Hindi (Devanagari script)")
    corrected_ingredients: List[str] = Field(default_factory=list, description="Corrected ingredient names resolving user typos")
    pantry_items_needed: List[str] = Field(default_factory=list, description="Common kitchen pantry spices/staples needed")
    pantry_items_needed_hi: List[str] = Field(default_factory=list, description="Common kitchen pantry spices/staples needed in Hindi (Devanagari script)")
    steps: List[str] = Field(default_factory=list, description="Clear, numbered step-by-step cooking instructions in English")
    steps_hi: List[str] = Field(default_factory=list, description="Clear, numbered step-by-step cooking instructions in authentic Hindi (Devanagari script)")
    chef_tip: str = Field(default="", description="Pro home chef tip for zero-waste or flavor enhancement in English")
    chef_tip_hi: str = Field(default="", description="Pro home chef tip for zero-waste or flavor enhancement in Hindi ('दादी माँ का नुस्खा')")


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
    days_elapsed: int = 0
    is_expired: bool
    is_expiring_soon: bool
    requires_expiry_date: bool
    storage_tip: str
    last_synced_at: Optional[str] = None


class ClassifyResponse(BaseModel):
    is_food: bool
    name_en: str = ""
    name_hi: str = ""
    category: str = "cooked_dish"
    shelf_life_days: Optional[int] = None
    requires_expiry_date: bool = False
    storage_tip: str = ""
    message: Optional[str] = None
    message_hi: Optional[str] = None
    analyzed_by: str = "Gemini AI"
    items: List[dict] = Field(default_factory=list)
    auto_added: List[InventoryItem] = Field(default_factory=list)
    needs_expiry: List[dict] = Field(default_factory=list)
    scanner_failed: bool = False
    manual_confirmation_required: bool = False


class AuthLoginRequest(BaseModel):
    fridge_id: str
    pin: str
    timezone: Optional[str] = None


class AuthCreateRequest(BaseModel):
    fridge_id: str
    pin: str
    timezone: Optional[str] = None


# ============================================================================
# 6. STORAGE & MULTI-TENANT FRIDGE SESSIONS (ENHANCED COOKIE SECURITY)
# ============================================================================

_IN_MEMORY_FRIDGES: Dict[str, dict] = {}
_IN_MEMORY_SESSIONS: Dict[str, dict] = {}
_IN_MEMORY_INVENTORIES: Dict[str, Dict[str, dict]] = {}

SESSION_SECRET = os.getenv(
    "SESSION_SECRET",
    "FridgeInventory_Secret_Session_Signer_v2_2026"
)

def sign_session_token(token: str) -> str:
    """Sign token with HMAC-SHA256 to prevent cookie tampering."""
    sig = hmac.new(SESSION_SECRET.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return f"{token}.{sig}"

def verify_and_extract_token(cookie_or_header: Optional[str]) -> Optional[str]:
    """Verify HMAC signature or extract raw token safely."""
    if not cookie_or_header:
        return None
    val = cookie_or_header.strip()
    if val.startswith("Bearer "):
        val = val[7:].strip()
    if not val:
        return None

    # Verify signed token format 'raw_token.signature'
    if "." in val:
        parts = val.split(".", 1)
        raw_token, provided_sig = parts[0], parts[1]
        expected_sig = hmac.new(SESSION_SECRET.encode("utf-8"), raw_token.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
        if hmac.compare_digest(expected_sig, provided_sig):
            return raw_token
        return None

    # Direct raw token support (for legacy sessions or API headers)
    if len(val) >= 20:
        return val

    return None

def set_session_cookie(response: Response, request: Request, raw_token: str):
    """Set 30-day signed persistent session cookie with secure transport detection."""
    signed_value = sign_session_token(raw_token)
    is_https = (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto") == "https"
        or request.headers.get("x-forwarded-ssl") == "on"
    )
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=signed_value,
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=is_https,
        path="/",
    )

def get_current_session_data(request: Request) -> Optional[dict]:
    """Retrieve full session dict from signed cookie or Authorization headers."""
    cookie_val = request.cookies.get(SESSION_COOKIE_NAME)
    auth_header = request.headers.get("Authorization") or request.headers.get("X-Fridge-Session")

    token = verify_and_extract_token(cookie_val) or verify_and_extract_token(auth_header)
    if not token:
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
                fid = data.get("fridge_id")
                if fid:
                    return {
                        "fridge_id": fid,
                        "token": token,
                        "pin": data.get("pin", ""),
                    }
        except Exception as exc:
            logger.error(f"Error checking Firestore session: {exc}")

    if token_hash in _IN_MEMORY_SESSIONS:
        sess = _IN_MEMORY_SESSIONS[token_hash]
        if sess.get("revoked"):
            return None
        if sess.get("expires_at") and sess["expires_at"] <= now_utc:
            return None
        fid = sess.get("fridge_id")
        if fid:
            return {
                "fridge_id": fid,
                "token": token,
                "pin": sess.get("pin", ""),
            }

    return None

def get_current_fridge_id(request: Request) -> Optional[str]:
    """Retrieve verified fridge_id from current active session."""
    sess = get_current_session_data(request)
    return sess.get("fridge_id") if sess else None


def require_authenticated_fridge(request: Request, requested_fridge_id: Optional[str] = None) -> str:
    """
    Enforces strict fridge authorization:
    - Verifies that a cryptographically signed session cookie or valid Authorization / X-Fridge-Session
      token exists and belongs to an active, unexpired fridge session.
    - If a fridge ID is passed in the URL (query parameter) or request body, ensures it strictly and
      case-sensitively matches the authenticated session's fridge ID.
    - NEVER treats knowing or passing a fridge ID alone as proof of permission.
    - Raises HTTP 401 Unauthorized if no valid session is present.
    - Raises HTTP 403 Forbidden if the requested fridge ID does not match the authenticated session.
    """
    sess = get_current_session_data(request)
    if not sess or not sess.get("fridge_id"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please log in with your Fridge ID and PIN to access or manage this fridge.",
            headers={"WWW-Authenticate": "Cookie"},
        )
    auth_fid = str(sess["fridge_id"]).strip()
    if requested_fridge_id:
        req_clean = str(requested_fridge_id).strip()
        # Strictly case-sensitive fridge identification
        if req_clean and req_clean != auth_fid:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Your active session is authorized for fridge '{auth_fid}', not '{req_clean}'.",
            )
    return auth_fid


def update_fridge_time_and_inventory(fridge_id: Optional[str], tz_name: Optional[str] = None) -> List[InventoryItem]:
    """
    Core automated time-tracking & inventory freshness sync engine:
    - Global multi-country support: calculates freshness in the fridge's local timezone.
    - Updates fridge metadata (last_active_at, last_sync_date, timezone).
    - For every stored food item, compares current local date with the day it was added (added_date).
    - Recomputes days_elapsed, days_left, is_expired, and is_expiring_soon.
    - Synchronizes any modified values back to Firestore and in-memory caches.
    """
    if not fridge_id:
        return []

    db, _ = init_firestore()

    # Determine fridge timezone: parameter > Firestore doc > in-memory doc > UTC
    active_tz_name = tz_name
    if not active_tz_name and db is not None:
        try:
            fdoc = db.collection("fridges").document(fridge_id).get()
            if fdoc.exists:
                active_tz_name = (fdoc.to_dict() or {}).get("timezone")
        except Exception:
            pass
    if not active_tz_name:
        active_tz_name = _IN_MEMORY_FRIDGES.get(fridge_id, {}).get("timezone", "UTC")

    tz = resolve_tz(active_tz_name)
    now_local = datetime.now(tz)
    today = now_local.date()
    tz_code = now_local.tzname() or active_tz_name or "UTC"
    now_str = now_local.strftime(f"%Y-%m-%d %H:%M:%S {tz_code}")

    # Update fridge metadata
    if db is not None:
        try:
            db.collection("fridges").document(fridge_id).set({
                "last_active_at": now_str,
                "last_sync_date": today.isoformat(),
                "timezone": active_tz_name,
            }, merge=True)
        except Exception as exc:
            logger.warning(f"Failed to update fridge sync metadata for {fridge_id}: {exc}")
    else:
        if fridge_id in _IN_MEMORY_FRIDGES:
            _IN_MEMORY_FRIDGES[fridge_id]["last_active_at"] = now_str
            _IN_MEMORY_FRIDGES[fridge_id]["last_sync_date"] = today.isoformat()
            _IN_MEMORY_FRIDGES[fridge_id]["timezone"] = active_tz_name

    # Fetch inventory documents
    raw_items = []
    if db is not None:
        try:
            docs = db.collection("fridges").document(fridge_id).collection("inventory").stream()
            for doc in docs:
                d = doc.to_dict() or {}
                d["id"] = doc.id
                raw_items.append(d)
        except Exception as exc:
            logger.error(f"Failed to fetch inventory from Firestore for {fridge_id}: {exc}")
            raw_items = list(_IN_MEMORY_INVENTORIES.get(fridge_id, {}).values())
    else:
        raw_items = list(_IN_MEMORY_INVENTORIES.get(fridge_id, {}).values())

    results: List[InventoryItem] = []
    updates_to_persist = {}

    for item in raw_items:
        doc_id = str(item.get("id", uuid.uuid4().hex[:8]))

        # 1. Parse added date and calculate days elapsed since added
        added_raw = str(item.get("added_date", "")).strip()[:10]
        try:
            added_d = datetime.strptime(added_raw, "%Y-%m-%d").date()
        except Exception:
            added_d = today
            added_raw = today.isoformat()

        days_elapsed = max(0, (today - added_d).days)

        # 2. Parse or calculate expiry date; detect and repair invalid expiry dates (unparseable or before added_date)
        raw_exp = str(item.get("expiry_date", "")).strip()[:10]
        exp_d = None
        if raw_exp:
            try:
                exp_d = datetime.strptime(raw_exp, "%Y-%m-%d").date()
            except Exception:
                exp_d = None

        if not exp_d or exp_d < added_d:
            shelf_days = item.get("shelf_life_days")
            if not shelf_days or shelf_days <= 0:
                match = lookup_local_database(str(item.get("name_en", "")))
                shelf_days = match[0] if match else 3
            exp_d = added_d + timedelta(days=shelf_days)
            raw_exp = exp_d.isoformat()


        days_left = (exp_d - today).days
        is_expired = days_left < 0
        is_expiring_soon = (0 <= days_left <= 2)

        fields_to_update = {
            "days_left": days_left,
            "days_elapsed": days_elapsed,
            "is_expired": is_expired,
            "is_expiring_soon": is_expiring_soon,
            "added_date": added_raw,
            "expiry_date": raw_exp,
            "last_synced_at": now_str,
        }

        # Check if values changed compared to stored doc
        if (item.get("days_left") != days_left or
            item.get("days_elapsed") != days_elapsed or
            item.get("is_expired") != is_expired or
            item.get("expiry_date") != raw_exp):
            updates_to_persist[doc_id] = fields_to_update

        # Update in-memory cache if present
        if fridge_id in _IN_MEMORY_INVENTORIES and doc_id in _IN_MEMORY_INVENTORIES[fridge_id]:
            _IN_MEMORY_INVENTORIES[fridge_id][doc_id].update(fields_to_update)

        raw_name = str(item.get("name_en", "")).strip()
        safe_name_en = raw_name if raw_name and raw_name.lower() not in ("none", "null") else "Food Item"
        safe_name_hi = str(item.get("name_hi", "")).strip() or safe_name_en

        results.append(
            InventoryItem(
                id=doc_id,
                name_en=safe_name_en,
                name_hi=safe_name_hi,
                category=item.get("category", "vegetable"),
                added_date=added_raw,
                expiry_date=raw_exp,
                days_left=days_left,
                days_elapsed=days_elapsed,
                is_expired=is_expired,
                is_expiring_soon=is_expiring_soon,
                requires_expiry_date=bool(item.get("requires_expiry_date", False)),
                storage_tip=item.get("storage_tip", ""),
                last_synced_at=now_str,
            )
        )

    # Persist changes to Firestore
    if db is not None and updates_to_persist:
        try:
            batch = db.batch()
            inv_col = db.collection("fridges").document(fridge_id).collection("inventory")
            for d_id, u_fields in updates_to_persist.items():
                batch.update(inv_col.document(d_id), u_fields)
            batch.commit()
            logger.info(f"Persisted time-tracking expiry sync for {len(updates_to_persist)} items in {fridge_id}")
        except Exception as exc:
            logger.warning(f"Batch update failed in {fridge_id}, attempting individual updates: {exc}")
            for d_id, u_fields in updates_to_persist.items():
                try:
                    db.collection("fridges").document(fridge_id).collection("inventory").document(d_id).update(u_fields)
                except Exception:
                    pass

    results.sort(key=lambda x: x.days_left)
    return results


def get_fridge_inventory(fridge_id: Optional[str], tz_name: Optional[str] = None) -> List[InventoryItem]:
    """Retrieve and dynamically time-sync all inventory items belonging to a specific fridge."""
    return update_fridge_time_and_inventory(fridge_id, tz_name=tz_name)


def save_fridge_inventory_item(fridge_id: str, item_data: InventoryItemCreate, client_tz: Optional[str] = None) -> InventoryItem:
    """Save an item to a specific fridge's inventory using its local timezone."""
    if not fridge_id:
        raise ValueError("fridge_id cannot be empty")

    db, _ = init_firestore()

    # Determine fridge timezone
    active_tz_name = client_tz
    if not active_tz_name and db is not None:
        try:
            fdoc = db.collection("fridges").document(fridge_id).get()
            if fdoc.exists:
                active_tz_name = (fdoc.to_dict() or {}).get("timezone")
        except Exception:
            pass
    if not active_tz_name:
        active_tz_name = _IN_MEMORY_FRIDGES.get(fridge_id, {}).get("timezone", "UTC")

    tz = resolve_tz(active_tz_name)
    now_local = datetime.now(tz)
    today = now_local.date()
    tz_code = now_local.tzname() or active_tz_name or "UTC"
    now_str = now_local.strftime(f"%Y-%m-%d %H:%M:%S {tz_code}")

    if item_data.expiry_date:
        try:
            exp_date = datetime.strptime(item_data.expiry_date.strip(), "%Y-%m-%d").date()
            if exp_date < today:
                # Invalid past expiry date for newly added item made/stored today - recalculate accurately
                days = item_data.shelf_life_days if (item_data.shelf_life_days and item_data.shelf_life_days > 0) else 3
                exp_date = today + timedelta(days=days)
        except Exception:
            days = item_data.shelf_life_days if (item_data.shelf_life_days and item_data.shelf_life_days > 0) else 3
            exp_date = today + timedelta(days=days)
    else:
        days = item_data.shelf_life_days if (item_data.shelf_life_days and item_data.shelf_life_days > 0) else 3
        exp_date = today + timedelta(days=days)

    days_left = max(0, (exp_date - today).days)
    doc_id = uuid.uuid4().hex[:10]
    safe_name_en = (item_data.name_en or "").strip()
    if not safe_name_en or safe_name_en.lower() in ("none", "null"):
        safe_name_en = "Fresh Food Item"
    safe_name_hi = (item_data.name_hi or "").strip() or safe_name_en

    payload = {
        "id": doc_id,
        "name_en": safe_name_en,
        "name_hi": safe_name_hi,
        "category": item_data.category.strip(),
        "added_date": today.isoformat(),
        "expiry_date": exp_date.isoformat(),
        "days_left": days_left,
        "days_elapsed": 0,
        "is_expired": False,
        "is_expiring_soon": (0 <= days_left <= 2),
        "requires_expiry_date": item_data.requires_expiry_date,
        "storage_tip": item_data.storage_tip.strip(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_synced_at": now_str,
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

    return InventoryItem(
        id=doc_id,
        name_en=safe_name_en,
        name_hi=safe_name_hi,
        category=item_data.category.strip(),
        added_date=today.isoformat(),
        expiry_date=exp_date.isoformat(),
        days_left=days_left,
        days_elapsed=0,
        is_expired=False,
        is_expiring_soon=(0 <= days_left <= 2),
        requires_expiry_date=item_data.requires_expiry_date,
        storage_tip=item_data.storage_tip.strip(),
        last_synced_at=now_str,
    )



def remove_fridge_inventory_item(fridge_id: str, item_id: str) -> bool:
    """Remove item from a specific fridge's inventory."""
    if not fridge_id or not item_id:
        return False

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
    "lays", "kurkure", "pringles", "doritos", "cheetos", "coke", "pepsi", "sprite",
    "fanta", "thumsup", "frooti", "maaza", "maggi", "oreo", "kitkat", "chocolate",
    "chocolates", "candy", "candies", "noodles", "cereal", "muesli", "tetrapack",
}

PACKAGED_SNACK_PHRASES = [
    "red bull", "redbull", "energy drink", "lays", "potato chips", "banana chips",
    "potato bites", "french fries", "onion rings", "cheese sticks", "veggie fingers",
    "kurkure", "doritos", "pringles", "cheetos", "maggi", "instant noodles",
    "oreo", "dairy milk", "kit kat", "kitkat", "chocolate bar", "coca cola",
    "pepsi", "sprite", "thums up", "thumsup", "frooti", "maaza"
]

# Comprehensive culinary dictionary and typo map for zero-waste recipe and food entry
CULINARY_TYPO_MAP: Dict[str, str] = {
    # Chocolates & Sweets
    "choklate": "Chocolate",
    "choclate": "Chocolate",
    "choclet": "Chocolate",
    "chocklate": "Chocolate",
    "choc": "Chocolate",
    # Breads & Bakery
    "bred": "Bread",
    "braed": "Bread",
    "bready": "Bread",
    "paav": "Pav / Bread",
    "pav": "Pav / Bread",
    "bun": "Bun / Bread",
    # Cheeses & Dairy
    "cheez": "Cheese",
    "chese": "Cheese",
    "cheeze": "Cheese",
    "cheddar": "Cheddar Cheese",
    "mozerella": "Mozzarella Cheese",
    "mozzarella": "Mozzarella Cheese",
    "paneeer": "Paneer",
    "paner": "Paneer",
    "panner": "Paneer",
    "dahi": "Curd",
    "curds": "Curd",
    "yoghurt": "Yogurt",
    "yogert": "Yogurt",
    "makhan": "Butter",
    "buttr": "Butter",
    "malai": "Cream / Malai",
    # Seasonings & Spices
    "peri peri": "Peri Peri",
    "piri piri": "Peri Peri",
    "periperi": "Peri Peri",
    "piripiri": "Peri Peri",
    "origano": "Oregano",
    "oregano": "Oregano",
    "chilli flakes": "Chilli Flakes",
    "chili flakes": "Chilli Flakes",
    # Vegetables & Produce
    "tommato": "Tomato",
    "tamatar": "Tomato",
    "tomatos": "Tomatoes",
    "potatos": "Potato",
    "potatoe": "Potato",
    "aloo": "Potato",
    "alloo": "Potato",
    "onions": "Onion",
    "piyaz": "Onion",
    "pyaz": "Onion",
    "pyaj": "Onion",
    "spinch": "Spinach",
    "palak": "Spinach",
    "spinich": "Spinach",
    "gajar": "Carrot",
    "carot": "Carrot",
    "shimla mirch": "Capsicum",
    "capsicum": "Capsicum",
    "kheera": "Cucumber",
    "adrak": "Ginger",
    "gingr": "Ginger",
    "lahsun": "Garlic",
    "garlick": "Garlic",
    "dhaniya": "Coriander",
    "coriender": "Coriander",
    "curry leaves": "Curry Leaves",
    "kadi patta": "Curry Leaves",
    "pudina": "Mint",
    # Snacks & Beverages
    "redbull": "Red Bull",
    "red bull": "Red Bull",
    "lays": "Lays Chips",
    "chips": "Potato Chips",
    "biskit": "Biscuits",
    "biscit": "Biscuits",
    "biscut": "Biscuits",
    "chikn": "Chicken",
    "chiken": "Chicken",
    "machli": "Fish",
    "anda": "Eggs",
    "ande": "Eggs",
}

_FOOD_LEXICON: Optional[List[str]] = None

def _get_known_food_lexicon() -> List[str]:
    lex = set(CULINARY_TYPO_MAP.values())
    if isinstance(EXPIRY_DATABASE, dict):
        for key, val in EXPIRY_DATABASE.items():
            lex.add(str(key).title())
            if isinstance(val, (list, tuple)) and len(val) >= 2:
                en_name = str(val[1])
                lex.add(en_name)
                for word in en_name.split():
                    if len(word) >= 4:
                        lex.add(word)
    elif isinstance(EXPIRY_DATABASE, list):
        for item in EXPIRY_DATABASE:
            if isinstance(item, dict) and "name_en" in item:
                lex.add(item["name_en"])
    return sorted(list(lex))

def correct_single_ingredient(raw_name: str) -> Tuple[str, bool]:
    """Corrects spelling typos in an ingredient using mapping + fuzzy matching."""
    global _FOOD_LEXICON
    clean = raw_name.strip()
    if not clean:
        return clean, False
    
    clean_lower = clean.lower()
    # 1. Exact match in typo map
    if clean_lower in CULINARY_TYPO_MAP:
        return CULINARY_TYPO_MAP[clean_lower], True

    # 2. Check phrase substrings in typo map (e.g. 'extra peri peri')
    for typo, correction in CULINARY_TYPO_MAP.items():
        if typo == clean_lower or f" {typo} " in f" {clean_lower} ":
            replaced = re.sub(rf'\b{re.escape(typo)}\b', correction, clean, flags=re.IGNORECASE)
            return replaced, True

    # 3. Fuzzy match using difflib against known food terms
    if _FOOD_LEXICON is None:
        _FOOD_LEXICON = _get_known_food_lexicon()

    matches = difflib.get_close_matches(clean_lower, [w.lower() for w in _FOOD_LEXICON], n=1, cutoff=0.76)
    if matches:
        match_lower = matches[0]
        for canon in _FOOD_LEXICON:
            if canon.lower() == match_lower:
                return canon, True

    return clean.title(), False

def correct_ingredients_list(items: List[str]) -> Tuple[List[str], Dict[str, str]]:
    """Takes a list of raw ingredients, corrects spelling, and returns (corrected_list, corrections_map)."""
    corrected_list = []
    corrections_map = {}
    for item in items:
        clean = str(item).strip()
        if not clean:
            continue
        corr, was_changed = correct_single_ingredient(clean)
        corrected_list.append(corr)
        if was_changed and corr.lower() != clean.lower():
            corrections_map[clean] = corr
    return corrected_list, corrections_map

def lookup_local_database(phrase: str) -> Optional[Tuple[int, str, str, bool]]:
    """Check EXPIRY_DATABASE for exact zero-latency local match.
    Does NOT greedily match single words from multi-word phrases to avoid corrupting packaged items.
    """
    if not EXPIRY_DATABASE:
        return None

    clean = phrase.strip().lower()
    if clean in EXPIRY_DATABASE:
        days, en, hi = EXPIRY_DATABASE[clean]
        return days, en, hi, False

    norm = re.sub(r"[^a-zA-Z\u0900-\u097f]+", " ", clean).strip()
    if norm in EXPIRY_DATABASE:
        days, en, hi = EXPIRY_DATABASE[norm]
        return days, en, hi, False

    return None


# ============================================================================
# 8. AI CLASSIFICATION & RECIPE RESCUE CORE
# ============================================================================

CLASSIFY_PROMPT_TEMPLATE = """You are an expert culinary AI for a household kitchen and fridge inventory app ("FridgeInventory").
Analyze the food input phrase: "{query}"

Classification Rules:
1. Multi-word packaged, processed, or frozen items (CRITICAL):
   - If the user types a prepared commercial snack or frozen product (e.g. 'potato bites', 'banana chips', 'onion rings', 'french fries', 'cheese sticks', 'veggie fingers', 'potato wedges', 'maggi noodles', 'lays chips'), classify the ENTIRE phrase as the item.
   - NEVER truncate, simplify, or reduce a compound food product to its raw agricultural ingredient (e.g. NEVER classify 'potato bites' as raw 'Potato', or 'banana chips' as 'Banana').
   - For processed/packaged snacks or frozen goods: set category=snack_bakery or packaged_grocery, requires_expiry_date=true, and shelf_life_days=0.
2. Spelling & Slang:
   - Correct common speech-to-text slips or Hinglish transliterations (e.g. 'tamatar' -> 'Tomato', 'panir'/'paneer' -> 'Paneer', 'palak' -> 'Spinach', 'dahi' -> 'Curd / Dahi', 'bhindee' -> 'Okra (Bhindi)', 'amul makkhan' -> 'Amul Butter', 'lays' -> 'Lays Chips', 'chawal' -> 'Cooked Rice').
3. is_food check:
   - Return is_food=true ONLY for edible/drinkable food, produce, dairy, meat/fish/poultry, eggs, grains, lentils, flours, spices, snacks, beverages, and cooked meals.
   - Return is_food=false for objects, electronics, medicines, cleaners, cosmetics, people, vehicles, furniture, or non-food words.
4. If is_food is false:
   - category=not_food, shelf_life_days=0, requires_expiry_date=false, name_en="", name_hi="", storage_tip="".
5. Expiry Date Requirement (requires_expiry_date):
   - Set requires_expiry_date=false for ALL everyday household food items, groceries, vegetables, fruits, dairy (fresh milk, paneer, curd, cheese, butter, cream), bread, eggs, meat, fish, cooked meals, rice, dal, flour, and staples. Give them an accurate, realistic shelf_life_days estimate (e.g., Milk: 3 days, Paneer: 3 days, Bread: 4 days, Curd: 5 days, Spinach: 3 days, Tomato: 6 days, Eggs: 14 days, Cooked Dal: 3 days) so the app automatically calculates their expiry and adds them immediately without interrupting the user!
   - Set requires_expiry_date=true ONLY for commercial sealed manufactured snacks and shelf-stable packaged foods that have printed manufacturer expiry dates (e.g., potato chips, canned goods, packaged frozen snacks like potato bites, sauce bottles, packaged biscuits).
   - If in doubt, default to requires_expiry_date=false with a reasonable shelf life so the user experience is smooth and fast.
6. Shelf Life:
   - For fresh produce, dairy, bread, or cooked home dishes (requires_expiry_date=false), provide a realistic estimated shelf life in days in an Indian refrigerator.
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

    # 2. Check for packaged snacks & brand drinks heuristic (e.g. "lays", "red bull", "potato bites")
    tokens = set(clean_phrase.lower().split())
    clean_lower = clean_phrase.lower()
    is_packaged = bool(tokens & PACKAGED_SNACK_WORDS) or any(p in clean_lower for p in PACKAGED_SNACK_PHRASES)
    if is_packaged:
        is_drink = any(w in clean_lower for w in ["red bull", "redbull", "energy drink", "coke", "pepsi", "sprite", "fanta", "thums up", "thumsup", "soda", "juice"])
        cat = "beverage" if is_drink else "snack_bakery"
        return ClassifyResponse(
            is_food=True,
            name_en=clean_phrase.title(),
            name_hi=clean_phrase,
            category=cat,
            shelf_life_days=None,
            requires_expiry_date=True,
            storage_tip="Store in a cool dry place or refrigerate. Check printed package date.",
            message="Packaged product detected. Printed expiry date required.",
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

    # If item is not in verified database and Gemini failed: request manual confirmation
    return ClassifyResponse(
        is_food=True,
        name_en=clean_phrase.title(),
        name_hi=clean_phrase,
        category="cooked_dish",
        shelf_life_days=None,
        requires_expiry_date=True,
        storage_tip="Store refrigerated in a clean container.",
        message=f"'{clean_phrase.title()}' requires shelf-life confirmation.",
        message_hi=f"'{clean_phrase}' के लिए शेल्फ-लाइफ की पुष्टि आवश्यक है।",
        analyzed_by="Manual Confirmation Required",
        manual_confirmation_required=True,
    )


PHOTO_CLASSIFY_PROMPT = """You are an expert culinary AI and food safety inspector for an Indian household smart refrigerator application (FridgeInventory).
Analyze this food/grocery photo carefully.

Instructions:
1. Identify all distinct food items, produce, groceries, cooked dishes, snacks, or beverages present in the photo.
2. If the photo does NOT contain food (e.g., electronic gadget, toy, furniture, human, animal, medicine, cleaner, document):
   - Set is_food=false, items=[], message="The photo does not appear to contain an edible food or kitchen grocery item."
3. If it IS food:
   - Set is_food=true.
   - For EACH food item detected:
     * Provide standard English name in `name_en` (e.g. "Paneer Butter Masala", "Fresh Tomato", "Aloo Paratha", "Apples", "Cooked Dal", "Lays Chips", "Amul Butter").
     * Provide Hindi translation in Devanagari script in `name_hi` (e.g. "पनीर बटर मसाला", "टमाटर", "आलू पराठा", "सेब", "पकी हुई दाल", "लेज़ चिप्स", "अमुल मक्खन").
     * Categorize into one of: 'dairy', 'produce', 'vegetable', 'fruit', 'cooked_dish', 'meat_fish_egg', 'grain_pulse_flour', 'snack_bakery', 'beverage', 'spice_condiment', 'packaged_grocery', 'oil_sugar_fat'.
     * Determine `requires_expiry_date`:
       - Set requires_expiry_date=true ONLY for commercial sealed manufactured packaged snacks/cans/bottles that have printed manufacturer expiration dates.
       - Set requires_expiry_date=false for all fresh produce, fruits, fresh vegetables, fresh dairy, eggs, breads, homemade dishes, cooked meals, opened leftovers.
     * Determine `shelf_life_days`:
       - If requires_expiry_date=false: Accurate estimated refrigerator shelf life in days from today (e.g. freshly cooked dal/curry/rice: 2 days, fresh milk: 2 days, paneer: 3 days, leafy greens: 3 days, apples: 21 days, eggs: 21 days).
       - If requires_expiry_date=true: Set shelf_life_days=0.
     * Provide practical Indian household refrigerator `storage_tip` (e.g. "Keep refrigerated in an airtight container. Consume within 2 days.").
"""

def classify_food_photo(
    image_data: str,
    mime_type: Optional[str] = "image/jpeg",
    fridge_id: Optional[str] = None,
    client_tz: Optional[str] = None,
) -> ClassifyResponse:
    """Classify a food photo using Google GenAI vision with fallback to smart food inspector.
    Fresh food items without printed expiry dates are automatically added to the active fridge.
    Packaged snacks or items requiring printed expiry dates are queued for date confirmation.
    """
    clean_data = (image_data or "").strip()
    if not clean_data:
        raise HTTPException(status_code=400, detail="Photo data cannot be empty.")

    detected_mime = mime_type or "image/jpeg"
    if clean_data.startswith("data:") and ";base64," in clean_data:
        header, b64_str = clean_data.split(";base64,", 1)
        detected_mime = header.replace("data:", "").strip() or detected_mime
    else:
        b64_str = clean_data

    try:
        raw_bytes = base64.b64decode(b64_str)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid base64 image data: {exc}")

    client = get_gemini_client()
    if client is not None and types is not None:
        try:
            part = types.Part.from_bytes(data=raw_bytes, mime_type=detected_mime)
        except Exception as exc:
            logger.warning(f"Failed to create Part from image bytes: {exc}")
            part = None

        if part is not None:
            for model_name in GEMINI_MODELS:
                try:
                    response = client.models.generate_content(
                        model=model_name,
                        contents=[part, PHOTO_CLASSIFY_PROMPT],
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=MultiFoodClassificationVerdict,
                            temperature=0.1,
                        ),
                    )
                    raw_text = (response.text or "").strip()
                    verdict = MultiFoodClassificationVerdict.model_validate_json(raw_text)

                    if not verdict.is_food or not verdict.items:
                        return ClassifyResponse(
                            is_food=False,
                            name_en="",
                            name_hi="",
                            category="not_food",
                            shelf_life_days=None,
                            requires_expiry_date=False,
                            storage_tip="",
                            message=verdict.message or "The photo does not appear to contain an edible food or kitchen grocery item.",
                            analyzed_by="Gemini Vision AI",
                            items=[],
                            auto_added=[],
                            needs_expiry=[],
                        )

                    valid_items = [
                        it for it in verdict.items
                        if it.name_en and it.category != FoodCategory.NOT_FOOD
                    ]
                    if not valid_items:
                        return ClassifyResponse(
                            is_food=False,
                            name_en="",
                            name_hi="",
                            category="not_food",
                            shelf_life_days=None,
                            requires_expiry_date=False,
                            storage_tip="",
                            message="The photo does not appear to contain an edible food or kitchen grocery item.",
                            analyzed_by="Gemini Vision AI",
                            items=[],
                            auto_added=[],
                            needs_expiry=[],
                        )

                    auto_added: List[InventoryItem] = []
                    needs_expiry: List[dict] = []
                    all_items_dicts: List[dict] = []

                    for it in valid_items:
                        cat_str = it.category.value if hasattr(it.category, "value") else str(it.category)
                        item_dict = {
                            "name_en": it.name_en,
                            "name_hi": it.name_hi or it.name_en,
                            "category": cat_str,
                            "shelf_life_days": it.shelf_life_days if not it.requires_expiry_date else None,
                            "requires_expiry_date": it.requires_expiry_date,
                            "storage_tip": it.storage_tip or "Store refrigerated.",
                        }
                        all_items_dicts.append(item_dict)

                        if not it.requires_expiry_date:
                            # Fresh items without printed expiry date are auto-added!
                            if fridge_id:
                                try:
                                    item_create = InventoryItemCreate(
                                        name_en=it.name_en,
                                        name_hi=it.name_hi or it.name_en,
                                        category=cat_str,
                                        shelf_life_days=it.shelf_life_days if it.shelf_life_days > 0 else 2,
                                        requires_expiry_date=False,
                                        expiry_date=None,
                                        storage_tip=it.storage_tip or "Keep refrigerated in an airtight container.",
                                    )
                                    saved = save_fridge_inventory_item(fridge_id, item_create, client_tz=client_tz)
                                    auto_added.append(saved)
                                except Exception as save_err:
                                    logger.error(f"Failed to auto-add fresh item {it.name_en}: {save_err}")
                                    needs_expiry.append(item_dict)
                            else:
                                needs_expiry.append(item_dict)
                        else:
                            # Packaged items requiring printed expiry date
                            needs_expiry.append(item_dict)

                    primary = valid_items[0]
                    cat_primary = primary.category.value if hasattr(primary.category, "value") else str(primary.category)

                    if auto_added and needs_expiry:
                        msg = f"Auto-added {len(auto_added)} fresh item(s) to fridge. {len(needs_expiry)} item(s) require package expiry date."
                    elif auto_added:
                        msg = f"Successfully analyzed photo and auto-added {len(auto_added)} fresh item(s) to your fridge!"
                    elif needs_expiry:
                        msg = f"Analyzed {len(needs_expiry)} item(s). Please verify the printed expiry dates."
                    else:
                        msg = "Visual analysis complete."

                    return ClassifyResponse(
                        is_food=True,
                        name_en=primary.name_en,
                        name_hi=primary.name_hi or primary.name_en,
                        category=cat_primary,
                        shelf_life_days=None if primary.requires_expiry_date else primary.shelf_life_days,
                        requires_expiry_date=primary.requires_expiry_date,
                        storage_tip=primary.storage_tip or "Keep refrigerated in an airtight container.",
                        analyzed_by="Gemini Vision AI",
                        items=all_items_dicts,
                        auto_added=auto_added,
                        needs_expiry=needs_expiry,
                        message=msg,
                    )
                except Exception as exc:
                    logger.warning(f"Gemini vision model {model_name} failed: {exc}")
                    continue

    # When Gemini cannot identify the food or all configured models fail:
    # NEVER silently present a generic classification as if the food was identified correctly.
    logger.warning("Gemini Vision was unable to identify food in the photo or all vision models were unreachable.")
    return ClassifyResponse(
        is_food=False,
        name_en="",
        name_hi="",
        category="not_food",
        shelf_life_days=None,
        requires_expiry_date=False,
        storage_tip="",
        message="AI could not identify food in this photo or AI vision models were unreachable. Please retake the photo clearly or enter item details manually.",
        message_hi="AI इस फ़ोटो में भोजन की पहचान नहीं कर सका या मॉडल अनुपलब्ध हैं। कृपया साफ़ फ़ोटो लें या मैन्युअल रूप से दर्ज करें।",
        analyzed_by="Gemini Vision AI",
        items=[],
        auto_added=[],
        needs_expiry=[],
        scanner_failed=True,
        manual_confirmation_required=True,
    )


HOWTO_RECIPE_PROMPT_TEMPLATE = """You are an expert master chef and cooking instructor for 'FridgeInventory'.
A user is asking: "How to make {dish}?".
Provide an authentic, mouth-watering, homestyle step-by-step recipe to make {dish}.

Requirements:
- recipe_name: Authentic dish name in English (e.g. 'Restaurant-Style Paneer Butter Masala')
- recipe_name_hi: In authentic Hindi Devanagari script (e.g. 'शाही पनीर बटर मसाला')
- meal_type: Dish type in English ('Main Course', 'Street Food Snack', 'Beverage / Drink', 'Traditional Dessert', 'Breakfast Special')
- meal_type_hi: Dish type in Hindi ('मुख्य भोजन', 'स्वादिष्ट स्नैक', 'ताज़ा पेय', 'पारंपरिक मिठाई', 'खास नाश्ता')
- pairing_notes: What this dish pairs best with (e.g. 'Serve hot with garlic naan, tandoori roti, or jeera rice.')
- pairing_notes_hi: Pairing notes in Hindi Devanagari
- prep_time_minutes: Realistic cooking time integer (10 to 45 mins)
- ingredients_used: Key primary ingredients needed for {dish}
- ingredients_used_hi: Key primary ingredients in Hindi Devanagari
- corrected_ingredients: Key ingredients list
- pantry_items_needed: Spices, oils, aromatics, and seasoning needed with measurements
- pantry_items_needed_hi: Spices and staples in Hindi with measurements
- steps: Detailed step-by-step instructions on how to cook this recipe in English
- steps_hi: Complete step-by-step instructions in natural, fluent Hindi (Devanagari script)
- chef_tip: Grandma's secret culinary tip ('दादी माँ का सीक्रेट नुस्खा') for making this recipe perfect
- chef_tip_hi: Secret culinary tip in Hindi
"""

def detect_recipe_dish_query(items: List[str]) -> Tuple[bool, str]:
    """Detects if user is asking how to make a specific recipe (e.g. 'how to make paneer butter masala', 'biryani recipe')."""
    joined = " ".join([str(i).strip() for i in items if str(i).strip()]).strip()
    if not joined:
        return False, ""
    joined_clean = joined.lower()

    # Prefixes like 'how to make', 'how to cook', 'recipe of'
    prefixes = [
        "how to make", "how to cook", "how to prepare", "how to bake",
        "how do i make", "how can i make", "how do you make", "how to do",
        "tell me how to make", "tell me how to cook", "tell me recipe of",
        "tell me the recipe of", "recipe of", "recipe for",
        "give me recipe of", "give me recipe for", "what is the recipe of",
        "recipe", "vidhi"
    ]
    for p in prefixes:
        if joined_clean.startswith(p):
            dish = joined[len(p):].strip()
            dish = re.sub(r'^(a|an|the|please)\s+', '', dish, flags=re.I).strip(" ?.,!:")
            if dish:
                return True, dish

    # Suffixes like 'recipe', 'kaise banaye', 'banane ki vidhi'
    suffixes = [
        "recipe", "ki recipe", "kaise banaye", "kaise banayein", "kaise banate hain",
        "kaise banta hai", "banane ki vidhi", "banana sikhayein", "sikhao"
    ]
    for s in suffixes:
        if joined_clean.endswith(s):
            dish = joined[:len(joined) - len(s)].strip()
            dish = re.sub(r'^(a|an|the|please)\s+', '', dish, flags=re.I).strip(" ?.,!:")
            if dish:
                return True, dish

    # Known popular composite dishes asked directly
    known_dishes = [
        "paneer butter masala", "shahi paneer", "kadai paneer", "palak paneer", "matar paneer", "paneer tikka",
        "biryani", "chicken biryani", "veg biryani", "mutton biryani", "pulao", "jeera rice",
        "dal tadka", "dal makhani", "yellow dal", "sambar", "chole", "chana masala", "rajma",
        "pav bhaji", "samosa", "pakoda", "pakora", "maggi", "noodles", "chowmein", "fried rice",
        "kheer", "halwa", "sooji halwa", "gajar halwa", "gulab jamun", "cake", "chocolate cake",
        "masala chai", "tea", "cold coffee", "coffee", "lassi", "mango lassi", "lemonade",
        "aloo paratha", "paratha", "poha", "upma", "dosa", "idli", "pasta", "pizza",
        "butter chicken", "chicken curry", "egg curry", "fish curry"
    ]
    for kd in known_dishes:
        if kd == joined_clean or re.search(rf'\b{re.escape(kd)}\b', joined_clean):
            return True, joined.strip(" ?.,!:")

    return False, ""

def get_dish_recipe_fallback(dish: str) -> RescueRecipe:
    """Provides authentic homestyle recipes for popular dishes when Gemini API is offline or not configured."""
    dl = dish.lower().strip()
    
    # 1. PANEER BUTTER MASALA / SHAHI PANEER
    if any(k in dl for k in ["paneer butter", "shahi paneer", "paneer makhani", "butter paneer"]):
        return RescueRecipe(
            recipe_name="Restaurant-Style Paneer Butter Masala",
            recipe_name_hi="शाही पनीर बटर मसाला",
            meal_type="Main Course",
            meal_type_hi="मुख्य भोजन",
            pairing_notes="Rich, velvety tomato-cashew butter gravy pairs perfectly with hot garlic naan, tandoori roti, or fragrant jeera rice.",
            pairing_notes_hi="मखमली काजू-टमाटर की मक्खन ग्रेवी गरमा-गरम बटर नान, तंदूरी रोटी या खुशबूदार जीरा राइस के साथ बेहद स्वादिष्ट लगती है।",
            prep_time_minutes=25,
            ingredients_used=["Fresh Paneer (250g)", "Butter (2 tbsp)", "Tomatoes (3 pureed)", "Cashew Paste / Fresh Cream (3 tbsp)", "Ginger-Garlic Paste (1 tbsp)"],
            ingredients_used_hi=["ताज़ा पनीर (250 ग्राम)", "मक्खन (2 बड़े चम्मच)", "टमाटर प्यूरी (3 बड़े टमाटर)", "काजू पेस्ट / मलाई (3 बड़े चम्मच)", "अदरक-लहसुन पेस्ट (1 बड़ा चम्मच)"],
            corrected_ingredients=["Paneer", "Butter", "Tomatoes", "Cashews", "Ginger-Garlic"],
            pantry_items_needed=["1/2 tsp Cumin seeds", "1 tsp Kashmiri Red Chilli powder", "1/2 tsp Turmeric", "1 tsp Garam Masala", "1 tsp Kasuri Methi (crushed)", "1/2 tsp Sugar", "Salt to taste"],
            pantry_items_needed_hi=["1/2 छोटा चम्मच जीरा", "1 छोटा चम्मच कश्मीरी लाल मिर्च", "1/2 छोटा चम्मच हल्दी", "1 छोटा चम्मच गरम मसाला", "1 छोटा चम्मच भुनी कसूरी मेथी", "1/2 छोटा चम्मच चीनी", "स्वादानुसार नमक"],
            steps=[
                "Melt 1 tbsp butter and 1 tsp oil in a pan. Add cumin seeds, finely chopped onion, and ginger-garlic paste; sauté until fragrant and lightly golden.",
                "Pour in fresh tomato puree with Kashmiri red chilli powder, turmeric, and salt. Cook on medium heat for 7-8 minutes until butter begins to separate from the masala.",
                "Stir in smooth cashew paste (or fresh cream) along with 1/2 cup warm water to create a velvety, glossy gravy. Simmer gently for 3 minutes.",
                "Gently add soft paneer cubes. Sprinkle crushed kasuri methi, garam masala, and a pinch of sugar to balance the tomato acidity.",
                "Simmer covered on low heat for 3-4 minutes so paneer absorbs the rich flavors. Top with extra butter and coriander. Serve hot!"
            ],
            steps_hi=[
                "कड़ाही में 1 बड़ा चम्मच मक्खन और थोड़ा तेल गरम करें। जीरा, बारीक कटा प्याज और अदरक-लहसुन पेस्ट डालकर सुनहरा होने तक भूनें।",
                "टमाटर की ताज़ी प्यूरी, कश्मीरी लाल मिर्च, हल्दी और स्वादानुसार नमक डालें। मध्यम आंच पर 7-8 मिनट तक मसाला तेल छोड़ने तक पकाएं।",
                "काजू का पेस्ट या ताज़ी मलाई और 1/2 कप गुनगुना पानी मिलाकर क्रीमी ग्रेवी बनाएं। 3 मिनट तक धीमी आंच पर उबलने दें।",
                "पनीर के टुकड़े डालें। ऊपर से भुनी हुई कसूरी मेथी, गरम मसाला और हल्की मिठास के लिए चुटकी भर चीनी मिलाएं।",
                "धीमी आंच पर ढककर 3-4 मिनट तक पकाएं ताकि पनीर में ग्रेवी का स्वाद समा जाए। ऊपर से थोड़ा मक्खन और हरा धनिया डालकर गरमा-गरम परोसें!"
            ],
            chef_tip="Soak paneer cubes in warm salted water for 10 minutes before adding to the gravy to keep them exceptionally soft and spongy!",
            chef_tip_hi="पनीर को ग्रेवी में डालने से पहले 10 मिनट गुनगुने नमकीन पानी में भिगोकर रखें, इससे पनीर एकदम सॉफ्ट और मखमली बनता है!"
        )

    # 2. BIRYANI (VEG / CHICKEN)
    if "biryani" in dl or "pulao" in dl:
        is_chk = "chicken" in dl or "mutton" in dl
        p_name = "Royal Chicken Dum Biryani" if is_chk else "Royal Awadhi Veg Dum Biryani"
        p_hi = "शाही चिकन दम बिरयानी" if is_chk else "शाही नवाबी वेज दम बिरयानी"
        m_ing = ["Chicken (500g)" if is_chk else "Mixed Veggies & Paneer (2 cups)", "Aged Basmati Rice (2 cups)", "Thick Curd / Yogurt (1/2 cup)", "Crispy Fried Onions (Birista, 1/2 cup)", "Ghee & Mint Leaves"]
        m_hi = ["चिकन (500 ग्राम)" if is_chk else "सब्जियां व पनीर (2 कप)", "बासमती चावल (2 कप)", "गाढ़ा दही (1/2 कप)", "तले हुए सुनहरे प्याज (बिरिस्ता)", "शुद्ध देसी घी और पुदीना पत्ता"]
        return RescueRecipe(
            recipe_name=p_name,
            recipe_name_hi=p_hi,
            meal_type="Main Course",
            meal_type_hi="मुख्य शाही भोजन",
            pairing_notes="Aromatic spiced rice layered over rich marinade. Serve with chilled cucumber boondi raita, sliced onions, and lemon wedges.",
            pairing_notes_hi="खुशबूदार खड़े मसालों के चावल और मैरिनेड की परतें। इसे ठंडे बूंदी रायते, प्याज के छल्लों और नींबू के साथ परोसें।",
            prep_time_minutes=35,
            ingredients_used=m_ing,
            ingredients_used_hi=m_hi,
            corrected_ingredients=["Basmati Rice", "Marinade", "Crispy Onions", "Ghee", "Mint"],
            pantry_items_needed=["Whole spices (bay leaf, cloves, cardamom, cinnamon)", "1 tbsp Biryani Masala", "1 tsp Chilli powder", "Saffron soaked in warm milk", "Salt"],
            pantry_items_needed_hi=["साबुत खड़े मसाले (तेजपत्ता, लौंग, इलायची, दालचीनी)", "1 बड़ा चम्मच बिरयानी मसाला", "1 चम्मच लाल मिर्च", "केसर वाला गुनगुना दूध", "स्वादानुसार नमक"],
            steps=[
                "Soak basmati rice for 30 minutes, then boil in salted water with whole spices until 70% cooked (grain breaks into 3 pieces). Drain completely.",
                f"Marinate {m_ing[0]} with curd, biryani masala, ginger-garlic paste, red chilli, turmeric, and salt for 15 minutes.",
                "In a heavy-bottomed pot, sear the marinated base in ghee and oil until cooked through and aromatic.",
                "Layer the 70% cooked rice over the masala base. Top generously with golden fried onions (birista), fresh mint, ghee, and saffron milk.",
                "Seal the pot with dough or foil and a tight lid. Cook on 'Dum' over low flame for 15-18 minutes. Rest 10 minutes, fluff gently, and serve!"
            ],
            steps_hi=[
                "बासमती चावल को 30 मिनट भिगोएं, फिर खड़े मसालों वाले उबलते पानी में 70% (चावल में एक कनी रहे) पकने तक उबालें और छान लें।",
                f"{m_hi[0]} को दही, बिरयानी मसाला, अदरक-लहसुन पेस्ट, मिर्च और नमक के साथ 15 मिनट मैरीनेट करें।",
                "एक भारी तले के बर्तन में घी गरम करें और मैरीनेट किए मिश्रण को अच्छी तरह भूनकर पका लें।",
                "मसाले के ऊपर उबले चावल की परत बिछाएं। ऊपर से सुनहरा तला प्याज (बिरिस्ता), हरा पुदीना, देसी घी और केसर वाला दूध डालें।",
                "बर्तन को फॉइल या आटे से सील करके धीमी आंच पर 15-18 मिनट 'दम' दें। 10 मिनट बाद हल्के हाथों से खोलें और परोसें!"
            ],
            chef_tip="Boil the rice with 1 tsp of vinegar and whole spices; this keeps each grain separate, extra long, and dazzling white!",
            chef_tip_hi="चावल उबालते समय पानी में 1 छोटा चम्मच सिरका और खड़े मसाले डालें, इससे चावल का हर दाना लंबा, खिला-खिला और चमकदार बनता है!"
        )

    # 3. DAL TADKA / DAL MAKHANI
    if "dal" in dl or "daal" in dl:
        is_makhani = "makhani" in dl
        p_name = "Restaurant-Style Dal Makhani" if is_makhani else "Dhaba-Style Double Tadka Dal"
        p_hi = "क्रीमी दाल मखनी" if is_makhani else "ढाबा स्टाइल डबल तड़का दाल"
        return RescueRecipe(
            recipe_name=p_name,
            recipe_name_hi=p_hi,
            meal_type="Main Course",
            meal_type_hi="मुख्य भोजन",
            pairing_notes="Comforting, velvety lentils tempered with sizzling garlic-cumin ghee. Enjoy with steamed basmati rice or tawa rotis.",
            pairing_notes_hi="देसी घी और लहसुन-जीरे के तड़के वाली स्वादिष्ट दाल। इसे गरमा-गरम सादे चावल या तवा रोटी के साथ खाएं।",
            prep_time_minutes=20,
            ingredients_used=["Toor / Moong Dal (1 cup)" if not is_makhani else "Whole Black Urad & Rajma (1 cup)", "Tomatoes (2 chopped)", "Garlic (6 cloves crushed)", "Ghee (2 tbsp)", "Fresh Coriander"],
            ingredients_used_hi=["तूर / मूंग दाल (1 कप)", "बारीक कटे टमाटर (2)", "कुटा हुआ लहसुन (6 कलियां)", "शुद्ध देसी घी (2 बड़े चम्मच)", "ताज़ा हरा धनिया"],
            corrected_ingredients=["Lentils", "Tomatoes", "Garlic", "Ghee", "Spices"],
            pantry_items_needed=["1 tsp Cumin seeds", "1/2 tsp Mustard seeds", "1/2 tsp Turmeric", "1 tsp Kashmiri Red Chilli powder", "Pinch of Hing (Asafoetida)", "Dry Red Chillies (2)"],
            pantry_items_needed_hi=["1 छोटा चम्मच जीरा", "1/2 छोटा चम्मच राई", "1/2 छोटा चम्मच हल्दी", "1 छोटा चम्मच कश्मीरी लाल मिर्च", "एक चुटकी हींग", "2 साबुत सूखी लाल मिर्च"],
            steps=[
                "Pressure cook washed dal with turmeric, salt, chopped tomatoes, and 3 cups water for 4-5 whistles until soft and creamy. Whisk gently.",
                "Prepare First Tadka: Heat 1 tbsp ghee in a pan, add cumin, chopped onions, and ginger. Sauté until golden, pour into cooked dal, and simmer for 5 minutes.",
                "Prepare Sizzling Second Tadka: In a small tadka pan, heat 1 tbsp pure desi ghee. Add hing, crushed garlic cloves, and whole red chillies until garlic turns nutty brown.",
                "Turn off the heat, stir in Kashmiri red chilli powder for brilliant color, and immediately pour the sizzling tadka over the dal. Cover with lid for 2 minutes to trap aroma.",
                "Garnish with chopped fresh coriander and serve piping hot!"
            ],
            steps_hi=[
                "दाल को हल्दी, नमक, कटे टमाटर और 3 कप पानी के साथ कुकर में 4-5 सीटी आने तक अच्छी तरह पकाएं। मथानी से हल्का घोंट लें।",
                "पहला तड़का: कड़ाही में 1 चम्मच घी गरम करें, जीरा, कटा प्याज और अदरक भूनें। उबली दाल में मिलाकर 5 मिनट धीमी आंच पर पकाएं।",
                "दूसरा स्पेशल तड़का: तड़का पैन में 1 बड़ा चम्मच शुद्ध देसी घी गरम करें। हींग, कुटी लहसुन और साबुत लाल मिर्च डालकर लहसुन को सुनहरा भूरा होने दें।",
                "गैस बंद करके कश्मीरी लाल मिर्च डालें और खौलता हुआ तड़का सीधे दाल पर डालकर तुरंत 2 मिनट के लिए ढक्कन बंद कर दें ताकि खुशबू अंदर समा जाए।",
                "ताज़ा हरा धनिया डालकर गरमा-गरम चावल या फुल्के के साथ परोसें!"
            ],
            chef_tip="Pouring the sizzling garlic-ghee tadka and immediately clamping the lid on for 2 minutes locks the smoky dhaba aroma directly into the dal!",
            chef_tip_hi="तड़का लगाते ही तुरंत 2 मिनट के लिए बर्तन को ढक दें, इससे ढाबे जैसी असली सौंधी देसी घी की खुशबू दाल के अंदर पूरी तरह समा जाती है!"
        )

    # 4. PAV BHAJI
    if "pav bhaji" in dl or "bhaji" in dl:
        return RescueRecipe(
            recipe_name="Mumbai Street-Style Buttery Pav Bhaji",
            recipe_name_hi="मुंबई स्ट्रीट-स्टाइल बटर पाव भाजी",
            meal_type="Street Food Snack",
            meal_type_hi="स्ट्रीट फूड स्नैक",
            pairing_notes="Tangy, spiced mashed vegetable bhaji loaded with melting Amul butter. Serve with soft toasted pav, crunchy raw onions, and fresh lemon wedges.",
            pairing_notes_hi="मक्खन से लबालब चटपटी मैश की हुई भाजी और तवे पर सिके मुलायम पाव, प्याज और नींबू के साथ।",
            prep_time_minutes=25,
            ingredients_used=["Potatoes, Cauliflower, Green Peas (boiled, 2 cups)", "Pav Buns (6-8)", "Tomatoes (3 finely chopped)", "Capsicum (1 finely chopped)", "Butter (3 tbsp)"],
            ingredients_used_hi=["उबले आलू, गोभी, हरी मटर (2 कप)", "पाव बन (6-8)", "बारीक कटे टमाटर (3)", "बारीक कटी शिमला मिर्च (1)", "मक्खन (3 बड़े चम्मच)"],
            corrected_ingredients=["Veggies", "Pav", "Tomatoes", "Capsicum", "Butter"],
            pantry_items_needed=["2 tbsp Pav Bhaji Masala", "1 tsp Kashmiri Red Chilli powder", "1 tbsp Ginger-Garlic paste", "1 tsp Kasuri Methi", "Salt to taste"],
            pantry_items_needed_hi=["2 बड़े चम्मच पाव भाजी मसाला", "1 चम्मच कश्मीरी लाल मिर्च", "1 चम्मच अदरक-लहसुन पेस्ट", "1 चम्मच कसूरी मेथी", "स्वादानुसार नमक"],
            steps=[
                "Boil potatoes, cauliflower, carrots, and green peas until fork-tender. Mash thoroughly with a potato masher.",
                "On a wide tawa or pan, melt 2 tbsp butter. Sauté finely chopped onions, capsicum, and ginger-garlic paste until soft.",
                "Add chopped tomatoes, pav bhaji masala, Kashmiri chilli powder, and salt. Cook until tomatoes turn completely pulpy and butter separates.",
                "Add the mashed boiled veggies with 1/2 cup warm water. Using the masher, mash continuously on the tawa on medium heat for 6-8 minutes until smooth and thick.",
                "Finish bhaji with a generous slab of butter, crushed kasuri methi, and lemon juice. Slit pav buns, toast on the buttered tawa with a pinch of pav bhaji masala, and serve together hot!"
            ],
            steps_hi=[
                "आलू, गोभी और हरी मटर को उबालकर मैशर से अच्छी तरह मैश कर लें।",
                "तवे पर 2 बड़े चम्मच मक्खन गरम करें। बारीक कटा प्याज, शिमला मिर्च और अदरक-लहसुन पेस्ट डालकर भूनें।",
                "टमाटर, पाव भाजी मसाला, कश्मीरी लाल मिर्च और नमक डालकर टमाटर के गलने तक अच्छी तरह पकाएं।",
                "उबली मैश की हुई सब्जियां और 1/2 कप पानी डालें। तवे पर ही मैशर से 6-8 मिनट तक लगातार दबाते हुए स्मूथ भाजी तैयार करें।",
                "ऊपर से ढेर सारा मक्खन, कसूरी मेथी और नींबू का रस निचोड़ें। पाव को बीच से काटकर तवे पर मक्खन और हल्के मसाले के साथ सेंकें और गरमा-गरम परोसें!"
            ],
            chef_tip="Toast the pav on the same tawa where the bhaji was prepared with a pinch of pav bhaji masala and chopped coriander for street-vendor taste!",
            chef_tip_hi="पाव को उसी तवे पर सेकें जिस पर भाजी बनी हो, थोड़ा सा भाजी मसाला और हरा धनिया डालकर सेकने से असली मुंबई चौपाटी वाला स्वाद आता है!"
        )

    # 5. KHEER / DESSERT
    if any(k in dl for k in ["kheer", "payasam", "pudding", "halwa", "gulab jamun", "dessert"]):
        return RescueRecipe(
            recipe_name="Royal Creamy Rice Kheer (Indian Rice Pudding)",
            recipe_name_hi="शाही मलाईदार चावल की खीर",
            meal_type="Traditional Dessert",
            meal_type_hi="पारंपरिक मिठाई",
            pairing_notes="Slow-simmered rich condensed milk kheer infused with cardamom and saffron. Serve warm or chilled garnished with slivered almonds and pistachios.",
            pairing_notes_hi="धीमी आंच पर पकी गाढ़ी मलाईदार केसर-इलायची वाली खीर। बादाम-पिस्ता की कतरन के साथ गरमा-गरम या ठंडी परोसें।",
            prep_time_minutes=30,
            ingredients_used=["Full Cream Milk (1 litre)", "Basmati Rice (1/4 cup, soaked & coarsely crushed)", "Sugar (1/2 cup)", "Almonds & Cashews (sliced, 2 tbsp)", "Saffron strands"],
            ingredients_used_hi=["फुल क्रीम दूध (1 लीटर)", "बासमती चावल (1/4 कप, भीगे व हल्के कुटे)", "चीनी (1/2 कप)", "काजू व बादाम कतरन (2 चम्मच)", "केसर के धागे"],
            corrected_ingredients=["Full Cream Milk", "Rice", "Sugar", "Nuts", "Saffron"],
            pantry_items_needed=["1/2 tsp Cardamom powder (Elaichi)", "1 tbsp Ghee", "Chironji / Raisins (optional)"],
            pantry_items_needed_hi=["1/2 छोटा चम्मच इलायची पाउडर", "1 चम्मच शुद्ध देसी घी", "चिरौंजी या किशमिश"],
            steps=[
                "Wash basmati rice, soak in water for 20 minutes, then coarsely crush between fingers with 1/2 tsp ghee.",
                "In a heavy-bottomed pan, bring 1 litre full-cream milk to a rolling boil. Lower heat and let it simmer for 5 minutes.",
                "Add the crushed rice into the boiling milk. Cook on low-medium flame, stirring continuously and scraping milk solids from pan sides.",
                "Simmer for 20 minutes until the rice is completely tender and milk has reduced to a thick, creamy consistency.",
                "Stir in sugar, cardamom powder, saffron strands, and roasted nuts. Simmer for 3 more minutes until sugar is fully dissolved. Serve chilled or warm!"
            ],
            steps_hi=[
                "बासमती चावल को धोकर 20 मिनट भिगोएं, फिर उंगलियों से हल्का सा दरदरा मसल लें और आधा चम्मच घी मिलाएँ।",
                "भारी तले के बर्तन में 1 लीटर फुल क्रीम दूध उबालें। उबाल आने पर आंच धीमी कर दें।",
                "दरदरा कुटा चावल दूध में डालें। धीमी आंच पर लगातार चलाते हुए पकाएं और किनारों पर जमने वाली मलाई को दूध में मिलाते रहें।",
                "20 मिनट तक पकाएं जब तक चावल पूरी तरह गल न जाए और दूध गाढ़ा व मलाईदार न हो जाए।",
                "चीनी, इलायची पाउडर, केसर और भुने मेवे डालें। 3 मिनट और पकाएं ताकि चीनी घुल जाए। ठंडा करके या गरमा-गरम परोसें!"
            ],
            chef_tip="Coarsely crushing the soaked rice with a drop of ghee before boiling releases rice starch uniformly, creating that rich rabdi-like creaminess without adding condensed milk!",
            chef_tip_hi="चावल को पकाने से पहले हल्के हाथ से मसलकर थोड़ा घी मिला लेने से दूध में स्टार्च समान रूप से घुलता है और बिना मावा डाले भी खीर एकदम रबड़ी जैसी गाढ़ी बनती है!"
        )

    # 6. UNIVERSAL DYNAMIC DISH CHEF (For any custom dish asked)
    clean_title = dish.title().strip()
    return RescueRecipe(
        recipe_name=f"Chef's Special Homestyle {clean_title}",
        recipe_name_hi=f"स्वादिष्ट होमस्टाइल {clean_title}",
        meal_type="Homestyle Special",
        meal_type_hi="घर का खास व्यंजन",
        pairing_notes=f"Authentic step-by-step master recipe for {clean_title}, seasoned with harmonious aromatics and home kitchen staples.",
        pairing_notes_hi=f"{clean_title} बनाने की आसान और प्रमाणिक होमस्टाइल विधि, घर की रसोई के ताज़ा मसालों के साथ।",
        prep_time_minutes=25,
        ingredients_used=[f"Primary ingredients for {clean_title}", "Cooking Oil / Butter (2 tbsp)", "Aromatics (Onion, Ginger, Garlic)", "Fresh Herbs"],
        ingredients_used_hi=[f"{clean_title} की मुख्य सामग्री", "तेल या मक्खन (2 बड़े चम्मच)", "अदरक, लहसुन, प्याज", "ताज़ा हरा धनिया"],
        corrected_ingredients=[clean_title, "Spices", "Oil", "Aromatics"],
        pantry_items_needed=["1 tsp Cumin or Mustard seeds", "1 tsp Ground Spices (Coriander, Garam Masala)", "1/2 tsp Turmeric & Chilli", "Salt to taste"],
        pantry_items_needed_hi=["1 छोटा चम्मच जीरा या राई", "1 चम्मच धनिया व गरम मसाला", "1/2 चम्मच हल्दी व लाल मिर्च", "स्वादानुसार नमक"],
        steps=[
            f"Preparation: Gather all fresh ingredients for {clean_title}. Wash, chop, and measure seasonings before starting.",
            "Base Tempering: Heat oil or butter in a suitable pan. Add whole spices, aromatics (onions, ginger, garlic), and sauté over medium flame until fragrant and golden.",
            f"Cooking Main Elements: Add the primary {clean_title} ingredients. Season with turmeric, coriander, chilli powder, and salt. Sauté well to coat each piece.",
            "Simmer & Blend: Pour in required water, broth, or gravy base. Cover with lid and simmer on low-medium flame for 12-15 minutes until tender and deeply flavorful.",
            "Finishing Touch: Uncover, stir in a dash of garam masala and fresh herbs. Serve piping hot with your favorite accompaniments!"
        ],
        steps_hi=[
            f"तैयारी: {clean_title} की सभी आवश्यक सामग्रियों को धोकर, काटकर और मसालों को नापकर रख लें।",
            "तड़का लगाएं: कड़ाही में तेल या मक्खन गरम करें। खड़े मसाले, बारीक कटा प्याज, अदरक और लहसुन डालकर सुनहरा होने तक भूनें।",
            f"सामग्री पकाएं: {clean_title} की मुख्य सामग्री डालें। हल्दी, धनिया पाउडर, लाल मिर्च और स्वादानुसार नमक डालकर अच्छी तरह मिलाएँ।",
            "धीमी आंच पर पकाएं: आवश्यकतानुसार पानी या ग्रेवी डालकर ढक्कन लगाएं और 12-15 मिनट धीमी आंच पर सभी स्वादों के मिलने तक पकाएं।",
            "अंतिम स्पर्श: ढक्कन खोलें, थोड़ा गरम मसाला और ताज़ा हरा धनिया मिलाएं। गरमा-गरम परोसें और आनंद लें!"
        ],
        chef_tip=f"For maximum depth of flavor in {clean_title}, roast your ground spices on low heat for 30 seconds before adding liquids!",
        chef_tip_hi=f"{clean_title} में बेहतरीन स्वाद और खुशबू के लिए पिसे मसालों को पानी डालने से पहले 30 सेकंड धीमी आंच पर सूखा भून लें!"
    )

RESCUE_PROMPT_TEMPLATE = """You are an expert master chef and zero-waste kitchen culinary genius for 'FridgeInventory'.
A household has these specific expiring items or leftovers that MUST be used today to prevent food waste:
{ingredients}

CRITICAL CULINARY RULES & PAIRING INTELLIGENCE:
1. DETECT INCOMPATIBLE INGREDIENTS OR DRINK + FOOD COMBINATIONS:
   - Carefully evaluate if these ingredients harmoniously belong together in ONE single pot/pan, OR if they should be prepared as separate complementary items (such as a SAVORY DISH + REFRESHING DRINK/BEVERAGE, or MAIN COURSE + DESSERT/SIDE)!
   - NEVER force jarring flavor clashes! For example, if given 'milk, chocolate, bread, cheese, peri peri':
     DO NOT combine chocolate and peri-peri cheese into a single weird concoction!
     Instead, make a delightful Food & Drink Zero-Waste Combo (e.g. 'Peri-Peri Grilled Cheese Toast' paired with a 'Chilled Creamy Chocolate Milkshake / Hot Cocoa').
   - If ingredients include drink bases or sweet beverages (milk, chocolate, cocoa, lemons/citrus, mint, roohafza, mango, coffee, tea, soda, yogurt/curd for lassi, fruit juice) alongside savory items (bread, cheese, peri-peri, vegetables, chicken, eggs, rice, savoury spices):
     Set meal_type to 'Food & Drink Combo' and meal_type_hi to 'भोजन और पेय कॉम्बो' and provide a paired menu!
   - In pairing_notes (English) and pairing_notes_hi (Hindi Devanagari), explain clearly to the home cook how and why these items were paired into complementary courses.
2. HARMONIOUS SINGLE-DISH COMBINATIONS:
   - If all ingredients naturally cook together (e.g., potatoes, onions, tomatoes, spinach, paneer, vegetables, dal, rice), set meal_type to 'Homestyle Meal' (meal_type_hi: 'घर का भोजन') or 'Savory Food' (meal_type_hi: 'स्वादिष्ट भोजन') and create one delicious, authentic, easy Indian dish (curry, dry sabzi, paratha, pulao, or snacks).
   - If only beverage/drink ingredients are provided (e.g. milk, chocolate, bananas, honey), set meal_type to 'Beverage / Drink' (meal_type_hi: 'ताज़ा पेय / शेक') and make an extraordinary drink.
3. COMPLETE DUAL-LANGUAGE OUTPUT (ENGLISH & AUTHENTIC HINDI):
   - You MUST provide every single field in English AND in authentic, natural Hindi (Devanagari script)!
   - 'steps_hi' MUST contain every step of the cooking instructions translated into fluent, natural Hindi. Do NOT leave steps_hi empty or in English!
   - 'pantry_items_needed_hi' MUST list all required pantry spices and items in Hindi (e.g. '1 चम्मच जीरा', 'स्वादानुसार नमक', 'हल्दी पाउडर').
   - 'chef_tip_hi' MUST be a traditional Indian culinary tip ('दादी माँ का नुस्खा').

Output Schema:
- recipe_name in English
- recipe_name_hi in Hindi (Devanagari script)
- meal_type ('Food & Drink Combo', 'Savory Food', 'Beverage / Drink', or 'Homestyle Meal')
- meal_type_hi in Hindi ('भोजन और पेय कॉम्बो', 'स्वादिष्ट भोजन', 'ताज़ा पेय / शेक', or 'घर का भोजन')
- pairing_notes in English
- pairing_notes_hi in Hindi (Devanagari script)
- prep_time_minutes (realistic integer, 10 to 30 mins)
- ingredients_used (list)
- ingredients_used_hi (list in Hindi)
- corrected_ingredients (list)
- pantry_items_needed (list in English)
- pantry_items_needed_hi (list in Hindi with quantities)
- steps (list of clear step-by-step cooking steps in English)
- steps_hi (list of clear, complete step-by-step cooking instructions in natural Hindi in Devanagari script)
- chef_tip (pro zero-waste tip in English)
- chef_tip_hi (authentic Indian kitchen tip in Hindi: 'दादी माँ का नुस्खा')
"""

def generate_rescue_recipe(items: List[str]) -> RescueRecipe:
    """Generate an Indian zero-waste rescue recipe or tell how to make any requested recipe."""
    raw_clean = [str(i).strip() for i in items if str(i).strip()]
    if not raw_clean:
        raise HTTPException(status_code=400, detail="Provide at least one expiring food item or ask how to make a recipe.")

    # Check if user is asking how to make a specific recipe / dish!
    is_recipe_ask, target_dish = detect_recipe_dish_query(raw_clean)
    if is_recipe_ask:
        # Try Gemini GenAI with HOWTO_RECIPE_PROMPT_TEMPLATE
        client = get_gemini_client()
        if client is not None and types is not None:
            for model_name in GEMINI_MODELS:
                try:
                    response = client.models.generate_content(
                        model=model_name,
                        contents=HOWTO_RECIPE_PROMPT_TEMPLATE.format(dish=target_dish),
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
                    logger.warning(f"Recipe how-to generation failed on {model_name}: {exc}")
                    continue
        # Fallback to authentic dish preset
        return get_dish_recipe_fallback(target_dish)

    # 1. Automatic culinary spelling and typo correction
    clean_items, corrections = correct_ingredients_list(raw_clean)

    # 2. Try Gemini GenAI
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
                recipe.corrected_ingredients = clean_items
                return recipe
            except Exception as exc:
                logger.warning(f"Rescue recipe generation failed on {model_name}: {exc}")
                continue

    # 3. Intelligent Culinary Fallback (Supports Drink + Food Combos)
    drink_kw = {"milk", "chocolate", "cocoa", "curd", "dahi", "yogurt", "shake", "smoothie",
                "juice", "lemon", "nimbu", "mint", "pudina", "roohafza", "orange", "mango",
                "coffee", "tea", "chai", "lassi", "buttermilk", "chaas", "soda", "syrup", "honey"}
    savory_kw = {"bread", "cheese", "peri peri", "chilli", "paneer", "potato", "onion", "tomato",
                 "capsicum", "chicken", "egg", "rice", "dal", "spinach", "palak", "garlic", "ginger",
                 "chips", "snack", "masala", "curry", "paratha", "roti", "flour", "atta", "butter"}

    drink_items = [it for it in clean_items if any(k in it.lower() for k in drink_kw)]
    savory_items = [it for it in clean_items if any(k in it.lower() for k in savory_kw)]

    # Case A: Food & Drink Combo (e.g. milk, chocolate, bread, cheese, peri-peri)
    if drink_items and savory_items:
        savory_title = " & ".join(savory_items[:2])
        drink_title = " & ".join(drink_items[:2])
        return RescueRecipe(
            recipe_name=f"Zero-Waste Combo: Toasted {savory_title} & Chilled {drink_title} Shake",
            recipe_name_hi=f"जीरो-वेस्ट कॉम्बो: क्रिस्पी {savory_title} टोस्ट और ठंडा {drink_title} शेक",
            meal_type="Food & Drink Combo",
            meal_type_hi="भोजन और पेय कॉम्बो",
            pairing_notes="To avoid clashing savory seasonings with sweet cocoa or milk, these ingredients are crafted into a café-style hot melt and cold beverage pairing.",
            pairing_notes_hi="मीठी और नमकीन चीज़ों का बेहतरीन स्वाद संतुलन बनाए रखने के लिए, इन्हें एक गरमा-गरम क्रिस्पी टोस्ट और ठंडे रिफ्रेशिंग शेक के कॉम्बो में तैयार किया गया है।",
            prep_time_minutes=15,
            ingredients_used=clean_items,
            ingredients_used_hi=clean_items,
            corrected_ingredients=clean_items,
            pantry_items_needed=[
                "1 tbsp Butter or Cooking Oil",
                "1/2 tsp Cumin or Mixed Herbs",
                "Pinch of Salt & Black Pepper",
                "1 tsp Sugar or Honey (for drink)",
                "Ice cubes",
            ],
            pantry_items_needed_hi=[
                "1 बड़ा चम्मच मक्खन या कुकिंग ऑयल",
                "1/2 छोटा चम्मच भुना जीरा या मिक्स्ड हर्ब्स",
                "एक चुटकी नमक और काली मिर्च पाउडर",
                "1 चम्मच चीनी या शहद (ड्रिंक के लिए)",
                "बर्फ के टुकड़े (Ice cubes)",
            ],
            steps=[
                f"Savory Course: Place {', '.join(savory_items)} between bread slices or onto a pan with butter and seasoning.",
                "Toast over medium heat on a tawa until golden, crispy, and cheese is smoothly melted.",
                f"Beverage: In a blender or shaker bottle, combine {', '.join(drink_items)} with ice and a touch of sweetener.",
                "Blend or shake vigorously for 30 seconds until frothy and chilled.",
                "Serve the piping hot savory sandwich immediately alongside the refreshing chilled drink for a satisfying zero-waste meal!",
            ],
            steps_hi=[
                f"नमकीन व्यंजन: ब्रेड स्लाइस के बीच में {', '.join(savory_items)} रखें और ऊपर से थोड़ा मक्खन व पसंदीदा मसाला लगाएं।",
                "तवे पर मध्यम आंच पर दोनों तरफ से तब तक सेकें जब तक कि टोस्ट सुनहरा, क्रिस्पी न हो जाए और चीज़ पिघल न जाए।",
                f"ठंडा पेय: मिक्सर जार या शेकर में {', '.join(drink_items)}, बर्फ के टुकड़े और स्वादानुसार थोड़ा शहद या चीनी डालें।",
                "30 सेकंड तक तेज़ी से ब्लेंड करें जब तक कि ऊपर बढ़िया झाग और गाढ़ा ठंडा शेक न बन जाए।",
                "गरमा-गरम क्रिस्पी टोस्ट को तुरंत ठंडे ताज़ा शेक के साथ परोसें और ज़ीरो-वेस्ट स्वादिष्ट नाश्ते का आनंद लें!",
            ],
            chef_tip="Pairing a spicy or savory toasted snack with a cold creamy milkshake or fruit drink balances the palate and utilizes both sweet and savory fridge perishables!",
            chef_tip_hi="तीखे या नमकीन स्नैक के साथ ठंडा क्रीमी शेक स्वाद का बेहतरीन संतुलन बनाता है और फ्रिज में रखी दोनों तरह की सामग्रियों की पूरी बर्बादी रोकता है!",
        )

    # Case B: Beverage Only
    if drink_items and not savory_items:
        primary_drink = drink_items[0]
        return RescueRecipe(
            recipe_name=f"Refreshing Homestyle {primary_drink} Cooler / Shake",
            recipe_name_hi=f"ताज़ा होमस्टाइल {primary_drink} शेक",
            meal_type="Beverage / Drink",
            meal_type_hi="ताज़ा पेय / शेक",
            pairing_notes="Perishable drink bases blended into a nutritious, refreshing homemade beverage.",
            pairing_notes_hi="फ्रिज में रखे जल्दी खराब होने वाले पेय पदार्थों से बना ताज़ा और पौष्टिक होमस्टाइल शेक।",
            prep_time_minutes=10,
            ingredients_used=clean_items,
            ingredients_used_hi=clean_items,
            corrected_ingredients=clean_items,
            pantry_items_needed=["1-2 tsp Honey or Sugar", "Pinch of Cardamom / Cinnamon", "Ice cubes"],
            pantry_items_needed_hi=["1-2 चम्मच शहद या चीनी", "एक चुटकी इलायची या दालचीनी पाउडर", "बर्फ के टुकड़े"],
            steps=[
                f"Wash and measure the beverage ingredients ({', '.join(clean_items)}).",
                "Add to a blender with ice cubes and a dash of cardamom or honey to taste.",
                "Blend on high for 45 seconds until silky smooth and frothy.",
                "Pour into tall glasses and serve chilled.",
            ],
            steps_hi=[
                f"पेय सामग्री ({', '.join(clean_items)}) को अच्छी तरह नापकर मिक्सर जार में डालें।",
                "जार में बर्फ के टुकड़े, एक चुटकी इलायची पाउडर और स्वादानुसार शहद या चीनी मिलाएँ।",
                "तेज़ गति पर 45 सेकंड तक अच्छी तरह ब्लेंड करें जब तक कि शेक एकदम स्मूथ और झागदार न हो जाए।",
                "ठंडे गिलासों में डालें, ऊपर से थोड़ी सी इलायची छिड़कें और तुरंत ठंडा-ठंडा परोसें।",
            ],
            chef_tip="Add a pinch of roasted cinnamon or cardamom powder to elevate dairy drinks and bring out natural sweetness!",
            chef_tip_hi="दूध या फल वाले शेक में एक चुटकी भुनी हुई इलायची या दालचीनी पाउडर डालने से प्राकृतिक खुशबू और मिठास कई गुना बढ़ जाती है!",
        )

    # Case C: Homestyle Savory Dish / Masala Stir-Fry
    primary = clean_items[0].title()
    all_items_str = ", ".join(clean_items)
    return RescueRecipe(
        recipe_name=f"Homestyle Kitchen Masala Bhurji / Stir-Fry ({primary})",
        recipe_name_hi=f"झटपट मसाला {primary} भुर्जी",
        meal_type="Homestyle Meal",
        meal_type_hi="घर का स्वादिष्ट भोजन",
        pairing_notes="Perishable ingredients harmoniously sautéed with everyday Indian spices.",
        pairing_notes_hi="फ्रिज में बची हुई सामग्री को रोज़मर्रा के भारतीय मसालों के साथ मिलाकर बनाई गई स्वादिष्ट झटपट सब्ज़ी।",
        prep_time_minutes=20,
        ingredients_used=clean_items,
        ingredients_used_hi=clean_items,
        corrected_ingredients=clean_items,
        pantry_items_needed=[
            "1-2 tbsp Cooking Mustard or Sunflower Oil",
            "1/2 tsp Cumin seeds (Jeera)",
            "1/4 tsp Turmeric powder (Haldi)",
            "1/2 tsp Red chilli powder (Lal Mirch)",
            "1/2 tsp Coriander powder (Dhaniya)",
            "Salt to taste",
            "1 green chilli & fresh coriander leaves (finely chopped)",
        ],
        pantry_items_needed_hi=[
            "1-2 बड़े चम्मच सरसों या रिफाइंड तेल",
            "1/2 छोटा चम्मच साबुत जीरा",
            "1/4 छोटा चम्मच हल्दी पाउडर",
            "1/2 छोटा चम्मच लाल मिर्च पाउडर",
            "1/2 छोटा चम्मच धनिया पाउडर",
            "स्वादानुसार नमक",
            "1 बारीक कटी हरी मिर्च और ताज़ा हरा धनिया",
        ],
        steps=[
            f"Thoroughly inspect and wash the expiring ingredients ({all_items_str}). Cut into uniform small pieces.",
            "Heat 1.5 tablespoons of oil in a kadai or pan over medium flame. Add cumin seeds and let them crackle.",
            f"Add any firm vegetables or {primary} first and sauté for 4-5 minutes until tender.",
            "Lower the heat and add turmeric powder, red chilli powder, coriander powder, and salt. Toss well so spices coat the ingredients evenly.",
            "Add softer ingredients (such as paneer, boiled potatoes, greens, or cooked rice) and cook for another 2-3 minutes.",
            "Turn off the heat, garnish with fresh chopped coriander and a squeeze of fresh lemon juice. Serve hot with rotis, parathas, or toast.",
        ],
        steps_hi=[
            f"फ्रिज के सामान ({all_items_str}) को अच्छी तरह धोकर एकसमान छोटे टुकड़ों में काट लें।",
            "कड़ाही या पैन में 1.5 बड़े चम्मच तेल मध्यम आंच पर गर्म करें। जीरा डालें और चटकने दें।",
            f"पहले सख्त सब्ज़ियों या {primary} को डालें और 4-5 मिनट तक नरम होने तक भूनें।",
            "आंच धीमी करें और हल्दी, लाल मिर्च, धनिया पाउडर व नमक डालकर अच्छी तरह मिलाएँ ताकि मसाला लिपट जाए।",
            "अब बाकी नरम सामग्री (जैसे पनीर, उबले आलू, हरी पत्तियाँ या पके चावल) डालें और 2-3 मिनट तक भूनें।",
            "गैस बंद करें, ऊपर से ताज़ा हरा धनिया और थोड़ा सा नींबू का रस निचोड़ें। गरमा-गरम रोटी, पराठे या टोस्ट के साथ परोसें!",
        ],
        chef_tip="To revive vegetables that have lost their crispness, soak them in chilled water for 10 minutes before chopping. A pinch of amchur (dry mango powder) or chaat masala balances flavors beautifully!",
        chef_tip_hi="मुरझाई हुई सब्ज़ियों को काटने से पहले 10 मिनट ठंडे पानी में भिगोकर रखें, वे एकदम ताज़ा हो जाएँगी। अंत में थोड़ा अमचूर या चाट मसाला डालने से स्वाद लाजवाब हो जाता है!",
    )


# ============================================================================
# 9. FASTAPI APPLICATION SETUP
# ============================================================================

app = FastAPI(
    title="FridgeInventory API",
    description="Intelligent Household Fridge & Pantry Inventory Management System with Gemini AI",
    version="2.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static asset directory for PWA icons
os.makedirs("static/icons", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/favicon.ico", summary="Browser Favicon")
def serve_favicon():
    """Serve favicon.ico with proper image/x-icon MIME type."""
    fav_path = "static/favicon.ico" if os.path.exists("static/favicon.ico") else "favicon.ico"
    return FileResponse(fav_path, media_type="image/x-icon", headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/apple-touch-icon.png", summary="iOS Apple Touch Icon")
@app.get("/apple-touch-icon-precomposed.png")
def serve_apple_touch_icon():
    """Serve Apple touch icon with image/png MIME type."""
    return FileResponse("static/icons/apple-touch-icon.png", media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


# ============================================================================
# 9b. VAPID KEY MANAGEMENT & WEB PUSH NOTIFICATION SYSTEM
# ============================================================================

_VAPID_PUBLIC_KEY: Optional[str] = None
_VAPID_PRIVATE_KEY: Optional[str] = None
_VAPID_SUBJECT: str = "mailto:fridgeinventory@noreply.app"
_IN_MEMORY_PUSH_SUBS: Dict[str, dict] = {}  # token_hash -> subscription_info

def _init_vapid_keys():
    """Load or auto-generate VAPID keys for Web Push Notifications."""
    global _VAPID_PUBLIC_KEY, _VAPID_PRIVATE_KEY, _VAPID_SUBJECT

    _VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "").strip().strip('"').strip("'")
    _VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "").strip().strip('"').strip("'")
    subject = os.getenv("VAPID_SUBJECT", "").strip().strip('"').strip("'")
    if subject:
        _VAPID_SUBJECT = subject

    if _VAPID_PUBLIC_KEY and _VAPID_PRIVATE_KEY:
        logger.info("VAPID keys loaded from environment variables.")
        return

    # Auto-generate VAPID keys if not provided
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        import base64

        private_key = ec.generate_private_key(ec.SECP256R1())
        private_numbers = private_key.private_numbers()
        public_numbers = private_numbers.public_key.public_numbers

        # Encode private key as URL-safe base64 (raw 32-byte)
        priv_bytes = private_numbers.private_value.to_bytes(32, byteorder='big')
        _VAPID_PRIVATE_KEY = base64.urlsafe_b64encode(priv_bytes).rstrip(b'=').decode('ascii')

        # Encode public key as uncompressed point (0x04 + x + y)
        x_bytes = public_numbers.x.to_bytes(32, byteorder='big')
        y_bytes = public_numbers.y.to_bytes(32, byteorder='big')
        pub_bytes = b'\x04' + x_bytes + y_bytes
        _VAPID_PUBLIC_KEY = base64.urlsafe_b64encode(pub_bytes).rstrip(b'=').decode('ascii')

        logger.info(f"Auto-generated VAPID keys. Public key: {_VAPID_PUBLIC_KEY[:20]}...")
        logger.info("Set VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY env vars in Render for persistence.")
        logger.info(f"VAPID_PUBLIC_KEY={_VAPID_PUBLIC_KEY}")
        logger.info(f"VAPID_PRIVATE_KEY={_VAPID_PRIVATE_KEY}")
    except Exception as exc:
        logger.error(f"Failed to auto-generate VAPID keys: {exc}")
        _VAPID_PUBLIC_KEY = None
        _VAPID_PRIVATE_KEY = None

_init_vapid_keys()


def _save_push_subscription(fridge_id: str, subscription_info: dict):
    """Persist a push subscription for a fridge."""
    db, _ = init_firestore()
    sub_key = hashlib.sha256(json.dumps(subscription_info, sort_keys=True).encode()).hexdigest()[:16]

    payload = {
        "fridge_id": fridge_id,
        "subscription": subscription_info,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    if db is not None:
        try:
            db.collection("push_subscriptions").document(sub_key).set(payload)
        except Exception as exc:
            logger.error(f"Failed to save push subscription to Firestore: {exc}")
            _IN_MEMORY_PUSH_SUBS[sub_key] = payload
    else:
        _IN_MEMORY_PUSH_SUBS[sub_key] = payload


def _remove_push_subscription(subscription_info: dict):
    """Remove a push subscription."""
    db, _ = init_firestore()
    sub_key = hashlib.sha256(json.dumps(subscription_info, sort_keys=True).encode()).hexdigest()[:16]

    if db is not None:
        try:
            db.collection("push_subscriptions").document(sub_key).delete()
        except Exception:
            pass
    _IN_MEMORY_PUSH_SUBS.pop(sub_key, None)


def _get_push_subscriptions_for_fridge(fridge_id: str) -> List[dict]:
    """Get all push subscriptions for a fridge."""
    db, _ = init_firestore()
    subs = []

    if db is not None:
        try:
            docs = db.collection("push_subscriptions").where("fridge_id", "==", fridge_id).stream()
            for doc in docs:
                d = doc.to_dict()
                if d.get("subscription"):
                    subs.append(d["subscription"])
        except Exception as exc:
            logger.error(f"Failed to fetch push subscriptions: {exc}")

    # Also check in-memory
    for sub_data in _IN_MEMORY_PUSH_SUBS.values():
        if sub_data.get("fridge_id") == fridge_id and sub_data.get("subscription"):
            subs.append(sub_data["subscription"])

    # Deduplicate subscriptions by endpoint
    seen_endpoints = set()
    unique_subs = []
    for s in subs:
        endpoint = s.get("endpoint")
        if endpoint and endpoint not in seen_endpoints:
            seen_endpoints.add(endpoint)
            unique_subs.append(s)
    return unique_subs


def _send_push_notification(subscription_info: dict, title: str, body: str, url: str = "/", tag: str = "fridge-alert", tz_name: Optional[str] = None):
    """Send a single Web Push notification with TTL buffering so offline devices receive it on reconnection."""
    if not HAVE_WEBPUSH or not _VAPID_PUBLIC_KEY or not _VAPID_PRIVATE_KEY:
        return False

    tz = resolve_tz(tz_name)
    payload = json.dumps({
        "title": title,
        "body": body,
        "icon": "/static/icons/icon-192.png",
        "badge": "/static/icons/icon-192.png",
        "url": url,
        "tag": tag,
        "timestamp": datetime.now(tz).strftime("%H:%M"),
    })

    try:
        webpush(
            subscription_info=subscription_info,
            data=payload,
            vapid_private_key=_VAPID_PRIVATE_KEY,
            vapid_claims={"sub": _VAPID_SUBJECT},
            ttl=86400,
            headers={"Urgency": "high"},
        )
        return True
    except WebPushException as exc:
        logger.warning(f"Push notification failed: {exc}")
        # If subscription expired (410 Gone), remove it
        if hasattr(exc, 'response') and exc.response is not None and exc.response.status_code == 410:
            _remove_push_subscription(subscription_info)
        return False
    except Exception as exc:
        logger.error(f"Unexpected push error: {exc}")
        return False


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

    fid = require_authenticated_fridge(request, requested_fridge_id=fridge)
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

    # Check if user asked 'how to make ...' or for a recipe directly in the command bar!
    is_rec, rec_dish = detect_recipe_dish_query([raw_cmd])
    if is_rec:
        return {
            "action": "recipe",
            "dish": rec_dish or raw_cmd,
            "message_en": f"Opening recipe for '{rec_dish or raw_cmd}'...",
            "message_hi": f"'{rec_dish or raw_cmd}' की रेसिपी तैयार की जा रही है...",
        }

    # 2. Tokenize & detect intent
    tokens = WORD_RE.findall(cmd_lower)
    if not tokens:
        raise HTTPException(status_code=400, detail="Could not understand command.")

    action = detect_action(tokens)

    # 3. Handle Take / Consume / Waste command (e.g. "i ate lays", "ate milk, spinach", "used milk and spinach")
    if action in ("take", "waste"):
        clean_target_text = cmd_lower
        for w in sorted(list(TAKE_WORDS | WASTE_WORDS | {"i", "me", "we"}), key=len, reverse=True):
            clean_target_text = re.sub(rf'^{re.escape(w)}\s+', '', clean_target_text)
            clean_target_text = re.sub(rf'\s+{re.escape(w)}$', '', clean_target_text)

        clean_target_text = re.sub(r'\s+(and|&|aur|tatha|\+|with)\s+', ',', clean_target_text)
        candidate_items = [p.strip() for p in re.split(r'[,;]+', clean_target_text) if p.strip()]

        if not candidate_items:
            target_words = [w for w in tokens if w not in STOP_WORDS and w not in TAKE_WORDS and w not in WASTE_WORDS]
            candidate_items = [" ".join(target_words).strip()] if target_words else []

        if not candidate_items or not candidate_items[0]:
            return {
                "action": "unknown",
                "message_en": "Please specify which item(s) you consumed or removed.",
                "message_hi": "कृपया बताएं कि आपने कौन सा सामान खाया या हटाया।",
            }

        removed_items = []
        not_found_items = []
        available_inv = list(inv)

        for target_phrase in candidate_items:
            phrase_tokens = [w for w in WORD_RE.findall(target_phrase) if w not in STOP_WORDS]
            effective_target = " ".join(phrase_tokens) if phrase_tokens else target_phrase
            matched = None

            # 1. Direct or substring match
            for it in available_inv:
                en_l = it.name_en.lower()
                hi_l = it.name_hi.lower() if it.name_hi else ""
                if (effective_target in en_l or en_l in effective_target or
                    (hi_l and (effective_target in hi_l or hi_l in effective_target))):
                    matched = it
                    break

            # 2. Token overlap match
            if not matched and phrase_tokens:
                for it in available_inv:
                    for pt in phrase_tokens:
                        if len(pt) >= 3 and (pt in it.name_en.lower() or (it.name_hi and pt in it.name_hi.lower())):
                            matched = it
                            break
                    if matched:
                        break

            if matched:
                if fid:
                    remove_fridge_inventory_item(fid, matched.id)
                removed_items.append(matched)
                available_inv.remove(matched)
            else:
                not_found_items.append(target_phrase)

        verb_en = "Consumed" if action == "take" else "Discarded"
        verb_hi = "इस्तेमाल हुआ" if action == "take" else "फेंका गया"

        if len(candidate_items) > 1 or len(removed_items) > 1:
            if not removed_items:
                return {
                    "action": "not_found",
                    "target": ", ".join(not_found_items),
                    "message_en": f"None of the items ({', '.join(not_found_items)}) were found in your fridge inventory.",
                    "message_hi": f"कोई भी सामान ({', '.join(not_found_items)}) फ्रिज में नहीं मिला।",
                }
            names_en = ", ".join(it.name_en for it in removed_items)
            names_hi = ", ".join(it.name_hi or it.name_en for it in removed_items)
            not_found_msg_en = f" (Not found: {', '.join(not_found_items)})" if not_found_items else ""
            not_found_msg_hi = f" (नहीं मिले: {', '.join(not_found_items)})" if not_found_items else ""
            return {
                "action": "removed_multi",
                "removed": [{"id": it.id, "name_en": it.name_en, "name_hi": it.name_hi} for it in removed_items],
                "not_found": not_found_items,
                "message_en": f"{verb_en} {len(removed_items)} item(s): {names_en}.{not_found_msg_en}",
                "message_hi": f"{len(removed_items)} सामान {verb_hi}: {names_hi}।{not_found_msg_hi}",
            }
        elif len(removed_items) == 1:
            m = removed_items[0]
            return {
                "action": "removed",
                "item_id": m.id,
                "name_en": m.name_en,
                "name_hi": m.name_hi,
                "message_en": f"{verb_en}: {m.name_en}.",
                "message_hi": f"{verb_hi}: {m.name_hi or m.name_en}।",
            }
        else:
            tf = candidate_items[0]
            return {
                "action": "not_found",
                "target": tf,
                "message_en": f"'{tf}' was not found in your fridge inventory.",
                "message_hi": f"'{tf}' आपके फ्रिज में नहीं मिला।",
            }

    # 4. Handle Add / Classify command
    clean_add_text = raw_cmd
    for w in sorted(list(ADD_WORDS | {"i", "we", "just", "brought", "bought", "bring", "add", "added", "laye", "kharida"}), key=len, reverse=True):
        clean_add_text = re.sub(rf'^(i\s+)?{re.escape(w)}\s+', '', clean_add_text, flags=re.IGNORECASE)
        clean_add_text = re.sub(rf'\s+{re.escape(w)}$', '', clean_add_text, flags=re.IGNORECASE)

    clean_add_text = re.sub(r'\s+(and|&|aur|tatha|\+|with)\s+', ',', clean_add_text, flags=re.IGNORECASE)
    parts = [p.strip() for p in re.split(r'[,;]+', clean_add_text) if p.strip()]

    # If compound items detected (e.g. "lays, red bull, milk, spinach")
    if len(parts) > 1:
        auto_added_list = []
        needs_expiry_list = []
        client_tz_str = request.headers.get("X-Client-Timezone")
        client_tz = resolve_tz(client_tz_str)
        now_local = datetime.now(client_tz)
        today_str = now_local.strftime("%Y-%m-%d")

        for phrase in parts:
            c = classify_food(phrase)
            if not c.is_food:
                continue

            if c.requires_expiry_date:
                # Packaged good (e.g. Lays, Red Bull) needing label date
                needs_expiry_list.append(c.model_dump())
            else:
                # Fresh item / Made today / Grocery (e.g. Milk, Spinach) - auto add immediately!
                days = c.shelf_life_days if (c.shelf_life_days and c.shelf_life_days > 0) else 4
                exp_date_obj = now_local + timedelta(days=days)
                exp_str = exp_date_obj.strftime("%Y-%m-%d")

                item_create = InventoryItemCreate(
                    name_en=c.name_en,
                    name_hi=c.name_hi,
                    category=c.category,
                    shelf_life_days=days,
                    requires_expiry_date=False,
                    expiry_date=exp_str,
                    storage_tip=c.storage_tip
                )
                if fid:
                    saved_item = save_fridge_inventory_item(fid, item_create, client_tz=client_tz_str)
                    auto_added_list.append(saved_item.model_dump())
                else:
                    auto_added_list.append({
                        "id": secrets.token_hex(5),
                        "name_en": c.name_en,
                        "name_hi": c.name_hi,
                        "category": c.category,
                        "added_date": today_str,
                        "expiry_date": exp_str,
                        "days_left": days,
                        "days_elapsed": 0,
                        "is_expired": False,
                        "is_expiring_soon": days <= 2,
                        "requires_expiry_date": False,
                        "storage_tip": c.storage_tip,
                        "last_synced_at": now_local.strftime("%Y-%m-%d %H:%M:%S")
                    })

        fresh_names = ", ".join(i.get("name_en", "") for i in auto_added_list)
        return {
            "action": "multi_add",
            "auto_added": auto_added_list,
            "needs_expiry": needs_expiry_list,
            "message_en": f"Processed {len(parts)} items: Added fresh ({fresh_names or 'none'}) to dashboard. Please verify expiry date for {len(needs_expiry_list)} packaged item(s).",
            "message_hi": f"{len(parts)} सामान: ताज़ा सामान फ्रिज में जोड़ा गया, पैकेज्ड सामान की तारीख दर्ज करें।",
        }

    # Single item fallback
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
    """Inspects cookie or header to verify whether user is logged into a family fridge."""
    sess = get_current_session_data(request)
    if sess and sess.get("fridge_id"):
        fid = sess["fridge_id"]
        client_tz = request.headers.get("X-Client-Timezone")
        update_fridge_time_and_inventory(fid, tz_name=client_tz)
        
        # Read active timezone for fridge
        db, _ = init_firestore()
        active_tz = client_tz
        if not active_tz and db is not None:
            try:
                fdoc = db.collection("fridges").document(fid).get()
                if fdoc.exists:
                    active_tz = (fdoc.to_dict() or {}).get("timezone")
            except Exception:
                pass
        if not active_tz:
            active_tz = _IN_MEMORY_FRIDGES.get(fid, {}).get("timezone", "UTC")
            
        tz = resolve_tz(active_tz)
        now_local = datetime.now(tz)
        tz_code = now_local.tzname() or active_tz or "UTC"
        pin = sess.get("pin", "")
        if not pin and db is not None:
            try:
                fdoc = db.collection("fridges").document(fid).get()
                if fdoc.exists:
                    pin = (fdoc.to_dict() or {}).get("pin", "")
            except Exception:
                pass
        if not pin:
            pin = _IN_MEMORY_FRIDGES.get(fid, {}).get("pin", "")

        if pin and db is not None:
            try:
                db.collection("fridges").document(fid).set({"pin": pin}, merge=True)
            except Exception:
                pass

        return {
            "authenticated": True,
            "fridge_id": fid,
            "fridge_name": fid,
            "pin": pin,
            "timezone": active_tz,
            "timezone_code": tz_code,
            "last_sync_time": now_local.strftime(f"%I:%M:%S %p {tz_code}"),
            "last_login_at": now_local.strftime(f"%Y-%m-%d %H:%M:%S {tz_code}"),
        }
    return {
        "authenticated": False,
        "fridge_id": None,
        "fridge_name": None,
    }


@app.post("/api/auth/login", summary="Login to existing family fridge")
def api_auth_login(payload: AuthLoginRequest, request: Request, response: Response):
    """Authenticate with Fridge ID and PIN; sets 30-day cryptographically signed cookie."""
    fid = payload.fridge_id.strip()
    pin = payload.pin.strip()

    if not fid or not pin:
        raise HTTPException(status_code=400, detail="Fridge ID and PIN are required.")
    if not FRIDGE_RE.fullmatch(fid):
        raise HTTPException(
            status_code=422,
            detail="Fridge ID must be 3-30 characters long with letters (a-z, A-Z) and numbers (0-9) only (case-sensitive).",
        )
    if not PIN_RE.fullmatch(pin):
        raise HTTPException(
            status_code=422,
            detail="Family PIN must be 4-30 characters long with letters (a-z, A-Z) and numbers (0-9) only (case-sensitive).",
        )

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
            raise HTTPException(status_code=404, detail=f"Fridge '{fid}' not found. Please create a new fridge.")
        fdata = _IN_MEMORY_FRIDGES[fid]
        authenticated = hmac.compare_digest(fdata["pin_hash"], hash_pin(pin, fdata["pin_salt"]))

    if not authenticated:
        raise HTTPException(status_code=401, detail="Incorrect PIN for this fridge. Please try again.")

    token = secrets.token_urlsafe(48)
    token_hash = _session_token_hash(token)
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)

    session_data = {
        "fridge_id": fid,
        "pin": pin,
        "created_at": firestore.SERVER_TIMESTAMP if db is not None else datetime.now(timezone.utc).isoformat(),
        "expires_at": expires_at,
        "revoked": False,
    }

    if db is not None:
        try:
            db.collection("fridge_sessions").document(token_hash).set(session_data)
        except Exception as exc:
            logger.error(f"Failed to record session in Firestore: {exc}")
            _IN_MEMORY_SESSIONS[token_hash] = session_data
    else:
        _IN_MEMORY_SESSIONS[token_hash] = session_data

    # Track login time and automatically update items against addition dates
    client_tz = payload.timezone or request.headers.get("X-Client-Timezone")
    tz = resolve_tz(client_tz)
    now_local = datetime.now(tz)
    tz_code = now_local.tzname() or client_tz or "UTC"
    login_time_str = now_local.strftime(f"%Y-%m-%d %H:%M:%S {tz_code}")

    if db is not None:
        try:
            db.collection("fridges").document(fid).set({
                "pin": pin,
                "last_login_at": login_time_str,
                "last_active_at": login_time_str,
                "timezone": client_tz or "UTC",
            }, merge=True)
        except Exception as exc:
            logger.warning(f"Failed to record login timestamp for {fid}: {exc}")
    else:
        if fid in _IN_MEMORY_FRIDGES:
            _IN_MEMORY_FRIDGES[fid]["pin"] = pin
            _IN_MEMORY_FRIDGES[fid]["last_login_at"] = login_time_str
            _IN_MEMORY_FRIDGES[fid]["last_active_at"] = login_time_str
            _IN_MEMORY_FRIDGES[fid]["timezone"] = client_tz or "UTC"

    # Synchronize all inventory items with current local time vs added_date
    synced_items = update_fridge_time_and_inventory(fid, tz_name=client_tz)

    if db is not None:
        try:
            db.collection("fridges").document(fid).set({"pin": pin}, merge=True)
        except Exception:
            pass
    elif fid in _IN_MEMORY_FRIDGES:
        _IN_MEMORY_FRIDGES[fid]["pin"] = pin

    set_session_cookie(response, request, token)
    return {
        "ok": True,
        "fridge_id": fid,
        "pin": pin,
        "timezone": client_tz or "UTC",
        "timezone_code": tz_code,
        "last_login_at": login_time_str,
        "synced_items_count": len(synced_items),
        "message": f"Successfully logged into fridge '{fid}'. All food items time-synced."
    }



@app.post("/api/auth/create", summary="Create a new family fridge")
def api_auth_create(payload: AuthCreateRequest, request: Request, response: Response):
    """Creates a new fridge entry with a hashed family PIN and sets signed session cookie."""
    fid = payload.fridge_id.strip()
    pin = payload.pin.strip()

    if not fid or not pin:
        raise HTTPException(status_code=400, detail="Fridge ID and PIN are required.")
    if not FRIDGE_RE.fullmatch(fid):
        raise HTTPException(
            status_code=422,
            detail="Fridge ID must be 3-30 alphanumeric characters (letters a-z, A-Z and numbers 0-9 only, case-sensitive). No symbols, hyphens, or spaces allowed.",
        )
    if not PIN_RE.fullmatch(pin):
        raise HTTPException(
            status_code=422,
            detail="Family PIN must be 4-30 alphanumeric characters (letters a-z, A-Z and numbers 0-9 only, case-sensitive). No symbols or spaces allowed.",
        )

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
                "pin": pin,
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
        _IN_MEMORY_FRIDGES[fid] = {"pin_salt": salt, "pin_hash": p_hash, "pin": pin}

    token = secrets.token_urlsafe(48)
    token_hash = _session_token_hash(token)
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)

    session_data = {
        "fridge_id": fid,
        "pin": pin,
        "created_at": firestore.SERVER_TIMESTAMP if db is not None else datetime.now(timezone.utc).isoformat(),
        "expires_at": expires_at,
        "revoked": False,
    }

    if db is not None:
        try:
            db.collection("fridge_sessions").document(token_hash).set(session_data)
        except Exception as exc:
            logger.error(f"Failed to record session in Firestore: {exc}")
            _IN_MEMORY_SESSIONS[token_hash] = session_data
    else:
        _IN_MEMORY_SESSIONS[token_hash] = session_data

    client_tz = payload.timezone or request.headers.get("X-Client-Timezone")
    tz = resolve_tz(client_tz)
    now_local = datetime.now(tz)
    tz_code = now_local.tzname() or client_tz or "UTC"
    login_time_str = now_local.strftime(f"%Y-%m-%d %H:%M:%S {tz_code}")

    if db is not None:
        try:
            db.collection("fridges").document(fid).set({
                "last_login_at": login_time_str,
                "last_active_at": login_time_str,
                "timezone": client_tz or "UTC",
            }, merge=True)
        except Exception as exc:
            logger.warning(f"Failed to record login timestamp for {fid}: {exc}")
    else:
        if fid in _IN_MEMORY_FRIDGES:
            _IN_MEMORY_FRIDGES[fid]["last_login_at"] = login_time_str
            _IN_MEMORY_FRIDGES[fid]["last_active_at"] = login_time_str
            _IN_MEMORY_FRIDGES[fid]["timezone"] = client_tz or "UTC"

    update_fridge_time_and_inventory(fid, tz_name=client_tz)

    set_session_cookie(response, request, token)
    return {
        "ok": True,
        "fridge_id": fid,
        "pin": pin,
        "timezone": client_tz or "UTC",
        "timezone_code": tz_code,
        "last_login_at": login_time_str,
        "message": f"Fridge '{fid}' successfully created!"
    }


@app.post("/api/auth/logout", summary="Logout and revoke browser session")
def api_auth_logout(request: Request, response: Response):
    """Revokes session and deletes the session cookie."""
    cookie_val = request.cookies.get(SESSION_COOKIE_NAME)
    auth_header = request.headers.get("Authorization") or request.headers.get("X-Fridge-Session")
    token = verify_and_extract_token(cookie_val) or verify_and_extract_token(auth_header)

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
        "web_push_enabled": HAVE_WEBPUSH and bool(_VAPID_PUBLIC_KEY),
        "web_push_details": {
            "pywebpush_installed": HAVE_WEBPUSH,
            "vapid_public_key_loaded": bool(_VAPID_PUBLIC_KEY),
            "vapid_private_key_loaded": bool(_VAPID_PRIVATE_KEY),
        },
    }


# ============================================================================
# 12b. WEB PUSH NOTIFICATION API ENDPOINTS
# ============================================================================

class PushSubscribeRequest(BaseModel):
    subscription: dict = Field(description="Browser PushSubscription JSON object")


@app.get("/api/push/vapid-public-key", summary="Get VAPID Public Key for Push")
@app.get("/api/vapid-public-key", summary="Get VAPID Public Key for Push (Alias)")
def api_push_vapid_key():
    """Returns the VAPID public key needed by the browser to subscribe to push."""
    if not _VAPID_PUBLIC_KEY:
        raise HTTPException(status_code=503, detail="Push notifications not configured. VAPID keys missing.")
    return {"publicKey": _VAPID_PUBLIC_KEY}


@app.post("/api/push/subscribe", summary="Subscribe to Push Notifications")
def api_push_subscribe(payload: PushSubscribeRequest, request: Request):
    """Register a browser push subscription for the current fridge."""
    fid = require_authenticated_fridge(request)
    if not payload.subscription or not payload.subscription.get("endpoint"):
        raise HTTPException(status_code=400, detail="Invalid push subscription object.")

    _save_push_subscription(fid, payload.subscription)
    return {"ok": True, "message": "Push notifications enabled for this device."}


@app.post("/api/push/unsubscribe", summary="Unsubscribe from Push Notifications")
def api_push_unsubscribe(payload: PushSubscribeRequest, request: Request):
    """Remove a browser push subscription."""
    _remove_push_subscription(payload.subscription)
    return {"ok": True, "message": "Push notifications disabled."}


@app.post("/api/push/test", summary="Send a Test Push Notification")
def api_push_test(request: Request):
    """Send a test notification to all subscribed devices for the current fridge."""
    fid = require_authenticated_fridge(request)

    subs = _get_push_subscriptions_for_fridge(fid)
    if not subs:
        raise HTTPException(status_code=404, detail="No push subscriptions found. Enable notifications first.")

    sent = 0
    for sub in subs:
        if _send_push_notification(
            sub,
            title="FridgeInventory Test",
            body="Push notifications are working! You'll get alerts when food is about to expire.",
            tag="test-notification",
        ):
            sent += 1

    return {"ok": True, "sent": sent, "total_subs": len(subs)}


@app.post("/api/push/check-expiry", summary="Check expiry and send push alerts")
def api_push_check_expiry(request: Request):
    """Manually trigger expiry check and send push notifications for items expiring soon."""
    fid = require_authenticated_fridge(request)

    client_tz = request.headers.get("X-Client-Timezone")
    items = update_fridge_time_and_inventory(fid, tz_name=client_tz)
    expiring = [i for i in items if 0 <= i.days_left <= 2]
    expired = [i for i in items if i.days_left < 0]

    if not expiring and not expired:
        return {"ok": True, "message": "No items expiring soon.", "alerts_sent": 0}

    subs = _get_push_subscriptions_for_fridge(fid)
    sent = 0

    if expiring:
        names = ", ".join([i.name_en for i in expiring[:5]])
        suffix = f" +{len(expiring) - 5} more" if len(expiring) > 5 else ""
        if _send_push_to_fridge(fid, subs,
            title=f"[Fridge Alert] {len(expiring)} item(s) expiring soon!",
            body=f"{names}{suffix} — use them before they go bad!",
            tag="expiry-warning",
        ):
            sent += 1

    if expired:
        names = ", ".join([i.name_en for i in expired[:5]])
        suffix = f" +{len(expired) - 5} more" if len(expired) > 5 else ""
        if _send_push_to_fridge(fid, subs,
            title=f"[Fridge Alert] {len(expired)} item(s) expired!",
            body=f"{names}{suffix} — check and discard if needed.",
            tag="expiry-alert",
        ):
            sent += 1

    return {"ok": True, "expiring_count": len(expiring), "expired_count": len(expired), "alerts_sent": sent}


def get_configured_cron_secret() -> str:
    """
    Read CRON_SECRET strictly from environment variables.
    NO hardcoded fallback permitted to prevent unauthorized global execution.
    """
    return (os.getenv("CRON_SECRET") or "").strip().strip('"').strip("'")


def verify_cron_authorization(request: Request) -> bool:
    """
    Application-level authentication check for automated cron execution:
    - Requires CRON_SECRET to be explicitly configured in environment variables.
    - Fails closed when CRON_SECRET is missing or empty.
    - Verifies Bearer token in 'Authorization: Bearer <CRON_SECRET>'
    - OR 'X-Cron-Key' header
    - OR query parameter '?key=<CRON_SECRET>'
    - Normal user fridge sessions are strictly REJECTED (cron operates globally across all fridges).
    """
    cron_secret = get_configured_cron_secret()
    if not cron_secret:
        logger.warning("CRON_SECRET environment variable is missing or empty. Global expiry cron is locked (failed closed).")
        return False

    auth_header = request.headers.get("Authorization") or ""
    cron_key = request.headers.get("X-Cron-Key") or ""
    query_key = request.query_params.get("key") or ""

    # 1. Bearer token
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if hmac.compare_digest(token, cron_secret):
            return True

    # 2. X-Cron-Key header
    if cron_key and hmac.compare_digest(cron_key.strip(), cron_secret):
        return True

    # 3. Query param key
    if query_key and hmac.compare_digest(query_key.strip(), cron_secret):
        return True

    # Ordinary user fridge sessions strictly disallowed
    return False

@app.api_route("/api/check-expiry-cron", methods=["GET", "POST"], summary="Automated Cron Route: Check Expiry Across All Fridges & Broadcast Web Push")
def api_check_expiry_cron(request: Request):
    """
    Automated background cron route (Protected with Application-Level Authorization):
    - Requires a separately configured CRON_SECRET in server environment variables.
    - Fails closed when CRON_SECRET is missing.
    - Ordinary fridge sessions are strictly not accepted.
    - Iterates across all stored fridge database collections.
    - Dynamically compares current time with food item addition dates.
    - Signs and broadcasts push alerts to registered user subscriptions using web-push.
    """
    cron_secret = get_configured_cron_secret()
    if not cron_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cron service unavailable: CRON_SECRET environment variable is not configured on this server.",
        )
    if not verify_cron_authorization(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Access to /api/check-expiry-cron requires a valid configured CRON_SECRET.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    db, _ = init_firestore()
    all_fridge_ids = set()

    if db is not None:
        try:
            for doc in db.collection("fridges").stream():
                all_fridge_ids.add(doc.id)
        except Exception as exc:
            logger.error(f"Failed to stream fridges in cron: {exc}")

    for fid in _IN_MEMORY_FRIDGES:
        all_fridge_ids.add(fid)

    for fid in _IN_MEMORY_INVENTORIES:
        all_fridge_ids.add(fid)

    total_alerts_broadcast = 0
    details_list: List[dict] = []

    for fid in sorted(all_fridge_ids):
        # Sync time & update days_left against added_date
        items = update_fridge_time_and_inventory(fid)
        # Find items expiring precisely within upcoming 24h window (0 to 1 day) or already expired
        expiring_24h = [i for i in items if 0 <= i.days_left <= 1]
        expired = [i for i in items if i.days_left < 0]

        subs = _get_push_subscriptions_for_fridge(fid)
        if not subs:
            continue

        sent_for_fridge = 0
        if expiring_24h:
            names = ", ".join([i.name_en for i in expiring_24h[:4]])
            suffix = f" (+{len(expiring_24h)-4} more)" if len(expiring_24h) > 4 else ""
            if _send_push_to_fridge(
                fid, subs,
                title=f"[Fridge Alert] {len(expiring_24h)} item(s) expiring within 24 hours!",
                body=f"{names}{suffix} — consume soon to avoid food waste!",
                tag="expiry-warning-24h",
            ):
                sent_for_fridge += 1

        if expired:
            names = ", ".join([i.name_en for i in expired[:4]])
            suffix = f" (+{len(expired)-4} more)" if len(expired) > 4 else ""
            if _send_push_to_fridge(
                fid, subs,
                title=f"[Fridge Alert] {len(expired)} item(s) expired in fridge!",
                body=f"{names}{suffix} — inspect and clear.",
                tag="expiry-alert",
            ):
                sent_for_fridge += 1

        total_alerts_broadcast += sent_for_fridge
        details_list.append({
            "fridge_id": fid,
            "items_count": len(items),
            "expiring_24h": len(expiring_24h),
            "expired": len(expired),
            "subscriptions": len(subs),
            "alerts_sent": sent_for_fridge
        })

    return {
        "ok": True,
        "service": "FridgeInventory Expiry Cron",
        "timestamp_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "total_fridges_checked": len(all_fridge_ids),
        "total_alerts_broadcast": total_alerts_broadcast,
        "details": details_list
    }



def _send_push_to_fridge(fridge_id: str, subs: List[dict], title: str, body: str, tag: str = "fridge-alert", tz_name: Optional[str] = None) -> bool:
    """Send push notification to all subscriptions for a fridge with TTL buffering."""
    if not subs:
        return False
    success = False
    for sub in subs:
        if _send_push_notification(sub, title=title, body=body, tag=tag, tz_name=tz_name):
            success = True
    return success


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


@app.post("/api/scanner/photo", response_model=ClassifyResponse, summary="Scan Food Photo with AI Vision")
@app.post("/classify/photo", response_model=ClassifyResponse, summary="Scan Food Photo with AI Vision (Alias)")
def api_classify_photo(payload: PhotoScanRequest, request: Request, fridge: Optional[str] = None):
    """Scan and identify food in an uploaded photo or camera snapshot using Gemini AI Vision.
    Fresh food items without printed expiry dates are automatically added to the active fridge inventory.
    Packaged snacks or items requiring printed expiry dates are returned in needs_expiry for user confirmation.
    """
    if not payload.image or not payload.image.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Photo image data cannot be empty.",
        )
    target_fid = (payload.fridge_id or fridge or "").strip()
    fid = None
    if target_fid:
        # Deliberate authorization check: knowing an ID alone is never proof of permission!
        fid = require_authenticated_fridge(request, requested_fridge_id=target_fid)
    else:
        fid = get_current_fridge_id(request)

    client_tz = payload.timezone or request.headers.get("X-Client-Timezone")
    return classify_food_photo(payload.image, payload.mime_type, fridge_id=fid, client_tz=client_tz)



class SpellingCheckRequest(BaseModel):
    text: str = Field(description="Raw ingredients string, comma or space separated")

@app.post("/api/rescue/correct-spelling", summary="Correct spelling errors in ingredient input")
def api_rescue_correct_spelling(payload: SpellingCheckRequest):
    """Detects and fixes spelling typos in user-entered ingredients."""
    parts = [p.strip() for p in re.split(r'[,;]+', payload.text) if p.strip()]
    corrected, corrections = correct_ingredients_list(parts)
    return {
        "original": parts,
        "corrected": corrected,
        "corrected_text": ", ".join(corrected),
        "corrections": corrections,
        "has_corrections": len(corrections) > 0,
    }

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
    """Retrieve all tracked items for the active fridge, dynamically syncing freshness with local timezone."""
    # Deliberate authorization check: strictly requires valid session matching requested fridge
    fid = require_authenticated_fridge(request, requested_fridge_id=fridge)
    client_tz = request.headers.get("X-Client-Timezone")
    return get_fridge_inventory(fid, tz_name=client_tz)


@app.post("/api/inventory", response_model=InventoryItem, summary="Add Item to Inventory")
def api_add_inventory(item: InventoryItemCreate, request: Request, fridge: Optional[str] = None):
    """Add a verified food item into the active fridge inventory."""
    # Deliberate authorization check: strictly requires valid session matching requested fridge
    fid = require_authenticated_fridge(request, requested_fridge_id=fridge)
    client_tz = request.headers.get("X-Client-Timezone")
    return save_fridge_inventory_item(fid, item, client_tz=client_tz)


@app.delete("/api/inventory/{item_id}", summary="Delete / Consume Inventory Item")
def api_delete_inventory(item_id: str, request: Request, fridge: Optional[str] = None):
    """Remove an item from the active fridge inventory."""
    # Deliberate authorization check: strictly requires valid session matching requested fridge
    fid = require_authenticated_fridge(request, requested_fridge_id=fridge)
    removed = remove_fridge_inventory_item(fid, item_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not found.")
    return {"ok": True, "message": f"Item {item_id} removed successfully."}


# ============================================================================
# 13. EMBEDDED FRONTEND UI (FASTAPI HTMLRESPONSE)
# ============================================================================

HTML_CONTENT = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>FridgeInventory - Smart Food Inventory & Zero-Waste Kitchen</title>
  <!-- Progressive Web App / Mobile Optimization -->
  <meta name="application-name" content="FridgeInventory">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="default">
  <meta name="apple-mobile-web-app-title" content="FridgeInventory">
  <meta name="theme-color" content="#F59E0B">
  <meta name="msapplication-TileColor" content="#F59E0B">
  <!-- PWA Manifest & App Icons -->
  <link rel="manifest" href="/manifest.json?v=3">
  <!-- Modern SVG & High-Resolution PNG App Icons & Favicons -->
  <link rel="icon" type="image/png" sizes="32x32" href="/static/icons/icon-192.png?v=3">
  <link rel="icon" type="image/png" sizes="192x192" href="/static/icons/icon-192.png?v=3">
  <link rel="icon" type="image/svg+xml" href="/static/icons/icon.svg?v=3">
  <link rel="apple-touch-icon" sizes="180x180" href="/static/icons/apple-touch-icon.png?v=3">
  <link rel="shortcut icon" type="image/x-icon" href="/favicon.ico?v=3">
  <link rel="icon" type="image/x-icon" href="/favicon.ico?v=3">
  <!-- Tailwind CSS CDN -->
  <script src="https://cdn.tailwindcss.com"></script>
  <!-- Google Fonts: Inter, Poppins, Noto Sans Devanagari -->
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Noto+Sans+Devanagari:wght@400;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          fontFamily: {
            sans: ['Inter', 'Noto Sans Devanagari', 'sans-serif'],
            mono: ['JetBrains Mono', 'monospace'],
          },
          colors: {
            brand: {
              50: '#fffbeb',
              100: '#fef3c7',
              200: '#fde68a',
              300: '#fcd34d',
              400: '#fbbf24',
              500: '#f59e0b',
              600: '#d97706',
              700: '#b45309',
              800: '#92400e',
              900: '#78350f',
              950: '#451a03'
            },
            leaf: {
              50: '#ecfdf5',
              100: '#d1fae5',
              200: '#a7f3d0',
              300: '#6ee7b7',
              400: '#34d399',
              500: '#10b981',
              600: '#059669',
              700: '#047857',
              800: '#065f46',
              900: '#064e3b',
              950: '#022c22'
            }
          }
        }
      }
    };
    const savedTheme = localStorage.getItem('fi_theme') || localStorage.getItem('rasoi_theme');
    if (savedTheme === 'dark' || (!savedTheme && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
      document.documentElement.classList.add('dark');
    } else {
      document.documentElement.classList.remove('dark');
    }
  </script>
  <style>
    select option {
      background-color: #1c1917;
      color: #f5f5f4;
    }
    html:not(.dark) select option {
      background-color: #ffffff;
      color: #1c1917;
    }
    @keyframes scannerLaser {
      0% { top: 3%; opacity: 0.85; }
      50% { top: 92%; opacity: 1; }
      100% { top: 3%; opacity: 0.85; }
    }
    .scanner-laser-bar {
      position: absolute;
      left: 0;
      right: 0;
      height: 3px;
      background: linear-gradient(90deg, transparent, #10b981, #06b6d4, transparent);
      box-shadow: 0 0 14px #10b981;
      animation: scannerLaser 1.8s ease-in-out infinite;
      pointer-events: none;
    }
    @keyframes scanPulse {
      0%, 100% { transform: scale(1); opacity: 1; }
      50% { transform: scale(1.05); opacity: 0.9; }
    }
    .animate-scan-pulse {
      animation: scanPulse 2s ease-in-out infinite;
    }
    @keyframes voiceTintPulse {
      0% {
        box-shadow: inset 0 0 70px 25px rgba(249, 115, 22, 0.45), inset 0 0 130px 50px rgba(234, 88, 12, 0.3);
        opacity: 0.85;
      }
      100% {
        box-shadow: inset 0 0 105px 40px rgba(249, 115, 22, 0.85), inset 0 0 200px 90px rgba(234, 88, 12, 0.65);
        opacity: 1;
      }
    }
    .voice-edge-tint {
      box-shadow: inset 0 0 85px 30px rgba(249, 115, 22, 0.7), inset 0 0 160px 65px rgba(234, 88, 12, 0.5);
      background: 
        radial-gradient(circle at 0% 0%, rgba(249, 115, 22, 0.6) 0%, transparent 45%),
        radial-gradient(circle at 100% 0%, rgba(249, 115, 22, 0.6) 0%, transparent 45%),
        radial-gradient(circle at 0% 100%, rgba(249, 115, 22, 0.6) 0%, transparent 45%),
        radial-gradient(circle at 100% 100%, rgba(249, 115, 22, 0.6) 0%, transparent 45%),
        radial-gradient(circle at center, transparent 52%, rgba(249, 115, 22, 0.22) 82%, rgba(234, 88, 12, 0.45) 100%);
      animation: voiceTintPulse 1.5s ease-in-out infinite alternate;
      pointer-events: none;
    }
    /* Mobile-first zero-overlap & smooth touch responsiveness */
    .scrollbar-none::-webkit-scrollbar {
      display: none;
    }
    .scrollbar-none {
      -ms-overflow-style: none;
      scrollbar-width: none;
    }
    @media (max-width: 640px) {
      input, select, textarea, button {
        touch-action: manipulation;
      }
      .chip {
        display: inline-flex;
        align-items: center;
        white-space: nowrap;
      }
    }
  </style>
</head>
<body class="bg-stone-50 dark:bg-stone-950 text-stone-800 dark:text-stone-100 min-h-screen flex flex-col antialiased selection:bg-brand-500 selection:text-white transition-colors duration-150">

  <!-- ORANGE TINT CORNER & EDGE VIGNETTE ON AUDIO RECORDING -->
  <div id="voice-recording-tint" class="voice-edge-tint fixed inset-0 z-[9999] transition-opacity duration-300 opacity-0 pointer-events-none hidden"></div>

  <!-- FLOATING VOICE LISTENING BANNER -->
  <div id="voice-recording-banner" class="fixed top-5 left-1/2 -translate-x-1/2 z-[10000] transition-all duration-300 transform -translate-y-12 opacity-0 pointer-events-none hidden">
    <div class="px-5 py-2.5 rounded-full bg-gradient-to-r from-orange-500 via-amber-500 to-orange-600 text-white font-bold text-xs sm:text-sm shadow-2xl flex items-center gap-3 border border-orange-300/60 ring-4 ring-orange-500/25">
      <span class="flex h-3 w-3 relative">
        <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-white opacity-75"></span>
        <span class="relative inline-flex rounded-full h-3 w-3 bg-white"></span>
      </span>
      <span id="voice-banner-text">🎙️ Listening... Speak now</span>
      <svg class="w-4 h-4 text-white animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>
    </div>
  </div>

  <!-- GITHUB-INSPIRED TOP HEADER (CLEAN, COMPACT & ZERO-OVERLAP ON MOBILE) -->
  <header class="bg-white dark:bg-stone-900 border-b border-stone-200 dark:border-stone-800 sticky top-0 z-40 transition-colors">
    <div class="max-w-6xl mx-auto px-3 sm:px-4 py-2 sm:py-2.5 flex items-center justify-between gap-2 sm:gap-3">
      
      <!-- Brand Logo, Title & Logged-in Fridge Badge (Desktop) -->
      <div class="flex items-center gap-2 sm:gap-2.5 min-w-0">
        <img src="/static/icons/icon-192.png?v=3" alt="FridgeInventory" class="w-8 h-8 rounded-xl shadow-xs shrink-0 object-contain">
        <div class="flex items-center gap-1.5 sm:gap-2 min-w-0">
          <span class="font-extrabold text-sm sm:text-base tracking-tight text-stone-900 dark:text-white truncate" data-i18n="app_title">FridgeInventory</span>
          <span class="text-[10px] bg-stone-100 dark:bg-stone-800 text-stone-500 dark:text-stone-400 font-semibold px-1.5 py-0.5 rounded-md hidden md:inline">v2.2</span>
          
          <!-- Compact Logged-in Fridge Pill (Visible on Desktop; Mobile gets dedicated pill in subheader) -->
          <div id="header-breadcrumb" class="hidden md:inline-flex items-center gap-1.5 px-2.5 py-1 rounded-xl bg-amber-50 dark:bg-stone-800 border border-amber-200 dark:border-amber-500/40 text-xs font-mono font-bold shadow-2xs truncate shrink-0">
            <span id="header-fridge-name" class="truncate max-w-[140px] lg:max-w-[200px] text-amber-950 dark:text-amber-200 font-extrabold tracking-wide">MY-FRIDGE</span>
            <button type="button" id="copy-fid-btn" class="text-amber-700/70 dark:text-amber-400/80 hover:text-amber-900 dark:hover:text-amber-100 transition p-0.5 shrink-0 cursor-pointer" title="Copy Fridge ID">
              <svg class="w-3.5 h-3.5 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"></path></svg>
            </button>
          </div>
        </div>
      </div>

      <!-- Controls: Offline Indicator, Language, Theme, Logout/Login -->
      <div class="flex items-center gap-1 sm:gap-2 text-xs shrink-0">
        
        <!-- Offline Status Badge (Visible when internet is disconnected) -->
        <span id="header-offline-badge" class="hidden items-center gap-1 px-2 py-1 rounded-full text-[10px] font-bold bg-amber-100 dark:bg-amber-950/70 text-amber-800 dark:text-amber-300 border border-amber-300 dark:border-amber-800 shrink-0" title="Device is offline. Local inventory cache and offline alerts active.">
          <span class="w-1.5 h-1.5 rounded-full bg-amber-500"></span>
          <span class="hidden sm:inline">Offline</span>
        </span>

        <!-- Add to Home Screen Button -->
        <button id="pwa-install-btn" type="button" onclick="handlePwaInstallClick()" class="px-2 sm:px-2.5 py-1.5 rounded-xl bg-gradient-to-r from-amber-500/10 to-brand-500/10 hover:from-amber-500/20 hover:to-brand-500/20 text-brand-700 dark:text-brand-300 font-semibold text-xs border border-amber-500/30 transition flex items-center gap-1 shadow-2xs shrink-0" title="Add to Home Screen / Install App">
          <svg class="w-3.5 h-3.5 text-brand-600 dark:text-brand-400 shrink-0 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 18h.01M8 21h8a2 2 0 002-2V5a2 2 0 00-2-2H8a2 2 0 00-2 2v14a2 2 0 002 2z"/></svg>
          <span class="hidden md:inline" data-i18n="pwa_install_btn">Add to Home Screen</span>
          <span class="hidden sm:inline md:hidden" data-i18n="pwa_install_btn_short">Install</span>
          <span class="inline sm:hidden font-bold text-[11px]">App</span>
        </button>

        <!-- Language Toggle Button -->
        <button id="lang-toggle-btn" class="px-2 sm:px-2.5 py-1.5 rounded-xl border border-stone-200 dark:border-stone-700 bg-stone-100 dark:bg-stone-800 hover:bg-stone-200 dark:hover:bg-stone-700 text-stone-700 dark:text-stone-300 font-semibold text-xs transition flex items-center gap-1 shrink-0" title="Switch Language">
          <svg class="w-3.5 h-3.5 shrink-0 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 5h12M9 3v2m1.048 9.5A18.022 18.022 0 016.412 9m6.088 9h7M11 21l5-10 5 10M12.751 5C11.783 10.77 8.07 15.61 3 18.129"></path></svg>
          <span id="lang-label" class="text-[11px] sm:text-xs">हिन्दी</span>
        </button>

        <!-- Theme Toggle Button -->
        <button id="theme-toggle-btn" class="p-1.5 sm:p-2 rounded-xl border border-stone-200 dark:border-stone-700 bg-stone-100 dark:bg-stone-800 hover:bg-stone-200 dark:hover:bg-stone-700 text-stone-700 dark:text-stone-300 transition shrink-0" title="Toggle Dark/Light Mode">
          <svg id="theme-icon-sun" class="w-4 h-4 hidden pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z"></path></svg>
          <svg id="theme-icon-moon" class="w-4 h-4 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z"></path></svg>
        </button>

        <!-- Auth Actions -->
        <div id="auth-logged-in" class="hidden items-center shrink-0">
          <button id="logout-btn" class="px-2 sm:px-2.5 py-1.5 rounded-xl bg-stone-100 dark:bg-stone-800 hover:bg-rose-50 dark:hover:bg-rose-950/60 hover:text-rose-600 text-stone-600 dark:text-stone-300 font-semibold text-xs border border-stone-200 dark:border-stone-700 transition flex items-center gap-1 shrink-0" title="Logout" data-i18n="logout_btn">
            <svg class="w-3.5 h-3.5 inline sm:hidden pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"/></svg>
            <span class="hidden sm:inline">Logout</span>
          </button>
        </div>

        <div id="auth-logged-out" class="flex items-center shrink-0">
          <button id="open-auth-btn" class="px-2.5 sm:px-3 py-1.5 rounded-xl bg-stone-900 dark:bg-white hover:bg-stone-800 dark:hover:bg-stone-100 text-white dark:text-stone-900 font-semibold text-xs shadow-sm transition flex items-center gap-1 sm:gap-1.5 shrink-0" data-i18n="login_create_btn">
            <svg class="w-3.5 h-3.5 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 16l-4-4m0 0l4-4m-4 4h14m-5 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h7a3 3 0 013 3v1"></path></svg>
            <span>Login</span>
          </button>
        </div>
      </div>
    </div>
  </header>

  <!-- DEDICATED LIVE FRESHNESS & STATUS SUB-BAR (RESPONSIVE & NON-OVERLAPPING ON ALL DEVICES) -->
  <div id="sub-header-bar" class="hidden bg-stone-100/95 dark:bg-stone-900/95 border-b border-stone-200/90 dark:border-stone-800 text-xs py-2 px-3 sm:px-4 transition-all">
    <div class="max-w-6xl mx-auto flex flex-col md:flex-row items-stretch md:items-center justify-between gap-2 sm:gap-2.5">
      
      <!-- Top Row on Mobile / Left on Desktop: Mobile Fridge ID, Live Clock & Timezone Selector -->
      <div class="flex items-center gap-1.5 sm:gap-2 flex-wrap min-w-0">
        
        <!-- Mobile-Only Fridge ID Pill (Takes zero space on desktop; provides instant fridge ID on mobile) -->
        <div id="sub-header-fridge-pill" class="inline-flex md:hidden items-center gap-1 px-2.5 py-1 rounded-xl bg-amber-50 dark:bg-stone-800 border border-amber-200 dark:border-amber-500/40 text-[11px] font-mono font-bold shadow-2xs shrink-0">
          <span id="sub-header-fridge-name" class="truncate max-w-[130px] text-amber-950 dark:text-amber-200 font-extrabold">MY-FRIDGE</span>
          <button type="button" id="sub-copy-fid-btn" class="text-amber-700/70 dark:text-amber-400/80 hover:text-amber-900 dark:hover:text-amber-100 transition p-0.5 shrink-0 cursor-pointer" title="Copy Fridge ID">
            <svg class="w-3 h-3 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"></path></svg>
          </button>
        </div>

        <!-- Live Clock Pill -->
        <div id="header-sync-pill" class="inline-flex items-center gap-1.5 px-2.5 sm:px-3 py-1 rounded-full text-[10px] sm:text-[11px] font-semibold bg-emerald-50 dark:bg-emerald-950/60 text-emerald-700 dark:text-emerald-300 border border-emerald-200 dark:border-emerald-800/80 shadow-2xs shrink-0" title="Live clock & automatic real-time freshness tracking">
          <span class="w-1.5 h-1.5 sm:w-2 sm:h-2 rounded-full bg-emerald-500 animate-pulse shrink-0"></span>
          <span id="header-sync-status" class="font-mono font-bold">Live • Syncing...</span>
        </div>

        <!-- Timezone Selector Dropdown -->
        <div class="inline-flex items-center gap-1 px-2 py-1 rounded-xl bg-white dark:bg-stone-800 border border-stone-200 dark:border-stone-700 shadow-2xs shrink-0">
          <svg class="w-3 h-3 text-stone-400 shrink-0 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9"></path></svg>
          <span class="text-stone-400 text-[9px] uppercase font-bold tracking-wider">TZ:</span>
          <select id="user-tz-select" class="bg-transparent text-[11px] sm:text-xs font-semibold text-stone-800 dark:text-stone-200 border-none outline-none cursor-pointer focus:ring-0 pr-1 max-w-[125px] sm:max-w-none truncate" title="Select Global Timezone">
            <option value="auto">Auto (Local Time)</option>
          </select>
        </div>
      </div>

      <!-- Bottom Row on Mobile / Right on Desktop: Family PIN, Push Alerts Status, and Share Button -->
      <div class="flex items-center gap-1.5 sm:gap-2 flex-wrap justify-between md:justify-end">
        
        <!-- Family PIN Container with Direct Revealed PIN & Eye Toggle -->
        <div id="header-pin-container" class="inline-flex items-center gap-1 px-2.5 py-1 rounded-xl bg-white dark:bg-stone-800 border border-stone-200 dark:border-stone-700 text-xs font-mono shadow-2xs shrink-0">
          <span class="text-[10px] text-stone-400 font-sans font-medium">PIN:</span>
          <span id="sidebar-fridge-pin" class="font-bold text-stone-800 dark:text-stone-100 tracking-wider">----</span>
          <button type="button" id="toggle-pin-btn" class="text-stone-400 hover:text-brand-600 dark:hover:text-brand-400 ml-0.5 p-0.5 cursor-pointer select-none" title="Hide PIN" aria-label="Show or hide PIN">
            <svg id="pin-eye-icon" class="w-3.5 h-3.5 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l18 18"></path></svg>
          </button>
          <button type="button" id="copy-pin-btn" class="text-stone-400 hover:text-brand-600 dark:hover:text-brand-400 p-0.5 cursor-pointer select-none" title="Copy PIN" aria-label="Copy PIN">
            <svg class="w-3 h-3 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"></path></svg>
          </button>
        </div>

        <!-- Quick Push Alerts Button -->
        <button type="button" id="header-push-pill" class="inline-flex items-center gap-1 px-2.5 py-1 rounded-xl bg-white dark:bg-stone-800 hover:bg-stone-50 dark:hover:bg-stone-750 border border-stone-200 dark:border-stone-700 text-xs font-semibold transition shadow-2xs shrink-0" title="View Offline Push Notifications">
          <svg class="w-3.5 h-3.5 text-amber-500 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"></path></svg>
          <span class="text-stone-600 dark:text-stone-400" data-i18n="header_alerts_label">Alerts:</span>
          <span id="push-status-badge" class="font-bold text-[10px] px-1.5 py-0.5 rounded-full bg-stone-100 dark:bg-stone-700 text-stone-600 dark:text-stone-300">OFF</span>
        </button>

        <!-- Share Fridge with Family Button -->
        <button type="button" id="share-fridge-btn" class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-xl bg-white dark:bg-stone-800 hover:bg-stone-50 dark:hover:bg-stone-750 text-stone-700 dark:text-stone-300 border border-stone-200 dark:border-stone-700 text-xs font-semibold transition shadow-2xs shrink-0" title="Share Fridge with Family">
          <svg class="w-3.5 h-3.5 text-brand-600 dark:text-brand-400 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8.684 13.342C8.886 12.938 9 12.482 9 12c0-.482-.114-.938-.316-1.342m0 2.684a3 3 0 110-2.684m0 2.684l6.632 3.316m-6.632-6l6.632-3.316m0 0a3 3 0 105.367-2.684 3 3 0 00-5.367 2.684zm0 9.316a3 3 0 105.368 2.684 3 3 0 00-5.368-2.684z"></path></svg>
          <span data-i18n="sidebar_share_btn">Share</span>
        </button>

      </div>
    </div>
  </div>

  <!-- MAIN CONTAINER -->
  <main class="flex-grow max-w-6xl w-full mx-auto px-4 py-5 space-y-6">

    <!-- LOGGED OUT WELCOME BUBBLE / CARD (SHOWN WHEN NOT LOGGED IN) -->
    <div id="logged-out-bubble" class="max-w-2xl mx-auto my-6 sm:my-10 p-7 sm:p-10 bg-white dark:bg-stone-900 rounded-3xl border border-stone-200/90 dark:border-stone-800 shadow-xl text-center space-y-6 transition-all">
      <img src="/static/icons/icon-192.png?v=3" alt="FridgeInventory" class="w-16 h-16 rounded-2xl shadow-md mx-auto object-contain">

      <div>
        <h2 class="text-2xl sm:text-3xl font-extrabold tracking-tight text-stone-900 dark:text-white" data-i18n="bubble_title">
          Welcome to FridgeInventory
        </h2>
        <p class="text-xs sm:text-sm text-stone-600 dark:text-stone-300 mt-2 max-w-lg mx-auto leading-relaxed" data-i18n="bubble_desc">
          Your smart household kitchen companion for effortless inventory tracking, offline push alerts, and AI-powered zero-waste cooking.
        </p>
      </div>

      <!-- Feature Highlight Cards -->
      <div class="grid grid-cols-1 sm:grid-cols-3 gap-3.5 text-left pt-1">
        <div class="p-4 rounded-2xl bg-stone-50 dark:bg-stone-800/60 border border-stone-200/70 dark:border-stone-700/60">
          <div class="w-8 h-8 rounded-xl bg-brand-100 dark:bg-stone-800 text-brand-700 dark:text-brand-300 flex items-center justify-center mb-2.5">
            <svg class="w-4.5 h-4.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"></path></svg>
          </div>
          <h3 class="font-bold text-xs sm:text-sm text-stone-900 dark:text-white" data-i18n="feat_track_title">Smart Food Tracking</h3>
          <p class="text-xs text-stone-500 dark:text-stone-400 mt-1" data-i18n="feat_track_desc">Keep real-time tabs on fresh produce, dairy, groceries, and cooked meals.</p>
        </div>
        <div class="p-4 rounded-2xl bg-stone-50 dark:bg-stone-800/60 border border-stone-200/70 dark:border-stone-700/60">
          <div class="w-8 h-8 rounded-xl bg-indigo-100 dark:bg-stone-800 text-indigo-700 dark:text-indigo-300 flex items-center justify-center mb-2.5">
            <svg class="w-4.5 h-4.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"></path></svg>
          </div>
          <h3 class="font-bold text-xs sm:text-sm text-stone-900 dark:text-white" data-i18n="feat_expiry_title">Offline Push Alerts</h3>
          <p class="text-xs text-stone-500 dark:text-stone-400 mt-1" data-i18n="feat_expiry_desc">Background Service Worker alerts you before food expires, even when offline.</p>
        </div>
        <div class="p-4 rounded-2xl bg-stone-50 dark:bg-stone-800/60 border border-stone-200/70 dark:border-stone-700/60">
          <div class="w-8 h-8 rounded-xl bg-emerald-100 dark:bg-stone-800 text-emerald-700 dark:text-emerald-300 flex items-center justify-center mb-2.5">
            <svg class="w-4.5 h-4.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg>
          </div>
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
    </div>

    <!-- MAIN APP DASHBOARD (SHOWN ONLY WHEN LOGGED IN) -->
    <div id="app-dashboard" class="hidden space-y-6">

      <!-- GITHUB-STYLE HORIZONTAL SUBNAV BAR -->
      <div class="border-b border-stone-200 dark:border-stone-800 -mx-4 px-4 sm:mx-0 sm:px-0">
        <nav class="flex space-x-1 sm:space-x-3 overflow-x-auto text-xs sm:text-sm font-semibold pb-px scrollbar-none touch-pan-x">
          <button id="tab-inventory-btn" class="tab-btn py-2.5 sm:py-3 px-2.5 sm:px-3 border-b-2 border-brand-600 text-brand-600 dark:text-brand-400 flex items-center gap-1.5 sm:gap-2 whitespace-nowrap shrink-0">
            <svg class="w-4 h-4 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"></path></svg>
            <span data-i18n="tab_inventory">Fridge Inventory</span>
            <span id="tab-inventory-count" class="bg-stone-100 dark:bg-stone-800 text-stone-700 dark:text-stone-300 text-[11px] px-2 py-0.5 rounded-full font-bold">0</span>
          </button>
          <button id="tab-urgent-btn" class="tab-btn py-2.5 sm:py-3 px-2.5 sm:px-3 border-b-2 border-transparent text-stone-500 dark:text-stone-400 hover:text-stone-800 dark:hover:text-stone-200 flex items-center gap-1.5 sm:gap-2 whitespace-nowrap shrink-0">
            <svg class="w-4 h-4 text-amber-500 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"></path></svg>
            <span data-i18n="tab_urgent">Expiring Soon</span>
            <span id="tab-urgent-count" class="bg-amber-100 dark:bg-amber-950/60 text-brand-700 dark:text-brand-300 text-[11px] px-2 py-0.5 rounded-full font-bold">0</span>
          </button>
          <button id="tab-push-btn" class="tab-btn py-2.5 sm:py-3 px-2.5 sm:px-3 border-b-2 border-transparent text-stone-500 dark:text-stone-400 hover:text-stone-800 dark:hover:text-stone-200 flex items-center gap-1.5 sm:gap-2 whitespace-nowrap shrink-0">
            <svg class="w-4 h-4 text-indigo-500 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"></path></svg>
            <span data-i18n="tab_push">Offline Push Alerts</span>
            <span id="push-nav-badge" class="text-[10px] px-2 py-0.5 rounded-full bg-stone-100 dark:bg-stone-800 text-stone-600 dark:text-stone-400 font-bold">OFF</span>
          </button>
          <button id="tab-rescue-btn" class="tab-btn py-2.5 sm:py-3 px-2.5 sm:px-3 border-b-2 border-transparent text-stone-500 dark:text-stone-400 hover:text-stone-800 dark:hover:text-stone-200 flex items-center gap-1.5 sm:gap-2 whitespace-nowrap shrink-0">
            <svg class="w-4 h-4 text-emerald-500 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg>
            <span data-i18n="tab_rescue">Rescue Chef</span>
            <span class="bg-leaf-50 dark:bg-leaf-950/60 text-leaf-700 dark:text-leaf-300 text-[11px] px-2 py-0.5 rounded-full font-bold">AI Zero-Waste</span>
          </button>
          <button id="tab-test-btn" class="tab-btn py-2.5 sm:py-3 px-2.5 sm:px-3 border-b-2 border-transparent text-stone-500 dark:text-stone-400 hover:text-stone-800 dark:hover:text-stone-200 flex items-center gap-1.5 sm:gap-2 whitespace-nowrap shrink-0">
            <svg class="w-4 h-4 text-stone-400 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>
            <span data-i18n="tab_scanner">Food Scanner</span>
          </button>
        </nav>
      </div>

      <!-- TAB 1: INVENTORY VIEW -->
      <section id="section-inventory" class="tab-section space-y-6">
        
        <!-- NATURAL LANGUAGE COMMAND BAR (GITHUB COMMAND PALETTE STYLE) -->
        <div class="bg-white dark:bg-stone-900 rounded-3xl p-5 sm:p-6 border border-stone-200/80 dark:border-stone-800 shadow-sm transition-colors">
          <div class="max-w-2xl">
            <h2 class="text-base sm:text-lg font-bold text-stone-900 dark:text-white flex items-center gap-2">
              <span data-i18n="cmd_heading">Smart Voice & Text Control</span>
            </h2>
            <p class="text-xs text-stone-500 dark:text-stone-400 mt-1 mb-4" data-i18n="cmd_sub">
              Tell it what you brought or consumed. Example: "brought 2kg palak", "i ate lays", "used milk", "khaya paneer", "clear all".
            </p>
          </div>

          <form id="command-form" class="flex flex-col sm:flex-row gap-2.5">
            <div class="relative flex-grow w-full">
              <input 
                type="text" 
                id="cmd-input" 
                placeholder="Type or speak (e.g. 'bought 2kg palak', 'i ate lays', 'used milk')..." 
                class="w-full pl-3.5 sm:pl-4 pr-20 sm:pr-24 py-2.5 sm:py-3 rounded-2xl border border-stone-200 dark:border-stone-700 focus:outline-none focus:ring-2 focus:ring-brand-500 text-xs sm:text-sm text-stone-800 dark:text-stone-100 bg-stone-50/50 dark:bg-stone-800 font-medium"
              />
              <div class="absolute right-1.5 sm:right-2 top-1/2 -translate-y-1/2 flex items-center gap-0.5 sm:gap-1">
                <button 
                  type="button" 
                  id="cmd-photo-btn" 
                  title="Snap or upload food photo to auto-add items"
                  class="p-1.5 rounded-xl text-stone-400 hover:text-emerald-600 dark:hover:text-emerald-400 hover:bg-stone-100 dark:hover:bg-stone-700 transition-colors"
                >
                  <svg class="w-4 h-4 sm:w-5 sm:h-5 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 13a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
                </button>
                <button 
                  type="button" 
                  id="mic-btn" 
                  title="Speak command"
                  class="p-1.5 rounded-xl text-stone-400 hover:text-brand-600 hover:bg-stone-100 dark:hover:bg-stone-700 transition-colors"
                >
                  <svg class="w-4 h-4 sm:w-5 sm:h-5 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 003-3V5a3 3 0 00-6 0v6a3 3 0 003 3z"></path>
                  </svg>
                </button>
              </div>
            </div>
            
            <!-- Hidden photo capture inputs for command bar -->
            <input type="file" id="cmd-camera-file" accept="image/*" capture="environment" class="hidden" />
            <input type="file" id="cmd-gallery-file" accept="image/*" class="hidden" />

            <div class="grid grid-cols-2 sm:flex items-center gap-2 w-full sm:w-auto shrink-0">
              <button 
                type="button" 
                id="cmd-camera-btn"
                class="flex-1 sm:flex-none px-3 sm:px-4 py-2.5 sm:py-3 bg-gradient-to-r from-emerald-600 via-teal-600 to-emerald-700 hover:from-emerald-500 hover:to-teal-500 text-white font-semibold rounded-2xl shadow-sm text-xs sm:text-sm flex items-center justify-center gap-1.5 sm:gap-2 transition-all transform active:scale-95"
                title="Snap food photo to auto-add items"
              >
                <svg class="w-4 h-4 pointer-events-none shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 13a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
                <span data-i18n="cmd_photo_btn" class="truncate">Add by Photo</span>
              </button>
              <button 
                type="submit" 
                id="cmd-submit-btn"
                class="flex-1 sm:flex-none px-3.5 sm:px-5 py-2.5 sm:py-3 bg-brand-600 hover:bg-brand-700 text-white font-semibold rounded-2xl shadow-sm text-xs sm:text-sm flex items-center justify-center gap-1.5 sm:gap-2 transition-all transform active:scale-95"
              >
                <span data-i18n="cmd_btn" class="truncate">Process</span>
                <svg class="w-4 h-4 pointer-events-none shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>
              </button>
            </div>
          </form>

          <!-- SUGGESTION CHIPS -->
          <div class="mt-3.5 flex items-center gap-1.5 flex-wrap text-xs text-stone-500 dark:text-stone-400">
            <span class="font-medium" data-i18n="suggestions_label">Quick Try:</span>
            <button type="button" id="cmd-photo-chip" class="chip hover:bg-emerald-100 dark:hover:bg-emerald-950/60 bg-emerald-50 dark:bg-emerald-950/40 text-emerald-700 dark:text-emerald-300 border border-emerald-300/40 px-2.5 py-1 rounded-lg flex items-center gap-1 transition">
              <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 13a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
              <span data-i18n="chip_photo_snap">📷 Snap Food Photo</span>
            </button>
            <button type="button" class="chip hover:bg-stone-200 dark:hover:bg-stone-700 bg-stone-100 dark:bg-stone-800 text-stone-700 dark:text-stone-300 px-2.5 py-1 rounded-lg">पालक (Palak)</button>
            <button type="button" class="chip hover:bg-stone-200 dark:hover:bg-stone-700 bg-stone-100 dark:bg-stone-800 text-stone-700 dark:text-stone-300 px-2.5 py-1 rounded-lg">पनीर (Paneer)</button>
            <button type="button" class="chip hover:bg-stone-200 dark:hover:bg-stone-700 bg-stone-100 dark:bg-stone-800 text-stone-700 dark:text-stone-300 px-2.5 py-1 rounded-lg">टमाटर (Tomato)</button>
            <button type="button" class="chip hover:bg-stone-200 dark:hover:bg-stone-700 bg-stone-100 dark:bg-stone-800 text-stone-700 dark:text-stone-300 px-2.5 py-1 rounded-lg">i ate lays</button>
            <button type="button" class="chip hover:bg-stone-200 dark:hover:bg-stone-700 bg-stone-100 dark:bg-stone-800 text-stone-700 dark:text-stone-300 px-2.5 py-1 rounded-lg">used milk</button>
          </div>
        </div>

        <!-- LIVE PHOTO SCANNING & AI FEEDBACK ON INVENTORY TAB -->
        <div id="inv-photo-status" class="hidden rounded-3xl bg-white dark:bg-stone-900 border border-emerald-200 dark:border-emerald-800/80 p-4 sm:p-5 shadow-sm transition-all">
        </div>

        <!-- METRIC STATS STRIP -->
        <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 sm:gap-4">
          <div class="bg-white dark:bg-stone-900 p-4 rounded-2xl border border-stone-200/80 dark:border-stone-800 shadow-sm flex items-center gap-3.5 transition-colors">
            <div class="w-10 h-10 rounded-xl bg-stone-100 dark:bg-stone-800 text-stone-700 dark:text-stone-300 flex items-center justify-center shrink-0">
              <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"></path></svg>
            </div>
            <div>
              <div class="text-2xl font-bold text-stone-900 dark:text-white" id="stat-total">0</div>
              <div class="text-xs font-medium text-stone-500 dark:text-stone-400" data-i18n="stat_total">Total Items</div>
            </div>
          </div>
          <div class="bg-white dark:bg-stone-900 p-4 rounded-2xl border border-stone-200/80 dark:border-stone-800 shadow-sm flex items-center gap-3.5 transition-colors">
            <div class="w-10 h-10 rounded-xl bg-leaf-50 dark:bg-stone-800 text-leaf-600 flex items-center justify-center shrink-0">
              <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
            </div>
            <div>
              <div class="text-2xl font-bold text-leaf-600" id="stat-fresh">0</div>
              <div class="text-xs font-medium text-stone-500 dark:text-stone-400" data-i18n="stat_fresh">Fresh (3+ Days)</div>
            </div>
          </div>
          <div class="bg-white dark:bg-stone-900 p-4 rounded-2xl border border-stone-200/80 dark:border-stone-800 shadow-sm flex items-center gap-3.5 transition-colors">
            <div class="w-10 h-10 rounded-xl bg-amber-50 dark:bg-stone-800 text-brand-600 flex items-center justify-center shrink-0">
              <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"></path></svg>
            </div>
            <div>
              <div class="text-2xl font-bold text-brand-600" id="stat-urgent">0</div>
              <div class="text-xs font-medium text-stone-500 dark:text-stone-400" data-i18n="stat_urgent">Expiring Soon</div>
            </div>
          </div>
          <div class="bg-white dark:bg-stone-900 p-4 rounded-2xl border border-stone-200/80 dark:border-stone-800 shadow-sm flex items-center gap-3.5 transition-colors">
            <div class="w-10 h-10 rounded-xl bg-rose-50 dark:bg-stone-800 text-rose-600 flex items-center justify-center shrink-0">
              <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
            </div>
            <div>
              <div class="text-2xl font-bold text-rose-600" id="stat-expired">0</div>
              <div class="text-xs font-medium text-stone-500 dark:text-stone-400" data-i18n="stat_expired">Expired</div>
            </div>
          </div>
        </div>

        <!-- REAL-TIME TIME SYNC STATUS BANNER -->
        <div id="sync-banner" class="p-3.5 bg-gradient-to-r from-amber-500/10 via-emerald-500/10 to-transparent dark:from-stone-800/80 dark:to-stone-900 border border-stone-200 dark:border-stone-800 rounded-2xl flex flex-col sm:flex-row sm:items-center justify-between gap-2 text-xs">
          <div class="flex items-center gap-2 text-stone-700 dark:text-stone-300">
            <span class="w-2 h-2 rounded-full bg-emerald-500 animate-pulse shrink-0"></span>
            <span class="font-bold" id="sync-engine-heading" data-i18n="sync_engine_heading">Live Freshness Engine:</span>
            <span id="sync-banner-text" data-i18n="time_sync_banner">
              Automatically comparing your country's local time against food addition dates. Freshness & expiry countdowns update continuously without refreshing.
            </span>
          </div>
          <div class="text-[11px] text-stone-500 dark:text-stone-400 font-mono shrink-0" id="sync-timestamp-display">Live Synced</div>
        </div>

        <!-- FILTER CONTROLS & RESCUE ACTION BAR -->
        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2.5 sm:gap-3 pt-1">
          <div id="filter-controls" class="flex items-center gap-1.5 text-xs overflow-x-auto pb-1 max-w-full scrollbar-none flex-nowrap sm:flex-wrap">
            <button class="filter-btn active bg-stone-900 dark:bg-white text-white dark:text-stone-900 px-3 sm:px-3.5 py-1.5 rounded-full font-medium shrink-0 whitespace-nowrap" data-filter="all" data-i18n="filter_all">All Items</button>
            <button class="filter-btn bg-stone-100 dark:bg-stone-800 text-stone-600 dark:text-stone-300 hover:bg-stone-200 px-3 sm:px-3.5 py-1.5 rounded-full font-medium shrink-0 whitespace-nowrap" data-filter="urgent" data-i18n="filter_urgent">Urgent</button>
            <button class="filter-btn bg-stone-100 dark:bg-stone-800 text-stone-600 dark:text-stone-300 hover:bg-stone-200 px-3 sm:px-3.5 py-1.5 rounded-full font-medium shrink-0 whitespace-nowrap" data-filter="fresh" data-i18n="filter_fresh">Fresh</button>
            <button class="filter-btn bg-stone-100 dark:bg-stone-800 text-stone-600 dark:text-stone-300 hover:bg-stone-200 px-3 sm:px-3.5 py-1.5 rounded-full font-medium shrink-0 whitespace-nowrap" data-filter="expired" data-i18n="filter_expired">Expired</button>
          </div>

          <!-- MULTI-SELECT RESCUE BAR -->
          <div id="rescue-action-bar" class="hidden bg-stone-900 dark:bg-stone-800 text-white px-3.5 py-2 rounded-2xl flex items-center justify-between gap-3 shadow-md w-full sm:w-auto">
            <span class="text-xs font-medium" id="rescue-selected-label">Selected <b id="selected-count" class="text-brand-400 font-bold">0</b> items</span>
            <button id="trigger-rescue-btn" class="px-3.5 py-1.5 bg-brand-600 hover:bg-brand-500 font-bold rounded-xl text-xs shadow transition inline-flex items-center gap-1.5 shrink-0">
              <span data-i18n="trigger_rescue_btn">Rescue Recipe</span>
              <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg>
            </button>
          </div>
        </div>

        <!-- INVENTORY GRID -->
        <div id="inventory-grid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          <!-- Populated via JS -->
        </div>

        <!-- EMPTY STATE -->
        <div id="empty-state" class="hidden text-center py-16 bg-white dark:bg-stone-900 rounded-3xl border border-dashed border-stone-200 dark:border-stone-800 p-6 transition-colors">
          <div class="w-12 h-12 mx-auto mb-3 rounded-2xl bg-stone-100 dark:bg-stone-800 text-stone-400 dark:text-stone-500 flex items-center justify-center">
            <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"></path></svg>
          </div>
          <h3 class="text-base font-bold text-stone-800 dark:text-stone-200" data-i18n="empty_title">Your Fridge is Empty</h3>
          <p class="text-xs text-stone-500 dark:text-stone-400 mt-1 max-w-sm mx-auto" data-i18n="empty_desc">
            Add fresh produce, groceries, or meals using the input above or your microphone.
          </p>
        </div>
      </section>

      <!-- TAB 2: EXPIRING SOON DEDICATED VIEW -->
      <!-- TAB 2: URGENT / EXPIRING SOON -->
      <section id="section-urgent" class="tab-section hidden space-y-6">
        <div class="bg-white dark:bg-stone-900 rounded-3xl p-6 sm:p-8 border border-stone-200/80 dark:border-stone-800 shadow-sm space-y-6">
          <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-stone-100 dark:border-stone-800 pb-5">
            <div>
              <h2 class="text-lg sm:text-xl font-bold text-stone-900 dark:text-white flex items-center gap-2">
                <svg class="w-5 h-5 text-amber-500 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>
                <span data-i18n="urgent_tab_title">Urgent Attention Needed</span>
              </h2>
              <p class="text-xs text-stone-500 dark:text-stone-400 mt-1" data-i18n="urgent_tab_desc">
                Items expiring within 48 hours or already expired. Consume or rescue them before they spoil!
              </p>
            </div>
            <div class="flex items-center gap-2">
              <button id="urgent-rescue-all-btn" class="px-4 py-2.5 bg-brand-600 hover:bg-brand-700 text-white font-bold rounded-xl text-xs shadow transition flex items-center gap-2">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
                <span data-i18n="urgent_rescue_all_btn">Rescue All with AI Chef</span>
              </button>
            </div>
          </div>

          <div id="urgent-grid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            <!-- Populated via JS -->
          </div>

          <div id="urgent-empty" class="hidden text-center py-12 bg-stone-50 dark:bg-stone-800/40 rounded-2xl border border-dashed border-stone-200 dark:border-stone-800 p-6">
            <div class="w-12 h-12 mx-auto rounded-full bg-emerald-50 dark:bg-emerald-950/60 text-emerald-600 dark:text-emerald-400 flex items-center justify-center">
              <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
            </div>
            <h4 class="font-bold text-sm text-stone-800 dark:text-stone-200 mt-2" data-i18n="urgent_empty_title">No Urgent Expirations!</h4>
            <p class="text-xs text-stone-500 dark:text-stone-400 mt-0.5" data-i18n="urgent_empty_desc">Everything in your fridge has plenty of shelf life remaining.</p>
          </div>
        </div>
      </section>

      <!-- TAB 3: OFFLINE PUSH ALERTS -->
      <section id="section-push" class="tab-section hidden space-y-6">
        <div class="bg-white dark:bg-stone-900 rounded-3xl p-6 sm:p-8 border border-stone-200/80 dark:border-stone-800 shadow-sm space-y-6">
          
          <!-- Simple Clean Header -->
          <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-stone-100 dark:border-stone-800 pb-6">
            <div class="flex items-start gap-4">
              <div class="w-12 h-12 rounded-2xl bg-amber-50 dark:bg-amber-950/50 text-amber-600 dark:text-amber-400 flex items-center justify-center shrink-0">
                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"></path></svg>
              </div>
              <div>
                <div class="flex items-center gap-2.5">
                  <h2 class="text-lg sm:text-xl font-bold text-stone-900 dark:text-white" data-i18n="push_hero_title">Expiry Alerts</h2>
                  <span id="push-hero-badge" class="text-xs font-bold px-2.5 py-0.5 rounded-full bg-stone-100 dark:bg-stone-800 text-stone-500">OFF</span>
                </div>
                <p class="text-xs text-stone-500 dark:text-stone-400 mt-1 max-w-xl leading-relaxed" data-i18n="push_hero_desc">
                  Get notified before food in your fridge expires, even when your browser or phone is closed.
                </p>
              </div>
            </div>

            <!-- Master Toggle Button -->
            <button id="push-main-toggle-btn" class="px-6 py-3 rounded-2xl bg-brand-600 hover:bg-brand-700 text-white font-bold text-sm shadow-md transition-all flex items-center justify-center gap-2 shrink-0">
              <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"></path></svg>
              <span id="push-main-toggle-text" data-i18n="push_toggle_enable">Enable Expiry Alerts</span>
            </button>
          </div>

          <!-- Chrome & Browser Permission Guide (Prominent & Helpful) -->
          <div id="push-chrome-guide-card" class="p-4 rounded-2xl bg-amber-50/70 dark:bg-amber-950/30 border border-amber-200/80 dark:border-amber-800/60 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 transition-all">
            <div class="flex items-start gap-3">
              <div class="w-10 h-10 rounded-xl bg-amber-100 dark:bg-amber-900/60 text-amber-700 dark:text-amber-300 flex items-center justify-center shrink-0">
                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"/></svg>
              </div>
              <div class="text-xs">
                <h4 class="font-bold text-stone-900 dark:text-white" data-i18n="push_guide_title">How to allow notifications in Google Chrome</h4>
                <p class="text-stone-600 dark:text-stone-300 mt-0.5 leading-relaxed" data-i18n="push_guide_desc">
                  If your browser blocks notifications: click the <strong>lock / site settings icon</strong> next to the web address at the top &gt; <strong>Site settings</strong> &gt; set <strong>Notifications</strong> to <strong>Allow</strong>.
                </p>
              </div>
            </div>
            <a href="https://support.google.com/chrome/answer/3220216" target="_blank" rel="noopener noreferrer" class="px-4 py-2.5 rounded-xl bg-white dark:bg-stone-800 hover:bg-stone-50 dark:hover:bg-stone-700 text-stone-800 dark:text-stone-100 border border-stone-200 dark:border-stone-700 font-semibold text-xs transition flex items-center gap-1.5 shrink-0 shadow-sm">
              <span data-i18n="push_guide_btn">Google Help Guide</span>
              <svg class="w-3.5 h-3.5 text-stone-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"></path></svg>
            </a>
          </div>

          <!-- Quick Action Buttons -->
          <div class="grid grid-cols-1 sm:grid-cols-3 gap-3.5">
            <div class="p-4 rounded-2xl bg-stone-50 dark:bg-stone-800/50 border border-stone-200/70 dark:border-stone-700/60 flex items-center justify-between gap-3">
              <div>
                <h4 class="font-bold text-xs text-stone-900 dark:text-white flex items-center gap-1.5">
                  <svg class="w-3.5 h-3.5 text-brand-600 dark:text-brand-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"/></svg>
                  <span data-i18n="push_card_test_title">Test Web Push</span>
                </h4>
                <p class="text-[11px] text-stone-500 dark:text-stone-400 mt-0.5" data-i18n="push_card_test_desc">Signed VAPID push (buffers 24h offline).</p>
              </div>
              <button id="push-tab-test-btn" class="px-3.5 py-2 rounded-xl bg-white dark:bg-stone-800 hover:bg-stone-100 dark:hover:bg-stone-700 border border-stone-200 dark:border-stone-700 text-stone-800 dark:text-stone-200 font-semibold text-xs transition shrink-0" data-i18n="push_btn_send_push">
                Send Push
              </button>
            </div>

            <div class="p-4 rounded-2xl bg-stone-50 dark:bg-stone-800/50 border border-stone-200/70 dark:border-stone-700/60 flex items-center justify-between gap-3">
              <div>
                <h4 class="font-bold text-xs text-stone-900 dark:text-white flex items-center gap-1.5">
                  <svg class="w-3.5 h-3.5 text-emerald-600 dark:text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
                  <span data-i18n="push_card_offline_title">Test Offline Alert</span>
                </h4>
                <p class="text-[11px] text-stone-500 dark:text-stone-400 mt-0.5" data-i18n="push_card_offline_desc">Local Service Worker (no Wi-Fi needed).</p>
              </div>
              <button id="push-tab-offline-test-btn" class="px-3.5 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white font-semibold text-xs shadow-sm transition shrink-0" data-i18n="push_btn_test_offline">
                Test Offline
              </button>
            </div>

            <div class="p-4 rounded-2xl bg-stone-50 dark:bg-stone-800/50 border border-stone-200/70 dark:border-stone-700/60 flex items-center justify-between gap-3">
              <div>
                <h4 class="font-bold text-xs text-stone-900 dark:text-white flex items-center gap-1.5">
                  <svg class="w-3.5 h-3.5 text-amber-600 dark:text-amber-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                  <span data-i18n="push_card_expiry_title">Check Expiry Now</span>
                </h4>
                <p class="text-[11px] text-stone-500 dark:text-stone-400 mt-0.5" data-i18n="push_card_expiry_desc">Scan fridge and alert for food expiring soon.</p>
              </div>
              <button id="push-tab-expiry-btn" class="px-3.5 py-2 rounded-xl bg-amber-600 hover:bg-amber-700 text-white font-semibold text-xs shadow-sm transition shrink-0" data-i18n="push_btn_check_now">
                Check Now
              </button>
            </div>
          </div>

          <!-- Subtle collapsible diagnostics for developers / technical users -->
          <details class="text-xs text-stone-400 dark:text-stone-500 pt-2 border-t border-stone-100 dark:border-stone-800/60">
            <summary class="cursor-pointer hover:text-stone-600 dark:hover:text-stone-300 font-medium select-none flex items-center gap-1.5">
              <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
              <span data-i18n="push_diag_title">Technical status & diagnostics</span>
            </summary>
            <div class="mt-3 p-4 rounded-2xl bg-stone-50/80 dark:bg-stone-800/30 border border-stone-200/60 dark:border-stone-700/40 space-y-3">
              <div class="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[11px]">
                <div class="p-2 bg-white dark:bg-stone-900 rounded-lg border border-stone-200/60 dark:border-stone-700/40">
                  <span class="text-stone-400">Worker:</span>
                  <div class="font-mono font-semibold text-stone-700 dark:text-stone-300 truncate" id="diag-sw-status">Active</div>
                </div>
                <div class="p-2 bg-white dark:bg-stone-900 rounded-lg border border-stone-200/60 dark:border-stone-700/40">
                  <span class="text-stone-400">Fridge:</span>
                  <div class="font-mono font-semibold text-brand-600 truncate" id="diag-fridge-id">--</div>
                </div>
                <div class="p-2 bg-white dark:bg-stone-900 rounded-lg border border-stone-200/60 dark:border-stone-700/40">
                  <span class="text-stone-400">VAPID:</span>
                  <div class="font-mono font-semibold text-stone-700 dark:text-stone-300 truncate" id="diag-vapid-key">Loaded</div>
                </div>
                <div class="p-2 bg-white dark:bg-stone-900 rounded-lg border border-stone-200/60 dark:border-stone-700/40">
                  <span class="text-stone-400">Endpoint:</span>
                  <div class="font-mono font-semibold text-stone-700 dark:text-stone-300 truncate" id="diag-endpoint">Waiting</div>
                </div>
              </div>
              <div class="text-[11px] text-stone-500 dark:text-stone-400 flex items-center gap-1.5">
                <span>Cron endpoint: <code class="px-1.5 py-0.5 rounded bg-stone-200 dark:bg-stone-700 font-mono text-[10px]">POST /api/check-expiry-cron</code></span>
              </div>
            </div>
          </details>

        </div>
      </section>

      <!-- TAB 4: ZERO-WASTE RESCUE RECIPE SECTION -->
      <section id="section-rescue" class="tab-section hidden space-y-6">
        <div class="bg-white dark:bg-stone-900 rounded-3xl p-6 sm:p-8 border border-stone-200/80 dark:border-stone-800 shadow-sm transition-colors">
          <div class="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 border-b border-stone-100 dark:border-stone-800 pb-5">
            <div>
              <h2 class="text-lg font-bold text-stone-900 dark:text-white flex items-center gap-2">
                <span data-i18n="rescue_title">Zero-Waste Rescue Chef</span>
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
            <div class="flex items-center justify-between mb-1">
              <label class="block text-xs font-semibold text-stone-700 dark:text-stone-300" data-i18n="custom_rescue_label">
                Custom Ingredients / Leftovers to Rescue (Comma-separated):
              </label>
              <span class="text-[10px] text-stone-400" data-i18n="rescue_sub_tip">Speaks or types dishes, drinks, or leftovers</span>
            </div>
            <div class="relative">
              <input 
                type="text" 
                id="rescue-items-input" 
                placeholder="Ask 'how to make biryani', 'paneer butter masala', or enter leftovers: milk, bread, cheese..." data-i18n-placeholder="custom_rescue_placeholder" 
                class="w-full pl-4 pr-12 py-2.5 rounded-xl border border-stone-200 dark:border-stone-700 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 bg-stone-50 dark:bg-stone-800 text-stone-800 dark:text-stone-100 font-medium"
              />
              <button 
                type="button" 
                id="rescue-mic-btn" 
                title="Speak ingredients via microphone"
                class="absolute right-2 top-1.5 p-1.5 rounded-xl text-stone-400 hover:text-brand-600 hover:bg-stone-100 dark:hover:bg-stone-700 transition-colors"
              >
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 003-3V5a3 3 0 00-6 0v6a3 3 0 003 3z"></path>
                </svg>
              </button>
            </div>

            <!-- SPELLING CORRECTION FEEDBACK BANNER -->
            <!-- QUICK HOW-TO RECIPE INSPIRATION CHIPS -->
            <div class="mt-2.5 flex items-center gap-1.5 flex-wrap text-xs text-stone-500 dark:text-stone-400">
              <span class="font-medium" data-i18n="recipe_quick_try">Quick Recipes:</span>
              <button type="button" class="recipe-quick-chip hover:bg-stone-200 dark:hover:bg-stone-700 bg-stone-100 dark:bg-stone-800 text-stone-700 dark:text-stone-300 px-2.5 py-1 rounded-lg transition cursor-pointer" data-query="how to make paneer butter masala">🍲 Paneer Butter Masala</button>
              <button type="button" class="recipe-quick-chip hover:bg-stone-200 dark:hover:bg-stone-700 bg-stone-100 dark:bg-stone-800 text-stone-700 dark:text-stone-300 px-2.5 py-1 rounded-lg transition cursor-pointer" data-query="how to make biryani">🍚 Biryani</button>
              <button type="button" class="recipe-quick-chip hover:bg-stone-200 dark:hover:bg-stone-700 bg-stone-100 dark:bg-stone-800 text-stone-700 dark:text-stone-300 px-2.5 py-1 rounded-lg transition cursor-pointer" data-query="how to make dal tadka">🥣 Dal Tadka</button>
              <button type="button" class="recipe-quick-chip hover:bg-stone-200 dark:hover:bg-stone-700 bg-stone-100 dark:bg-stone-800 text-stone-700 dark:text-stone-300 px-2.5 py-1 rounded-lg transition cursor-pointer" data-query="how to make kheer">🍮 Kheer</button>
              <button type="button" class="recipe-quick-chip hover:bg-stone-200 dark:hover:bg-stone-700 bg-stone-100 dark:bg-stone-800 text-stone-700 dark:text-stone-300 px-2.5 py-1 rounded-lg transition cursor-pointer" data-query="how to make pav bhaji">🍛 Pav Bhaji</button>
            </div>

            <div id="rescue-spell-badge"  class="hidden mt-2 p-2.5 rounded-xl bg-amber-50 dark:bg-stone-800 border border-amber-200 dark:border-stone-700 text-xs text-amber-900 dark:text-amber-200 flex items-center justify-between gap-2">
              <div class="flex items-center gap-1.5">
                <svg class="w-3.5 h-3.5 text-amber-600 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
                <span id="rescue-spell-text" class="font-medium"></span>
              </div>
              <button type="button" id="dismiss-spell-btn" class="text-stone-400 hover:text-stone-600 text-xs font-bold leading-none">&times;</button>
            </div>
          </div>

          <!-- RECIPE RESULT CARD -->
          <div id="recipe-container" class="mt-6 hidden space-y-6">
            <div class="bg-stone-50 dark:bg-stone-800/60 border border-stone-200 dark:border-stone-700 rounded-2xl p-6 relative">
              <div class="flex items-start justify-between gap-3 border-b border-stone-200/60 dark:border-stone-700 pb-4">
                <div class="min-w-0 flex-1">
                  <div class="flex items-center gap-2 mb-1 flex-wrap">
                    <span class="text-xs font-bold text-brand-600 dark:text-brand-400 uppercase tracking-wider" data-i18n="recipe_card_tag">Zero-Waste Culinary Recipe</span>
                    <span id="recipe-meal-type" class="px-2.5 py-0.5 rounded-full text-[10px] font-bold bg-indigo-50 dark:bg-indigo-950/70 text-indigo-700 dark:text-indigo-300 border border-indigo-200 dark:border-indigo-800" data-i18n="recipe_homestyle_badge">Homestyle Meal</span>
                  </div>
                  <h3 id="recipe-name" class="text-lg sm:text-2xl font-bold text-stone-900 dark:text-white break-words"></h3>
                  <h4 id="recipe-name-hi" class="text-sm sm:text-base font-semibold text-brand-600 dark:text-brand-400"></h4>
                  <div class="flex items-center gap-2 mt-2.5 flex-wrap">
                    <span id="recipe-time" class="bg-white dark:bg-stone-900 border border-stone-200 dark:border-stone-700 text-stone-700 dark:text-stone-300 text-xs font-semibold px-3 py-1 rounded-full inline-flex items-center gap-1.5 shrink-0">
                      <svg class="w-3.5 h-3.5 text-stone-400 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
                      <span id="recipe-time-text">20 mins</span>
                    </span>
                    <button id="speak-recipe-btn" onclick="toggleRecipeSpeech()" class="px-2.5 py-1 bg-white dark:bg-stone-900 hover:bg-stone-100 dark:hover:bg-stone-750 border border-stone-200 dark:border-stone-700 rounded-xl text-stone-600 dark:text-stone-300 text-xs font-semibold inline-flex items-center gap-1.5 shrink-0 transition cursor-pointer" title="Listen to recipe spoken out loud">
                      <svg id="speak-icon-idle" class="w-3.5 h-3.5 text-brand-600 dark:text-brand-400 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.536 8.464a5 5 0 010 7.072m2.828-9.9a9 9 0 010 12.728M5.586 15H4a1 1 0 01-1-1v-4a1 1 0 011-1h1.586l4.707-4.707C10.923 3.663 12 4.109 12 5v14c0 .891-1.077 1.337-1.707.707L5.586 15z"/></svg>
                      <span id="speak-recipe-btn-text" data-i18n="btn_speak_recipe">🔊 Listen Recipe</span>
                    </button>
                    <button id="copy-recipe-btn"  class="px-2.5 py-1 bg-white dark:bg-stone-900 hover:bg-stone-100 dark:hover:bg-stone-750 border border-stone-200 dark:border-stone-700 rounded-xl text-stone-600 dark:text-stone-300 text-xs font-medium inline-flex items-center gap-1 shrink-0" title="Copy">
                      <svg class="w-3.5 h-3.5 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"></path></svg>
                      <span data-i18n="btn_copy">Copy</span>
                    </button>
                  </div>
                </div>
                <button id="close-recipe-btn" class="px-2.5 sm:px-3 py-1.5 rounded-xl border border-stone-200 dark:border-stone-700 hover:bg-stone-200 dark:hover:bg-stone-700 text-stone-600 dark:text-stone-300 text-xs font-semibold flex items-center gap-1.5 transition shrink-0">
                  <svg class="w-3.5 h-3.5 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
                  <span class="hidden sm:inline" data-i18n="close_recipe_btn">Close Recipe</span>
                  <span class="inline sm:hidden">Close</span>
                </button>
              </div>

              <!-- PAIRING / COMBO NOTES (SHOWN FOR DRINKS + FOOD) -->
              <div id="recipe-pairing-box" class="hidden mt-3.5 p-3.5 rounded-2xl bg-amber-50/80 dark:bg-stone-800/80 border border-amber-200 dark:border-stone-700 text-xs text-amber-900 dark:text-amber-200">
                <div class="font-bold flex items-center gap-1.5 mb-1 text-stone-900 dark:text-stone-100">
                  <svg class="w-3.5 h-3.5 text-amber-600 dark:text-amber-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"></path></svg>
                  <span data-i18n="recipe_pairing_title">Chef's Zero-Waste Course & Pairing Notes:</span>
                </div>
                <p id="recipe-pairing-text" class="text-stone-700 dark:text-stone-300 leading-relaxed"></p>
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
            <div class="inline-flex items-center justify-center w-12 h-12 rounded-2xl bg-brand-50 dark:bg-stone-800 text-brand-600 mb-2">
              <svg class="w-6 h-6 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
            </div>
            <p class="text-sm font-semibold text-stone-800 dark:text-stone-200" data-i18n="recipe_loading">Consulting Indian Culinary Intelligence...</p>
          </div>
        </div>
      </section>

      <!-- TAB 5: STANDALONE FOOD SCANNER SECTION -->
      <section id="section-test" class="tab-section hidden space-y-4">
        <div class="bg-white dark:bg-stone-900 rounded-3xl p-6 sm:p-8 border border-stone-200 dark:border-stone-800 shadow-sm max-w-2xl mx-auto transition-colors">
          
          <div class="flex items-center justify-between gap-3 mb-1 flex-wrap">
            <h2 class="text-lg font-bold text-stone-900 dark:text-white" data-i18n="scanner_title">AI Food Inspector & Storage Advisor</h2>
            <div class="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-emerald-50 dark:bg-emerald-950/60 border border-emerald-200 dark:border-emerald-800/80 text-emerald-700 dark:text-emerald-300 text-[10px] font-bold">
              <span class="w-2 h-2 rounded-full bg-emerald-500 animate-ping"></span>
              <span data-i18n="scanner_ai_vision_badge">AI Vision Ready</span>
            </div>
          </div>
          
          <p class="text-xs text-stone-500 dark:text-stone-400 mb-4" data-i18n="scanner_desc">
            Type any food name or snap a photo using the camera bubble to inspect its culinary category, Hindi name, standard shelf life, and storage advice.
          </p>

          <!-- TAKE PHOTO BUBBLE CALLOUT BANNER -->
          <div class="mb-5 p-4 rounded-2xl bg-gradient-to-r from-emerald-500/10 via-teal-500/10 to-transparent border border-emerald-500/20 dark:border-emerald-500/30 flex items-center justify-between gap-3 flex-wrap">
            <div class="flex items-center gap-3">
              <!-- Animated Glowing Take Photo Bubble Button -->
              <button 
                type="button" 
                id="scanner-photo-bubble-btn" 
                class="group relative inline-flex items-center justify-center gap-2 px-4 py-2.5 rounded-full bg-gradient-to-r from-emerald-600 via-teal-600 to-emerald-700 hover:from-emerald-500 hover:to-teal-500 text-white font-bold text-xs shadow-md hover:shadow-lg transition-all duration-300 transform hover:scale-105 active:scale-95 cursor-pointer ring-2 ring-emerald-400/40"
                title="Take photo of food with your camera or webcam"
              >
                <span class="absolute -top-1 -right-1 flex h-3 w-3">
                  <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-300 opacity-75"></span>
                  <span class="relative inline-flex rounded-full h-3 w-3 bg-emerald-400"></span>
                </span>
                <svg class="w-4 h-4 transform group-hover:rotate-12 transition-transform duration-200" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 13a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
                <span data-i18n="scanner_take_photo">Take Photo</span>
              </button>

              <div>
                <div class="text-xs font-bold text-stone-800 dark:text-stone-200" data-i18n="scanner_instant_title">Instant AI Food Camera</div>
                <div class="text-[11px] text-stone-500 dark:text-stone-400" data-i18n="scanner_instant_desc">Snap any fresh dish or grocery to auto-identify & track</div>
              </div>
            </div>

            <!-- Upload / Live Camera secondary buttons -->
            <div class="flex items-center gap-2">
              <button 
                type="button" 
                id="scanner-open-webcam-btn" 
                class="px-3 py-1.5 rounded-xl bg-white dark:bg-stone-800 border border-stone-200 dark:border-stone-700 hover:bg-stone-50 dark:hover:bg-stone-700 text-stone-700 dark:text-stone-300 text-[11px] font-semibold transition flex items-center gap-1.5 shadow-xs"
                title="Open live desktop webcam viewfinder"
              >
                <svg class="w-3.5 h-3.5 text-stone-500 dark:text-stone-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>
                <span class="hidden sm:inline" data-i18n="scanner_live_cam_btn">Live Camera</span>
              </button>
              
              <button 
                type="button" 
                id="scanner-upload-file-btn" 
                class="px-3 py-1.5 rounded-xl bg-white dark:bg-stone-800 border border-stone-200 dark:border-stone-700 hover:bg-stone-50 dark:hover:bg-stone-700 text-stone-700 dark:text-stone-300 text-[11px] font-semibold transition flex items-center gap-1.5 shadow-xs"
                title="Upload image from device storage"
              >
                <svg class="w-3.5 h-3.5 text-stone-500 dark:text-stone-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12"/></svg>
                <span class="hidden sm:inline" data-i18n="scanner_upload_img_btn">Upload Image</span>
              </button>
            </div>
          </div>

          <!-- Hidden inputs for camera capture & file picker -->
          <input 
            type="file" 
            id="scanner-camera-input" 
            accept="image/*" 
            capture="environment" 
            class="hidden"
          />
          <input 
            type="file" 
            id="scanner-gallery-input" 
            accept="image/*" 
            class="hidden"
          />

          <!-- Text Search Bar Row with microphone + inline camera icon (Mobile Responsive) -->
          <div class="flex flex-col sm:flex-row gap-2">
            <div class="relative flex-grow w-full">
              <input 
                type="text" 
                id="scanner-input" 
                placeholder="Or type any food (e.g. Paneer Butter Masala, Gajar, Dal Makhani)..." data-i18n-placeholder="scanner_input_placeholder" 
                class="w-full pl-3.5 sm:pl-4 pr-20 sm:pr-24 py-2.5 rounded-xl border border-stone-200 dark:border-stone-700 text-xs sm:text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 bg-stone-50 dark:bg-stone-800 text-stone-800 dark:text-stone-100 font-medium"
              />
              <div class="absolute right-1.5 sm:right-2 top-1/2 -translate-y-1/2 flex items-center gap-0.5 sm:gap-1">
                <button 
                  type="button" 
                  id="scanner-mic-btn" 
                  class="text-stone-400 hover:text-brand-600 dark:hover:text-brand-400 transition p-1.5 rounded-lg hover:bg-stone-100 dark:hover:bg-stone-700"
                  title="Voice command for food scanner"
                >
                  <svg class="w-4 h-4 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 003-3V5a3 3 0 10-6 0v6a3 3 0 003 3z"/></svg>
                </button>
                <button 
                  type="button" 
                  id="scanner-inline-camera-icon" 
                  class="text-stone-400 hover:text-emerald-600 dark:hover:text-emerald-400 transition p-1.5 rounded-lg hover:bg-stone-100 dark:hover:bg-stone-700"
                  title="Snap photo with camera"
                >
                  <svg class="w-4 h-4 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 13a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
                </button>
              </div>
            </div>
            
            <button 
              type="button" 
              id="scanner-btn" 
              class="w-full sm:w-auto px-5 py-2.5 bg-stone-900 dark:bg-white text-white dark:text-stone-900 font-semibold rounded-xl text-xs sm:text-sm transition hover:opacity-90 shrink-0"
              data-i18n="scanner_inspect_btn"
            >
              Inspect
            </button>
          </div>

          <!-- Scan Status & Results Container -->
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
          <div class="flex items-center justify-between mb-1">
            <label class="block text-xs font-semibold" data-i18n="auth_fid_label">Fridge ID:</label>
            <span class="text-[10px] text-amber-600 dark:text-amber-400 font-semibold px-1.5 py-0.5 rounded bg-amber-50 dark:bg-amber-950/60 border border-amber-200/60 dark:border-amber-800/60" data-i18n="auth_case_sensitive">Case-Sensitive</span>
          </div>
          <input 
            type="text" 
            id="login-fid" 
            placeholder="e.g. SharmaKitchen or test (3-30 letters/numbers)" 
            pattern="[a-zA-Z0-9]{3,30}"
            class="w-full px-4 py-2.5 rounded-xl border border-stone-200 dark:border-stone-700 bg-stone-50 dark:bg-stone-800 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 font-mono"
            required
          />
          <div class="text-[10px] text-stone-400 dark:text-stone-500 mt-1" data-i18n="auth_login_fid_hint">Letters (a-z, A-Z) and numbers (0-9) only. No symbols or spaces.</div>
        </div>
        <div>
          <div class="flex items-center justify-between mb-1">
            <label class="block text-xs font-semibold" data-i18n="auth_pin_label">Family PIN / Password:</label>
            <span class="text-[10px] text-amber-600 dark:text-amber-400 font-semibold px-1.5 py-0.5 rounded bg-amber-50 dark:bg-amber-950/60 border border-amber-200/60 dark:border-amber-800/60" data-i18n="auth_case_sensitive">Case-Sensitive</span>
          </div>
          <div class="relative">
            <input 
              type="password" 
              id="login-pin" 
              placeholder="Enter fridge PIN (e.g. Secret123)..." 
              pattern="[a-zA-Z0-9]{4,30}"
              class="w-full pl-4 pr-10 py-2.5 rounded-xl border border-stone-200 dark:border-stone-700 bg-stone-50 dark:bg-stone-800 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 font-mono"
              required
            />
            <button 
              type="button" 
              id="toggle-login-pin-btn" 
              tabindex="-1"
              class="absolute right-2 top-1/2 -translate-y-1/2 p-1.5 rounded-lg text-stone-400 hover:text-stone-600 dark:hover:text-stone-200 hover:bg-stone-100 dark:hover:bg-stone-700 transition cursor-pointer select-none"
              title="Show / Hide PIN"
              aria-label="Toggle password visibility"
            >
              <svg class="w-4 h-4 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/></svg>
            </button>
          </div>
          <div class="text-[10px] text-stone-400 dark:text-stone-500 mt-1" data-i18n="auth_login_pin_hint">Letters and numbers only. Exact casing verified.</div>
        </div>
        <button type="submit" class="w-full py-2.5 bg-brand-600 hover:bg-brand-700 text-white font-semibold rounded-xl text-sm shadow transition" data-i18n="auth_login_submit">
          Open Fridge
        </button>
      </form>

      <!-- FORM: CREATE NEW FRIDGE -->
      <form id="create-form" class="mt-4 space-y-3.5 hidden">
        <div>
          <div class="flex items-center justify-between mb-1">
            <label class="block text-xs font-semibold" data-i18n="auth_create_fid_label">Choose Fridge ID:</label>
            <span class="text-[10px] text-emerald-600 dark:text-emerald-400 font-semibold px-1.5 py-0.5 rounded bg-emerald-50 dark:bg-emerald-950/60 border border-emerald-200/60 dark:border-emerald-800/60" data-i18n="auth_letters_numbers">Letters & Numbers</span>
          </div>
          <input 
            type="text" 
            id="create-fid" 
            placeholder="e.g. SharmaKitchen or MyFridge24 (3-30 chars)" 
            pattern="[a-zA-Z0-9]{3,30}"
            class="w-full px-4 py-2.5 rounded-xl border border-stone-200 dark:border-stone-700 bg-stone-50 dark:bg-stone-800 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 font-mono"
            required
          />
          <div class="text-[10px] text-stone-400 dark:text-stone-500 mt-1" data-i18n="auth_create_fid_hint">Case-sensitive: saved exactly as typed. Allowed: a-z, A-Z, 0-9.</div>
        </div>
        <div>
          <div class="flex items-center justify-between mb-1">
            <label class="block text-xs font-semibold" data-i18n="auth_create_pin_label">Set Family PIN / Password:</label>
            <span class="text-[10px] text-emerald-600 dark:text-emerald-400 font-semibold px-1.5 py-0.5 rounded bg-emerald-50 dark:bg-emerald-950/60 border border-emerald-200/60 dark:border-emerald-800/60" data-i18n="auth_min_chars">Min 4 Alphanumeric</span>
          </div>
          <div class="relative">
            <input 
              type="password" 
              id="create-pin" 
              placeholder="e.g. SecretPin42 (letters & numbers only)" 
              pattern="[a-zA-Z0-9]{4,30}"
              class="w-full pl-4 pr-10 py-2.5 rounded-xl border border-stone-200 dark:border-stone-700 bg-stone-50 dark:bg-stone-800 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 font-mono"
              required
            />
            <button 
              type="button" 
              id="toggle-create-pin-btn" 
              tabindex="-1"
              class="absolute right-2 top-1/2 -translate-y-1/2 p-1.5 rounded-lg text-stone-400 hover:text-stone-600 dark:hover:text-stone-200 hover:bg-stone-100 dark:hover:bg-stone-700 transition cursor-pointer select-none"
              title="Show / Hide PIN"
              aria-label="Toggle password visibility"
            >
              <svg class="w-4 h-4 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/></svg>
            </button>
          </div>
          <div class="text-[10px] text-stone-400 dark:text-stone-500 mt-1" data-i18n="auth_create_pin_hint">Letters (a-z, A-Z) and numbers (0-9) only. Strictly case-sensitive.</div>
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
        <div class="flex items-center gap-2">
          <h3 class="font-bold text-base" data-i18n="modal_confirm_title">Confirm Fridge Entry</h3>
          <span id="modal-queue-pill" class="hidden text-[10px] font-bold px-2 py-0.5 rounded-full bg-brand-100 dark:bg-brand-950/70 text-brand-800 dark:text-brand-300 border border-brand-200 dark:border-brand-800">Item 1 of 2</span>
        </div>
        <button id="close-modal-btn" class="text-stone-400 hover:text-stone-600 text-xl font-bold">&times;</button>
      </div>

      <div class="mt-4 space-y-4">
        <!-- Display View (when item was identified by AI) -->
        <div id="modal-item-display" class="p-3.5 bg-stone-50 dark:bg-stone-800 rounded-2xl border border-stone-200 dark:border-stone-700 flex items-center justify-between">
          <div>
            <h4 id="modal-name-en" class="font-bold text-base leading-tight"></h4>
            <div id="modal-name-hi" class="text-xs font-semibold text-brand-600 dark:text-brand-400"></div>
          </div>
          <span id="modal-category-badge" class="text-[11px] font-bold uppercase px-2.5 py-1 rounded-md bg-white dark:bg-stone-900 border border-stone-200 dark:border-stone-700 text-stone-700 dark:text-stone-300"></span>
        </div>

        <!-- Editable Input View (when manual confirmation or manual entry is needed) -->
        <div id="modal-item-edit-fields" class="hidden space-y-3 p-3.5 bg-amber-50/60 dark:bg-amber-950/30 rounded-2xl border border-amber-200/80 dark:border-amber-800/60">
          <div>
            <label class="block text-xs font-bold text-stone-700 dark:text-stone-200 mb-1" data-i18n="modal_name_label">Food Item Name (खाद्य सामग्री का नाम):</label>
            <input 
              type="text" 
              id="modal-name-input" 
              placeholder="e.g. Paneer, Milk, Fresh Tomatoes" 
              class="w-full px-3.5 py-2 rounded-xl border border-stone-300 dark:border-stone-700 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 bg-white dark:bg-stone-900 text-stone-900 dark:text-stone-100 font-semibold"
            />
          </div>
          <div class="grid grid-cols-2 gap-2">
            <div>
              <label class="block text-[11px] font-bold text-stone-600 dark:text-stone-300 mb-1" data-i18n="modal_cat_label">Category (श्रेणी):</label>
              <select 
                id="modal-category-select" 
                class="w-full px-2.5 py-2 rounded-xl border border-stone-300 dark:border-stone-700 text-xs font-semibold focus:outline-none focus:ring-2 focus:ring-brand-500 bg-white dark:bg-stone-900 text-stone-800 dark:text-stone-200 cursor-pointer"
              >
                <option value="vegetable">Vegetable / सब्ज़ी</option>
                <option value="fruit">Fruit / फल</option>
                <option value="dairy">Dairy / डेयरी</option>
                <option value="cooked_dish">Cooked Dish / भोजन</option>
                <option value="snack_bakery">Snack & Bakery / स्नैक</option>
                <option value="beverage">Drink / पेय पदार्थ</option>
                <option value="grain_pulse_flour">Grains / अनाज व दालें</option>
                <option value="meat_fish_egg">Eggs & Meat / अंडा व मीट</option>
                <option value="spice_condiment">Spices / मसाले</option>
              </select>
            </div>
            <div>
              <label class="block text-[11px] font-bold text-stone-600 dark:text-stone-300 mb-1" data-i18n="modal_hi_label">Hindi Name (Optional):</label>
              <input 
                type="text" 
                id="modal-name-hi-input" 
                placeholder="उदा: पनीर, दूध" 
                class="w-full px-2.5 py-2 rounded-xl border border-stone-300 dark:border-stone-700 text-xs focus:outline-none focus:ring-2 focus:ring-brand-500 bg-white dark:bg-stone-900 text-stone-900 dark:text-stone-100"
              />
            </div>
          </div>
        </div>

        <div id="modal-packaged-alert" class="p-3 bg-amber-50 dark:bg-amber-950/40 rounded-xl border border-amber-300 dark:border-amber-800 text-xs text-amber-900 dark:text-amber-200 hidden">
          <div class="font-bold mb-0.5" data-i18n="modal_packaged_warn">Printed Expiry Date Required</div>
          <span data-i18n="modal_packaged_desc">Packaged snack / frozen item detected. Please check and enter the printed label date.</span>
        </div>

        <div>
          <label class="block text-xs font-semibold mb-1" id="modal-date-label" data-i18n="modal_date_label">Expiry Date:</label>
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

  <!-- SCANNER WEBCAM VIEWFINDER MODAL -->
  <div id="scanner-webcam-modal" class="fixed inset-0 bg-stone-900/75 backdrop-blur-sm z-50 flex items-center justify-center p-4 hidden">
    <div class="bg-white dark:bg-stone-900 rounded-3xl max-w-md w-full p-6 shadow-2xl border border-stone-200 dark:border-stone-800 text-stone-800 dark:text-stone-100 space-y-4">
      <div class="flex items-center justify-between border-b border-stone-100 dark:border-stone-800 pb-3">
        <div class="flex items-center gap-2">
          <svg class="w-5 h-5 text-emerald-600 dark:text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 13a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
          <h3 class="font-bold text-sm" data-i18n="scanner_webcam_title">Live Food Camera Viewfinder</h3>
        </div>
        <button id="close-webcam-modal-btn" type="button" class="text-stone-400 hover:text-stone-600 dark:hover:text-stone-200 text-2xl font-bold leading-none">&times;</button>
      </div>

      <div class="relative rounded-2xl overflow-hidden bg-black aspect-video flex items-center justify-center border border-stone-800 shadow-inner">
        <video id="webcam-video-stream" autoplay playsinline muted class="w-full h-full object-cover"></video>
        <!-- Overlay Viewfinder Targeting Rect -->
        <div class="absolute inset-6 border border-emerald-400/40 rounded-xl pointer-events-none flex items-center justify-center">
          <div class="w-16 h-16 border-2 border-emerald-400 rounded-lg animate-pulse"></div>
        </div>
        <!-- Live Badge -->
        <span class="absolute top-3 left-3 px-2 py-0.5 rounded-full bg-rose-600 text-white font-bold text-[9px] tracking-wider animate-pulse uppercase flex items-center gap-1">
          <span class="w-1.5 h-1.5 rounded-full bg-white"></span>
          <span data-i18n="webcam_live_badge">LIVE CAMERA</span>
        </span>
      </div>

      <div class="flex items-center justify-between gap-2 pt-1">
        <button 
          type="button" 
          id="webcam-switch-cam-btn" 
          class="px-3 py-2 rounded-xl bg-stone-100 dark:bg-stone-800 hover:bg-stone-200 text-stone-700 dark:text-stone-300 font-semibold text-xs transition flex items-center gap-1.5"
          title="Switch front/back camera"
        >
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
          <span data-i18n="webcam_flip_btn">Flip</span>
        </button>

        <button 
          type="button" 
          id="webcam-snap-btn" 
          class="flex-1 py-2.5 rounded-full bg-gradient-to-r from-emerald-600 to-teal-600 hover:from-emerald-700 hover:to-teal-700 text-white font-bold text-sm shadow-md hover:shadow-lg transition flex items-center justify-center gap-2 active:scale-95"
        >
          <span class="w-2.5 h-2.5 rounded-full bg-white animate-ping"></span>
          <span data-i18n="scanner_snap_btn">Snap Food Photo</span>
        </button>

        <button 
          type="button" 
          id="webcam-choose-file-btn" 
          class="px-3 py-2 rounded-xl bg-stone-100 dark:bg-stone-800 hover:bg-stone-200 text-stone-700 dark:text-stone-300 font-semibold text-xs transition flex items-center justify-center"
          title="Upload from device storage"
        >
          <svg class="w-4 h-4 text-stone-500 dark:text-stone-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12"/></svg>
        </button>
      </div>
    </div>
  </div>

    <!-- ADD TO HOME SCREEN / PWA INSTALL GUIDE MODAL -->
  <div id="home-screen-modal" class="fixed inset-0 bg-stone-900/60 backdrop-blur-sm z-50 flex items-center justify-center p-4 hidden">
    <div class="bg-white dark:bg-stone-900 rounded-3xl max-w-lg w-full max-h-[90vh] overflow-y-auto p-5 sm:p-7 shadow-2xl border border-stone-200 dark:border-stone-800 text-stone-800 dark:text-stone-100 space-y-4 animate-scale-in">
      
      <!-- Modal Header -->
      <div class="flex items-center justify-between border-b border-stone-100 dark:border-stone-800 pb-3">
        <div class="flex items-center gap-2.5">
          <img src="/static/icons/icon-192.png?v=3" alt="FridgeInventory" class="w-9 h-9 rounded-xl shadow-sm shrink-0 object-contain">
          <div>
            <h3 class="font-bold text-sm sm:text-base leading-tight" data-i18n="pwa_modal_title">Add to Home Screen</h3>
            <p class="text-[11px] text-stone-500 dark:text-stone-400" data-i18n="pwa_modal_subtitle">1-tap app launch with offline alerts</p>
          </div>
        </div>
        <button id="close-pwa-modal-btn" type="button" class="text-stone-400 hover:text-stone-600 dark:hover:text-stone-200 text-2xl font-bold leading-none p-1 cursor-pointer">&times;</button>
      </div>

      <!-- Platform Switcher Tabs -->
      <div class="flex items-center p-1 rounded-2xl bg-stone-100 dark:bg-stone-800 border border-stone-200 dark:border-stone-700 text-xs font-semibold">
        <button type="button" id="pwa-tab-ios" onclick="switchPwaTab('ios')" class="flex-1 py-1.5 rounded-xl transition text-center bg-white dark:bg-stone-900 text-brand-600 dark:text-brand-400 shadow-xs cursor-pointer">
          🍏 iPhone / iPad
        </button>
        <button type="button" id="pwa-tab-android" onclick="switchPwaTab('android')" class="flex-1 py-1.5 rounded-xl transition text-center text-stone-600 dark:text-stone-400 hover:text-stone-900 dark:hover:text-stone-100 cursor-pointer">
          🤖 Android
        </button>
        <button type="button" id="pwa-tab-desktop" onclick="switchPwaTab('desktop')" class="flex-1 py-1.5 rounded-xl transition text-center text-stone-600 dark:text-stone-400 hover:text-stone-900 dark:hover:text-stone-100 cursor-pointer">
          💻 Computer
        </button>
      </div>

      <!-- Tab 1: iOS Safari Steps -->
      <div id="pwa-content-ios" class="space-y-2.5 text-xs">
        <div class="p-3 rounded-2xl bg-stone-50 dark:bg-stone-800/60 border border-stone-200 dark:border-stone-700/80 flex items-start gap-3">
          <span class="w-6 h-6 rounded-full bg-brand-500 text-white font-bold text-xs flex items-center justify-center shrink-0">1</span>
          <div>
            <div class="font-bold text-stone-900 dark:text-stone-100" data-i18n="pwa_ios_step1_title">Tap the Share Button in Safari</div>
            <p class="text-stone-500 dark:text-stone-400 text-[11px] mt-0.5" data-i18n="pwa_ios_step1_desc">At the bottom of your iPhone screen (or top toolbar on iPad), tap the Share icon (<span class="font-mono text-brand-600 font-bold">⎋ / Share</span>).</p>
          </div>
        </div>
        <div class="p-3 rounded-2xl bg-stone-50 dark:bg-stone-800/60 border border-stone-200 dark:border-stone-700/80 flex items-start gap-3">
          <span class="w-6 h-6 rounded-full bg-brand-500 text-white font-bold text-xs flex items-center justify-center shrink-0">2</span>
          <div>
            <div class="font-bold text-stone-900 dark:text-stone-100" data-i18n="pwa_ios_step2_title">Select "Add to Home Screen"</div>
            <p class="text-stone-500 dark:text-stone-400 text-[11px] mt-0.5" data-i18n="pwa_ios_step2_desc">Scroll down the share sheet and tap <span class="font-semibold text-stone-800 dark:text-stone-200">"Add to Home Screen" ([+] icon)</span>.</p>
          </div>
        </div>
        <div class="p-3 rounded-2xl bg-stone-50 dark:bg-stone-800/60 border border-stone-200 dark:border-stone-700/80 flex items-start gap-3">
          <span class="w-6 h-6 rounded-full bg-brand-500 text-white font-bold text-xs flex items-center justify-center shrink-0">3</span>
          <div>
            <div class="font-bold text-stone-900 dark:text-stone-100" data-i18n="pwa_ios_step3_title">Tap "Add" in the Top Right</div>
            <p class="text-stone-500 dark:text-stone-400 text-[11px] mt-0.5" data-i18n="pwa_ios_step3_desc">Tap <span class="font-semibold text-stone-800 dark:text-stone-200">"Add"</span> to confirm. FridgeInventory will appear right on your iPhone home screen!</p>
          </div>
        </div>
        <div class="p-2.5 rounded-xl bg-amber-50/70 dark:bg-amber-950/20 border border-amber-200/80 dark:border-amber-800/40 text-[11px] text-amber-900 dark:text-amber-200 flex items-start gap-2">
          <span class="text-xs shrink-0">💡</span>
          <span data-i18n="pwa_ios_note">Note: Must be opened in Apple Safari. In-app browsers (Instagram, WhatsApp) do not support home screen installation.</span>
        </div>
      </div>

      <!-- Tab 2: Android Chrome Steps -->
      <div id="pwa-content-android" class="space-y-2.5 text-xs hidden">
        <div class="p-3 rounded-2xl bg-stone-50 dark:bg-stone-800/60 border border-stone-200 dark:border-stone-700/80 flex items-start gap-3">
          <span class="w-6 h-6 rounded-full bg-emerald-500 text-white font-bold text-xs flex items-center justify-center shrink-0">1</span>
          <div>
            <div class="font-bold text-stone-900 dark:text-stone-100" data-i18n="pwa_android_step1_title">Tap the Chrome Menu (⋮)</div>
            <p class="text-stone-500 dark:text-stone-400 text-[11px] mt-0.5" data-i18n="pwa_android_step1_desc">In Chrome, Samsung Internet, or Brave, tap the 3 vertical dots (<span class="font-bold">⋮</span>) in the top-right corner.</p>
          </div>
        </div>
        <div class="p-3 rounded-2xl bg-stone-50 dark:bg-stone-800/60 border border-stone-200 dark:border-stone-700/80 flex items-start gap-3">
          <span class="w-6 h-6 rounded-full bg-emerald-500 text-white font-bold text-xs flex items-center justify-center shrink-0">2</span>
          <div>
            <div class="font-bold text-stone-900 dark:text-stone-100" data-i18n="pwa_android_step2_title">Tap "Install App" or "Add to Home screen"</div>
            <p class="text-stone-500 dark:text-stone-400 text-[11px] mt-0.5" data-i18n="pwa_android_step2_desc">Select <span class="font-semibold text-stone-800 dark:text-stone-200">"Install app"</span> (or <span class="font-semibold text-stone-800 dark:text-stone-200">"Add to Home screen"</span>).</p>
          </div>
        </div>
        <div class="p-3 rounded-2xl bg-stone-50 dark:bg-stone-800/60 border border-stone-200 dark:border-stone-700/80 flex items-start gap-3">
          <span class="w-6 h-6 rounded-full bg-emerald-500 text-white font-bold text-xs flex items-center justify-center shrink-0">3</span>
          <div>
            <div class="font-bold text-stone-900 dark:text-stone-100" data-i18n="pwa_android_step3_title">Confirm Installation</div>
            <p class="text-stone-500 dark:text-stone-400 text-[11px] mt-0.5" data-i18n="pwa_android_step3_desc">Tap <span class="font-semibold text-stone-800 dark:text-stone-200">"Install"</span>. The app will be added to your home screen with offline push support!</p>
          </div>
        </div>
        <div class="p-2.5 rounded-xl bg-emerald-50/70 dark:bg-emerald-950/20 border border-emerald-200/80 dark:border-emerald-800/40 text-[11px] text-emerald-900 dark:text-emerald-200 flex items-start gap-2">
          <span class="text-xs shrink-0">💡</span>
          <span data-i18n="pwa_android_tip">Tip: If a banner appears at the bottom asking to add FridgeInventory to Home screen, you can also tap that directly!</span>
        </div>
      </div>

      <!-- Tab 3: Desktop Steps -->
      <div id="pwa-content-desktop" class="space-y-3 text-xs hidden">
        <!-- Method 1: Chrome 3-Dots Menu -->
        <div class="p-3.5 rounded-2xl bg-stone-50 dark:bg-stone-800/60 border border-cyan-200 dark:border-cyan-800/50 space-y-2.5">
          <div class="flex items-center gap-2 font-bold text-stone-900 dark:text-stone-100">
            <span class="px-2 py-0.5 rounded-md bg-cyan-600 text-white font-extrabold text-[10px] tracking-wide" data-i18n="pwa_desk_method1_badge">Method 1</span>
            <span class="text-cyan-800 dark:text-cyan-300 font-bold" data-i18n="pwa_desk_method1_title">Chrome Menu: "Cast, save, and share"</span>
          </div>
          <div class="space-y-2 text-stone-700 dark:text-stone-300">
            <div class="flex items-start gap-2.5">
              <span class="w-5 h-5 rounded-full bg-cyan-600 text-white font-bold text-[10px] flex items-center justify-center shrink-0">1</span>
              <div>
                <span class="font-semibold text-stone-900 dark:text-stone-100" data-i18n="pwa_desk_m1_s1_title">Click the Three Dots (⋮):</span>
                <span class="text-stone-500 dark:text-stone-400 text-[11px]" data-i18n="pwa_desk_m1_s1_desc">In the top-right corner of Google Chrome, click the 3 vertical dots (⋮).</span>
              </div>
            </div>
            <div class="flex items-start gap-2.5">
              <span class="w-5 h-5 rounded-full bg-cyan-600 text-white font-bold text-[10px] flex items-center justify-center shrink-0">2</span>
              <div>
                <span class="font-semibold text-stone-900 dark:text-stone-100" data-i18n="pwa_desk_m1_s2_title">Select "Cast, save, and share":</span>
                <span class="text-stone-500 dark:text-stone-400 text-[11px]" data-i18n="pwa_desk_m1_s2_desc">Hover or click "Cast, save, and share" (or "Save and share" in some Chrome versions).</span>
              </div>
            </div>
            <div class="flex items-start gap-2.5">
              <span class="w-5 h-5 rounded-full bg-cyan-600 text-white font-bold text-[10px] flex items-center justify-center shrink-0">3</span>
              <div>
                <span class="font-semibold text-stone-900 dark:text-stone-100" data-i18n="pwa_desk_m1_s3_title">Click "Install FridgeInventory...":</span>
                <span class="text-stone-500 dark:text-stone-400 text-[11px]" data-i18n="pwa_desk_m1_s3_desc">Click "Install FridgeInventory..." (or "Install page as app...").</span>
              </div>
            </div>
            <div class="flex items-start gap-2.5">
              <span class="w-5 h-5 rounded-full bg-cyan-600 text-white font-bold text-[10px] flex items-center justify-center shrink-0">4</span>
              <div>
                <span class="font-semibold text-stone-900 dark:text-stone-100" data-i18n="pwa_desk_m1_s4_title">Click "Install" to Confirm:</span>
                <span class="text-stone-500 dark:text-stone-400 text-[11px]" data-i18n="pwa_desk_m1_s4_desc">In the confirmation popup, click "Install". The app will open in its own clean window!</span>
              </div>
            </div>
          </div>
        </div>

        <!-- Method 2: Address Bar Quick Shortcut -->
        <div class="p-3.5 rounded-2xl bg-stone-50 dark:bg-stone-800/60 border border-stone-200 dark:border-stone-700/80 space-y-2.5">
          <div class="flex items-center gap-2 font-bold text-stone-900 dark:text-stone-100">
            <span class="px-2 py-0.5 rounded-md bg-stone-600 text-white font-extrabold text-[10px] tracking-wide" data-i18n="pwa_desk_method2_badge">Method 2</span>
            <span class="font-bold" data-i18n="pwa_desk_method2_title">URL Address Bar Quick Shortcut</span>
          </div>
          <div class="space-y-2 text-stone-700 dark:text-stone-300">
            <div class="flex items-start gap-2.5">
              <span class="w-5 h-5 rounded-full bg-stone-600 text-white font-bold text-[10px] flex items-center justify-center shrink-0">1</span>
              <div>
                <span class="font-semibold text-stone-900 dark:text-stone-100" data-i18n="pwa_desk_m2_s1_title">Click the Install Icon:</span>
                <span class="text-stone-500 dark:text-stone-400 text-[11px]" data-i18n="pwa_desk_m2_s1_desc">On the right side of the address bar, look for the install icon (💻 or ⊕).</span>
              </div>
            </div>
            <div class="flex items-start gap-2.5">
              <span class="w-5 h-5 rounded-full bg-stone-600 text-white font-bold text-[10px] flex items-center justify-center shrink-0">2</span>
              <div>
                <span class="font-semibold text-stone-900 dark:text-stone-100" data-i18n="pwa_desk_m2_s2_title">Click "Install":</span>
                <span class="text-stone-500 dark:text-stone-400 text-[11px]" data-i18n="pwa_desk_m2_s2_desc">Click "Install" in the confirmation popup.</span>
              </div>
            </div>
          </div>
        </div>

        <!-- Other Browsers Hint -->
        <div class="p-2.5 rounded-xl bg-amber-50/70 dark:bg-amber-950/20 border border-amber-200/80 dark:border-amber-800/40 text-[11px] text-amber-900 dark:text-amber-200 flex items-start gap-2">
          <span class="text-xs shrink-0">💡</span>
          <div>
            <span class="font-bold" data-i18n="pwa_desk_other_title">Other Browsers:</span>
            <span class="text-stone-600 dark:text-stone-400 block mt-0.5" data-i18n="pwa_desk_other_desc"><b>Edge:</b> Click "..." &rarr; Apps &rarr; "Install FridgeInventory". <br><b>Mac Safari:</b> In top menu bar, click File &rarr; "Add to Dock...".</span>
          </div>
        </div>
      </div>

      <!-- Native Install Button (Only visible when native deferredPrompt is available) -->
      <div id="pwa-native-prompt-container" class="hidden">
        <button type="button" id="pwa-trigger-install-btn" class="w-full py-2.5 rounded-xl bg-gradient-to-r from-brand-600 to-amber-600 hover:from-brand-700 hover:to-amber-700 text-white font-bold text-xs shadow-md transition flex items-center justify-center gap-2 cursor-pointer">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/></svg>
          <span data-i18n="pwa_trigger_btn">Click Here to Install Now</span>
        </button>
      </div>

      <button type="button" id="pwa-dismiss-btn" class="w-full py-2.5 rounded-xl border border-stone-200 dark:border-stone-700 text-stone-700 dark:text-stone-300 font-semibold text-xs hover:bg-stone-50 dark:hover:bg-stone-800 transition cursor-pointer" data-i18n="pwa_close_btn">
        Got It, Thanks!
      </button>
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
    let currentFridgePin = '';
    let pinRevealed = true;
    let currentLang = localStorage.getItem('fi_lang') || localStorage.getItem('rasoi_lang') || 'en';
    let selectedItemsForRescue = new Set();
    let pendingModalItem = null;
    let swRegistration = null;
    let pushSubscription = null;

    // Multi-Country Global Timezones
    const GLOBAL_TIMEZONES = [
      { id: 'auto', name: 'Auto (Detected Local Time)' },
      { id: 'America/New_York', name: 'New York (EST/EDT)' },
      { id: 'America/Chicago', name: 'Chicago (CST/CDT)' },
      { id: 'America/Denver', name: 'Denver (MST/MDT)' },
      { id: 'America/Los_Angeles', name: 'Los Angeles (PST/PDT)' },
      { id: 'America/Toronto', name: 'Toronto (EST/EDT)' },
      { id: 'America/Vancouver', name: 'Vancouver (PST/PDT)' },
      { id: 'Europe/London', name: 'London / UK (GMT/BST)' },
      { id: 'Europe/Paris', name: 'Paris / Berlin (CET/CEST)' },
      { id: 'Asia/Dubai', name: 'Dubai / Gulf (GST)' },
      { id: 'Asia/Kolkata', name: 'India (IST)' },
      { id: 'Asia/Singapore', name: 'Singapore (SGT)' },
      { id: 'Asia/Tokyo', name: 'Tokyo (JST)' },
      { id: 'Australia/Sydney', name: 'Sydney (AEST/AEDT)' },
      { id: 'Pacific/Auckland', name: 'Auckland (NZST)' },
      { id: 'UTC', name: 'UTC' },
    ];

    let userTimezone = localStorage.getItem('fi_user_tz') || 'auto';
    function getEffectiveTimezone() {
      if (userTimezone && userTimezone !== 'auto') return userTimezone;
      try {
        return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
      } catch (e) {
        return 'UTC';
      }
    }

    // Comprehensive Bilingual Text Dictionary (English & Hindi)
    const i18n = {
      en: {
        app_title: "FridgeInventory",
        app_subtitle: "Smart Food Inventory & Zero-Waste Kitchen",
        lang_btn: "हिन्दी",
        logout_btn: "Logout",
        login_create_btn: "Login",
        sidebar_title: "Fridge Space",
        sidebar_connected: "Active & Connected",
        sidebar_fid_label: "Fridge Name",
        sidebar_pin_label: "Family PIN / Password",
        sidebar_share_btn: "Share with Family",
        sidebar_logout_btn: "Logout from Fridge",
        bubble_title: "Welcome to FridgeInventory",
        bubble_desc: "Your smart household kitchen companion for effortless inventory tracking, offline push alerts, and AI-powered zero-waste cooking.",
        bubble_login_btn: "Login to Fridge",
        bubble_create_btn: "Create a New Fridge",
        feat_track_title: "Smart Food Tracking",
        feat_track_desc: "Keep real-time tabs on fresh produce, dairy, groceries, and cooked meals.",
        feat_expiry_title: "Offline Push Alerts",
        feat_expiry_desc: "Background Service Worker alerts you before food expires, even when offline.",
        feat_ai_title: "AI Rescue Chef",
        feat_ai_desc: "Turn expiring ingredients into homestyle recipes with one click.",
        stat_total: "Total Items",
        stat_fresh: "Fresh (3+ Days)",
        stat_urgent: "Expiring Soon",
        stat_expired: "Expired",
        cmd_heading: "Smart Voice & Text Control Center",
        cmd_sub: "Tell it what you brought or consumed. Example: 'brought 2kg palak', 'i ate lays', 'used milk', 'clear all'.",
        cmd_placeholder: "Type or speak (e.g. 'bought 2kg palak', 'i ate lays', 'used milk')...",
        cmd_btn: "Process Command",
        suggestions_label: "Quick Try:",
        tab_inventory: "Fridge Inventory",
        tab_urgent: "Expiring Soon",
        tab_push: "Offline Push Alerts",
        tab_rescue: "Rescue Chef",
        tab_scanner: "Food Scanner",
        time_sync_banner: "Live Time Tracking: Automatically compared with food addition dates on login. Fresh shelf lives synced.",
        filter_all: "All Items",
        filter_urgent: "Urgent",
        filter_fresh: "Fresh",
        filter_expired: "Expired",
        empty_title: "Your Fridge is Empty",
        empty_desc: "Add fresh produce, groceries, or meals using the input above or your microphone.",
        urgent_tab_title: "Urgent Attention Needed",
        urgent_tab_desc: "Items expiring within 48 hours or already expired. Consume or rescue them before they spoil!",
        push_hero_title: "Expiry Alerts",
        push_hero_desc: "Get notified before food in your fridge expires, even when your browser or phone is closed.",
        push_guide_title: "How to allow notifications in Google Chrome",
        push_guide_desc: "If your browser blocks notifications: click the lock / tune icon next to the web address at the top > Site settings > set Notifications to Allow.",
        push_guide_btn: "Google Help Guide",
        rescue_title: "Zero-Waste Rescue & Recipe Chef",
        rescue_desc: "Ask how to make any dish (e.g. 'how to make paneer butter masala') or select expiring leftovers to create an authentic homestyle dish.",
        rescue_run_btn: "Generate Rescue Recipe",
        custom_rescue_label: "Ask How to Make Any Recipe or Enter Ingredients to Rescue:",
        close_recipe_btn: "Close Recipe",
        recipe_card_tag: "Zero-Waste Homestyle Recipe",
        rescued_items_hdr: "Rescued Ingredients",
        pantry_items_hdr: "Pantry Spices & Staples",
        instructions_hdr: "Cooking Instructions:",
        chef_tip_hdr: "Chef's Zero-Waste Tip:",
        recipe_loading: "Consulting Culinary Intelligence...",
        scanner_title: "AI Food Inspector & Storage Advisor",
        scanner_desc: "Type any food name or snap a photo using the camera bubble to inspect its culinary category, Hindi name, standard shelf life, and storage advice.",
        scanner_inspect_btn: "Inspect Food",
        scanner_take_photo: "Take Photo",
        scanner_snap_btn: "Snap Food Photo",
        scanner_webcam_title: "Live Food Camera Viewfinder",
        auth_modal_title: "Fridge Login & Setup",
        auth_login_tab: "Login to Fridge",
        auth_create_tab: "Create New Fridge",
        auth_fid_label: "Fridge ID:",
        auth_pin_label: "Family PIN:",
        auth_login_submit: "Open Fridge",
        auth_create_fid_label: "Choose Fridge ID:",
        auth_create_pin_label: "Set Family PIN (min 4 characters):",
        auth_create_submit: "Create & Save Fridge",
        modal_confirm_title: "Confirm Fridge Entry",
        modal_name_label: "Food Item Name:",
        modal_cat_label: "Category:",
        modal_packaged_warn: "Printed Expiry Date Required",
        modal_packaged_desc: "Packaged snack or frozen item detected. Please check and enter the printed label date.",
        modal_tip_label: "Kitchen Storage Tip:",
        modal_cancel: "Cancel",
        modal_save: "Save to Fridge",
        cmd_photo_btn: "Add by Photo",
        chip_photo_snap: "📷 Snap Food Photo",
        pwa_install_btn: "Add to Home Screen",
        pwa_install_btn_short: "Install",
        pwa_modal_title: "Add to Home Screen",
        pwa_modal_subtitle: "1-tap app launch with offline alerts",
        pwa_ios_step1_title: "Tap the Share Button in Safari",
        pwa_ios_step1_desc: "At the bottom of your iPhone screen (or top toolbar on iPad), tap the Share icon (⎋ / Share).",
        pwa_ios_step2_title: "Select 'Add to Home Screen'",
        pwa_ios_step2_desc: "Scroll down the share sheet and tap 'Add to Home Screen' ([+] icon).",
        pwa_ios_step3_title: "Tap 'Add' in the Top Right",
        pwa_ios_step3_desc: "Tap 'Add' to confirm. FridgeInventory will appear right on your iPhone home screen!",
        pwa_ios_note: "Note: Must be opened in Apple Safari. In-app browsers (Instagram, WhatsApp) do not support home screen installation.",
        pwa_android_step1_title: "Tap the Chrome Menu (⋮)",
        pwa_android_step1_desc: "In Chrome, Samsung Internet, or Brave, tap the 3 vertical dots (⋮) in the top-right corner.",
        pwa_android_step2_title: "Tap 'Install App' or 'Add to Home screen'",
        pwa_android_step2_desc: "Select 'Install app' (or 'Add to Home screen').",
        pwa_android_step3_title: "Confirm Installation",
        pwa_android_step3_desc: "Tap 'Install'. The app will be added to your home screen with offline push support!",
        pwa_android_tip: "Tip: If a banner appears at the bottom asking to add FridgeInventory to Home screen, you can also tap that directly!",
        pwa_desk_method1_badge: "Method 1",
        pwa_desk_method1_title: "Chrome Menu: 'Cast, save, and share'",
        pwa_desk_m1_s1_title: "Click the Three Dots (⋮):",
        pwa_desk_m1_s1_desc: "In the top-right corner of Google Chrome, click the 3 vertical dots (⋮).",
        pwa_desk_m1_s2_title: "Select 'Cast, save, and share':",
        pwa_desk_m1_s2_desc: "Hover or click 'Cast, save, and share' (or 'Save and share' in some Chrome versions).",
        pwa_desk_m1_s3_title: "Click 'Install FridgeInventory...':",
        pwa_desk_m1_s3_desc: "Click 'Install FridgeInventory...' (or 'Install page as app...').",
        pwa_desk_m1_s4_title: "Click 'Install' to Confirm:",
        pwa_desk_m1_s4_desc: "In the confirmation popup, click 'Install'. The app will open in its own clean window!",
        pwa_desk_method2_badge: "Method 2",
        pwa_desk_method2_title: "URL Address Bar Quick Shortcut",
        pwa_desk_m2_s1_title: "Click the Install Icon:",
        pwa_desk_m2_s1_desc: "On the right side of the address bar, look for the install icon (💻 or ⊕).",
        pwa_desk_m2_s2_title: "Click 'Install':",
        pwa_desk_m2_s2_desc: "Click 'Install' in the confirmation popup.",
        pwa_desk_other_title: "Other Browsers:",
        pwa_desk_other_desc: "Edge: Click '...' > Apps > 'Install FridgeInventory'. Mac Safari: In top menu bar, click File > 'Add to Dock...'.",
        pwa_desk_step1_title: "Click the Install Icon in the URL Bar",
        pwa_desk_step1_desc: "In Chrome, Edge, or Brave, click the desktop install icon (⊕ or 💻) on the right side of the address bar.",
        pwa_desk_step2_title: "Confirm 'Install'",
        pwa_desk_step2_desc: "Click Install to run FridgeInventory in its own distraction-free standalone desktop window.",
        pwa_trigger_btn: "Click Here to Install Now",
        pwa_close_btn: "Got It, Thanks!",
        badge_pin_protected: "PIN Protected",
        btn_speak_recipe: "🔊 Listen Recipe",
        btn_stop_speech: "⏹️ Stop Voice",
        recipe_quick_try: "Quick Recipes:",
        custom_rescue_placeholder: "Ask 'how to make biryani', 'paneer butter masala', or enter leftovers: milk, bread, cheese...",
        header_alerts_label: "Alerts:",
        header_alerts_off: "OFF",
        header_alerts_on: "ON",
        sync_engine_heading: "Live Freshness Engine:",
        sync_timestamp_live: "Live Synced",
        trigger_rescue_btn: "Rescue Recipe",
        urgent_rescue_all_btn: "Rescue All with AI Chef",
        urgent_empty_title: "No Urgent Expirations!",
        urgent_empty_desc: "Everything in your fridge has plenty of shelf life remaining.",
        push_toggle_enable: "Enable Expiry Alerts",
        push_toggle_disable: "Disable Expiry Alerts",
        push_card_test_title: "Test Web Push",
        push_card_test_desc: "Signed VAPID push (buffers 24h offline).",
        push_btn_send_push: "Send Push",
        push_card_offline_title: "Test Offline Alert",
        push_card_offline_desc: "Local Service Worker (no Wi-Fi needed).",
        push_btn_test_offline: "Test Offline",
        push_card_expiry_title: "Check Expiry Now",
        push_card_expiry_desc: "Scan fridge and alert for food expiring soon.",
        push_btn_check_now: "Check Now",
        push_diag_title: "Technical status & diagnostics",
        rescue_sub_tip: "Speaks or types dishes, drinks, or leftovers",
        recipe_homestyle_badge: "Homestyle Meal",
        btn_copy: "Copy",
        recipe_pairing_title: "Chef's Zero-Waste Course & Pairing Notes:",
        scanner_ai_vision_badge: "AI Vision Ready",
        scanner_instant_title: "Instant AI Food Camera",
        scanner_instant_desc: "Snap any fresh dish or grocery to auto-identify & track",
        scanner_live_cam_btn: "Live Camera",
        scanner_upload_img_btn: "Upload Image",
        scanner_input_placeholder: "Or type any food (e.g. Paneer Butter Masala, Gajar, Dal Makhani)...",
        webcam_live_badge: "LIVE CAMERA",
        webcam_flip_btn: "Flip",
        auth_case_sensitive: "Case-Sensitive",
        auth_letters_numbers: "Letters & Numbers",
        auth_min_chars: "Min 4 Alphanumeric",
        auth_login_fid_hint: "Letters (a-z, A-Z) and numbers (0-9) only. No symbols or spaces.",
        auth_login_pin_hint: "Letters and numbers only. Exact casing verified.",
        auth_create_fid_hint: "Case-sensitive: saved exactly as typed. Allowed: a-z, A-Z, 0-9.",
        auth_create_pin_hint: "Letters (a-z, A-Z) and numbers (0-9) only. Strictly case-sensitive.",
        modal_hi_label: "Hindi Name (Optional):",
        modal_date_label: "Expiry Date:",
      },
      hi: {
        app_title: "FridgeInventory",
        app_subtitle: "स्मार्ट फूड इन्वेंटरी और ज़ीरो-वेस्ट किचन",
        lang_btn: "English",
        logout_btn: "लॉगआउट",
        login_create_btn: "लॉग इन",
        sidebar_title: "फ्रिज स्पेस",
        sidebar_connected: "सक्रिय और कनेक्टेड",
        sidebar_fid_label: "फ्रिज का नाम",
        sidebar_pin_label: "फैमिली PIN / पासवर्ड",
        sidebar_share_btn: "परिवार के साथ शेयर करें",
        sidebar_logout_btn: "फ्रिज से लॉगआउट करें",
        bubble_title: "FridgeInventory में आपका स्वागत है",
        bubble_desc: "इन्वेंटरी ट्रैकिंग, ऑफ़लाइन पुश अलर्ट और ज़ीरो-वेस्ट कुकिंग के लिए आपका स्मार्ट साथी।",
        bubble_login_btn: "फ्रिज में लॉग इन करें",
        bubble_create_btn: "नया फ्रिज बनाएँ",
        feat_track_title: "स्मार्ट फूड ट्रैकिंग",
        feat_track_desc: "ताज़ी सब्ज़ियों, डेयरी और किराना सामान पर नज़र रखें।",
        feat_expiry_title: "ऑफ़लाइन पुश अलर्ट",
        feat_expiry_desc: "बैकग्राउंड सर्विस वर्कर द्वारा समय से पहले चेतावनी, ऐप बंद होने पर भी।",
        feat_ai_title: "AI रेस्क्यू शेफ़",
        feat_ai_desc: "समाप्त होने वाले सामान से स्वादिष्ट होमस्टाइल रेसिपी बनाएँ।",
        stat_total: "कुल सामान",
        stat_fresh: "ताज़ा (3+ दिन)",
        stat_urgent: "जल्द समाप्त",
        stat_expired: "समय समाप्त",
        cmd_heading: "स्मार्ट आवाज़ और टेक्स्ट कमांड सेंटर",
        cmd_sub: "बताएं क्या लाए या खाया। जैसे: '2 किलो पालक लाया', 'i ate lays', 'दूध इस्तेमाल किया', 'सब खत्म'।",
        cmd_placeholder: "टाइप करें या बोलें (जैसे: '2 किलो पालक लाया', 'i ate lays', 'दूध पिया')...",
        cmd_btn: "कमांड चलाएँ",
        cmd_photo_btn: "फोटो से जोड़ें",
        chip_photo_snap: "📷 भोजन की फोटो लें",
        suggestions_label: "त्वरित प्रयास:",
        tab_inventory: "फ्रिज सामग्री",
        tab_urgent: "जल्द समाप्त",
        tab_push: "ऑफ़लाइन पुश अलर्ट",
        tab_rescue: "रेस्क्यू शेफ़",
        tab_scanner: "खाद्य परीक्षक",
        time_sync_banner: "लाइव टाइम ट्रैकिंग: लॉग इन पर सामान जोड़ने की तारीख से तुलना करके शेल्फ लाइफ अपडेट की गई।",
        filter_all: "सभी सामान",
        filter_urgent: "जल्द समाप्त",
        filter_fresh: "ताज़ा",
        filter_expired: "समय समाप्त",
        empty_title: "आपका फ्रिज खाली है",
        empty_desc: "ऊपर दिए गए इनपुट या माइक्रोफ़ोन का उपयोग करके ताज़ी सब्ज़ियाँ, किराना या खाना जोड़ें।",
        urgent_tab_title: "तुरंत ध्यान देने योग्य सामान",
        urgent_tab_desc: "सामान जो 48 घंटे में खराब हो सकते हैं या समाप्त हो चुके हैं।",
        push_hero_title: "एक्सपायरी अलर्ट",
        push_hero_desc: "खाना खराब होने से पहले अपने फोन या कंप्यूटर पर नोटिफिकेशन पाएं, चाहे ऐप बंद ही क्यों न हो।",
        push_guide_title: "गूगल क्रोम में नोटिफिकेशन कैसे चालू करें",
        push_guide_desc: "यदि नोटिफिकेशन ब्लॉक हैं: ऊपर वेबसाइट पते के पास ताले के निशान पर क्लिक करें > Site settings > Notifications को Allow करें।",
        push_guide_btn: "गूगल हेल्प गाइड",
        rescue_title: "ज़ीरो-वेस्ट रेस्क्यू एवं रेसिपी शेफ़",
        rescue_desc: "कोई भी रेसिपी पूछें (जैसे: 'पनीर बटर मसाला कैसे बनाएं') या फ्रिज में बचे हुए सामान से स्वादिष्ट होमस्टाइल रेसिपी बनाएँ।",
        rescue_run_btn: "रेस्क्यू रेसिपी बनाएँ",
        custom_rescue_label: "कोई भी रेसिपी पूछें (जैसे: कैसे बनाएं) या सामग्री दर्ज करें:",
        close_recipe_btn: "रेसिपी बंद करें",
        recipe_card_tag: "जीरो-वेस्ट होमस्टाइल रेसिपी",
        rescued_items_hdr: "बचाई गई सामग्री",
        pantry_items_hdr: "रसोई के मसाले और आवश्यक सामान",
        instructions_hdr: "पकाने की विधि:",
        chef_tip_hdr: "दादी माँ का नुस्खा (जीरो-वेस्ट टिप):",
        recipe_loading: "रेसिपी तैयार की जा रही है...",
        scanner_title: "खाद्य परीक्षक एवं भंडारण गाइड",
        scanner_desc: "किसी भी भोजन का नाम लिखें या कैमरा बबल से फोटो खींचकर शेल्फ-लाइफ, श्रेणी और भंडारण सलाह जानें।",
        scanner_inspect_btn: "परीक्षण करें",
        scanner_take_photo: "फोटो लें",
        scanner_snap_btn: "भोजन की फोटो लें",
        scanner_webcam_title: "लाइव फूड कैमरा व्यूफ़ाइंडर",
        auth_modal_title: "फ्रिज लॉग इन और सेटअप",
        auth_login_tab: "फ्रिज खोलें",
        auth_create_tab: "नया फ्रिज बनाएँ",
        auth_fid_label: "फ्रिज ID:",
        auth_pin_label: "फैमिली PIN:",
        auth_login_submit: "फ्रिज खोलें",
        auth_create_fid_label: "फ्रिज ID चुनें:",
        auth_create_pin_label: "फैमिली PIN सेट करें (कम से कम 4 अक्षर/अंक):",
        auth_create_submit: "फ्रिज बनाएँ और जोड़ें",
        modal_confirm_title: "सामान की पुष्टि करें",
        modal_name_label: "खाद्य सामग्री का नाम:",
        modal_cat_label: "श्रेणी:",
        modal_packaged_warn: "पैकेट की एक्सपायरी आवश्यक है",
        modal_packaged_desc: "पैकेज्ड स्नैक या फ्रोजन सामान। कृपया पैकेट पर छपी एक्सपायरी तारीख दर्ज करें।",
        modal_tip_label: "रसोई भंडारण सुझाव:",
        modal_cancel: "रद्द करें",
        modal_save: "फ्रिज में सेव करें",
        cmd_photo_btn: "फोटो से जोड़ें",
        chip_photo_snap: "📷 भोजन की फोटो लें",
        pwa_install_btn: "होम स्क्रीन पर जोड़ें",
        pwa_install_btn_short: "इंस्टॉल",
        pwa_modal_title: "होम स्क्रीन पर जोड़ें",
        pwa_modal_subtitle: "नेटिव ऐप की तरह 1-टैप लॉन्च और ऑफ़लाइन अलर्ट",
        pwa_ios_step1_title: "सफ़ारी (Safari) में शेयर बटन दबाएँ",
        pwa_ios_step1_desc: "अपने iPhone के नीचे (या iPad के ऊपर), शेयर आइकन (⎋ / Share) पर टैप करें।",
        pwa_ios_step2_title: "'होम स्क्रीन पर जोड़ें' चुनें",
        pwa_ios_step2_desc: "नीचे स्क्रॉल करें और 'Add to Home Screen' ([+] चिह्न) पर टैप करें।",
        pwa_ios_step3_title: "ऊपर दाएँ 'जोड़ें' (Add) पर टैप करें",
        pwa_ios_step3_desc: "'Add' पर टैप करें। FridgeInventory अब आपके iPhone की होम स्क्रीन पर आ जाएगा!",
        pwa_ios_note: "सुझाव: सफ़ारी (Safari) ब्राउज़र में ही खोलें। अन्य ऐप्स (WhatsApp, Instagram) के अंदर होम स्क्रीन का विकल्प नहीं मिलता।",
        pwa_android_step1_title: "क्रोम के 3 डॉट्स मेनू (⋮) पर टैप करें",
        pwa_android_step1_desc: "Chrome या Samsung Internet में ऊपर दाएँ कोने में 3 डॉट्स (⋮) पर टैप करें।",
        pwa_android_step2_title: "'ऐप इंस्टॉल करें' या 'होम स्क्रीन पर जोड़ें' चुनें",
        pwa_android_step2_desc: "'Install app' या 'Add to Home screen' विकल्प पर टैप करें।",
        pwa_android_step3_title: "इंस्टॉलेशन की पुष्टि करें",
        pwa_android_step3_desc: "'Install' पर क्लिक करें। ऐप आपकी होम स्क्रीन और ऐप ड्रॉअर में जुड़ जाएगा!",
        pwa_android_tip: "सुझाव: यदि स्क्रीन के नीचे 'Add FridgeInventory to Home screen' का बैनर दिखे, तो सीधे उस पर टैप कर सकते हैं।",
        pwa_desk_method1_badge: "तरीका 1",
        pwa_desk_method1_title: "क्रोम मेनू: 'Cast, save, and share'",
        pwa_desk_m1_s1_title: "3 डॉट्स (⋮) पर क्लिक करें:",
        pwa_desk_m1_s1_desc: "Chrome में ऊपर दाएँ कोने में 3 डॉट्स मेनू (⋮) पर क्लिक करें।",
        pwa_desk_m1_s2_title: "'Cast, save, and share' चुनें:",
        pwa_desk_m1_s2_desc: "'Cast, save, and share' (या 'Save and share') विकल्प पर माउस ले जाएँ या क्लिक करें।",
        pwa_desk_m1_s3_title: "'Install FridgeInventory...' पर क्लिक करें:",
        pwa_desk_m1_s3_desc: "'Install FridgeInventory...' (या 'Install page as app...') विकल्प चुनें।",
        pwa_desk_m1_s4_title: "'Install' पर क्लिक करके पुष्टि करें:",
        pwa_desk_m1_s4_desc: "पॉपअप में 'Install' बटन दबाएँ। ऐप एक अलग डेस्कटॉप विंडो में खुल जाएगा!",
        pwa_desk_method2_badge: "तरीका 2",
        pwa_desk_method2_title: "एड्रेस बार शॉर्टकट से इंस्टॉल करें",
        pwa_desk_m2_s1_title: "एड्रेस बार में इंस्टॉल आइकन दबाएँ:",
        pwa_desk_m2_s1_desc: "ब्राउज़र के URL एड्रेस बार के दाईं ओर स्थित ऐप इंस्टॉल आइकन (💻 या ⊕) पर क्लिक करें।",
        pwa_desk_m2_s2_title: "'Install' पर क्लिक करें:",
        pwa_desk_m2_s2_desc: "दिखाई देने वाले पॉपअप में 'Install' दबाएँ।",
        pwa_desk_other_title: "अन्य ब्राउज़र:",
        pwa_desk_other_desc: "Edge: '...' मेनू > Apps > 'Install FridgeInventory'। Mac Safari: मेनू बार में File > 'Add to Dock...' > Add।",
        pwa_desk_step1_title: "ब्राउज़र एड्रेस बार में इंस्टॉल आइकन दबाएँ",
        pwa_desk_step1_desc: "Chrome या Edge में एड्रेस बार के दाईं ओर स्थित इंस्टॉल चिह्न (⊕ या 💻) पर क्लिक करें।",
        pwa_desk_step2_title: "'Install' पर क्लिक करें",
        pwa_desk_step2_desc: "FridgeInventory बिना किसी रुकावट के एक स्वतंत्र ऐप विंडो में खुल जाएगा।",
        pwa_trigger_btn: "अभी इंस्टॉल करने के लिए यहाँ क्लिक करें",
        pwa_close_btn: "समझ गया, धन्यवाद!",
        badge_pin_protected: "PIN द्वारा सुरक्षित",
        btn_speak_recipe: "🔊 रेसिपी सुनें",
        btn_stop_speech: "⏹️ आवाज़ रोकें",
        recipe_quick_try: "त्वरित रेसिपी:",
        custom_rescue_placeholder: "पूछें 'पनीर बटर मसाला कैसे बनाएं', 'बिरयानी', या सामग्री दर्ज करें: दूध, ब्रेड...",
        header_alerts_label: "अलर्ट:",
        header_alerts_off: "बंद",
        header_alerts_on: "चालू",
        sync_engine_heading: "लाइव ताजगी ट्रैकर:",
        sync_timestamp_live: "लाइव सिंक",
        trigger_rescue_btn: "रेस्क्यू रेसिपी",
        urgent_rescue_all_btn: "AI शेफ़ से सभी रेस्क्यू करें",
        urgent_empty_title: "कोई जल्द समाप्त होने वाला सामान नहीं!",
        urgent_empty_desc: "आपके फ्रिज का सारा सामान पूरी तरह ताज़ा है।",
        push_toggle_enable: "एक्सपायरी अलर्ट चालू करें",
        push_toggle_disable: "अलर्ट बंद करें",
        push_card_test_title: "वेब पुश का परीक्षण करें",
        push_card_test_desc: "हस्ताक्षरित VAPID पुश (24 घंटे ऑफ़लाइन सुरक्षित)।",
        push_btn_send_push: "पुश भेजें",
        push_card_offline_title: "ऑफ़लाइन अलर्ट का परीक्षण करें",
        push_card_offline_desc: "लोकल सर्विस वर्कर (इंटरनेट की आवश्यकता नहीं)।",
        push_btn_test_offline: "ऑफ़लाइन टेस्ट",
        push_card_expiry_title: "अभी एक्सपायरी चेक करें",
        push_card_expiry_desc: "फ्रिज स्कैन करें और जल्द समाप्त होने वाले सामान की चेतावनी दें।",
        push_btn_check_now: "अभी चेक करें",
        push_diag_title: "तकनीकी स्थिति और डायग्नोस्टिक्स",
        rescue_sub_tip: "व्यंजनों, पेय या बचे हुए खाने के नाम बोलें या लिखें",
        recipe_homestyle_badge: "स्वादिष्ट घरेलू भोजन",
        btn_copy: "कॉपी करें",
        recipe_pairing_title: "शेफ़ के सुझाव और परोसने की सलाह:",
        scanner_ai_vision_badge: "AI विज़न सक्रिय",
        scanner_instant_title: "त्वरित AI भोजन कैमरा",
        scanner_instant_desc: "पहचान और ट्रैकिंग के लिए किसी भी भोजन या किराना सामान की फोटो लें",
        scanner_live_cam_btn: "लाइव कैमरा",
        scanner_upload_img_btn: "फोटो अपलोड करें",
        scanner_input_placeholder: "या किसी भी भोजन का नाम लिखें (जैसे: पनीर बटर मसाला, गाजर, दाल मखनी)...",
        webcam_live_badge: "लाइव कैमरा",
        webcam_flip_btn: "कैमरा बदलें",
        auth_case_sensitive: "केस-सेंसिटिव",
        auth_letters_numbers: "अक्षर और अंक",
        auth_min_chars: "न्यूनतम 4 अक्षर/अंक",
        auth_login_fid_hint: "केवल अक्षर (a-z, A-Z) और अंक (0-9)। कोई प्रतीक या स्पेस नहीं।",
        auth_login_pin_hint: "केवल अक्षर और अंक। बड़े/छोटे अक्षरों का मिलान आवश्यक है।",
        auth_create_fid_hint: "जैसे टाइप करेंगे वैसे ही सेव होगा। अनुमत: a-z, A-Z, 0-9।",
        auth_create_pin_hint: "केवल अक्षर और अंक। बड़े और छोटे अक्षरों का ध्यान रखें।",
        modal_hi_label: "हिंदी नाम (वैकल्पिक):",
        modal_date_label: "एक्सपायरी तारीख:",
      }
    };

    function applyLanguage(lang) {
      currentLang = lang;
      localStorage.setItem('fi_lang', lang);
      localStorage.setItem('rasoi_lang', lang);
      const texts = i18n[lang] || i18n.en;

      document.querySelectorAll('[data-i18n]').forEach(el => {
        const key = el.getAttribute('data-i18n');
        if (texts[key]) el.textContent = texts[key];
      });

      document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
        const key = el.getAttribute('data-i18n-placeholder');
        if (texts[key]) el.placeholder = texts[key];
      });

      const cmdInput = document.getElementById('cmd-input');
      if (cmdInput && texts.cmd_placeholder) cmdInput.placeholder = texts.cmd_placeholder;
      const langLabel = document.getElementById('lang-label');
      if (langLabel) langLabel.textContent = texts.lang_btn;

      renderInventory();
      updateStats();
      updatePushUI(!!pushSubscription);
      updateLiveClock();
      updateSidebarPinDisplay();

      if (typeof latestRecipe !== 'undefined' && latestRecipe) {
        const rCont = document.getElementById('recipe-container');
        if (rCont && !rCont.classList.contains('hidden')) {
          renderRecipe(latestRecipe);
        }
      }
    }

    document.getElementById('lang-toggle-btn').addEventListener('click', () => {
      applyLanguage(currentLang === 'en' ? 'hi' : 'en');
      renderInventory();
    });

    // Progressive Web App (PWA) & Add to Home Screen Engine
    let deferredInstallPrompt = null;

    function isAppInstalled() {
      return window.matchMedia('(display-mode: standalone)').matches ||
             window.navigator.standalone === true ||
             document.referrer.includes('android-app://');
    }

    function detectPlatform() {
      const ua = navigator.userAgent || '';
      // Support iOS including iPadOS where navigator.platform is MacIntel with touch points
      const isIOS = /iPhone|iPad|iPod/i.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
      const isAndroid = /Android/i.test(ua);
      if (isIOS) return 'ios';
      if (isAndroid) return 'android';
      return 'desktop';
    }

    function updateInstallButtonVisibility() {
      const btn = document.getElementById('pwa-install-btn');
      if (!btn) return;
      if (isAppInstalled()) {
        btn.classList.add('hidden');
      } else {
        btn.classList.remove('hidden');
      }
    }

    window.addEventListener('beforeinstallprompt', (e) => {
      e.preventDefault();
      deferredInstallPrompt = e;
      updateInstallButtonVisibility();
      const promptContainer = document.getElementById('pwa-native-prompt-container');
      if (promptContainer) promptContainer.classList.remove('hidden');
    });

    window.addEventListener('appinstalled', () => {
      deferredInstallPrompt = null;
      document.getElementById('home-screen-modal')?.classList.add('hidden');
      updateInstallButtonVisibility();
      showToast(
        currentLang === 'hi' ? 'FridgeInventory सफलतापूर्वक इंस्टॉल हो गया!' : 'FridgeInventory successfully installed!',
        'success'
      );
    });

    async function handlePwaInstallClick() {
      if (isAppInstalled()) {
        showToast(
          currentLang === 'hi' ? 'ऐप पहले से इंस्टॉल है!' : 'FridgeInventory is already running as an installed app!',
          'info'
        );
        updateInstallButtonVisibility();
        return;
      }
      if (deferredInstallPrompt) {
        // Native install prompt available (Chromium on Android / Desktop)
        await triggerNativeInstallPrompt();
      } else {
        // Show platform-specific guidance
        openHomeScreenModal();
      }
    }

    async function triggerNativeInstallPrompt() {
      if (!deferredInstallPrompt) {
        openHomeScreenModal();
        return;
      }
      try {
        deferredInstallPrompt.prompt();
        const choice = await deferredInstallPrompt.userChoice;
        if (choice && choice.outcome === 'accepted') {
          showToast(
            currentLang === 'hi' ? 'ऐप इंस्टॉल किया जा रहा है...' : 'Installing FridgeInventory...',
            'success'
          );
        } else {
          showToast(
            currentLang === 'hi' ? 'इंस्टॉलेशन रद्द किया गया' : 'Installation dismissed',
            'info'
          );
        }
      } catch (err) {
        console.warn('Install prompt error:', err);
      } finally {
        deferredInstallPrompt = null;
        const promptContainer = document.getElementById('pwa-native-prompt-container');
        if (promptContainer) promptContainer.classList.add('hidden');
        document.getElementById('home-screen-modal')?.classList.add('hidden');
        updateInstallButtonVisibility();
      }
    }

    function openHomeScreenModal() {
      const modal = document.getElementById('home-screen-modal');
      if (modal) modal.classList.remove('hidden');
      const platform = detectPlatform();
      switchPwaTab(platform);

      // Only display the direct native install button if a real deferred prompt event exists
      const promptContainer = document.getElementById('pwa-native-prompt-container');
      if (promptContainer) {
        if (deferredInstallPrompt) {
          promptContainer.classList.remove('hidden');
        } else {
          promptContainer.classList.add('hidden');
        }
      }
    }

    function switchPwaTab(platform) {
      const tabs = ['ios', 'android', 'desktop'];
      tabs.forEach(p => {
        const btn = document.getElementById(`pwa-tab-${p}`);
        const content = document.getElementById(`pwa-content-${p}`);
        if (p === platform) {
          if (btn) {
            btn.className = 'flex-1 py-1.5 rounded-xl transition text-center bg-white dark:bg-stone-900 text-brand-600 dark:text-brand-400 shadow-xs font-bold';
          }
          if (content) content.classList.remove('hidden');
        } else {
          if (btn) {
            btn.className = 'flex-1 py-1.5 rounded-xl transition text-center text-stone-600 dark:text-stone-400 hover:text-stone-900 dark:hover:text-stone-100 font-semibold';
          }
          if (content) content.classList.add('hidden');
        }
      });
    }

    document.getElementById('close-pwa-modal-btn')?.addEventListener('click', () => {
      document.getElementById('home-screen-modal')?.classList.add('hidden');
    });
    document.getElementById('pwa-dismiss-btn')?.addEventListener('click', () => {
      document.getElementById('home-screen-modal')?.classList.add('hidden');
    });
    document.getElementById('pwa-trigger-install-btn')?.addEventListener('click', () => {
      triggerNativeInstallPrompt();
    });

    // Manual Food Entry Modal Handler
    function openManualEntryModal(prefillName = '', defaultCategory = 'vegetable') {
      const modal = document.getElementById('classify-modal');
      const modalItemDisplay = document.getElementById('modal-item-display');
      const modalItemEdit = document.getElementById('modal-item-edit-fields');
      const modalDateInput = document.getElementById('modal-date-input');
      const modalDateHelp = document.getElementById('modal-date-help');
      const modalPackagedAlert = document.getElementById('modal-packaged-alert');
      const modalStorageTip = document.getElementById('modal-storage-tip');
      const modalQueuePill = document.getElementById('modal-queue-pill');

      if (modalQueuePill) modalQueuePill.classList.add('hidden');
      if (modalPackagedAlert) modalPackagedAlert.classList.add('hidden');

      if (modalItemDisplay) modalItemDisplay.classList.add('hidden');
      if (modalItemEdit) modalItemEdit.classList.remove('hidden');

      const nameInput = document.getElementById('modal-name-input');
      if (nameInput) {
        nameInput.value = prefillName;
        setTimeout(() => nameInput.focus(), 150);
      }
      const catSelect = document.getElementById('modal-category-select');
      if (catSelect) catSelect.value = defaultCategory;

      const defaultDate = new Date();
      defaultDate.setDate(defaultDate.getDate() + 3);
      if (modalDateInput) modalDateInput.value = defaultDate.toISOString().split('T')[0];
      if (modalDateHelp) {
        modalDateHelp.textContent = currentLang === 'hi' 
          ? 'अनुमानित 3 दिन की शेल्फ लाइफ सेट की गई है। आप अपनी इच्छानुसार तारीख बदल सकते हैं।'
          : 'Default 3 days shelf life set. You can customize the date.';
      }
      if (modalStorageTip) {
        modalStorageTip.textContent = currentLang === 'hi'
          ? 'ताज़ा रखने के लिए साफ़ और एयरटाइट डिब्बे में फ्रिज में रखें।'
          : 'Keep refrigerated in an airtight container for maximum freshness.';
      }

      pendingModalItem = {
        is_manual: true,
        name_en: prefillName || 'Fresh Food Item',
        name_hi: '',
        category: defaultCategory,
        shelf_life_days: 3,
        requires_expiry_date: false
      };

      if (modal) modal.classList.remove('hidden');
    }

    // Theme Management
    function initTheme() {
      const isDark = document.documentElement.classList.contains('dark');
      document.getElementById('theme-icon-sun').classList.toggle('hidden', !isDark);
      document.getElementById('theme-icon-moon').classList.toggle('hidden', isDark);
    }

    function toggleTheme() {
      if (document.documentElement.classList.contains('dark')) {
        document.documentElement.classList.remove('dark');
        localStorage.setItem('fi_theme', 'light');
        localStorage.setItem('rasoi_theme', 'light');
      } else {
        document.documentElement.classList.add('dark');
        localStorage.setItem('fi_theme', 'dark');
        localStorage.setItem('rasoi_theme', 'dark');
      }
      initTheme();
    }

    document.getElementById('theme-toggle-btn').addEventListener('click', toggleTheme);

    // Toast Notifications (Bilingual English & Hindi)
    function showToast(message, type = 'info', messageHi = null) {
      const finalMsg = (currentLang === 'hi' && messageHi) ? messageHi : message;
      const el = document.createElement('div');
      el.className = `pointer-events-auto px-4 py-2.5 rounded-xl shadow-lg text-xs font-semibold flex items-center gap-2 transition-all transform translate-y-2 opacity-0 ${
        type === 'success' ? 'bg-leaf-600 text-white' :
        type === 'error' ? 'bg-rose-600 text-white' :
        type === 'warning' ? 'bg-brand-600 text-white' : 'bg-stone-800 text-white'
      }`;
      el.textContent = finalMsg;
      document.getElementById('toast-container').appendChild(el);
      requestAnimationFrame(() => el.classList.remove('translate-y-2', 'opacity-0'));
      setTimeout(() => {
        el.classList.add('opacity-0', 'translate-y-2');
        setTimeout(() => el.remove(), 300);
      }, 3500);
    }

    // Helper: URL-safe base64 to Uint8Array for VAPID key
    function urlBase64ToUint8Array(base64String) {
      const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
      const base64 = (base64String + padding).replace(/\-/g, '+').replace(/_/g, '/');
      const rawData = window.atob(base64);
      const outputArray = new Uint8Array(rawData.length);
      for (let i = 0; i < rawData.length; ++i) {
        outputArray[i] = rawData.charCodeAt(i);
      }
      return outputArray;
    }

        // Preserve remembered fridge pin in localStorage for instant display
    
    // Auth & Fridge Management
    async function checkAuth() {
      try {
        localStorage.removeItem('fi_token');
      } catch(e){}

      try {
        const activeTz = getEffectiveTimezone();
        const res = await fetch('/api/auth/me', {
          credentials: 'same-origin',
          headers: { 'X-Client-Timezone': activeTz }
        });
        const data = await res.json();
        if (data.authenticated && data.fridge_id) {
          if (data.timezone && data.timezone !== 'UTC' && (!userTimezone || userTimezone === 'auto')) {
            userTimezone = data.timezone;
            const tzSelect = document.getElementById('user-tz-select');
            if (tzSelect) tzSelect.value = userTimezone;
          }
          setAuthenticatedState(data.fridge_id, data.pin || '');
          updateLiveClock();
        } else {
          setLoggedOutState();
        }
      } catch (e) {
        setLoggedOutState();
      }
    }

    function setAuthenticatedState(fid, pin = '') {
      currentFridge = fid;
      if (pin) {
        currentFridgePin = pin;
        try { localStorage.setItem('fi_last_pin_' + fid, pin); } catch(e){}
      } else {
        try { currentFridgePin = localStorage.getItem('fi_last_pin_' + fid) || currentFridgePin || ''; } catch(e){}
      }
      pinRevealed = true; // Straight up reveal PIN directly on screen without any prompts

      document.getElementById('auth-logged-in').classList.remove('hidden');
      document.getElementById('auth-logged-out').classList.add('hidden');
      document.getElementById('header-breadcrumb').classList.remove('hidden');
      document.getElementById('header-breadcrumb').classList.add('flex');
      
      const subBar = document.getElementById('sub-header-bar');
      if (subBar) subBar.classList.remove('hidden');

      document.getElementById('header-sync-pill').classList.remove('hidden');
      document.getElementById('header-sync-pill').classList.add('inline-flex');
      document.getElementById('header-pin-container').classList.remove('hidden');
      document.getElementById('header-pin-container').classList.add('inline-flex');
      document.getElementById('header-push-pill').classList.remove('hidden');
      document.getElementById('share-fridge-btn').classList.remove('hidden');
      document.getElementById('share-fridge-btn').classList.add('inline-flex');

      document.getElementById('header-fridge-name').textContent = fid;
      const subFid = document.getElementById('sub-header-fridge-name');
      if (subFid) subFid.textContent = fid;
      const diagFid = document.getElementById('diag-fridge-id');
      if (diagFid) diagFid.textContent = fid;

      updateSidebarPinDisplay();

      document.getElementById('logged-out-bubble').classList.add('hidden');
      document.getElementById('app-dashboard').classList.remove('hidden');
      loadInventory();
    }

    function setLoggedOutState() {
      if (currentFridge) {
        try { localStorage.removeItem('fi_last_pin_' + currentFridge); } catch(e){}
      }
      currentFridge = null;
      currentFridgePin = '';
      pinRevealed = true;
      document.getElementById('auth-logged-in').classList.add('hidden');
      document.getElementById('auth-logged-out').classList.remove('hidden');
      document.getElementById('header-breadcrumb').classList.add('hidden');
      document.getElementById('header-sync-pill').classList.add('hidden');
      document.getElementById('header-pin-container').classList.add('hidden');
      document.getElementById('header-push-pill').classList.add('hidden');
      document.getElementById('share-fridge-btn').classList.add('hidden');

      const subBar = document.getElementById('sub-header-bar');
      if (subBar) subBar.classList.add('hidden');

      document.getElementById('logged-out-bubble').classList.remove('hidden');
      document.getElementById('app-dashboard').classList.add('hidden');
      inventory = [];
    }

    function updateSidebarPinDisplay() {
      const pinEl = document.getElementById('sidebar-fridge-pin');
      const eyeIcon = document.getElementById('pin-eye-icon');
      const toggleBtn = document.getElementById('toggle-pin-btn');
      if (!pinEl) return;
      const effectivePin = currentFridgePin || (currentFridge ? localStorage.getItem('fi_last_pin_' + currentFridge) : '') || '';
      if (!effectivePin) {
        pinEl.textContent = '••••';
        if (eyeIcon) {
          eyeIcon.innerHTML = '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"></path><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"></path>';
        }
        if (toggleBtn) toggleBtn.setAttribute('title', 'Show PIN');
        return;
      }
      if (pinRevealed) {
        pinEl.textContent = effectivePin;
        if (eyeIcon) {
          eyeIcon.innerHTML = '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l18 18"></path>';
        }
        if (toggleBtn) toggleBtn.setAttribute('title', 'Hide PIN');
      } else {
        pinEl.textContent = '•'.repeat(Math.max(4, effectivePin.length));
        if (eyeIcon) {
          eyeIcon.innerHTML = '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"></path><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"></path>';
        }
        if (toggleBtn) toggleBtn.setAttribute('title', 'Show PIN');
      }
    }
    window.updateSidebarPinDisplay = updateSidebarPinDisplay;

    let _lastPinToggleTime = 0;
    function handleToggleSidebarPin(e) {
      if (e) {
        if (typeof e.preventDefault === 'function') e.preventDefault();
        if (typeof e.stopPropagation === 'function') e.stopPropagation();
      }
      const now = Date.now();
      if (now - _lastPinToggleTime < 200) return;
      _lastPinToggleTime = now;

      // Pure instantaneous toggle - NO popups, prompts, or verification
      pinRevealed = !pinRevealed;
      updateSidebarPinDisplay();
    }
    window.handleToggleSidebarPin = handleToggleSidebarPin;

    let _lastPinCopyTime = 0;
    function handleCopySidebarPin(e) {
      if (e) {
        if (typeof e.preventDefault === 'function') e.preventDefault();
        if (typeof e.stopPropagation === 'function') e.stopPropagation();
      }
      const now = Date.now();
      if (now - _lastPinCopyTime < 200) return;
      _lastPinCopyTime = now;

      const effectivePin = currentFridgePin || (currentFridge ? localStorage.getItem('fi_last_pin_' + currentFridge) : '') || '';
      if (effectivePin) {
        navigator.clipboard.writeText(effectivePin).then(() => {
          showToast(currentLang === 'hi' ? 'पिन कॉपी किया गया!' : 'PIN copied to clipboard!', 'success');
        });
      } else {
        showToast(currentLang === 'hi' ? 'कोई पिन उपलब्ध नहीं' : 'No PIN available', 'warning');
      }
    }
    window.handleCopySidebarPin = handleCopySidebarPin;

    const togglePinBtnEl = document.getElementById('toggle-pin-btn');
    if (togglePinBtnEl) {
      togglePinBtnEl.addEventListener('click', handleToggleSidebarPin);
    }
    const copyPinBtnEl = document.getElementById('copy-pin-btn');
    if (copyPinBtnEl) {
      copyPinBtnEl.addEventListener('click', handleCopySidebarPin);
    }

    document.getElementById('copy-fid-btn')?.addEventListener('click', () => {
      if (currentFridge) {
        navigator.clipboard.writeText(currentFridge).then(() => {
          showToast(currentLang === 'hi' ? 'फ्रिज ID कॉपी किया गया!' : 'Fridge ID copied to clipboard!', 'success');
        });
      }
    });
    document.getElementById('sub-copy-fid-btn')?.addEventListener('click', () => {
      if (currentFridge) {
        navigator.clipboard.writeText(currentFridge).then(() => {
          showToast(currentLang === 'hi' ? 'फ्रिज ID कॉपी किया गया!' : 'Fridge ID copied to clipboard!', 'success');
        });
      }
    });

    document.getElementById('share-fridge-btn').addEventListener('click', () => {
      if (currentFridge) {
        const pinToShare = currentFridgePin || (currentFridge ? localStorage.getItem('fi_last_pin_' + currentFridge) : '') || '';
        const text = `FridgeInventory Login Details:\nFridge ID: ${currentFridge}\nPIN / Password: ${pinToShare || '(Your Family PIN)'}\nURL: ${window.location.origin}`;
        navigator.clipboard.writeText(text).then(() => {
          showToast(currentLang === 'hi' ? 'फ्रिज विवरण शेयर करने के लिए कॉपी किया गया!' : 'Fridge details copied to share with family!', 'success');
        });
      }
    });

    async function handleLogout() {
      try {
        await fetch('/api/auth/logout', { 
          method: 'POST',
          credentials: 'same-origin'
        });
      } catch (e) {
        console.error('Logout error:', e);
      }
      setLoggedOutState();
      showToast(currentLang === 'hi' ? 'लॉगआउट सफल।' : 'Logged out successfully.', 'info');
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

    // PIN / Password Input Visibility Eye Toggles
    let _lastInputToggleTime = 0;
    function toggleInputPasswordVisibility(inputId, btnEl) {
      const now = Date.now();
      if (now - _lastInputToggleTime < 200) return;
      _lastInputToggleTime = now;

      const pinInput = document.getElementById(inputId);
      if (!pinInput) return;
      const isPass = (pinInput.getAttribute('type') || pinInput.type) === 'password';
      const newType = isPass ? 'text' : 'password';
      pinInput.setAttribute('type', newType);
      pinInput.type = newType;

      if (btnEl) {
        if (isPass) {
          btnEl.innerHTML = '<svg class="w-4 h-4 text-brand-600 dark:text-brand-400 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l18 18"/></svg>';
          btnEl.setAttribute('title', 'Hide PIN');
        } else {
          btnEl.innerHTML = '<svg class="w-4 h-4 text-stone-400 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/></svg>';
          btnEl.setAttribute('title', 'Show PIN');
        }
      }
    }
    window.toggleInputPasswordVisibility = toggleInputPasswordVisibility;

    const toggleLoginPinBtn = document.getElementById('toggle-login-pin-btn');
    if (toggleLoginPinBtn) {
      toggleLoginPinBtn.addEventListener('click', (e) => {
        if (e) {
          e.preventDefault();
          e.stopPropagation();
        }
        toggleInputPasswordVisibility('login-pin', toggleLoginPinBtn);
      });
    }

    const toggleCreatePinBtn = document.getElementById('toggle-create-pin-btn');
    if (toggleCreatePinBtn) {
      toggleCreatePinBtn.addEventListener('click', (e) => {
        if (e) {
          e.preventDefault();
          e.stopPropagation();
        }
        toggleInputPasswordVisibility('create-pin', toggleCreatePinBtn);
      });
    }

    loginForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fid = document.getElementById('login-fid').value.trim();
      const pin = document.getElementById('login-pin').value.trim();

      if (!/^[a-zA-Z0-9]{3,30}$/.test(fid)) {
        showToast('Fridge ID must be 3-30 characters with letters (a-z, A-Z) and numbers (0-9) only (case-sensitive).', 'error');
        return;
      }
      if (!/^[a-zA-Z0-9]{4,30}$/.test(pin)) {
        showToast('PIN must be 4-30 characters with letters (a-z, A-Z) and numbers (0-9) only (case-sensitive).', 'error');
        return;
      }

      const activeTz = getEffectiveTimezone();
      try {
        const res = await fetch('/api/auth/login', {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/json',
            'X-Client-Timezone': activeTz,
          },
          body: JSON.stringify({ fridge_id: fid, pin: pin, timezone: activeTz })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Login failed');
        currentFridgePin = pin;
        pinRevealed = true;
        authModal.classList.add('hidden');
        setAuthenticatedState(data.fridge_id, data.pin || pin);
        updateLiveClock();
        showToast(data.message || 'Login successful!', 'success', 'लॉग इन सफल!');
      } catch (err) {
        showToast(err.message, 'error');
      }
    });

    createForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fid = document.getElementById('create-fid').value.trim();
      const pin = document.getElementById('create-pin').value.trim();

      if (!/^[a-zA-Z0-9]{3,30}$/.test(fid)) {
        showToast('Fridge ID must be 3-30 characters with letters (a-z, A-Z) and numbers (0-9) only (case-sensitive).', 'error');
        return;
      }
      if (!/^[a-zA-Z0-9]{4,30}$/.test(pin)) {
        showToast('PIN must be 4-30 characters with letters (a-z, A-Z) and numbers (0-9) only (case-sensitive).', 'error');
        return;
      }

      const activeTz = getEffectiveTimezone();
      try {
        const res = await fetch('/api/auth/create', {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/json',
            'X-Client-Timezone': activeTz,
          },
          body: JSON.stringify({ fridge_id: fid, pin: pin, timezone: activeTz })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Fridge creation failed');
        currentFridgePin = pin;
        pinRevealed = true;
        authModal.classList.add('hidden');
        setAuthenticatedState(data.fridge_id, data.pin || pin);
        updateLiveClock();
        showToast(data.message, 'success', 'नया फ्रिज सफलतापूर्वक बनाया गया!');
      } catch (err) {
        showToast(err.message, 'error');
      }
    });

    document.getElementById('logout-btn').addEventListener('click', handleLogout);

    // Load Inventory Items with Strict Authorization & Offline Caching
    async function loadInventory(silent = false) {
      if (!currentFridge) return;
      const activeTz = getEffectiveTimezone();
      try {
        const url = `/api/inventory?fridge=${encodeURIComponent(currentFridge)}`;
        const res = await fetch(url, {
          credentials: 'same-origin',
          headers: { 
            'X-Client-Timezone': activeTz
          }
        });
        if (res.status === 401 || res.status === 403) {
          setLoggedOutState();
          showToast('Session unauthorized or expired. Please log in.', 'error', 'सत्र अनधिकृत या समाप्त। कृपया लॉग इन करें।');
          return;
        }
        if (!res.ok) throw new Error('Failed to load inventory');
        inventory = await res.json();
        // Save to offline cache for disconnected access
        try {
          localStorage.setItem('fi_cached_inventory', JSON.stringify(inventory));
        } catch (e) {}
        renderInventory();
        updateStats();
        checkLocalOfflineExpiryAlerts();
      } catch (err) {
        // Fallback to offline local cache if network is unavailable
        const cached = localStorage.getItem('fi_cached_inventory');
        if (cached) {
          try {
            inventory = JSON.parse(cached);
            renderInventory();
            updateStats();
            checkLocalOfflineExpiryAlerts();
            if (!silent) {
              showToast(currentLang === 'hi' ? 'ऑफ़लाइन मोड: कैश्ड इन्वेंटरी दिखाई जा रही है' : 'Offline mode: Showing cached inventory', 'info');
            }
            return;
          } catch (e) {}
        }
        if (!silent) {
          showToast('Could not load inventory', 'error');
        }
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
      document.getElementById('tab-urgent-count').textContent = urgent + expired;
    }

    function renderInventory() {
      const grid = document.getElementById('inventory-grid');
      const empty = document.getElementById('empty-state');
      const urgentGrid = document.getElementById('urgent-grid');
      const urgentEmpty = document.getElementById('urgent-empty');

      grid.innerHTML = '';
      if (urgentGrid) urgentGrid.innerHTML = '';
      
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

      // Populate main inventory grid
      filtered.forEach(item => {
        const card = createItemCard(item);
        grid.appendChild(card);
      });

      // Populate urgent items view (<= 2 days or expired)
      const urgentItems = inventory.filter(i => i.days_left <= 2);
      if (urgentGrid && urgentEmpty) {
        if (urgentItems.length === 0) {
          urgentEmpty.classList.remove('hidden');
        } else {
          urgentEmpty.classList.add('hidden');
          urgentItems.forEach(item => {
            const card = createItemCard(item);
            urgentGrid.appendChild(card);
          });
        }
      }

      // Checkbox handlers
      document.querySelectorAll('.rescue-check').forEach(box => {
        box.addEventListener('change', (e) => {
          const name = e.target.dataset.name;
          if (e.target.checked) selectedItemsForRescue.add(name);
          else selectedItemsForRescue.delete(name);
          updateRescueBar();
        });
      });

      // Delete handlers
      document.querySelectorAll('.delete-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
          await deleteItem(e.target.dataset.id);
        });
      });
    }

    function createItemCard(item) {
      let badgeClass = 'bg-leaf-50 dark:bg-stone-800 text-leaf-700 dark:text-leaf-300 border-leaf-200 dark:border-stone-700';
      let statusText = `${item.days_left}d left`;
      
      if (item.days_left < 0) {
        badgeClass = 'bg-rose-50 dark:bg-stone-800 text-rose-700 dark:text-rose-300 border-rose-200 dark:border-stone-700 font-semibold';
        statusText = currentLang === 'hi' ? `अवधि समाप्त (${Math.abs(item.days_left)} दिन पहले)` : `Expired ${Math.abs(item.days_left)}d ago`;
      } else if (item.days_left === 0) {
        badgeClass = 'bg-amber-100 dark:bg-stone-800 text-brand-800 dark:text-brand-300 border-amber-300 dark:border-stone-700 font-bold animate-pulse';
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

      const CATEGORY_NAMES_HI = {
        vegetable: 'सब्ज़ी',
        fruit: 'फल',
        dairy: 'डेयरी',
        cooked_dish: 'भोजन',
        snack_bakery: 'स्नैक',
        beverage: 'पेय पदार्थ',
        grain_pulse_flour: 'अनाज व दालें',
        meat_fish_egg: 'अंडा व मीट',
        spice_condiment: 'मसाले'
      };
      const catDisplay = currentLang === 'hi' ? (CATEGORY_NAMES_HI[item.category] || item.category.replace(/_/g, ' ')) : item.category.replace(/_/g, ' ');
      const elapsedText = currentLang === 'hi' ? (item.days_elapsed === 0 ? 'आज' : `${item.days_elapsed} दिन पहले`) : (item.days_elapsed === 0 ? 'Today' : `${item.days_elapsed}d ago`);

      const card = document.createElement('div');
      card.className = `bg-white dark:bg-stone-900 rounded-2xl p-4 border border-stone-200/80 dark:border-stone-800 shadow-sm hover:shadow-md transition-all flex flex-col justify-between ${
        item.days_left < 0 ? 'opacity-85' : ''
      }`;
      card.innerHTML = `
        <div class="space-y-3">
          <div class="flex items-start justify-between gap-2">
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-1.5 flex-wrap">
                <h4 class="font-bold text-stone-900 dark:text-white text-sm leading-tight break-words">${displayName}</h4>
                <span class="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-stone-100 dark:bg-stone-800 text-stone-500 uppercase shrink-0">${catDisplay}</span>
              </div>
              ${subName ? `<p class="text-xs text-stone-500 dark:text-stone-400 mt-0.5 truncate">${subName}</p>` : ''}
            </div>
            <span class="text-[11px] px-2.5 py-0.5 rounded-full border ${badgeClass} shrink-0 whitespace-nowrap">
              ${statusText}
            </span>
          </div>

          <div class="text-[11px] text-stone-500 dark:text-stone-400 space-y-1.5">
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-1 text-[11px]">
              <div>${currentLang === 'hi' ? 'जोड़ा गया:' : 'Added:'} <b class="text-stone-700 dark:text-stone-300 font-medium">${item.added_date}</b> (${elapsedText})</div>
              <div class="sm:text-right">${currentLang === 'hi' ? 'समाप्त होगा:' : 'Expires:'} <b class="text-stone-700 dark:text-stone-300 font-medium">${item.expiry_date}</b></div>
            </div>
            ${item.storage_tip ? `<div class="text-stone-600 dark:text-stone-300 bg-stone-50 dark:bg-stone-800/80 p-2 rounded-xl border border-stone-100 dark:border-stone-700/80 leading-relaxed text-[11px]">${item.storage_tip}</div>` : ''}
          </div>
        </div>

        <div class="mt-3 pt-3 border-t border-stone-100 dark:border-stone-800 flex items-center justify-between gap-2">
          <label class="flex items-center gap-1.5 text-xs text-stone-600 dark:text-stone-300 cursor-pointer select-none min-w-0">
            <input type="checkbox" class="rescue-check rounded text-brand-600 focus:ring-brand-500 shrink-0" data-name="${item.name_en}" ${isChecked ? 'checked' : ''}>
            <span class="truncate">${currentLang === 'hi' ? 'रेस्क्यू में जोड़ें' : 'Select for Rescue'}</span>
          </label>
          <button class="delete-btn text-xs text-stone-400 hover:text-rose-600 font-medium transition px-2 py-1 rounded-lg hover:bg-stone-100 dark:hover:bg-stone-800 shrink-0 cursor-pointer" data-id="${item.id}">
            ${currentLang === 'hi' ? 'हटाएँ' : 'Clear'}
          </button>
        </div>
      `;
      return card;
    }

    function updateRescueBar() {
      const count = selectedItemsForRescue.size;
      const countElem = document.getElementById('selected-count');
      if (countElem) countElem.textContent = count;
      const labelElem = document.getElementById('rescue-selected-label');
      if (labelElem) {
        labelElem.innerHTML = currentLang === 'hi' 
          ? `<b id="selected-count" class="text-brand-400 font-bold">${count}</b> सामान चुने गए`
          : `Selected <b id="selected-count" class="text-brand-400 font-bold">${count}</b> items`;
      }
      document.getElementById('rescue-action-bar').classList.toggle('hidden', count === 0);
    }

    async function deleteItem(id) {
      try {
        const url = currentFridge ? `/api/inventory/${id}?fridge=${encodeURIComponent(currentFridge)}` : `/api/inventory/${id}`;
        const res = await fetch(url, { 
          method: 'DELETE',
          credentials: 'same-origin'
        });
        if (res.status === 401 || res.status === 403) {
          setLoggedOutState();
          showToast('Session unauthorized. Please log in.', 'error', 'सत्र अनधिकृत। कृपया लॉग इन करें।');
          return;
        }
        if (!res.ok) throw new Error('Delete failed');
        inventory = inventory.filter(i => i.id !== id);
        try { localStorage.setItem('fi_cached_inventory', JSON.stringify(inventory)); } catch(e){}
        renderInventory();
        updateStats();
        showToast(currentLang === 'hi' ? 'सामान हटा दिया गया' : 'Item removed', 'info');
      } catch (err) {
        showToast('Failed to delete item', 'error');
      }
    }

    // Modal Handling for Adding Classified Food (Supports Sequential Multi-Item Queue)
    const modal = document.getElementById('classify-modal');
    const modalNameEn = document.getElementById('modal-name-en');
    const modalNameHi = document.getElementById('modal-name-hi');
    const modalCatBadge = document.getElementById('modal-category-badge');
    const modalPackagedAlert = document.getElementById('modal-packaged-alert');
    const modalDateInput = document.getElementById('modal-date-input');
    const modalDateHelp = document.getElementById('modal-date-help');
    const modalStorageTip = document.getElementById('modal-storage-tip');
    const modalQueuePill = document.getElementById('modal-queue-pill');

    let modalQueue = [];
    let modalQueueTotal = 0;
    let modalQueueIndex = 0;

    function openModalWithClassification(result, currentIdx = 0, totalCount = 0) {
      pendingModalItem = result;

      const modalItemDisplay = document.getElementById('modal-item-display');
      const modalItemEdit = document.getElementById('modal-item-edit-fields');
      if (modalItemDisplay) modalItemDisplay.classList.remove('hidden');
      if (modalItemEdit) modalItemEdit.classList.add('hidden');

      modalNameEn.textContent = result.name_en;
      modalNameHi.textContent = result.name_hi;
      modalCatBadge.textContent = result.category.replace(/_/g, ' ');
      modalStorageTip.textContent = result.storage_tip || 'Keep in a cool dry spot or refrigerated.';

      if (modalQueuePill) {
        if (totalCount > 1 && currentIdx > 0) {
          modalQueuePill.textContent = `Item ${currentIdx} of ${totalCount}`;
          modalQueuePill.classList.remove('hidden');
        } else {
          modalQueuePill.classList.add('hidden');
        }
      }

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

    function processNextModalQueueItem() {
      if (modalQueue.length > 0) {
        modalQueueIndex++;
        const nextItem = modalQueue.shift();
        openModalWithClassification(nextItem, modalQueueIndex, modalQueueTotal);
      } else {
        pendingModalItem = null;
        modalQueueTotal = 0;
        modalQueueIndex = 0;
        modal.classList.add('hidden');
        if (modalQueuePill) modalQueuePill.classList.add('hidden');
        document.getElementById('cmd-input').value = '';
      }
    }

    document.getElementById('close-modal-btn').addEventListener('click', () => {
      if (modalQueue.length > 0) {
        const skipped = pendingModalItem ? pendingModalItem.name_en : 'Item';
        showToast(`Skipped ${skipped}`, 'info');
        processNextModalQueueItem();
      } else {
        modal.classList.add('hidden');
        if (modalQueuePill) modalQueuePill.classList.add('hidden');
        pendingModalItem = null;
        modalQueue = [];
      }
    });

    document.getElementById('modal-cancel-btn').addEventListener('click', () => {
      if (modalQueue.length > 0) {
        const skipped = pendingModalItem ? pendingModalItem.name_en : 'Item';
        showToast(`Skipped ${skipped}`, 'info');
        processNextModalQueueItem();
      } else {
        modal.classList.add('hidden');
        if (modalQueuePill) modalQueuePill.classList.add('hidden');
        pendingModalItem = null;
        modalQueue = [];
      }
    });

    document.getElementById('modal-confirm-btn').addEventListener('click', async () => {
      if (!pendingModalItem) return;

      if (pendingModalItem.is_manual) {
        const manualName = (document.getElementById('modal-name-input')?.value || '').trim();
        const manualHi = (document.getElementById('modal-name-hi-input')?.value || '').trim();
        const manualCat = document.getElementById('modal-category-select')?.value || 'cooked_dish';
        if (!manualName) {
          showToast('Please enter food item name.', 'warning', 'कृपया खाद्य सामग्री का नाम लिखें।');
          return;
        }
        pendingModalItem.name_en = manualName;
        pendingModalItem.name_hi = manualHi || manualName;
        pendingModalItem.category = manualCat;
      }

      const expiry = modalDateInput.value;
      if (!expiry) {
        showToast('Please select a valid expiry date.', 'error', 'कृपया एक वैध समाप्ति तारीख चुनें।');
        return;
      }

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
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/json',
            'X-Client-Timezone': getEffectiveTimezone()
          },
          body: JSON.stringify(payload)
        });

        if (!res.ok) throw new Error('Could not add item');
        const saved = await res.json();
        inventory.unshift(saved);
        try { localStorage.setItem('fi_cached_inventory', JSON.stringify(inventory)); } catch(e){}
        renderInventory();
        updateStats();
        showToast(currentLang === 'hi' ? `${saved.name_hi || saved.name_en} जोड़ा गया!` : `Added ${saved.name_en}!`, 'success');

        // Check if more packaged items are waiting in the sequential queue
        if (modalQueue.length > 0) {
          processNextModalQueueItem();
        } else {
          modal.classList.add('hidden');
          if (modalQueuePill) modalQueuePill.classList.add('hidden');
          pendingModalItem = null;
          modalQueueTotal = 0;
          modalQueueIndex = 0;
          document.getElementById('cmd-input').value = '';
        }
      } catch (err) {
        showToast('Error saving item to inventory', 'error');
      }
    });

    // AUTO-ADD FRESH ITEMS (Skip modal for everyday groceries)
    async function autoAddFreshItem(classification) {
      const days = (classification.shelf_life_days && classification.shelf_life_days > 0) ? classification.shelf_life_days : 4;
      const today = new Date();
      const expDate = new Date();
      expDate.setDate(today.getDate() + days);

      const payload = {
        name_en: classification.name_en,
        name_hi: classification.name_hi,
        category: classification.category,
        shelf_life_days: classification.shelf_life_days,
        requires_expiry_date: false,
        expiry_date: expDate.toISOString().split('T')[0],
        storage_tip: classification.storage_tip,
      };

      try {
        const url = currentFridge ? `/api/inventory?fridge=${encodeURIComponent(currentFridge)}` : '/api/inventory';
        const res = await fetch(url, {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/json',
            'X-Client-Timezone': getEffectiveTimezone(),
          },
          body: JSON.stringify(payload),
        });

        if (!res.ok) throw new Error('Could not add item');
        const saved = await res.json();
        inventory.unshift(saved);
        try { localStorage.setItem('fi_cached_inventory', JSON.stringify(inventory)); } catch(e){}
        renderInventory();
        updateStats();
        document.getElementById('cmd-input').value = '';

        const displayName = currentLang === 'hi' ? (saved.name_hi || saved.name_en) : saved.name_en;
        const tipText = classification.storage_tip ? ` | ${classification.storage_tip}` : '';
        showToast(`Added ${displayName} (${days}d shelf-life)${tipText}`, 'success');
      } catch (err) {
        showToast('Error saving item', 'error');
      }
    }

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
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/json',
            'X-Client-Timezone': getEffectiveTimezone()
          },
          body: JSON.stringify({ command: text })
        });
        if (res.status === 401 || res.status === 403) {
          setLoggedOutState();
          showToast('Session unauthorized or expired. Please log in.', 'error', 'सत्र अनधिकृत या समाप्त। कृपया लॉग इन करें।');
          return;
        }

        if (!res.ok) {
          const errData = await res.json();
          throw new Error(errData.detail || 'Command processing failed');
        }

        const data = await res.json();

        // 1. Clear action
        if (data.action === 'recipe') {
          const tabRescueBtn = document.getElementById('tab-rescue-btn');
          if (tabRescueBtn) tabRescueBtn.click();
          const rescueInput = document.getElementById('rescue-items-input');
          if (rescueInput) rescueInput.value = data.dish || rawInput;
          stopRecipeSpeech();
          triggerRecipeGeneration([data.dish || rawInput]);
          showToast(currentLang === 'hi' ? `'${data.dish}' की रेसिपी तैयार हो रही है!` : `Generating recipe for '${data.dish}'!`, 'info');
          return;
        }

        if (data.action === 'clear') {
          inventory = [];
          try { localStorage.setItem('fi_cached_inventory', JSON.stringify(inventory)); } catch(e){}
          renderInventory();
          updateStats();
          cmdInput.value = '';
          showToast(currentLang === 'hi' ? data.message_hi : data.message_en, 'success');
        }
        // 2a. Single Consume / Remove action (e.g. "i ate milk")
        else if (data.action === 'removed') {
          inventory = inventory.filter(i => i.id !== data.item_id);
          try { localStorage.setItem('fi_cached_inventory', JSON.stringify(inventory)); } catch(e){}
          renderInventory();
          updateStats();
          cmdInput.value = '';
          showToast(currentLang === 'hi' ? data.message_hi : data.message_en, 'success');
        }
        // 2b. Multi-Remove action (e.g. "ate milk, spinach" or "consumed bread and butter")
        else if (data.action === 'removed_multi') {
          const removedIds = new Set(data.removed_ids || []);
          inventory = inventory.filter(i => !removedIds.has(i.id));
          try { localStorage.setItem('fi_cached_inventory', JSON.stringify(inventory)); } catch(e){}
          renderInventory();
          updateStats();
          cmdInput.value = '';
          showToast(currentLang === 'hi' ? data.message_hi : data.message_en, 'success');
        }
        // 3. Item not found for consumption
        else if (data.action === 'not_found') {
          showToast(currentLang === 'hi' ? data.message_hi : data.message_en, 'warning');
        }
        // 4. Single Add / Classify action
        else if (data.action === 'classify') {
          const c = data.classification;
          if (!c.is_food) {
            showToast(c.message || 'Not recognized as a food item.', 'warning');
          } else if (c.requires_expiry_date) {
            modalQueue = [];
            modalQueueTotal = 1;
            modalQueueIndex = 1;
            openModalWithClassification(c, 1, 1);
          } else {
            autoAddFreshItem(c);
          }
        }
        // 5. Multi-Add action (e.g. "brought lays, red bull, milk, spinach")
        else if (data.action === 'multi_add') {
          // Fresh items made today were already persisted on the backend
          if (data.auto_added && data.auto_added.length > 0) {
            data.auto_added.forEach(item => inventory.unshift(item));
            try { localStorage.setItem('fi_cached_inventory', JSON.stringify(inventory)); } catch(e){}
            renderInventory();
            updateStats();
          }

          // Packaged items requiring printed expiry date open modal sequentially one-by-one
          const needsExpiry = data.needs_expiry || [];
          if (needsExpiry.length > 0) {
            modalQueue = [...needsExpiry];
            modalQueueTotal = needsExpiry.length;
            modalQueueIndex = 1;
            const firstItem = modalQueue.shift();
            openModalWithClassification(firstItem, 1, modalQueueTotal);
            if (data.auto_added && data.auto_added.length > 0) {
              const freshNames = data.auto_added.map(i => i.name_en).join(', ');
              showToast(`Added fresh: ${freshNames}. Setting expiry for packaged snacks...`, 'info');
            }
          } else {
            cmdInput.value = '';
            showToast(currentLang === 'hi' ? data.summary_hi : data.summary_en, 'success');
          }
        }
      } catch (err) {
        showToast(err.message, 'error');
      } finally {
        cmdSubmitBtn.disabled = false;
        cmdSubmitBtn.innerHTML = `<span>${i18n[currentLang].cmd_btn}</span><svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>`;
      }
    });

    // Universal Voice Recognition Controller with Edge & Corner Orange Tint
    function showVoiceOrangeTint(buttonEl) {
      const tint = document.getElementById('voice-recording-tint');
      const banner = document.getElementById('voice-recording-banner');
      const bannerText = document.getElementById('voice-banner-text');

      if (tint) {
        tint.classList.remove('hidden');
        requestAnimationFrame(() => {
          tint.classList.remove('opacity-0');
          tint.classList.add('opacity-100');
        });
      }

      if (banner) {
        if (bannerText) {
          bannerText.textContent = currentLang === 'hi' 
            ? '🎙️ सुन रहा हूँ... बोलिए (जैसे "2 किलो पालक लाया")' 
            : '🎙️ Listening... Speak now (e.g. "bought 2kg palak")';
        }
        banner.classList.remove('hidden');
        requestAnimationFrame(() => {
          banner.classList.remove('-translate-y-12', 'opacity-0');
          banner.classList.add('translate-y-0', 'opacity-100');
        });
      }

      if (buttonEl) {
        buttonEl.classList.add('text-orange-500', 'ring-4', 'ring-orange-400/70', 'bg-orange-50', 'dark:bg-orange-950/60', 'scale-110', 'animate-pulse');
      }
    }

    function hideVoiceOrangeTint(buttonEl) {
      const tint = document.getElementById('voice-recording-tint');
      const banner = document.getElementById('voice-recording-banner');

      if (tint) {
        tint.classList.remove('opacity-100');
        tint.classList.add('opacity-0');
        setTimeout(() => {
          if (tint.classList.contains('opacity-0')) {
            tint.classList.add('hidden');
          }
        }, 300);
      }

      if (banner) {
        banner.classList.remove('translate-y-0', 'opacity-100');
        banner.classList.add('-translate-y-12', 'opacity-0');
        setTimeout(() => {
          if (banner.classList.contains('opacity-0')) {
            banner.classList.add('hidden');
          }
        }, 300);
      }

      if (buttonEl) {
        buttonEl.classList.remove('text-orange-500', 'ring-4', 'ring-orange-400/70', 'bg-orange-50', 'dark:bg-orange-950/60', 'scale-110', 'animate-pulse', 'text-rose-600');
      }
    }

    function setupVoiceRecognition(buttonEl, inputEl, onComplete) {
      if (!buttonEl) return;
      const hasSpeech = ('webkitSpeechRecognition' in window) || ('SpeechRecognition' in window);

      if (!hasSpeech) {
        buttonEl.addEventListener('click', () => {
          showToast(currentLang === 'hi' ? 'इस ब्राउज़र में वॉइस रिकॉग्निशन समर्थित नहीं है। कृपया Chrome या Safari का उपयोग करें।' : 'Voice speech recognition not supported in this browser. Please use Chrome or Safari.', 'warning');
        });
        return;
      }

      const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
      let rec = null;
      let isRecording = false;

      function stopSession() {
        if (rec && isRecording) {
          try { rec.stop(); } catch(e){}
        }
        isRecording = false;
        hideVoiceOrangeTint(buttonEl);
      }

      buttonEl.addEventListener('click', (e) => {
        e.preventDefault();

        if (isRecording) {
          stopSession();
          return;
        }

        try {
          rec = new SpeechRec();
          rec.continuous = false;
          rec.interimResults = false;
          rec.lang = currentLang === 'hi' ? 'hi-IN' : 'en-IN';

          rec.onstart = () => {
            isRecording = true;
            showVoiceOrangeTint(buttonEl);
          };

          rec.onresult = (evt) => {
            const transcript = evt.results[0][0].transcript;
            stopSession();
            if (inputEl) {
              inputEl.value = transcript;
              inputEl.focus();
            }
            if (onComplete) onComplete(transcript);
          };

          rec.onerror = (err) => {
            console.warn('Speech error:', err);
            stopSession();
            if (err.error === 'not-allowed') {
              showToast(currentLang === 'hi' ? 'माइक्रोफ़ोन अनुमति बंद है। कृपया ब्राउज़र सेटिंग्स में अनुमति दें।' : 'Microphone permission blocked. Please allow mic in browser settings.', 'warning');
            } else if (err.error !== 'no-speech') {
              showToast(currentLang === 'hi' ? 'आवाज़ पहचान में समस्या आई, कृपया पुनः प्रयास करें' : 'Could not hear voice. Try again.', 'warning');
            }
          };

          rec.onend = () => {
            stopSession();
          };

          // Immediate visual orange tint so user knows microphone is starting
          isRecording = true;
          showVoiceOrangeTint(buttonEl);
          rec.start();
          showToast(currentLang === 'hi' ? '🎙️ सुन रहा हूँ... बोलिए' : '🎙️ Listening... Speak now', 'info');
        } catch (err) {
          console.error('Failed to start voice recognition:', err);
          stopSession();
          showToast(currentLang === 'hi' ? 'माइक्रोफ़ोन सक्रिय नहीं हो सका' : 'Could not activate microphone', 'error');
        }
      });
    }

    // Attach voice listeners to all 3 inputs: Command bar, Rescue Recipe, and Food Scanner
    setupVoiceRecognition(document.getElementById('mic-btn'), cmdInput, () => commandForm.dispatchEvent(new Event('submit')));
    setupVoiceRecognition(document.getElementById('rescue-mic-btn'), document.getElementById('rescue-items-input'), (text) => checkAndCorrectRescueSpelling(text));
    setupVoiceRecognition(document.getElementById('scanner-mic-btn'), document.getElementById('scanner-input'), () => document.getElementById('scanner-btn').click());

    // Command Bar Photo Intake Triggers (Native Mobile Camera / Desktop Webcam)
    function openCommandPhotoCapture() {
      const isMobile = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent) || ('ontouchstart' in window);
      if (isMobile) {
        const camFile = document.getElementById('cmd-camera-file');
        if (camFile) camFile.click();
      } else {
        startWebcamStream('environment');
      }
    }

    const cmdPhotoBtn = document.getElementById('cmd-photo-btn');
    if (cmdPhotoBtn) cmdPhotoBtn.addEventListener('click', openCommandPhotoCapture);

    const cmdCameraBtn = document.getElementById('cmd-camera-btn');
    if (cmdCameraBtn) cmdCameraBtn.addEventListener('click', openCommandPhotoCapture);

    const cmdPhotoChip = document.getElementById('cmd-photo-chip');
    if (cmdPhotoChip) {
      cmdPhotoChip.addEventListener('click', (e) => {
        e.stopPropagation();
        openCommandPhotoCapture();
      });
    }

    const cmdCameraFile = document.getElementById('cmd-camera-file');
    if (cmdCameraFile) {
      cmdCameraFile.addEventListener('change', (e) => {
        if (e.target.files && e.target.files[0]) {
          scanFoodPhoto(e.target.files[0]);
          e.target.value = '';
        }
      });
    }

    const cmdGalleryFile = document.getElementById('cmd-gallery-file');
    if (cmdGalleryFile) {
      cmdGalleryFile.addEventListener('change', (e) => {
        if (e.target.files && e.target.files[0]) {
          scanFoodPhoto(e.target.files[0]);
          e.target.value = '';
        }
      });
    }

    // Quick Chips (Exclude photo snap chip from text command submission)
    document.querySelectorAll('.chip').forEach(chip => {
      chip.addEventListener('click', () => {
        if (chip.id === 'cmd-photo-chip') return;
        cmdInput.value = chip.textContent;
        commandForm.dispatchEvent(new Event('submit'));
      });
    });

    // GITHUB-STYLE TAB NAVIGATION (5 TABS)
    const tabInvBtn = document.getElementById('tab-inventory-btn');
    const tabUrgentBtn = document.getElementById('tab-urgent-btn');
    const tabPushBtn = document.getElementById('tab-push-btn');
    const tabRescBtn = document.getElementById('tab-rescue-btn');
    const tabTestBtn = document.getElementById('tab-test-btn');

    const secInv = document.getElementById('section-inventory');
    const secUrgent = document.getElementById('section-urgent');
    const secPush = document.getElementById('section-push');
    const secResc = document.getElementById('section-rescue');
    const secTest = document.getElementById('section-test');

    function switchTab(activeBtn, activeSec) {
      [tabInvBtn, tabUrgentBtn, tabPushBtn, tabRescBtn, tabTestBtn].forEach(b => {
        b.className = 'tab-btn py-3 px-3 border-b-2 border-transparent text-stone-500 dark:text-stone-400 hover:text-stone-800 dark:hover:text-stone-200 flex items-center gap-2 whitespace-nowrap';
      });
      [secInv, secUrgent, secPush, secResc, secTest].forEach(s => s.classList.add('hidden'));

      activeBtn.className = 'tab-btn py-3 px-3 border-b-2 border-brand-600 text-brand-600 dark:text-brand-400 flex items-center gap-2 whitespace-nowrap font-bold';
      activeSec.classList.remove('hidden');
    }

    tabInvBtn.addEventListener('click', () => switchTab(tabInvBtn, secInv));
    tabUrgentBtn.addEventListener('click', () => switchTab(tabUrgentBtn, secUrgent));
    tabPushBtn.addEventListener('click', () => switchTab(tabPushBtn, secPush));
    tabRescBtn.addEventListener('click', () => switchTab(tabRescBtn, secResc));
    tabTestBtn.addEventListener('click', () => switchTab(tabTestBtn, secTest));

    document.getElementById('header-push-pill').addEventListener('click', () => {
      switchTab(tabPushBtn, secPush);
    });

    document.getElementById('urgent-rescue-all-btn').addEventListener('click', () => {
      const urgentItems = inventory.filter(i => i.days_left <= 2).map(i => i.name_en);
      switchTab(tabRescBtn, secResc);
      document.getElementById('rescue-items-input').value = urgentItems.join(', ');
      triggerRecipeGeneration(urgentItems);
    });

    document.getElementById('trigger-rescue-btn').addEventListener('click', () => {
      switchTab(tabRescBtn, secResc);
      const items = Array.from(selectedItemsForRescue);
      document.getElementById('rescue-items-input').value = items.join(', ');
      triggerRecipeGeneration(items);
    });

    document.querySelectorAll('.filter-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        document.querySelectorAll('.filter-btn').forEach(b => {
          b.className = 'filter-btn bg-stone-100 dark:bg-stone-800 text-stone-600 dark:text-stone-300 hover:bg-stone-200 px-3.5 py-1.5 rounded-full font-medium';
        });
        e.target.className = 'filter-btn active bg-stone-900 dark:bg-white text-white dark:text-stone-900 px-3.5 py-1.5 rounded-full font-medium';
        currentFilter = e.target.dataset.filter;
        renderInventory();
      });
    });

    // Zero-Waste Rescue Generation & Close Button
    const generateRecipeBtn = document.getElementById('generate-recipe-btn');
    const recipeContainer = document.getElementById('recipe-container');
    const recipeLoader = document.getElementById('recipe-loader');
    const closeRecipeBtn = document.getElementById('close-recipe-btn');
    const rescueItemsInput = document.getElementById('rescue-items-input');
    const rescueSpellBadge = document.getElementById('rescue-spell-badge');

    // Live spelling correction for Rescue Recipe custom ingredients
    async function checkAndCorrectRescueSpelling(customStr = null) {
      const val = customStr !== null ? customStr : (rescueItemsInput ? rescueItemsInput.value.trim() : '');
      if (!val) {
        if (rescueSpellBadge) rescueSpellBadge.classList.add('hidden');
        return;
      }
      try {
        const res = await fetch('/api/rescue/correct-spelling', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ items: val })
        });
        if (res.ok) {
          const data = await res.json();
          if (data.has_corrections && data.corrected_string) {
            if (rescueItemsInput) rescueItemsInput.value = data.corrected_string;
            if (rescueSpellBadge) {
              const pairs = Object.entries(data.corrections_made || {}).map(([o, c]) => `${o} → ${c}`).join(', ');
              rescueSpellBadge.textContent = `Corrected: ${pairs}`;
              rescueSpellBadge.classList.remove('hidden');
            }
          } else if (rescueSpellBadge) {
            rescueSpellBadge.classList.add('hidden');
          }
        }
      } catch (e) {
        console.warn('Spelling check error:', e);
      }
    }

    if (rescueItemsInput) {
      rescueItemsInput.addEventListener('blur', () => checkAndCorrectRescueSpelling());
      rescueItemsInput.addEventListener('change', () => checkAndCorrectRescueSpelling());
    }

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
        latestRecipe = recipe;
        renderRecipe(recipe);
      } catch (err) {
        showToast(err.message, 'error');
      } finally {
        recipeLoader.classList.add('hidden');
        generateRecipeBtn.disabled = false;
      }
    }

    let latestRecipe = null;

    function renderRecipe(recipe) {
      if (!recipe) return;
      latestRecipe = recipe;
      const isHindi = currentLang === 'hi';

      // Title & Subtitle based on active language
      const titleEl = document.getElementById('recipe-name');
      const subTitleEl = document.getElementById('recipe-name-hi');
      if (isHindi) {
        titleEl.textContent = recipe.recipe_name_hi || recipe.recipe_name;
        subTitleEl.textContent = (recipe.recipe_name && recipe.recipe_name !== recipe.recipe_name_hi) ? recipe.recipe_name : 'ज़ीरो-वेस्ट होमस्टाइल रेसिपी';
      } else {
        titleEl.textContent = recipe.recipe_name;
        subTitleEl.textContent = recipe.recipe_name_hi || '';
      }

      // Cooking Time
      const timeVal = isHindi ? `${recipe.prep_time_minutes} मिनट` : `${recipe.prep_time_minutes} mins`;
      const timeText = document.getElementById('recipe-time-text');
      if (timeText) timeText.textContent = timeVal;
      else document.getElementById('recipe-time').textContent = timeVal;

      // Chef Zero-Waste Tip
      const tipVal = isHindi ? (recipe.chef_tip_hi || recipe.chef_tip) : (recipe.chef_tip || recipe.chef_tip_hi);
      document.getElementById('recipe-tip').textContent = tipVal || '';

      // Meal Type pill
      const mealTypeEl = document.getElementById('recipe-meal-type');
      if (mealTypeEl) {
        const mealTypeVal = isHindi ? (recipe.meal_type_hi || recipe.meal_type) : (recipe.meal_type || recipe.meal_type_hi);
        if (mealTypeVal) {
          mealTypeEl.textContent = mealTypeVal.replace(/_/g, ' ');
          mealTypeEl.classList.remove('hidden');
        } else {
          mealTypeEl.classList.add('hidden');
        }
      }

      // Pairing Box (e.g. Drink + Food where combining in single dish won't work)
      const pairingBox = document.getElementById('recipe-pairing-box');
      const pairingText = document.getElementById('recipe-pairing-text');
      if (pairingBox && pairingText) {
        const pairingVal = isHindi ? (recipe.pairing_notes_hi || recipe.pairing_notes) : (recipe.pairing_notes || recipe.pairing_notes_hi);
        if (pairingVal) {
          pairingText.textContent = pairingVal;
          pairingBox.classList.remove('hidden');
        } else {
          pairingBox.classList.add('hidden');
        }
      }

      // Rescued Ingredients
      const rescuedList = document.getElementById('recipe-rescued-list');
      rescuedList.innerHTML = '';
      const rescuedItems = isHindi && recipe.ingredients_used_hi && recipe.ingredients_used_hi.length > 0
        ? recipe.ingredients_used_hi
        : (recipe.ingredients_used || []);
      rescuedItems.forEach(i => {
        const li = document.createElement('li');
        li.textContent = i;
        rescuedList.appendChild(li);
      });

      // Pantry Items
      const pantryList = document.getElementById('recipe-pantry-list');
      pantryList.innerHTML = '';
      const pantryItems = isHindi && recipe.pantry_items_needed_hi && recipe.pantry_items_needed_hi.length > 0
        ? recipe.pantry_items_needed_hi
        : (recipe.pantry_items_needed || []);
      pantryItems.forEach(i => {
        const li = document.createElement('li');
        li.textContent = i;
        pantryList.appendChild(li);
      });

      // Cooking Steps
      const stepsList = document.getElementById('recipe-steps-list');
      stepsList.innerHTML = '';
      const steps = isHindi && recipe.steps_hi && recipe.steps_hi.length > 0
        ? recipe.steps_hi
        : (recipe.steps || []);
      steps.forEach((step, idx) => {
        const li = document.createElement('li');
        li.className = 'flex items-start gap-2.5';
        li.innerHTML = `<span class="w-5 h-5 rounded-full bg-brand-100 dark:bg-stone-800 text-brand-800 dark:text-brand-300 font-bold text-xs flex items-center justify-center shrink-0 mt-0.5">${idx + 1}</span><span>${step}</span>`;
        stepsList.appendChild(li);
      });

      recipeContainer.classList.remove('hidden');
    }

        // ========================================================
    // VOICE AUDIO RECIPE NARRATOR (SPEECH SYNTHESIS ENGINE)
    // ========================================================
    let isRecipeSpeaking = false;

    function updateSpeechButtonUI(speaking) {
      isRecipeSpeaking = speaking;
      const btnText = document.getElementById('speak-recipe-btn-text');
      const iconIdle = document.getElementById('speak-icon-idle');
      const btn = document.getElementById('speak-recipe-btn');
      if (!btnText) return;

      if (speaking) {
        btnText.textContent = currentLang === 'hi' ? '⏹️ आवाज़ रोकें' : '⏹️ Stop Voice';
        if (btn) btn.className = 'px-2.5 py-1 bg-amber-50 dark:bg-amber-950/60 border border-amber-300 dark:border-amber-700 rounded-xl text-brand-700 dark:text-brand-300 text-xs font-bold inline-flex items-center gap-1.5 shrink-0 transition cursor-pointer shadow-sm';
      } else {
        btnText.textContent = currentLang === 'hi' ? '🔊 रेसिपी सुनें' : '🔊 Listen Recipe';
        if (btn) btn.className = 'px-2.5 py-1 bg-white dark:bg-stone-900 hover:bg-stone-100 dark:hover:bg-stone-750 border border-stone-200 dark:border-stone-700 rounded-xl text-stone-600 dark:text-stone-300 text-xs font-semibold inline-flex items-center gap-1.5 shrink-0 transition cursor-pointer';
      }
    }

    function stopRecipeSpeech() {
      if ('speechSynthesis' in window) {
        window.speechSynthesis.cancel();
      }
      updateSpeechButtonUI(false);
    }
    window.stopRecipeSpeech = stopRecipeSpeech;

    function toggleRecipeSpeech() {
      if (!('speechSynthesis' in window)) {
        showToast('Voice narration is not supported in this browser.', 'warning', 'इस ब्राउज़र में ऑडियो आवाज़ समर्थित नहीं है।');
        return;
      }

      if (isRecipeSpeaking) {
        stopRecipeSpeech();
        return;
      }

      const recipe = latestRecipe;
      if (!recipe) return;

      const isHindi = currentLang === 'hi';
      const name = isHindi ? (recipe.recipe_name_hi || recipe.recipe_name) : recipe.recipe_name;
      const time = isHindi ? `${recipe.prep_time_minutes} मिनट` : `${recipe.prep_time_minutes} minutes`;
      
      const ingredients = isHindi && recipe.ingredients_used_hi && recipe.ingredients_used_hi.length > 0
        ? recipe.ingredients_used_hi.join(', ')
        : (recipe.ingredients_used || []).join(', ');

      const steps = isHindi && recipe.steps_hi && recipe.steps_hi.length > 0
        ? recipe.steps_hi.map((s, idx) => `कदम ${idx + 1}: ${s}`).join('. ')
        : (recipe.steps || []).map((s, idx) => `Step ${idx + 1}: ${s}`).join('. ');

      const tip = isHindi ? (recipe.chef_tip_hi || recipe.chef_tip || '') : (recipe.chef_tip || recipe.chef_tip_hi || '');

      let textToSpeak = '';
      if (isHindi) {
        textToSpeak = `${name} बनाने की विधि। पकाने का अनुमानित समय लगभग ${time} है। आवश्यक सामग्री: ${ingredients}। पकाने के निर्देश: ${steps}। शेफ़ का सीक्रेट नुस्खा: ${tip}।`;
      } else {
        textToSpeak = `Here is how to make ${name}. Estimated cooking time is ${time}. Main ingredients: ${ingredients}. Cooking instructions: ${steps}. Chef's secret tip: ${tip}.`;
      }

      window.speechSynthesis.cancel();
      const utter = new SpeechSynthesisUtterance(textToSpeak);
      utter.rate = 0.95;
      utter.lang = isHindi ? 'hi-IN' : 'en-US';

      // Pick localized voice if available
      try {
        const voices = window.speechSynthesis.getVoices();
        if (isHindi) {
          const hiVoice = voices.find(v => v.lang.startsWith('hi') || v.lang.includes('Hindi'));
          if (hiVoice) utter.voice = hiVoice;
        } else {
          const enVoice = voices.find(v => v.lang.startsWith('en-IN') || v.lang.startsWith('en-US') || v.lang.startsWith('en-GB'));
          if (enVoice) utter.voice = enVoice;
        }
      } catch (e) {}

      utter.onstart = () => updateSpeechButtonUI(true);
      utter.onend = () => updateSpeechButtonUI(false);
      utter.onerror = () => updateSpeechButtonUI(false);

      updateSpeechButtonUI(true);
      window.speechSynthesis.speak(utter);
    }
    window.toggleRecipeSpeech = toggleRecipeSpeech;

    // Quick Recipe Inspiration Chips click handler
    document.querySelectorAll('.recipe-quick-chip').forEach(chip => {
      chip.addEventListener('click', () => {
        const query = chip.getAttribute('data-query');
        const input = document.getElementById('rescue-items-input');
        if (input) input.value = query;
        stopRecipeSpeech();
        triggerRecipeGeneration([query]);
      });
    });

    document.getElementById('generate-recipe-btn').addEventListener('click', async () => {
      const customText = document.getElementById('rescue-items-input').value.trim();
      if (customText) {
        await checkAndCorrectRescueSpelling(customText);
      }
      const updatedText = document.getElementById('rescue-items-input').value.trim();
      let items = updatedText ? updatedText.split(',').map(s => s.trim()).filter(Boolean) : Array.from(selectedItemsForRescue);
      if (items.length === 0) {
        items = inventory.filter(i => i.days_left <= 2).map(i => i.name_en);
      }
      triggerRecipeGeneration(items);
    });

    document.getElementById('copy-recipe-btn').addEventListener('click', () => {
      const name = document.getElementById('recipe-name').textContent;
      const steps = Array.from(document.querySelectorAll('#recipe-steps-list li')).map(li => li.innerText).join('\n');
      const header = currentLang === 'hi' ? 'पकाने की विधि:' : 'Instructions:';
      navigator.clipboard.writeText(`${name}\n\n${header}\n${steps}`).then(() => {
        showToast(currentLang === 'hi' ? 'रेसिपी क्लिपबोर्ड में कॉपी की गई!' : 'Recipe copied to clipboard!', 'success');
      });
    });

    // ========================================================
    // FOOD SCANNER & AI PHOTO INSPECTOR
    // ========================================================

    let webcamStream = null;
    let currentWebcamFacing = 'environment'; // 'environment' (back) or 'user' (front)

    // Helper: compress & resize image to max dimension ~1024px for quick base64 AI scanning
    async function compressAndResizeImage(fileOrBlob, maxDim = 1024, quality = 0.85) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = (e) => {
          const img = new Image();
          img.onload = () => {
            let width = img.width;
            let height = img.height;
            if (width > maxDim || height > maxDim) {
              if (width > height) {
                height = Math.round((height * maxDim) / width);
                width = maxDim;
              } else {
                width = Math.round((width * maxDim) / height);
                height = maxDim;
              }
            }
            const canvas = document.createElement('canvas');
            canvas.width = width;
            canvas.height = height;
            const ctx = canvas.getContext('2d');
            ctx.drawImage(img, 0, 0, width, height);
            const dataUrl = canvas.toDataURL('image/jpeg', quality);
            resolve({ dataUrl, width, height });
          };
          img.onerror = reject;
          img.src = e.target.result;
        };
        reader.onerror = reject;
        reader.readAsDataURL(fileOrBlob);
      });
    }

    // Helper: Render standard food scanner results with 1-tap "Add to Fridge"
    function renderScannerResult(data, photoUrl = null, queryText = '') {
      const resBox = document.getElementById('scanner-result');
      resBox.classList.remove('hidden');

      // OBVIOUS SCANNER FAILURE / UNRECOGNIZED CARD
      if (!data.is_food || data.scanner_failed) {
        resBox.innerHTML = `
          <div class="p-6 rounded-3xl bg-amber-50/90 dark:bg-amber-950/40 border-2 border-amber-300 dark:border-amber-700/80 text-xs text-amber-900 dark:text-amber-100 space-y-4 shadow-md transition-all">
            <div class="flex items-start gap-3">
              <div class="w-10 h-10 rounded-2xl bg-amber-200 dark:bg-amber-900/80 text-amber-800 dark:text-amber-200 flex items-center justify-center shrink-0 shadow-sm">
                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>
              </div>
              <div class="flex-1 min-w-0">
                <h4 class="font-bold text-sm text-amber-900 dark:text-amber-200">${currentLang === 'hi' ? 'भोजन की पहचान नहीं हो सकी' : 'Could Not Identify Food in Photo'}</h4>
                <p class="mt-1 text-stone-600 dark:text-stone-300 text-xs leading-relaxed">
                  ${currentLang === 'hi' 
                    ? (data.message_hi || data.message || 'AI इस फ़ोटो में किसी खाद्य सामग्री को स्पष्ट रूप से नहीं पहचान सका, या AI विज़न मॉडल अस्थायी रूप से अनुपलब्ध हैं। कृपया साफ़ रोशनी में फ़ोटो लें या मैन्युअल रूप से आइटम दर्ज करें।') 
                    : (data.message || 'Gemini AI was unable to detect recognizable food in this photo, or all vision models were unreachable. Please retake the photo clearly or enter item details manually.')}
                </p>
              </div>
            </div>

            ${photoUrl ? `
              <div class="flex items-center gap-3 p-2.5 rounded-2xl bg-white/70 dark:bg-stone-900/60 border border-amber-200/60 dark:border-amber-800/40">
                <img src="${photoUrl}" class="w-14 h-14 rounded-xl object-cover border border-amber-300/40 shrink-0"/>
                <div class="text-[11px] text-stone-500 dark:text-stone-400">
                  <span class="font-semibold text-stone-700 dark:text-stone-300">${currentLang === 'hi' ? 'अपलोड की गई फ़ोटो' : 'Captured Photo'}</span><br/>
                  ${currentLang === 'hi' ? 'कोई फ़र्ज़ी सामान नहीं जोड़ा गया — कृपया मैन्युअल पुष्टि करें।' : 'Zero generic items were auto-added. Please enter details manually.'}
                </div>
              </div>
            ` : ''}

            <div class="flex flex-col sm:flex-row items-center gap-2 pt-1">
              <button type="button" onclick="openManualEntryModal()" class="w-full sm:flex-1 py-2.5 px-4 rounded-xl bg-gradient-to-r from-amber-500 to-brand-600 hover:from-amber-600 hover:to-brand-700 text-white font-bold text-xs shadow transition flex items-center justify-center gap-1.5">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/></svg>
                <span>${currentLang === 'hi' ? 'मैन्युअल रूप से नाम व तारीख जोड़ें' : 'Enter Item Manually'}</span>
              </button>
              <button type="button" onclick="openCommandPhotoCapture()" class="w-full sm:w-auto py-2.5 px-4 rounded-xl bg-stone-200 dark:bg-stone-800 hover:bg-stone-300 dark:hover:bg-stone-700 text-stone-800 dark:text-stone-200 font-semibold text-xs transition flex items-center justify-center gap-1.5">
                <svg class="w-4 h-4 text-stone-600 dark:text-stone-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 13a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
                <span>${currentLang === 'hi' ? 'दोबारा फ़ोटो लें' : 'Retake Photo'}</span>
              </button>
            </div>
          </div>
        `;
        return;
      }

      // MULTI-ITEM SCAN RESULTS SUMMARY (Auto-added fresh + Packaged modal prompt)
      if ((data.auto_added && data.auto_added.length > 0) || (data.needs_expiry && data.needs_expiry.length > 0)) {
        const autoAddedCount = data.auto_added ? data.auto_added.length : 0;
        const needsExpiryCount = data.needs_expiry ? data.needs_expiry.length : 0;
        const totalItemsCount = autoAddedCount + needsExpiryCount;

        resBox.innerHTML = `
          <div class="p-5 rounded-3xl bg-stone-50 dark:bg-stone-800/90 border border-stone-200 dark:border-stone-700 text-xs space-y-4 shadow-sm transition-all">
            ${photoUrl ? `
              <div class="flex items-center gap-3 pb-3 border-b border-stone-200 dark:border-stone-700/60">
                <div class="relative w-16 h-16 rounded-2xl overflow-hidden shrink-0 border-2 border-emerald-500/40 shadow-sm bg-black">
                  <img src="${photoUrl}" alt="Scanned Food Photo" class="w-full h-full object-cover"/>
                  <span class="absolute bottom-0 inset-x-0 bg-stone-900/80 text-white text-[8px] text-center font-bold py-0.5">PHOTO</span>
                </div>
                <div class="min-w-0 flex-1">
                  <div class="flex items-center gap-2 flex-wrap">
                    <span class="px-2 py-0.5 rounded-full bg-emerald-100 dark:bg-emerald-950 text-emerald-700 dark:text-emerald-300 font-bold text-[10px] tracking-wide flex items-center gap-1">
                      <svg class="w-3 h-3 text-emerald-600 dark:text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 13a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
                      <span>AI MULTI-FOOD SCAN</span>
                    </span>
                    <span class="text-[10px] text-stone-400">• ${data.analyzed_by || 'Gemini Vision AI'}</span>
                  </div>
                  <div class="text-[11px] text-stone-600 dark:text-stone-300 font-semibold mt-1">
                    AI identified ${totalItemsCount} food item${totalItemsCount > 1 ? 's' : ''} in photo
                  </div>
                </div>
              </div>
            ` : ''}

            ${autoAddedCount > 0 ? `
              <div class="p-3.5 rounded-2xl bg-emerald-50/80 dark:bg-emerald-950/40 border border-emerald-200/80 dark:border-emerald-800/60 space-y-2">
                <div class="flex items-center justify-between text-xs font-bold text-emerald-800 dark:text-emerald-200">
                  <div class="flex items-center gap-1.5">
                    <svg class="w-4 h-4 text-emerald-600 dark:text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                    <span>Automatically Added Fresh (No Expiry Needed)</span>
                  </div>
                  <span class="px-2 py-0.5 rounded-full bg-emerald-200/60 dark:bg-emerald-900/80 text-emerald-800 dark:text-emerald-200 text-[10px] font-bold">${autoAddedCount} added</span>
                </div>
                <div class="divide-y divide-emerald-100 dark:divide-emerald-900/40 text-[11px]">
                  ${data.auto_added.map(item => `
                    <div class="py-1.5 flex items-center justify-between">
                      <div class="flex items-center gap-2">
                        <span class="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>
                        <span class="font-semibold text-stone-800 dark:text-stone-200">${item.name_en}</span>
                        ${item.name_hi ? `<span class="text-[10px] text-brand-600 dark:text-brand-400">(${item.name_hi})</span>` : ''}
                      </div>
                      <span class="text-stone-500 dark:text-stone-400 text-[10px] font-medium">${item.days_left || item.shelf_life_days || 2}d estimated freshness</span>
                    </div>
                  `).join('')}
                </div>
              </div>
            ` : ''}

            ${needsExpiryCount > 0 ? `
              <div class="p-3.5 rounded-2xl bg-amber-50/80 dark:bg-amber-950/40 border border-amber-200/80 dark:border-amber-800/60 space-y-2">
                <div class="flex items-center justify-between text-xs font-bold text-amber-800 dark:text-amber-200">
                  <div class="flex items-center gap-1.5">
                    <svg class="w-4 h-4 text-amber-600 dark:text-amber-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                    <span>Packaged Items (Modal Prompt Opened)</span>
                  </div>
                  <span class="px-2 py-0.5 rounded-full bg-amber-200/60 dark:bg-amber-900/80 text-amber-800 dark:text-amber-200 text-[10px] font-bold">${needsExpiryCount} queued</span>
                </div>
                <div class="divide-y divide-amber-100 dark:divide-amber-900/40 text-[11px]">
                  ${data.needs_expiry.map(item => `
                    <div class="py-1.5 flex items-center justify-between">
                      <div class="flex items-center gap-2">
                        <span class="w-1.5 h-1.5 rounded-full bg-amber-500"></span>
                        <span class="font-semibold text-stone-800 dark:text-stone-200">${item.name_en}</span>
                        ${item.name_hi ? `<span class="text-[10px] text-brand-600 dark:text-brand-400">(${item.name_hi})</span>` : ''}
                      </div>
                      <span class="text-amber-700 dark:text-amber-300 text-[10px] font-medium">Printed expiry date required</span>
                    </div>
                  `).join('')}
                </div>
              </div>
            ` : ''}
          </div>
        `;
        return;
      }

      const displayNameEn = data.name_en || queryText || 'Fresh Food';
      const displayNameHi = data.name_hi || '';
      const displayCategory = data.category || 'cooked_dish';
      const shelfDays = data.shelf_life_days;

      const shelfLifeText = shelfDays 
        ? `<span class="text-emerald-600 dark:text-emerald-400 font-bold">${shelfDays} days</span> <span class="text-stone-400 text-[11px] font-normal">(Made / stored today)</span>`
        : `<span class="text-amber-600 dark:text-amber-400 font-bold">Package Expiry Date Required</span>`;

      const requiresExpiryBadge = data.requires_expiry_date
        ? `<span class="px-2 py-0.5 rounded-full bg-amber-100 dark:bg-amber-900/60 text-amber-700 dark:text-amber-300 font-bold text-[10px]">Yes (Packaged)</span>`
        : `<span class="px-2 py-0.5 rounded-full bg-emerald-100 dark:bg-emerald-900/60 text-emerald-700 dark:text-emerald-300 font-bold text-[10px]">No (Automatic Expiry)</span>`;

      // Safe JSON encoding for onclick attribute
      const itemPayload = {
        name_en: displayNameEn,
        name_hi: displayNameHi,
        category: displayCategory,
        shelf_life_days: shelfDays || 2,
        requires_expiry_date: !!data.requires_expiry_date,
        storage_tip: data.storage_tip || ''
      };
      const encodedPayload = encodeURIComponent(JSON.stringify(itemPayload));

      resBox.innerHTML = `
        <div class="p-5 rounded-3xl bg-stone-50 dark:bg-stone-800/90 border border-stone-200 dark:border-stone-700 text-xs space-y-4 shadow-sm transition-all">
          
          ${photoUrl ? `
            <div class="flex items-center gap-3 pb-3 border-b border-stone-200 dark:border-stone-700/60">
              <div class="relative w-16 h-16 rounded-2xl overflow-hidden shrink-0 border-2 border-emerald-500/40 shadow-sm bg-black">
                <img src="${photoUrl}" alt="Scanned Food Photo" class="w-full h-full object-cover"/>
                <span class="absolute bottom-0 inset-x-0 bg-stone-900/80 text-white text-[8px] text-center font-bold py-0.5">PHOTO</span>
              </div>
              <div class="min-w-0">
                <div class="flex items-center gap-2">
                  <span class="px-2 py-0.5 rounded-full bg-emerald-100 dark:bg-emerald-950 text-emerald-700 dark:text-emerald-300 font-bold text-[10px] tracking-wide flex items-center gap-1">
                    <svg class="w-3 h-3 text-emerald-600 dark:text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 13a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
                    <span>AI VISION SCANNED</span>
                  </span>
                  <span class="text-[10px] text-stone-400 truncate">• ${data.analyzed_by || 'Gemini Vision AI'}</span>
                </div>
                <div class="text-[11px] text-stone-500 dark:text-stone-400 mt-1">Identified dish & shelf-life from camera snapshot</div>
              </div>
            </div>
          ` : ''}

          <!-- Name & Category -->
          <div class="flex items-start justify-between gap-3">
            <div>
              <div class="font-bold text-base text-stone-900 dark:text-white flex items-center gap-2">
                <svg class="w-4 h-4 text-brand-600 dark:text-brand-400 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253"/></svg>
                <span>${displayNameEn}</span>
              </div>
              ${displayNameHi ? `<div class="text-brand-600 dark:text-brand-400 font-semibold text-xs mt-0.5 pl-6">${displayNameHi}</div>` : ''}
            </div>
            <span class="font-bold uppercase text-[10px] px-2.5 py-1 rounded-full bg-white dark:bg-stone-900 border border-stone-200 dark:border-stone-700 text-stone-700 dark:text-stone-300 tracking-wider shrink-0 shadow-2xs">
              ${displayCategory}
            </span>
          </div>

          <!-- Shelf Life & Package Expiry Requirement -->
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
            <div class="p-3 rounded-2xl bg-white dark:bg-stone-900 border border-stone-100 dark:border-stone-700/60 shadow-2xs">
              <div class="text-[10px] text-stone-400 uppercase tracking-wider font-semibold mb-1 flex items-center gap-1">
                <svg class="w-3 h-3 text-stone-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                <span>Estimated Shelf Life</span>
              </div>
              <div class="text-xs">${shelfLifeText}</div>
            </div>
            <div class="p-3 rounded-2xl bg-white dark:bg-stone-900 border border-stone-100 dark:border-stone-700/60 shadow-2xs">
              <div class="text-[10px] text-stone-400 uppercase tracking-wider font-semibold mb-1 flex items-center gap-1">
                <svg class="w-3 h-3 text-stone-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"/></svg>
                <span>Package Expiry Required</span>
              </div>
              <div class="text-xs">${requiresExpiryBadge}</div>
            </div>
          </div>

          <!-- Storage Tip -->
          <div class="p-3 rounded-2xl bg-white dark:bg-stone-900 border border-stone-100 dark:border-stone-700/60 shadow-2xs">
            <div class="text-[10px] text-stone-400 uppercase tracking-wider font-semibold mb-1 flex items-center gap-1">
              <svg class="w-3 h-3 text-amber-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"/></svg>
              <span>Storage Advice</span>
            </div>
            <div class="text-stone-700 dark:text-stone-300 text-xs leading-relaxed">${data.storage_tip || 'Keep refrigerated in an airtight container.'}</div>
          </div>

          <!-- Action Button: 1-Tap Add to Fridge -->
          <div class="pt-1 flex items-center justify-between gap-3 flex-wrap">
            <button 
              type="button" 
              onclick="addScannedItemDirectly('${encodedPayload}', this)"
              class="w-full sm:w-auto flex-1 inline-flex items-center justify-center gap-2 px-5 py-2.5 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white font-bold text-xs shadow-sm hover:shadow transition active:scale-95"
            >
              <svg class="w-3.5 h-3.5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/></svg>
              <span>Add to Fridge Inventory</span>
            </button>
            <span class="text-[10px] text-stone-400 text-right w-full sm:w-auto">
              Added today • Non-negative accurate expiry
            </span>
          </div>
        </div>
      `;
    }

    // Direct 1-tap add from scanner to active fridge
    window.addScannedItemDirectly = async function(encodedPayload, btnElem) {
      try {
        const item = JSON.parse(decodeURIComponent(encodedPayload));
        if (btnElem) {
          btnElem.disabled = true;
          btnElem.innerHTML = '<svg class="w-3.5 h-3.5 animate-spin inline-block mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg><span>Adding to Fridge...</span>';
        }

        const fid = currentFridge || 'test';
        const activeTz = getEffectiveTimezone();
        const url = `/api/inventory?fridge=${encodeURIComponent(fid)}`;

        const res = await fetch(url, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Client-Timezone': activeTz
          },
          body: JSON.stringify({
            name_en: item.name_en,
            name_hi: item.name_hi || '',
            category: item.category || 'cooked_dish',
            shelf_life_days: item.shelf_life_days || 2,
            requires_expiry_date: item.requires_expiry_date || false,
            storage_tip: item.storage_tip || ''
          })
        });

        if (!res.ok) {
          throw new Error('Failed to save item');
        }

        const saved = await res.json();
        if (btnElem) {
          btnElem.className = 'w-full sm:w-auto flex-1 inline-flex items-center justify-center gap-2 px-5 py-2.5 rounded-xl bg-stone-200 dark:bg-stone-700 text-stone-800 dark:text-stone-200 font-bold text-xs cursor-default';
          btnElem.innerHTML = `<svg class="w-3.5 h-3.5 text-emerald-600 dark:text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg><span>Added! (${saved.days_left}d left until ${saved.expiry_date})</span>`;
        }

        await loadInventory(true);
        showToast(`Added ${saved.name_en} to fridge!`, 'success');
      } catch (err) {
        console.error('Error adding scanned item:', err);
        if (btnElem) {
          btnElem.disabled = false;
          btnElem.innerHTML = '<svg class="w-3.5 h-3.5 text-rose-500 inline-block mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg><span>Failed to Add. Retry?</span>';
        }
        showToast('Failed to save to fridge.', 'error');
      }
    };

    // Core Photo Scanner Function (Works from Command Bar and Food Scanner Tab)
    async function scanFoodPhoto(fileOrBlob) {
      if (!fileOrBlob) return;

      const resBox = document.getElementById('scanner-result');
      if (resBox) resBox.classList.remove('hidden');

      const invStatus = document.getElementById('inv-photo-status');
      if (invStatus) invStatus.classList.remove('hidden');

      // Immediate visual feedback with photo thumbnail + laser scanning animation
      const objectUrl = URL.createObjectURL(fileOrBlob);

      const scanningHtml = `
        <div class="p-4 sm:p-5 rounded-3xl bg-stone-50 dark:bg-stone-800/90 border border-stone-200 dark:border-stone-700 text-xs space-y-4 shadow-sm animate-pulse-subtle">
          <div class="relative w-full max-w-xs mx-auto rounded-2xl overflow-hidden aspect-video bg-black flex items-center justify-center border border-stone-800 shadow-md">
            <img src="${objectUrl}" alt="Scanning..." class="w-full h-full object-cover opacity-80"/>
            <div class="scanner-laser-bar"></div>
            <div class="absolute inset-0 bg-gradient-to-t from-black/60 via-transparent to-transparent"></div>
            <span class="absolute bottom-2 left-2 px-2 py-0.5 rounded-full bg-emerald-500 text-white font-bold text-[9px] tracking-wider uppercase animate-pulse">
              AI SCANNING...
            </span>
          </div>
          <div class="text-center space-y-1">
            <div class="text-xs font-bold text-stone-800 dark:text-stone-200 flex items-center justify-center gap-2">
              <svg class="w-4 h-4 text-emerald-600 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>
              <span>${currentLang === 'hi' ? 'Gemini AI भोजन की फोटो का विश्लेषण कर रहा है...' : 'Gemini AI Vision Analyzing Food Photo...'}</span>
            </div>
            <p class="text-[11px] text-stone-500 dark:text-stone-400">${currentLang === 'hi' ? 'सामान का नाम, शेल्फ लाइफ व ताज़गी की जाँच करके फ्रिज में जोड़ा जा रहा है' : 'Detecting dish name, culinary category, freshness & accurate shelf life'}</p>
          </div>
        </div>
      `;

      if (resBox) resBox.innerHTML = scanningHtml;
      if (invStatus) {
        invStatus.innerHTML = `
          <div class="flex flex-col sm:flex-row items-center gap-3.5 text-xs">
            <div class="relative w-28 h-20 rounded-xl overflow-hidden bg-black shrink-0 border border-emerald-500/50 shadow-sm">
              <img src="${objectUrl}" class="w-full h-full object-cover opacity-80" />
              <div class="scanner-laser-bar"></div>
            </div>
            <div class="flex-grow space-y-1 text-center sm:text-left">
              <div class="font-bold text-stone-900 dark:text-stone-100 flex items-center justify-center sm:justify-start gap-2">
                <svg class="w-4 h-4 text-emerald-600 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>
                <span>${currentLang === 'hi' ? 'Gemini AI भोजन की फोटो का विश्लेषण कर रहा है...' : 'Gemini AI Vision Analyzing Food Photo...'}</span>
              </div>
              <p class="text-stone-500 dark:text-stone-400 text-[11px]">${currentLang === 'hi' ? 'ताज़ी सामग्री अपने-आप फ्रिज इन्वेंटरी में जोड़ी जा रही है...' : 'Detecting food items & automatically adding to FridgeInventory...'}</p>
            </div>
          </div>
        `;
      }

      try {
        const { dataUrl } = await compressAndResizeImage(fileOrBlob, 1024, 0.85);

        const activeTz = getEffectiveTimezone();
        const fid = currentFridge || '';

        const res = await fetch('/api/scanner/photo', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 
            'Content-Type': 'application/json',
            'X-Client-Timezone': activeTz
          },
          body: JSON.stringify({
            image: dataUrl,
            mime_type: 'image/jpeg',
            fridge_id: fid,
            timezone: activeTz
          })
        });

        if (!res.ok) {
          throw new Error(`HTTP error ${res.status}`);
        }

        const data = await res.json();
        renderScannerResult(data, dataUrl);

        // Make scanner failure obvious: never auto-add fake food or pretend success
        if (!data.is_food || data.scanner_failed) {
          if (invStatus) {
            invStatus.innerHTML = `
              <div class="flex items-center justify-between gap-3 text-xs p-3 rounded-2xl bg-amber-50 dark:bg-amber-950/60 border border-amber-300 dark:border-amber-700">
                <div class="flex items-center gap-2">
                  <span class="text-base">⚠️</span>
                  <div>
                    <span class="font-bold text-amber-900 dark:text-amber-200">${currentLang === 'hi' ? 'भोजन नहीं पहचाना जा सका' : 'Food Not Recognized'}</span>
                    <div class="text-[11px] text-stone-600 dark:text-stone-300">${currentLang === 'hi' ? 'मैन्युअल रूप से नाम दर्ज करें या दोबारा फ़ोटो लें' : 'Enter details manually or retake photo'}</div>
                  </div>
                </div>
                <div class="flex items-center gap-1.5 shrink-0">
                  <button type="button" onclick="openManualEntryModal()" class="px-3 py-1.5 rounded-xl bg-amber-600 hover:bg-amber-700 text-white font-bold text-xs shadow-xs transition">${currentLang === 'hi' ? 'मैन्युअल जोड़ें' : 'Enter Manually'}</button>
                  <button type="button" onclick="openCommandPhotoCapture()" class="px-2.5 py-1.5 rounded-xl bg-stone-200 dark:bg-stone-700 text-stone-700 dark:text-stone-200 font-semibold text-xs transition">${currentLang === 'hi' ? 'फ़ोटो लें' : 'Retake'}</button>
                </div>
              </div>
            `;
          }
          showToast('Could not identify food. Please enter manually or retake photo.', 'warning', 'भोजन की पहचान नहीं हो सकी — कृपया मैन्युअल रूप से दर्ज करें');
          return;
        }

        // 1. Fresh items without printed expiry date were automatically persisted to DB and returned in auto_added
        if (data.auto_added && data.auto_added.length > 0) {
          data.auto_added.forEach(item => inventory.unshift(item));
          try { localStorage.setItem('fi_cached_inventory', JSON.stringify(inventory)); } catch(e){}
          renderInventory();
          updateStats();
          checkUrgentAndNotify();
          const freshNames = data.auto_added.map(i => currentLang === 'hi' ? (i.name_hi || i.name_en) : i.name_en).join(', ');
          showToast(currentLang === 'hi' ? `फ्रिज में जोड़ा गया: ${freshNames}` : `Auto-added fresh item(s) to fridge: ${freshNames}`, 'success');

          if (invStatus) {
            invStatus.innerHTML = `
              <div class="flex items-center justify-between gap-3 text-xs">
                <div class="flex items-center gap-2.5">
                  <div class="w-7 h-7 rounded-full bg-emerald-100 dark:bg-emerald-900/60 text-emerald-600 dark:text-emerald-300 flex items-center justify-center shrink-0">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
                  </div>
                  <div>
                    <span class="font-bold text-emerald-700 dark:text-emerald-300">${currentLang === 'hi' ? 'फ्रिज में जोड़ा गया:' : 'Added to FridgeInventory:'}</span>
                    <span class="text-stone-700 dark:text-stone-300 ml-1 font-medium">${freshNames}</span>
                  </div>
                </div>
                <button type="button" onclick="document.getElementById('inv-photo-status').classList.add('hidden')" class="text-stone-400 hover:text-stone-600 dark:hover:text-stone-200 text-xs">✕</button>
              </div>
            `;
            setTimeout(() => { if (invStatus) invStatus.classList.add('hidden'); }, 6000);
          }
        }

        // 2. Packaged items or items requiring printed expiry date open modal sequentially one-by-one
        const needsExpiry = data.needs_expiry || (data.requires_expiry_date ? [data] : []);
        if (needsExpiry.length > 0) {
          modalQueue = [...needsExpiry];
          modalQueueTotal = needsExpiry.length;
          modalQueueIndex = 1;
          const firstItem = modalQueue.shift();
          openModalWithClassification(firstItem, 1, modalQueueTotal);
          if (data.auto_added && data.auto_added.length > 0) {
            showToast(`Setting printed expiry date for ${firstItem.name_en}...`, 'info');
          } else {
            showToast(`Please enter printed expiry date for ${firstItem.name_en}`, 'info');
          }
        } else if (data.auto_added && data.auto_added.length > 0) {
          showToast(currentLang === 'hi' ? `सभी ${data.auto_added.length} सामान सुरक्षित रूप से जोड़े गए!` : `All ${data.auto_added.length} item(s) automatically added with computed shelf life!`, 'success');
        }
      } catch (err) {
        console.error('Photo scan error:', err);
        const errorHtml = `
          <div class="p-5 rounded-2xl bg-rose-50 dark:bg-rose-950/40 border border-rose-200 dark:border-rose-800 text-xs text-rose-800 dark:text-rose-200 space-y-3">
            <div class="font-bold flex items-center gap-2">
              <svg class="w-4 h-4 text-rose-600" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>
              <span>AI Photo Inspection Failed</span>
            </div>
            <p>Could not analyze the photo. Please ensure a clear photo of food and try again.</p>
            <button 
              type="button" 
              onclick="openCommandPhotoCapture()" 
              class="px-4 py-1.5 rounded-xl bg-rose-600 hover:bg-rose-700 text-white font-semibold text-xs transition"
            >
              Retry Camera Snap
            </button>
          </div>
        `;
        if (resBox) resBox.innerHTML = errorHtml;
        if (invStatus) invStatus.innerHTML = errorHtml;
      }
    }

    // WEBCAM LIVE STREAM CONTROLS
    async function startWebcamStream(facingMode = 'environment') {
      const modal = document.getElementById('scanner-webcam-modal');
      const video = document.getElementById('webcam-video-stream');
      modal.classList.remove('hidden');

      if (webcamStream) {
        stopWebcamStream();
      }

      currentWebcamFacing = facingMode;

      try {
        webcamStream = await navigator.mediaDevices.getUserMedia({
          video: {
            facingMode: { ideal: facingMode },
            width: { ideal: 1280 },
            height: { ideal: 720 }
          },
          audio: false
        });
        video.srcObject = webcamStream;
      } catch (err) {
        console.warn('Webcam stream failed or permission denied:', err);
        stopWebcamStream();
        modal.classList.add('hidden');
        // Fall back to native camera / file input
        showToast('Camera access unavailable. Opening device photo picker...', 'info');
        document.getElementById('scanner-camera-input').click();
      }
    }

    function stopWebcamStream() {
      const modal = document.getElementById('scanner-webcam-modal');
      const video = document.getElementById('webcam-video-stream');
      if (webcamStream) {
        webcamStream.getTracks().forEach(track => track.stop());
        webcamStream = null;
      }
      if (video) video.srcObject = null;
      if (modal) modal.classList.add('hidden');
    }

    // Take Photo Bubble Click Handler
    document.getElementById('scanner-photo-bubble-btn').addEventListener('click', () => {
      const isMobile = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent) || ('ontouchstart' in window);
      if (isMobile) {
        // Direct mobile camera launch
        document.getElementById('scanner-camera-input').click();
      } else {
        // On desktop, open live camera viewfinder
        startWebcamStream('environment');
      }
    });

    // Secondary buttons
    document.getElementById('scanner-open-webcam-btn').addEventListener('click', () => {
      startWebcamStream('environment');
    });

    document.getElementById('scanner-upload-file-btn').addEventListener('click', () => {
      document.getElementById('scanner-gallery-input').click();
    });

    document.getElementById('scanner-inline-camera-icon').addEventListener('click', () => {
      document.getElementById('scanner-camera-input').click();
    });

    // File Input change listeners
    document.getElementById('scanner-camera-input').addEventListener('change', (e) => {
      if (e.target.files && e.target.files[0]) {
        scanFoodPhoto(e.target.files[0]);
        e.target.value = '';
      }
    });

    document.getElementById('scanner-gallery-input').addEventListener('change', (e) => {
      if (e.target.files && e.target.files[0]) {
        scanFoodPhoto(e.target.files[0]);
        e.target.value = '';
      }
    });

    // Webcam Modal buttons
    document.getElementById('close-webcam-modal-btn').addEventListener('click', stopWebcamStream);

    document.getElementById('webcam-choose-file-btn').addEventListener('click', () => {
      stopWebcamStream();
      document.getElementById('scanner-gallery-input').click();
    });

    document.getElementById('webcam-switch-cam-btn').addEventListener('click', () => {
      const newFacing = currentWebcamFacing === 'environment' ? 'user' : 'environment';
      startWebcamStream(newFacing);
    });

    document.getElementById('webcam-snap-btn').addEventListener('click', () => {
      const video = document.getElementById('webcam-video-stream');
      if (!video || !video.videoWidth) {
        showToast('Camera not ready. Please wait a moment.', 'warning');
        return;
      }

      const canvas = document.createElement('canvas');
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      const ctx = canvas.getContext('2d');
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

      canvas.toBlob((blob) => {
        stopWebcamStream();
        if (blob) {
          scanFoodPhoto(blob);
        }
      }, 'image/jpeg', 0.9);
    });

    // Standalone Text Scanner Inspect Button
    document.getElementById('scanner-btn').addEventListener('click', async () => {
      const text = document.getElementById('scanner-input').value.trim();
      if (!text) return;

      const resBox = document.getElementById('scanner-result');
      resBox.classList.remove('hidden');
      resBox.innerHTML = '<div class="text-xs text-stone-500 animate-pulse">Inspecting food data...</div>';

      try {
        const res = await fetch('/classify', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ item: text })
        });
        const data = await res.json();
        renderScannerResult(data, null, text);
      } catch (err) {
        resBox.innerHTML = '<div class="text-xs text-rose-500">Failed to inspect item.</div>';
      }
    });

    // ========================================================
    // OFFLINE WEB PUSH NOTIFICATION SYSTEM (SERVICE WORKER + PUSH API)
    // ========================================================

    function updatePushUI(isSubscribed) {
      const headerBadge = document.getElementById('push-status-badge');
      const navBadge = document.getElementById('push-nav-badge');
      const heroBadge = document.getElementById('push-hero-badge');
      const toggleBtn = document.getElementById('push-main-toggle-btn');
      const toggleText = document.getElementById('push-main-toggle-text');

      if (isSubscribed) {
        if (headerBadge) {
          headerBadge.textContent = currentLang === 'hi' ? 'चालू' : 'ON';
          headerBadge.className = 'font-bold text-[10px] px-1.5 py-0.5 rounded-full bg-emerald-100 dark:bg-emerald-950 text-emerald-700 dark:text-emerald-300';
        }
        if (navBadge) {
          navBadge.textContent = currentLang === 'hi' ? 'चालू' : 'ON';
          navBadge.className = 'text-[10px] px-2 py-0.5 rounded-full bg-emerald-100 dark:bg-emerald-950 text-emerald-700 dark:text-emerald-300 font-bold';
        }
        if (heroBadge) {
          heroBadge.textContent = currentLang === 'hi' ? 'सक्रिय' : 'ACTIVE';
          heroBadge.className = 'text-xs font-bold px-2.5 py-0.5 rounded-full bg-emerald-100 dark:bg-emerald-950 text-emerald-700 dark:text-emerald-300';
        }
        if (toggleText) toggleText.textContent = currentLang === 'hi' ? 'अलर्ट बंद करें' : 'Disable Offline Expiry Alerts';
        if (toggleBtn) toggleBtn.className = 'px-6 py-3 rounded-2xl bg-stone-200 dark:bg-stone-800 hover:bg-stone-300 dark:hover:bg-stone-700 text-stone-800 dark:text-stone-200 font-bold text-sm transition-all flex items-center justify-center gap-2 shrink-0';
      } else {
        if (headerBadge) {
          headerBadge.textContent = currentLang === 'hi' ? 'बंद' : 'OFF';
          headerBadge.className = 'font-bold text-[10px] px-1.5 py-0.5 rounded-full bg-stone-200 dark:bg-stone-700 text-stone-600 dark:text-stone-300';
        }
        if (navBadge) {
          navBadge.textContent = currentLang === 'hi' ? 'बंद' : 'OFF';
          navBadge.className = 'text-[10px] px-2 py-0.5 rounded-full bg-stone-100 dark:bg-stone-800 text-stone-600 dark:text-stone-400 font-bold';
        }
        if (heroBadge) {
          heroBadge.textContent = currentLang === 'hi' ? 'बंद' : 'OFF';
          heroBadge.className = 'text-xs font-bold px-2.5 py-0.5 rounded-full bg-stone-100 dark:bg-stone-800 text-stone-500';
        }
        if (toggleText) toggleText.textContent = currentLang === 'hi' ? 'ऑफ़लाइन एक्सपायरी अलर्ट चालू करें' : 'Enable Offline Expiry Alerts';
        if (toggleBtn) toggleBtn.className = 'px-6 py-3 rounded-2xl bg-brand-600 hover:bg-brand-700 text-white font-bold text-sm shadow-md transition-all flex items-center justify-center gap-2 shrink-0';
      }
    }

    async function initPushNotifications() {
      if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
        console.log('Web Push API or Service Workers not supported in this browser.');
        const swDiag = document.getElementById('diag-sw-status');
        if (swDiag) swDiag.textContent = 'Not Supported';
        return;
      }

      try {
        swRegistration = await navigator.serviceWorker.register('/sw.js', { scope: '/' });
        console.log('Service Worker registered successfully with scope:', swRegistration.scope);
        
        const swDiag = document.getElementById('diag-sw-status');
        if (swDiag) swDiag.textContent = 'Active (/sw.js)';

        // Inspect existing push subscription
        const subscription = await swRegistration.pushManager.getSubscription();
        pushSubscription = subscription;
        updatePushUI(!!subscription);

        if (subscription) {
          const endpointEl = document.getElementById('diag-endpoint');
          if (endpointEl) endpointEl.textContent = subscription.endpoint.slice(0, 35) + '...';
        }

        // Fetch & display VAPID key
        try {
          const vapidRes = await fetch('/api/push/vapid-public-key');
          if (vapidRes.ok) {
            const { publicKey } = await vapidRes.json();
            const vapidEl = document.getElementById('diag-vapid-key');
            if (vapidEl) vapidEl.textContent = publicKey.slice(0, 16) + '...';
          }
        } catch(e){}
      } catch (err) {
        console.error('Service Worker registration failed:', err);
      }
    }

    async function subscribePush() {
      if (!swRegistration) {
        showToast(currentLang === 'hi' ? 'सर्विस वर्कर लोड नहीं हुआ' : 'Service Worker not ready', 'error');
        return;
      }

      try {
        // 1. Get VAPID public key from backend
        const vapidRes = await fetch('/api/push/vapid-public-key');
        if (!vapidRes.ok) {
          const err = await vapidRes.json();
          showToast(err.detail || 'Push not configured', 'error');
          return;
        }
        const { publicKey } = await vapidRes.json();

        // 2. Request native browser notification permission & subscribe via PushManager
        const subscription = await swRegistration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(publicKey),
        });

        // 3. Send subscription object to backend to tie with current Fridge ID
        const res = await fetch('/api/push/subscribe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ subscription: subscription.toJSON() }),
        });

        if (!res.ok) {
          const err = await res.json();
          throw new Error(err.detail || 'Subscription failed');
        }

        pushSubscription = subscription;
        updatePushUI(true);
        const endpointEl = document.getElementById('diag-endpoint');
        if (endpointEl) endpointEl.textContent = subscription.endpoint.slice(0, 35) + '...';

        showToast(currentLang === 'hi' ? 'ऑफ़लाइन पुश अलर्ट सक्रिय!' : 'Offline push alerts enabled!', 'success');
      } catch (err) {
        console.error('Push subscribe error:', err);
        if (err.name === 'NotAllowedError' || (window.Notification && Notification.permission === 'denied')) {
          showToast(currentLang === 'hi' ? 'नोटिफिकेशन ब्लॉक हैं! नीचे गूगल गाइड पर क्लिक करें।' : 'Notifications blocked! See Google Chrome guide below.', 'warning');
          const guide = document.getElementById('push-chrome-guide-card');
          if (guide) {
            guide.scrollIntoView({ behavior: 'smooth', block: 'center' });
            guide.classList.add('ring-4', 'ring-amber-500');
            setTimeout(() => guide.classList.remove('ring-4', 'ring-amber-500'), 3500);
          }
        } else {
          showToast(err.message || 'Failed to enable notifications', 'error');
        }
      }
    }

    async function unsubscribePush() {
      if (!pushSubscription) return;

      try {
        const subJSON = pushSubscription.toJSON();
        await pushSubscription.unsubscribe();

        await fetch('/api/push/unsubscribe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ subscription: subJSON }),
        });

        pushSubscription = null;
        updatePushUI(false);
        const endpointEl = document.getElementById('diag-endpoint');
        if (endpointEl) endpointEl.textContent = 'Unsubscribed';

        showToast(currentLang === 'hi' ? 'पुश नोटिफिकेशन बंद' : 'Push alerts disabled', 'info');
      } catch (err) {
        showToast('Failed to unsubscribe', 'error');
      }
    }

    // Push Toggle Buttons
    document.getElementById('push-main-toggle-btn').addEventListener('click', () => {
      if (pushSubscription) {
        unsubscribePush();
      } else {
        subscribePush();
      }
    });

    // Test Push Button
    document.getElementById('push-tab-test-btn').addEventListener('click', async () => {
      try {
        const res = await fetch('/api/push/test', { method: 'POST' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Test failed');
        showToast(currentLang === 'hi' ? 'टेस्ट नोटिफिकेशन भेजा गया!' : `Test notification sent to ${data.sent} device(s)!`, 'success');
      } catch (err) {
        showToast(err.message, 'error');
      }
    });

    // Offline Service Worker Direct Notification Test
    document.getElementById('push-tab-offline-test-btn').addEventListener('click', triggerLocalOfflineTest);

    // Check Expiry & Broadcast Button (24h window)
    document.getElementById('push-tab-expiry-btn').addEventListener('click', async () => {
      try {
        const res = await fetch('/api/push/check-expiry', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Client-Timezone': getEffectiveTimezone(),
          }
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Check failed');
        if (data.alerts_sent > 0) {
          showToast(currentLang === 'hi' ? `${data.expiring_count} जल्द समाप्त, ${data.expired_count} समाप्त — अलर्ट भेजा!` : `${data.expiring_count} expiring, ${data.expired_count} expired — alerts sent!`, 'success');
        } else {
          showToast(currentLang === 'hi' ? 'कोई सामान 24 घंटे में समाप्त नहीं हो रहा' : data.message || 'No items expiring in 24h window', 'info');
        }
      } catch (err) {
        showToast(err.message, 'error');
      }
    });

    // Multi-Country Timezone Selector
    function initTimezoneSelector() {
      const select = document.getElementById('user-tz-select');
      if (!select) return;
      select.innerHTML = '';
      GLOBAL_TIMEZONES.forEach(tz => {
        const opt = document.createElement('option');
        opt.value = tz.id;
        opt.textContent = tz.name;
        if (tz.id === userTimezone) opt.selected = true;
        select.appendChild(opt);
      });
      select.addEventListener('change', (e) => {
        userTimezone = e.target.value;
        try { localStorage.setItem('fi_user_tz', userTimezone); } catch(e){}
        updateLiveClock();
        if (currentFridge) {
          loadInventory(true);
        }
        showToast(currentLang === 'hi' ? `टाइमज़ोन बदला गया: ${getEffectiveTimezone()}` : `Timezone updated: ${getEffectiveTimezone()}`, 'info');
      });
    }

    // Real-Time Live Clock Ticker (Ticking every second without refresh)
    function updateLiveClock() {
      const activeTz = getEffectiveTimezone();
      const now = new Date();
      let timeStr = '';
      let fullStr = '';
      let tzAbbr = '';

      try {
        timeStr = new Intl.DateTimeFormat('en-US', {
          timeZone: activeTz,
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
          hour12: true
        }).format(now);

        fullStr = new Intl.DateTimeFormat('en-US', {
          timeZone: activeTz,
          year: 'numeric',
          month: 'short',
          day: 'numeric',
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
          hour12: true,
          timeZoneName: 'short'
        }).format(now);

        const parts = new Intl.DateTimeFormat('en-US', {
          timeZone: activeTz,
          timeZoneName: 'short'
        }).formatToParts(now);
        const tzPart = parts.find(p => p.type === 'timeZoneName');
        tzAbbr = tzPart ? tzPart.value : (activeTz.split('/').pop().replace('_', ' '));
      } catch (e) {
        timeStr = now.toLocaleTimeString();
        fullStr = now.toLocaleString();
        tzAbbr = 'Local';
      }

      const statusEl = document.getElementById('header-sync-status');
      if (statusEl) {
        statusEl.textContent = (currentLang === 'hi' ? 'लाइव • ' : 'Live • ') + `${timeStr} ${tzAbbr}`;
      }

      const syncTsEl = document.getElementById('sync-timestamp-display');
      if (syncTsEl) {
        syncTsEl.textContent = fullStr;
      }

      const engineHeadingEl = document.getElementById('sync-engine-heading');
      if (engineHeadingEl) {
        const countryOrCity = activeTz === 'auto' ? 'Auto Local' : activeTz.split('/').pop().replace('_', ' ');
        engineHeadingEl.textContent = (currentLang === 'hi' ? 'लाइव ताजगी ट्रैकर' : 'Live Freshness Engine') + ` (${countryOrCity} • ${tzAbbr}):`;
      }
    }

    // Automatic Urgent Notification Engine: Automatically sends notification when any item hits urgent category (<= 2 days or expired)
    function checkUrgentAndNotify() {
      if (!inventory || inventory.length === 0) return;

      // Urgent category: days_left <= 2 (expiring within 48h or already expired)
      const urgentItems = inventory.filter(i => typeof i.days_left === 'number' && i.days_left <= 2);
      if (urgentItems.length === 0) return;

      const cacheKey = 'fi_urgent_notified_v4';
      let notifiedCache = {};
      try {
        notifiedCache = JSON.parse(localStorage.getItem(cacheKey) || '{}');
      } catch (e) {}

      const now = Date.now();
      // Purge entries older than 8 hours
      for (const k in notifiedCache) {
        if (now - (notifiedCache[k]?.time || 0) > 8 * 3600 * 1000) {
          delete notifiedCache[k];
        }
      }

      // Identify newly urgent items or items transitioning to a new day state
      const newlyUrgent = urgentItems.filter(item => {
        const stateKey = `${item.id}_${item.days_left}`;
        return !notifiedCache[stateKey];
      });

      if (newlyUrgent.length === 0) return;

      // Record state in cache
      newlyUrgent.forEach(item => {
        const stateKey = `${item.id}_${item.days_left}`;
        notifiedCache[stateKey] = { time: now, name: item.name_en, days_left: item.days_left };
      });
      try { localStorage.setItem(cacheKey, JSON.stringify(notifiedCache)); } catch (e) {}

      const count = newlyUrgent.length;
      const names = newlyUrgent.map(i => currentLang === 'hi' ? (i.name_hi || i.name_en) : i.name_en).slice(0, 3).join(', ');
      const suffix = newlyUrgent.length > 3 ? ` +${newlyUrgent.length - 3} more` : '';

      const title = currentLang === 'hi'
        ? `🚨 [अर्जेंट खाद्य चेतावनी] ${count} सामान जल्द समाप्त हो रहे हैं!`
        : `🚨 [Urgent Food Alert] ${count} item${count > 1 ? 's' : ''} expiring soon!`;

      const body = currentLang === 'hi'
        ? `${names}${suffix} अर्जेंट श्रेणी में आ गए हैं! कृपया खराब होने से पहले उपयोग करें या रेस्क्यू शेफ़ रेसिपी बनाएं।`
        : `${names}${suffix} hit the urgent category! Consume or rescue them before they spoil.`;

      // 1. Direct Web Browser OS Notification
      if ('Notification' in window) {
        if (Notification.permission === 'granted') {
          try {
            if (swRegistration && swRegistration.showNotification) {
              swRegistration.showNotification(title, {
                body,
                icon: '/static/icons/icon-192.png',
                badge: '/static/icons/icon-192.png',
                tag: 'urgent-expiry-' + Date.now(),
                data: { url: '/#urgent' },
                vibrate: [200, 100, 200],
                requireInteraction: true
              });
            } else {
              new Notification(title, {
                body,
                icon: '/static/icons/icon-192.png',
                tag: 'urgent-expiry-' + Date.now(),
                requireInteraction: true
              });
            }
          } catch (err) {
            console.warn('Native notification failed:', err);
          }
        } else if (Notification.permission === 'default') {
          Notification.requestPermission().then(perm => {
            if (perm === 'granted') {
              try {
                new Notification(title, { body, icon: '/static/icons/icon-192.png' });
              } catch(e){}
            }
          }).catch(()=>{});
        }
      }

      // 2. Direct Service Worker PostMessage
      if (swRegistration && swRegistration.active) {
        try {
          swRegistration.active.postMessage({
            type: 'SHOW_OFFLINE_NOTIFICATION',
            title,
            body,
            tag: 'urgent-expiry-alert',
            url: '/'
          });
        } catch(e){}
      }

      // 3. Trigger Backend Push Alert (if online and logged in)
      if (navigator.onLine && currentFridge) {
        fetch('/api/push/check-expiry', {
          method: 'POST',
          headers: { 'X-Client-Timezone': getEffectiveTimezone() }
        }).catch(()=>{});
      }

      // 4. In-App Alert Toast
      showToast(`${title} ${body}`, 'warning', 8000);
    }

    const checkLocalOfflineExpiryAlerts = checkUrgentAndNotify;

    async function triggerLocalOfflineTest() {
      if (!swRegistration || !swRegistration.active) {
        showToast(currentLang === 'hi' ? 'सर्विस वर्कर सक्रिय नहीं है' : 'Service Worker not active yet', 'error');
        return;
      }
      swRegistration.active.postMessage({
        type: 'SHOW_OFFLINE_NOTIFICATION',
        title: currentLang === 'hi' ? '[टेस्ट] ऑफ़लाइन नोटिफिकेशन' : '[Test] Native Offline Notification',
        body: currentLang === 'hi' ? 'यह नोटिफिकेशन बिना किसी इंटरनेट कनेक्शन के सर्विस वर्कर द्वारा भेजा गया है!' : 'Delivered directly by Service Worker without requiring active internet!',
        tag: 'offline-test-notification',
        url: '/'
      });
      showToast(currentLang === 'hi' ? 'ऑफ़लाइन नोटिफिकेशन भेजा गया!' : 'Offline notification triggered locally via Service Worker!', 'success');
    }

    // Offline / Online Connection Detection
    function initOfflineNetworkListeners() {
      function updateOnlineStatus() {
        const offlineBadge = document.getElementById('header-offline-badge');
        if (offlineBadge) {
          if (!navigator.onLine) {
            offlineBadge.classList.remove('hidden');
            offlineBadge.classList.add('flex');
            showToast(currentLang === 'hi' ? 'इंटरनेट डिस्कनेक्ट हो गया — ऑफ़लाइन मोड चालू' : 'You are offline — local storage & alerts active', 'warning');
          } else {
            offlineBadge.classList.add('hidden');
            offlineBadge.classList.remove('flex');
          }
        }
      }

      window.addEventListener('online', () => {
        updateOnlineStatus();
        showToast(currentLang === 'hi' ? 'इंटरनेट फिर से कनेक्ट हुआ — डेटा सिंक हो रहा है' : 'Internet reconnected — syncing fridge data', 'success');
        if (currentFridge) loadInventory(true);
      });

      window.addEventListener('offline', () => {
        updateOnlineStatus();
        checkLocalOfflineExpiryAlerts();
      });

      updateOnlineStatus();
    }

    // Initialize on page load
    window.addEventListener('DOMContentLoaded', () => {
      initTheme();
      initTimezoneSelector();
      applyLanguage(currentLang);
      checkAuth();
      initPushNotifications();
      initOfflineNetworkListeners();
      updateInstallButtonVisibility();

      // Live Real-Time Clock Ticker (Updates every second with zero refresh needed)
      updateLiveClock();
      setInterval(updateLiveClock, 1000);

      // Continuous Background Freshness Engine Sync (Every 30 seconds)
      setInterval(() => {
        if (currentFridge) {
          loadInventory(true);
        }
      }, 30000);

      // Automatic live sync when user switches back to this tab or wakes up device
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible' && currentFridge) {
          updateLiveClock();
          loadInventory(true);
        }
      });
      window.addEventListener('focus', () => {
        if (currentFridge) {
          updateLiveClock();
          loadInventory(true);
        }
      });
    });
  </script>
</body>
</html>
"""

SERVICE_WORKER_JS = """
// Service Worker for FridgeInventory Progressive Web App, Web Push & Offline Support
const CACHE_NAME = 'fridge-inventory-v2.6';

const OFFLINE_SHELL_ASSETS = [
  '/',
  '/manifest.json',
  '/favicon.ico',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/icon-maskable-512.png',
  '/static/icons/apple-touch-icon.png',
  '/static/icons/icon.svg',
];

// Install: Cache essential app shell
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(OFFLINE_SHELL_ASSETS).catch(() => {});
    })
  );
  self.skipWaiting();
});

// Activate: Clean old caches and claim clients immediately
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
      );
    }).then(() => self.clients.claim())
  );
});

// Offline Fetch Handler: Network first with Cache fallback for app shell
self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  if (event.request.url.includes('/api/')) return;

  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response && response.status === 200) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        }
        return response;
      })
      .catch(() => caches.match(event.request).then((cached) => cached || caches.match('/')))
  );
});

// Message Event: Allows client to trigger local notifications even when internet is disconnected
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SHOW_OFFLINE_NOTIFICATION') {
    const title = event.data.title || 'Fridge Expiry Alert';
    const options = {
      body: event.data.body || 'An item in your fridge is expiring today!',
      icon: event.data.icon || '/static/icons/icon-192.png',
      badge: event.data.badge || '/static/icons/icon-192.png',
      tag: event.data.tag || 'offline-alert',
      renotify: true,
      vibrate: [200, 100, 200],
      data: { url: event.data.url || '/' },
      actions: [
        { action: 'open', title: 'Open Fridge' },
        { action: 'dismiss', title: 'Dismiss' },
      ],
    };
    event.waitUntil(self.registration.showNotification(title, options));
  }
});

// Push Event: Handles incoming background push notifications
self.addEventListener('push', (event) => {
  let data = {
    title: 'Fridge Inventory Alert',
    body: 'An item in your fridge is expiring soon!',
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/icon-192.png',
    url: '/',
    tag: 'fridge-alert',
  };

  if (event.data) {
    try {
      data = { ...data, ...event.data.json() };
    } catch (e) {
      data.body = event.data.text();
    }
  }

  const options = {
    body: data.body,
    icon: data.icon || '/static/icons/icon-192.png',
    badge: data.badge || '/static/icons/icon-192.png',
    tag: data.tag || 'fridge-alert',
    renotify: true,
    requireInteraction: data.tag === 'expiry-alert',
    vibrate: [200, 100, 200],
    data: { url: data.url || '/' },
    actions: [
      { action: 'open', title: 'Open Fridge' },
      { action: 'dismiss', title: 'Dismiss' },
    ],
  };

  event.waitUntil(
    self.registration.showNotification(data.title, options)
  );
});

// Notification Click Event: Focus existing window or open a new one
self.addEventListener('notificationclick', (event) => {
  event.notification.close();

  if (event.action === 'dismiss') return;

  const urlToOpen = event.notification.data?.url || '/';

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if (client.url.includes(self.registration.scope) && 'focus' in client) {
          return client.focus();
        }
      }
      return clients.openWindow(urlToOpen);
    })
  );
});
"""


@app.get("/manifest.json", summary="PWA Web App Manifest")
def serve_manifest():
    """Serve progressive web app manifest for Add to Home Screen with full PWA icon suite."""
    manifest = {
        "name": "FridgeInventory",
        "short_name": "FridgeInventory",
        "description": "Smart Household Food Inventory, Expiry Tracking & Zero-Waste Recipe Generator",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#F59E0B",
        "theme_color": "#F59E0B",
        "categories": ["food", "utilities", "lifestyle"],
        "icons": [
            {
                "src": "/static/icons/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any"
            },
            {
                "src": "/static/icons/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any"
            },
            {
                "src": "/static/icons/icon-maskable-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable"
            },
            {
                "src": "/static/icons/apple-touch-icon.png",
                "sizes": "180x180",
                "type": "image/png",
                "purpose": "any"
            }
        ]
    }
    return Response(
        content=json.dumps(manifest, indent=2),
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
    )


@app.get("/sw.js", summary="Service Worker for Push Notifications")
def serve_service_worker():
    """Serve the Service Worker JavaScript file with proper scope headers."""
    return Response(
        content=SERVICE_WORKER_JS,
        media_type="application/javascript",
        headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


@app.get("/", response_class=HTMLResponse, summary="Serve FridgeInventory Frontend UI")
def serve_ui():
    """Returns the single-page application dashboard for FridgeInventory."""
    return HTMLResponse(content=HTML_CONTENT, status_code=200)



# ============================================================================
# 14. PRODUCTION EXECUTION ENTRYPOINT
# ============================================================================

if __name__ == "__main__":
    server_port = int(os.getenv("PORT", "10000"))
    server_host = os.getenv("HOST", "0.0.0.0")

    logger.info("=" * 70)
    logger.info("Starting FridgeInventory Production Server")
    logger.info(f"Binding strictly to http://{server_host}:{server_port}")
    logger.info("Target Environment: Render / Production ASGI")
    logger.info("=" * 70)

    init_firestore()

    uvicorn.run(
        app,
        host=server_host,
        port=server_port,
        log_level="info",
        access_log=True,
    )
