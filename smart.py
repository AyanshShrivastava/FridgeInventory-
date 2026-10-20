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
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

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
from fastapi.responses import HTMLResponse
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
    days_elapsed: int = 0
    is_expired: bool
    is_expiring_soon: bool
    requires_expiry_date: bool
    storage_tip: str
    last_synced_at: Optional[str] = None


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
                        "pin": data.get("pin", ""),
                        "token": token,
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
                "pin": sess.get("pin", ""),
                "token": token,
            }

    return None

def get_current_fridge_id(request: Request) -> Optional[str]:
    """Retrieve verified fridge_id from current active session."""
    sess = get_current_session_data(request)
    return sess.get("fridge_id") if sess else None


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

        # 2. Parse or calculate expiry date
        raw_exp = str(item.get("expiry_date", "")).strip()[:10]
        exp_d = None
        if raw_exp:
            try:
                exp_d = datetime.strptime(raw_exp, "%Y-%m-%d").date()
            except Exception:
                exp_d = None

        if not exp_d:
            shelf_days = item.get("shelf_life_days")
            if not shelf_days or shelf_days <= 0:
                match = lookup_local_database(item.get("name_en", ""))
                shelf_days = match[0] if match else 4
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

        results.append(
            InventoryItem(
                id=doc_id,
                name_en=item.get("name_en", "Food Item"),
                name_hi=item.get("name_hi", ""),
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
        except Exception:
            days = item_data.shelf_life_days if (item_data.shelf_life_days and item_data.shelf_life_days > 0) else 5
            exp_date = today + timedelta(days=days)
    else:
        days = item_data.shelf_life_days if (item_data.shelf_life_days and item_data.shelf_life_days > 0) else 5
        exp_date = today + timedelta(days=days)

    days_left = (exp_date - today).days
    doc_id = uuid.uuid4().hex[:10]
    payload = {
        "id": doc_id,
        "name_en": item_data.name_en.strip(),
        "name_hi": item_data.name_hi.strip(),
        "category": item_data.category.strip(),
        "added_date": today.isoformat(),
        "expiry_date": exp_date.isoformat(),
        "days_left": days_left,
        "days_elapsed": 0,
        "is_expired": days_left < 0,
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
        name_en=item_data.name_en.strip(),
        name_hi=item_data.name_hi.strip(),
        category=item_data.category.strip(),
        added_date=today.isoformat(),
        expiry_date=exp_date.isoformat(),
        days_left=days_left,
        days_elapsed=0,
        is_expired=days_left < 0,
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
        recipe_name=f"Homestyle Kitchen Masala Bhurji / Stir-Fry ({primary})",
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
    version="2.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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

    _VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "").strip()
    _VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "").strip()
    subject = os.getenv("VAPID_SUBJECT", "").strip()
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
        "icon": "/favicon.ico",
        "badge": "/favicon.ico",
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

    fid = get_current_fridge_id(request) or (fridge.strip().lower() if fridge else None)
    inv = get_fridge_inventory(fid) if fid else []
    cmd_lower = raw_cmd.lower()

    # 1. Clear check
    if any(p in cmd_lower for p in CLEAR_PHRASES):
        if not fid:
            raise HTTPException(status_code=401, detail="Please login to your fridge to manage items.")
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
            if not fid:
                raise HTTPException(status_code=401, detail="Please login to your fridge to manage items.")
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
        return {
            "authenticated": True,
            "fridge_id": fid,
            "fridge_name": fid,
            "pin": sess.get("pin", ""),
            "timezone": active_tz,
            "timezone_code": tz_code,
            "last_sync_time": now_local.strftime(f"%I:%M:%S %p {tz_code}"),
            "last_login_at": now_local.strftime(f"%Y-%m-%d %H:%M:%S {tz_code}"),
        }
    return {
        "authenticated": False,
        "fridge_id": None,
        "fridge_name": None,
        "pin": "",
    }


@app.post("/api/auth/login", summary="Login to existing family fridge")
def api_auth_login(payload: AuthLoginRequest, request: Request, response: Response):
    """Authenticate with Fridge ID and PIN; sets 30-day cryptographically signed cookie."""
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

    # Synchronize all inventory items with current local time vs added_date
    synced_items = update_fridge_time_and_inventory(fid, tz_name=client_tz)

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
    fid = payload.fridge_id.strip().lower()
    pin = payload.pin.strip()

    if not fid or not pin:
        raise HTTPException(status_code=400, detail="Fridge ID and PIN are required.")
    if not FRIDGE_RE.fullmatch(fid):
        raise HTTPException(
            status_code=422,
            detail="Fridge ID must be 3-30 lowercase characters (letters, numbers, hyphens, underscores). Example: 'sharma-kitchen'.",
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
    fid = get_current_fridge_id(request)
    if not fid:
        raise HTTPException(status_code=401, detail="Login to your fridge to enable notifications.")
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
    fid = get_current_fridge_id(request)
    if not fid:
        raise HTTPException(status_code=401, detail="Login to your fridge first.")

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
    fid = get_current_fridge_id(request)
    if not fid:
        raise HTTPException(status_code=401, detail="Login to your fridge first.")

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
            title=f"⚠️ {len(expiring)} item(s) expiring soon!",
            body=f"{names}{suffix} — use them before they go bad!",
            tag="expiry-warning",
        ):
            sent += 1

    if expired:
        names = ", ".join([i.name_en for i in expired[:5]])
        suffix = f" +{len(expired) - 5} more" if len(expired) > 5 else ""
        if _send_push_to_fridge(fid, subs,
            title=f"🚨 {len(expired)} item(s) expired!",
            body=f"{names}{suffix} — check and discard if needed.",
            tag="expiry-alert",
        ):
            sent += 1

    return {"ok": True, "expiring_count": len(expiring), "expired_count": len(expired), "alerts_sent": sent}


@app.api_route("/api/check-expiry-cron", methods=["GET", "POST"], summary="Automated Cron Route: Check Expiry Across All Fridges & Broadcast Web Push")
def api_check_expiry_cron(request: Request):
    """
    Automated background cron route (Requirement 4):
    - Iterates across all stored fridge database collections.
    - Dynamically compares current time with food item addition dates.
    - Finds items whose expiryDate values are within the upcoming 24-hour window (0 <= days_left <= 1) or expired.
    - Signs and broadcasts push alerts to registered user subscriptions using web-push.
    """
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
                title=f"⚠️ {len(expiring_24h)} item(s) expiring within 24 hours!",
                body=f"{names}{suffix} — consume soon to avoid food waste!",
                tag="expiry-warning-24h",
            ):
                sent_for_fridge += 1

        if expired:
            names = ", ".join([i.name_en for i in expired[:4]])
            suffix = f" (+{len(expired)-4} more)" if len(expired) > 4 else ""
            if _send_push_to_fridge(
                fid, subs,
                title=f"🚨 {len(expired)} item(s) expired in fridge!",
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
    fid = get_current_fridge_id(request) or (fridge.strip().lower() if fridge else None)
    if not fid:
        return []
    client_tz = request.headers.get("X-Client-Timezone")
    return get_fridge_inventory(fid, tz_name=client_tz)


@app.post("/api/inventory", response_model=InventoryItem, summary="Add Item to Inventory")
def api_add_inventory(item: InventoryItemCreate, request: Request, fridge: Optional[str] = None):
    """Add a verified food item into the active fridge inventory."""
    fid = get_current_fridge_id(request) or (fridge.strip().lower() if fridge else None)
    if not fid:
        raise HTTPException(status_code=401, detail="Please login to your fridge to add items.")
    client_tz = request.headers.get("X-Client-Timezone")
    return save_fridge_inventory_item(fid, item, client_tz=client_tz)


@app.delete("/api/inventory/{item_id}", summary="Delete / Consume Inventory Item")
def api_delete_inventory(item_id: str, request: Request, fridge: Optional[str] = None):
    """Remove an item from the active fridge inventory."""
    fid = get_current_fridge_id(request) or (fridge.strip().lower() if fridge else None)
    if not fid:
        raise HTTPException(status_code=401, detail="Please login to your fridge to delete items.")
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
            brand: { 50: '#fffbeb', 100: '#fef3c7', 500: '#f59e0b', 600: '#d97706', 700: '#b45309' },
            leaf: { 50: '#ecfdf5', 100: '#d1fae5', 500: '#10b981', 600: '#059669', 700: '#047857' }
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
</head>
<body class="bg-stone-50 dark:bg-stone-950 text-stone-800 dark:text-stone-100 min-h-screen flex flex-col antialiased selection:bg-brand-500 selection:text-white transition-colors duration-150">

  <!-- GITHUB-INSPIRED TOP HEADER -->
  <header class="bg-white dark:bg-stone-900 border-b border-stone-200 dark:border-stone-800 sticky top-0 z-40 transition-colors">
    <div class="max-w-6xl mx-auto px-4 py-2.5 flex items-center justify-between gap-3">
      
      <!-- Brand Logo, Title & Repo Breadcrumbs -->
      <div class="flex items-center gap-3 min-w-0">
        <div class="w-9 h-9 rounded-xl bg-gradient-to-tr from-brand-600 to-amber-500 flex items-center justify-center text-white shadow-sm shrink-0">
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"></path></svg>
        </div>
        <div class="flex items-center gap-2 truncate">
          <span class="font-extrabold text-base tracking-tight text-stone-900 dark:text-white hidden sm:inline" data-i18n="app_title">FridgeInventory</span>
          <span class="text-[11px] bg-stone-100 dark:bg-stone-800 text-stone-600 dark:text-stone-400 font-semibold px-2 py-0.5 rounded-full hidden sm:inline">v2.2</span>
          
          <!-- Logged-in Breadcrumb Path -->
          <div id="header-breadcrumb" class="hidden items-center gap-1.5 px-2.5 py-1 rounded-xl bg-stone-100 dark:bg-stone-800/80 border border-stone-200 dark:border-stone-700/80 text-xs font-mono font-bold text-stone-800 dark:text-stone-200">
            <span class="text-stone-400 dark:text-stone-500">fridge/</span>
            <span id="header-fridge-name" class="text-brand-600 dark:text-brand-400">MY-FRIDGE</span>
            <button type="button" id="copy-fid-btn" class="ml-1 text-stone-400 hover:text-stone-600 dark:hover:text-stone-200 transition" title="Copy Fridge ID">
              <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"></path></svg>
            </button>
          </div>
        </div>
      </div>

      <!-- Controls: Status Pill, PIN, Push Pill, Language, Theme, Logout -->
      <div class="flex items-center gap-2 text-xs shrink-0">
        
        <!-- Live Multi-Country Time Sync Pill & Timezone Selector (Shown when logged in) -->
        <div id="header-sync-pill" class="hidden md:inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-semibold bg-emerald-50 dark:bg-emerald-950/50 text-emerald-700 dark:text-emerald-300 border border-emerald-200 dark:border-emerald-800/60" title="Live clock & automatic real-time freshness sync across all countries">
          <span class="w-2 h-2 rounded-full bg-emerald-500 animate-pulse shrink-0"></span>
          <span id="header-sync-status">Live • Syncing...</span>
          <select id="user-tz-select" class="bg-transparent text-[10px] font-mono font-bold text-emerald-800 dark:text-emerald-200 border-none outline-none cursor-pointer pr-1 focus:ring-0 ml-0.5" title="Country / Timezone">
            <option value="auto">Auto (Local)</option>
          </select>
        </div>

        <!-- Offline Status Badge (Visible when internet is disconnected) -->
        <span id="header-offline-badge" class="hidden items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-bold bg-amber-100 dark:bg-amber-950/60 text-amber-800 dark:text-amber-300 border border-amber-300 dark:border-amber-800" title="Device is offline. Local inventory cache and offline alerts active.">
          <span class="w-1.5 h-1.5 rounded-full bg-amber-500"></span>
          <span>Offline Active</span>
        </span>

        <!-- Quick PIN Button (Shown when logged in) -->
        <div id="header-pin-container" class="hidden sm:flex items-center gap-1 px-2.5 py-1 rounded-xl bg-stone-100 dark:bg-stone-800 border border-stone-200 dark:border-stone-700 text-xs font-mono">
          <span class="text-[11px] text-stone-400 font-sans">PIN:</span>
          <span id="sidebar-fridge-pin" class="font-bold text-stone-700 dark:text-stone-300">••••</span>
          <button type="button" id="toggle-pin-btn" class="text-stone-400 hover:text-brand-600 dark:hover:text-brand-400 ml-1" title="Show / Hide PIN">
            <svg id="pin-eye-icon" class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"></path><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"></path></svg>
          </button>
          <button type="button" id="copy-pin-btn" class="text-stone-400 hover:text-brand-600 dark:hover:text-brand-400" title="Copy PIN">
            <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"></path></svg>
          </button>
        </div>

        <!-- Push Alerts Quick Pill (Shown when logged in) -->
        <button type="button" id="header-push-pill" class="hidden px-2.5 py-1 rounded-xl bg-stone-100 dark:bg-stone-800 hover:bg-stone-200 dark:hover:bg-stone-700 border border-stone-200 dark:border-stone-700 text-xs font-medium flex items-center gap-1.5 transition" title="Go to Offline Web Push Alerts">
          <svg class="w-3.5 h-3.5 text-amber-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"></path></svg>
          <span id="push-status-badge" class="font-bold text-[10px] px-1.5 py-0.5 rounded-full bg-stone-200 dark:bg-stone-700 text-stone-600 dark:text-stone-300">OFF</span>
        </button>

        <!-- Share Invite Button (Shown when logged in) -->
        <button type="button" id="share-fridge-btn" class="hidden sm:flex items-center gap-1 px-2.5 py-1 rounded-xl bg-stone-100 dark:bg-stone-800 hover:bg-stone-200 dark:hover:bg-stone-700 text-stone-700 dark:text-stone-300 border border-stone-200 dark:border-stone-700 text-xs font-medium transition" title="Share Fridge with Family">
          <svg class="w-3.5 h-3.5 text-stone-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8.684 13.342C8.886 12.938 9 12.482 9 12c0-.482-.114-.938-.316-1.342m0 2.684a3 3 0 110-2.684m0 2.684l6.632 3.316m-6.632-6l6.632-3.316m0 0a3 3 0 105.367-2.684 3 3 0 00-5.367 2.684zm0 9.316a3 3 0 105.368 2.684 3 3 0 00-5.368-2.684z"></path></svg>
          <span class="hidden md:inline" data-i18n="sidebar_share_btn">Share</span>
        </button>

        <!-- Language Toggle Button -->
        <button id="lang-toggle-btn" class="px-2.5 py-1.5 rounded-xl border border-stone-200 dark:border-stone-700 bg-stone-100 dark:bg-stone-800 hover:bg-stone-200 dark:hover:bg-stone-700 text-stone-700 dark:text-stone-300 font-medium transition flex items-center gap-1.5" title="Switch Language">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 5h12M9 3v2m1.048 9.5A18.022 18.022 0 016.412 9m6.088 9h7M11 21l5-10 5 10M12.751 5C11.783 10.77 8.07 15.61 3 18.129"></path></svg>
          <span id="lang-label">हिन्दी</span>
        </button>

        <!-- Theme Toggle Button -->
        <button id="theme-toggle-btn" class="p-2 rounded-xl border border-stone-200 dark:border-stone-700 bg-stone-100 dark:bg-stone-800 hover:bg-stone-200 dark:hover:bg-stone-700 text-stone-700 dark:text-stone-300 transition" title="Toggle Dark/Light Mode">
          <svg id="theme-icon-sun" class="w-4 h-4 hidden" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z"></path></svg>
          <svg id="theme-icon-moon" class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z"></path></svg>
        </button>

        <!-- Auth Actions -->
        <div id="auth-logged-in" class="hidden flex items-center gap-1.5">
          <button id="logout-btn" class="px-2.5 py-1.5 rounded-xl bg-stone-100 dark:bg-stone-800 hover:bg-rose-50 dark:hover:bg-rose-950/60 hover:text-rose-600 text-stone-600 dark:text-stone-300 font-medium border border-stone-200 dark:border-stone-700 transition" title="Logout" data-i18n="logout_btn">
            Logout
          </button>
        </div>

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
  <main class="flex-grow max-w-6xl w-full mx-auto px-4 py-5 space-y-6">

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
          Your smart household kitchen companion for effortless inventory tracking, offline push alerts, and AI-powered zero-waste cooking.
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
          <div class="text-xl mb-1.5">🔔</div>
          <h3 class="font-bold text-xs sm:text-sm text-stone-900 dark:text-white" data-i18n="feat_expiry_title">Offline Push Alerts</h3>
          <p class="text-xs text-stone-500 dark:text-stone-400 mt-1" data-i18n="feat_expiry_desc">Background Service Worker alerts you before food expires, even when offline.</p>
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
    </div>

    <!-- MAIN APP DASHBOARD (SHOWN ONLY WHEN LOGGED IN) -->
    <div id="app-dashboard" class="hidden space-y-6">

      <!-- GITHUB-STYLE HORIZONTAL SUBNAV BAR -->
      <div class="border-b border-stone-200 dark:border-stone-800 -mx-4 px-4 sm:mx-0 sm:px-0">
        <nav class="flex space-x-1 sm:space-x-3 overflow-x-auto text-xs sm:text-sm font-semibold pb-px scrollbar-none">
          <button id="tab-inventory-btn" class="tab-btn py-3 px-3 border-b-2 border-brand-600 text-brand-600 dark:text-brand-400 flex items-center gap-2 whitespace-nowrap">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"></path></svg>
            <span data-i18n="tab_inventory">Fridge Inventory</span>
            <span id="tab-inventory-count" class="bg-stone-100 dark:bg-stone-800 text-stone-700 dark:text-stone-300 text-[11px] px-2 py-0.5 rounded-full font-bold">0</span>
          </button>
          <button id="tab-urgent-btn" class="tab-btn py-3 px-3 border-b-2 border-transparent text-stone-500 dark:text-stone-400 hover:text-stone-800 dark:hover:text-stone-200 flex items-center gap-2 whitespace-nowrap">
            <svg class="w-4 h-4 text-amber-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"></path></svg>
            <span data-i18n="tab_urgent">Expiring Soon</span>
            <span id="tab-urgent-count" class="bg-amber-100 dark:bg-amber-950/60 text-brand-700 dark:text-brand-300 text-[11px] px-2 py-0.5 rounded-full font-bold">0</span>
          </button>
          <button id="tab-push-btn" class="tab-btn py-3 px-3 border-b-2 border-transparent text-stone-500 dark:text-stone-400 hover:text-stone-800 dark:hover:text-stone-200 flex items-center gap-2 whitespace-nowrap">
            <svg class="w-4 h-4 text-indigo-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"></path></svg>
            <span data-i18n="tab_push">Offline Push Alerts</span>
            <span id="push-nav-badge" class="text-[10px] px-2 py-0.5 rounded-full bg-stone-100 dark:bg-stone-800 text-stone-600 dark:text-stone-400 font-bold">OFF</span>
          </button>
          <button id="tab-rescue-btn" class="tab-btn py-3 px-3 border-b-2 border-transparent text-stone-500 dark:text-stone-400 hover:text-stone-800 dark:hover:text-stone-200 flex items-center gap-2 whitespace-nowrap">
            <svg class="w-4 h-4 text-emerald-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg>
            <span data-i18n="tab_rescue">Rescue Chef</span>
            <span class="bg-leaf-50 dark:bg-leaf-950/60 text-leaf-700 dark:text-leaf-300 text-[11px] px-2 py-0.5 rounded-full font-bold">AI Zero-Waste</span>
          </button>
          <button id="tab-test-btn" class="tab-btn py-3 px-3 border-b-2 border-transparent text-stone-500 dark:text-stone-400 hover:text-stone-800 dark:hover:text-stone-200 flex items-center gap-2 whitespace-nowrap">
            <svg class="w-4 h-4 text-stone-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>
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
            <div class="relative flex-grow">
              <input 
                type="text" 
                id="cmd-input" 
                placeholder="Type or speak (e.g. 'bought 2kg palak', 'i ate lays', 'used milk')..." 
                class="w-full pl-4 pr-12 py-3 rounded-2xl border border-stone-200 dark:border-stone-700 focus:outline-none focus:ring-2 focus:ring-brand-500 text-sm text-stone-800 dark:text-stone-100 bg-stone-50/50 dark:bg-stone-800 font-medium"
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
            <span class="font-bold" id="sync-engine-heading">Live Freshness Engine:</span>
            <span id="sync-banner-text" data-i18n="time_sync_banner">
              Automatically comparing your country's local time against food addition dates. Freshness & expiry countdowns update continuously without refreshing.
            </span>
          </div>
          <div class="text-[11px] text-stone-500 dark:text-stone-400 font-mono shrink-0" id="sync-timestamp-display">Live Synced</div>
        </div>

        <!-- FILTER CONTROLS & RESCUE ACTION BAR -->
        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 pt-1">
          <div id="filter-controls" class="flex items-center gap-1.5 text-xs">
            <button class="filter-btn active bg-stone-900 dark:bg-white text-white dark:text-stone-900 px-3.5 py-1.5 rounded-full font-medium" data-filter="all" data-i18n="filter_all">All Items</button>
            <button class="filter-btn bg-stone-100 dark:bg-stone-800 text-stone-600 dark:text-stone-300 hover:bg-stone-200 px-3.5 py-1.5 rounded-full font-medium" data-filter="urgent" data-i18n="filter_urgent">Urgent</button>
            <button class="filter-btn bg-stone-100 dark:bg-stone-800 text-stone-600 dark:text-stone-300 hover:bg-stone-200 px-3.5 py-1.5 rounded-full font-medium" data-filter="fresh" data-i18n="filter_fresh">Fresh</button>
            <button class="filter-btn bg-stone-100 dark:bg-stone-800 text-stone-600 dark:text-stone-300 hover:bg-stone-200 px-3.5 py-1.5 rounded-full font-medium" data-filter="expired" data-i18n="filter_expired">Expired</button>
          </div>

          <!-- MULTI-SELECT RESCUE BAR -->
          <div id="rescue-action-bar" class="hidden bg-stone-900 dark:bg-stone-800 text-white px-3.5 py-2 rounded-2xl flex items-center justify-between gap-3 shadow-md">
            <span class="text-xs font-medium">Selected <b id="selected-count" class="text-brand-400 font-bold">0</b> items</span>
            <button id="trigger-rescue-btn" class="px-3.5 py-1.5 bg-brand-600 hover:bg-brand-500 font-bold rounded-xl text-xs shadow transition">
              Rescue Recipe ✨
            </button>
          </div>
        </div>

        <!-- INVENTORY GRID -->
        <div id="inventory-grid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          <!-- Populated via JS -->
        </div>

        <!-- EMPTY STATE -->
        <div id="empty-state" class="hidden text-center py-16 bg-white dark:bg-stone-900 rounded-3xl border border-dashed border-stone-200 dark:border-stone-800 p-6 transition-colors">
          <div class="text-3xl mb-2">🥬</div>
          <h3 class="text-base font-bold text-stone-800 dark:text-stone-200" data-i18n="empty_title">Your Fridge is Empty</h3>
          <p class="text-xs text-stone-500 dark:text-stone-400 mt-1 max-w-sm mx-auto" data-i18n="empty_desc">
            Add fresh produce, groceries, or meals using the input above or your microphone.
          </p>
        </div>
      </section>

      <!-- TAB 2: EXPIRING SOON DEDICATED VIEW -->
      <section id="section-urgent" class="tab-section hidden space-y-6">
        <div class="bg-white dark:bg-stone-900 rounded-3xl p-6 sm:p-8 border border-stone-200/80 dark:border-stone-800 shadow-sm space-y-6">
          <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-stone-100 dark:border-stone-800 pb-5">
            <div>
              <h2 class="text-lg sm:text-xl font-bold text-stone-900 dark:text-white flex items-center gap-2">
                <span class="text-amber-500">⚠️</span>
                <span data-i18n="urgent_tab_title">Urgent Attention Needed</span>
              </h2>
              <p class="text-xs text-stone-500 dark:text-stone-400 mt-1" data-i18n="urgent_tab_desc">
                Items expiring within 48 hours or already expired. Consume or rescue them before they spoil!
              </p>
            </div>
            <div class="flex items-center gap-2">
              <button id="urgent-rescue-all-btn" class="px-4 py-2.5 bg-brand-600 hover:bg-brand-700 text-white font-bold rounded-xl text-xs shadow transition flex items-center gap-1.5">
                <span>Rescue All with AI Chef ✨</span>
              </button>
            </div>
          </div>

          <div id="urgent-grid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            <!-- Populated via JS -->
          </div>

          <div id="urgent-empty" class="hidden text-center py-12 bg-stone-50 dark:bg-stone-800/40 rounded-2xl border border-dashed border-stone-200 dark:border-stone-800 p-6">
            <span class="text-2xl">🎉</span>
            <h4 class="font-bold text-sm text-stone-800 dark:text-stone-200 mt-1">No Urgent Expirations!</h4>
            <p class="text-xs text-stone-500 dark:text-stone-400 mt-0.5">Everything in your fridge has plenty of shelf life remaining.</p>
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
              <span id="push-main-toggle-text">Enable Expiry Alerts</span>
            </button>
          </div>

          <!-- Chrome & Browser Permission Guide (Prominent & Helpful) -->
          <div id="push-chrome-guide-card" class="p-4 rounded-2xl bg-amber-50/70 dark:bg-amber-950/30 border border-amber-200/80 dark:border-amber-800/60 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 transition-all">
            <div class="flex items-start gap-3">
              <div class="w-10 h-10 rounded-xl bg-amber-100 dark:bg-amber-900/60 text-amber-700 dark:text-amber-300 flex items-center justify-center shrink-0 text-xl">
                🔔
              </div>
              <div class="text-xs">
                <h4 class="font-bold text-stone-900 dark:text-white" data-i18n="push_guide_title">How to allow notifications in Google Chrome</h4>
                <p class="text-stone-600 dark:text-stone-300 mt-0.5 leading-relaxed" data-i18n="push_guide_desc">
                  If your browser blocks notifications: click the <strong>lock / tune icon 🔒</strong> next to the web address at the top &gt; <strong>Site settings</strong> &gt; set <strong>Notifications</strong> to <strong>Allow</strong>.
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
                <h4 class="font-bold text-xs text-stone-900 dark:text-white">🔔 Test Web Push</h4>
                <p class="text-[11px] text-stone-500 dark:text-stone-400 mt-0.5">Signed VAPID push (buffers 24h offline).</p>
              </div>
              <button id="push-tab-test-btn" class="px-3.5 py-2 rounded-xl bg-white dark:bg-stone-800 hover:bg-stone-100 dark:hover:bg-stone-700 border border-stone-200 dark:border-stone-700 text-stone-800 dark:text-stone-200 font-semibold text-xs transition shrink-0">
                Send Push
              </button>
            </div>

            <div class="p-4 rounded-2xl bg-stone-50 dark:bg-stone-800/50 border border-stone-200/70 dark:border-stone-700/60 flex items-center justify-between gap-3">
              <div>
                <h4 class="font-bold text-xs text-stone-900 dark:text-white">⚡ Test Offline Alert</h4>
                <p class="text-[11px] text-stone-500 dark:text-stone-400 mt-0.5">Local Service Worker (no Wi-Fi needed).</p>
              </div>
              <button id="push-tab-offline-test-btn" class="px-3.5 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white font-semibold text-xs shadow-sm transition shrink-0">
                Test Offline
              </button>
            </div>

            <div class="p-4 rounded-2xl bg-stone-50 dark:bg-stone-800/50 border border-stone-200/70 dark:border-stone-700/60 flex items-center justify-between gap-3">
              <div>
                <h4 class="font-bold text-xs text-stone-900 dark:text-white">⏳ Check Expiry Now</h4>
                <p class="text-[11px] text-stone-500 dark:text-stone-400 mt-0.5">Scan fridge and alert for food expiring soon.</p>
              </div>
              <button id="push-tab-expiry-btn" class="px-3.5 py-2 rounded-xl bg-amber-600 hover:bg-amber-700 text-white font-semibold text-xs shadow-sm transition shrink-0">
                Check Now
              </button>
            </div>
          </div>

          <!-- Subtle collapsible diagnostics for developers / technical users -->
          <details class="text-xs text-stone-400 dark:text-stone-500 pt-2 border-t border-stone-100 dark:border-stone-800/60">
            <summary class="cursor-pointer hover:text-stone-600 dark:hover:text-stone-300 font-medium select-none">
              ⚙️ Technical status & diagnostics
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
            <label class="block text-xs font-semibold text-stone-700 dark:text-stone-300 mb-1" data-i18n="custom_rescue_label">
              Custom Ingredients / Leftovers to Rescue (Comma-separated):
            </label>
            <input 
              type="text" 
              id="rescue-items-input" 
              placeholder="e.g. leftover boiled potatoes, sour curd, coriander stems, capsicum..."
              class="w-full px-4 py-2.5 rounded-xl border border-stone-200 dark:border-stone-700 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 bg-stone-50 dark:bg-stone-800 text-stone-800 dark:text-stone-100 font-medium"
            />
          </div>

          <!-- RECIPE RESULT CARD -->
          <div id="recipe-container" class="mt-6 hidden space-y-6">
            <div class="bg-stone-50 dark:bg-stone-800/60 border border-stone-200 dark:border-stone-700 rounded-2xl p-6 relative">
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

      <!-- TAB 5: STANDALONE FOOD SCANNER SECTION -->
      <section id="section-test" class="tab-section hidden space-y-4">
        <div class="bg-white dark:bg-stone-900 rounded-3xl p-6 sm:p-8 border border-stone-200 dark:border-stone-800 shadow-sm max-w-2xl mx-auto transition-colors">
          <h2 class="text-lg font-bold text-stone-900 dark:text-white mb-1" data-i18n="scanner_title">AI Food Inspector & Storage Advisor</h2>
          <p class="text-xs text-stone-500 dark:text-stone-400 mb-4" data-i18n="scanner_desc">
            Test any Indian food name, brand, or dish to inspect its culinary category, Hindi name, standard shelf life, and whether package expiry is mandatory.
          </p>

          <div class="flex gap-2">
            <input 
              type="text" 
              id="scanner-input" 
              placeholder="Type any food (e.g. Gajar, Haldiram Bhujia, Amul Butter)..." 
              class="flex-grow px-4 py-2.5 rounded-xl border border-stone-200 dark:border-stone-700 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 bg-stone-50 dark:bg-stone-800 text-stone-800 dark:text-stone-100 font-medium"
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
            placeholder="e.g. sharma-kitchen or my-fridge" 
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
        <h3 class="font-bold text-base" data-i18n="modal_confirm_title">Confirm Fridge Entry</h3>
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
          <span data-i18n="modal_packaged_desc">Packaged snack / frozen item detected. Please check and enter the printed label date.</span>
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
    let currentFridgePin = '';
    let pinRevealed = false;
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

    // Bilingual Text Dictionary
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
        cmd_heading: "Smart Voice & Text Control",
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
        push_guide_desc: "If your browser blocks notifications: click the lock / tune icon 🔒 next to the web address at the top > Site settings > set Notifications to Allow.",
        push_guide_btn: "Google Help Guide",
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
        modal_packaged_desc: "Packaged snack or frozen item detected. Please check and enter the printed label date.",
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
        cmd_heading: "स्मार्ट आवाज़ और टेक्स्ट नियंत्रण",
        cmd_sub: "बताएं क्या लाए या खाया। जैसे: '2 किलो पालक लाया', 'i ate lays', 'दूध इस्तेमाल किया', 'सब खत्म'।",
        cmd_placeholder: "टाइप करें या बोलें (जैसे: '2 किलो पालक लाया', 'i ate lays', 'दूध पिया')...",
        cmd_btn: "कमांड चलाएँ",
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
        push_guide_desc: "यदि नोटिफिकेशन ब्लॉक हैं: ऊपर वेबसाइट पते के पास ताले 🔒 के निशान पर क्लिक करें > Site settings > Notifications को Allow करें।",
        push_guide_btn: "गूगल हेल्प गाइड",
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
        modal_packaged_desc: "पैकेज्ड स्नैक या फ्रोजन सामान। कृपया पैकेट पर छपी एक्सपायरी तारीख दर्ज करें।",
        modal_tip_label: "रसोई भंडारण सुझाव:",
        modal_cancel: "रद्द करें",
        modal_save: "फ्रिज में सेव करें"
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

      const cmdInput = document.getElementById('cmd-input');
      if (cmdInput) cmdInput.placeholder = texts.cmd_placeholder;
      const langLabel = document.getElementById('lang-label');
      if (langLabel) langLabel.textContent = texts.lang_btn;
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

    // Auth & Fridge Management
    async function checkAuth() {
      try {
        const activeTz = getEffectiveTimezone();
        const res = await fetch('/api/auth/me', {
          headers: { 'X-Client-Timezone': activeTz }
        });
        const data = await res.json();
        if (data.authenticated && data.fridge_id) {
          if (data.timezone && data.timezone !== 'UTC' && (!userTimezone || userTimezone === 'auto')) {
            userTimezone = data.timezone;
            const tzSelect = document.getElementById('user-tz-select');
            if (tzSelect) tzSelect.value = userTimezone;
          }
          setAuthenticatedState(data.fridge_id, data.pin);
          updateLiveClock();
        } else {
          setLoggedOutState();
        }
      } catch (e) {
        setLoggedOutState();
      }
    }

    function setAuthenticatedState(fid, pin) {
      currentFridge = fid;
      if (pin) currentFridgePin = pin;
      else if (!currentFridgePin) currentFridgePin = localStorage.getItem('fi_last_pin_' + fid) || '';
      if (fid && currentFridgePin) {
        try { localStorage.setItem('fi_last_pin_' + fid, currentFridgePin); } catch(e){}
      }

      document.getElementById('auth-logged-in').classList.remove('hidden');
      document.getElementById('auth-logged-out').classList.add('hidden');
      document.getElementById('header-breadcrumb').classList.remove('hidden');
      document.getElementById('header-breadcrumb').classList.add('flex');
      document.getElementById('header-sync-pill').classList.remove('hidden');
      document.getElementById('header-sync-pill').classList.add('inline-flex');
      document.getElementById('header-pin-container').classList.remove('hidden');
      document.getElementById('header-pin-container').classList.add('flex');
      document.getElementById('header-push-pill').classList.remove('hidden');
      document.getElementById('share-fridge-btn').classList.remove('hidden');
      document.getElementById('share-fridge-btn').classList.add('flex');

      document.getElementById('header-fridge-name').textContent = fid.toUpperCase();
      const diagFid = document.getElementById('diag-fridge-id');
      if (diagFid) diagFid.textContent = fid;

      updateSidebarPinDisplay();

      document.getElementById('logged-out-bubble').classList.add('hidden');
      document.getElementById('app-dashboard').classList.remove('hidden');
      loadInventory();
    }

    function setLoggedOutState() {
      currentFridge = null;
      currentFridgePin = '';
      pinRevealed = false;
      document.getElementById('auth-logged-in').classList.add('hidden');
      document.getElementById('auth-logged-out').classList.remove('hidden');
      document.getElementById('header-breadcrumb').classList.add('hidden');
      document.getElementById('header-sync-pill').classList.add('hidden');
      document.getElementById('header-pin-container').classList.add('hidden');
      document.getElementById('header-push-pill').classList.add('hidden');
      document.getElementById('share-fridge-btn').classList.add('hidden');

      document.getElementById('logged-out-bubble').classList.remove('hidden');
      document.getElementById('app-dashboard').classList.add('hidden');
      inventory = [];
    }

    function updateSidebarPinDisplay() {
      const pinEl = document.getElementById('sidebar-fridge-pin');
      const eyeIcon = document.getElementById('pin-eye-icon');
      if (!pinEl) return;
      if (!currentFridgePin) {
        pinEl.textContent = '••••';
        return;
      }
      if (pinRevealed) {
        pinEl.textContent = currentFridgePin;
        if (eyeIcon) {
          eyeIcon.innerHTML = '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l18 18"></path>';
        }
      } else {
        pinEl.textContent = '•'.repeat(Math.max(4, currentFridgePin.length));
        if (eyeIcon) {
          eyeIcon.innerHTML = '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"></path><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"></path>';
        }
      }
    }

    document.getElementById('toggle-pin-btn').addEventListener('click', () => {
      pinRevealed = !pinRevealed;
      updateSidebarPinDisplay();
    });

    document.getElementById('copy-pin-btn').addEventListener('click', () => {
      if (currentFridgePin) {
        navigator.clipboard.writeText(currentFridgePin).then(() => {
          showToast(currentLang === 'hi' ? 'पिन कॉपी किया गया!' : 'PIN copied to clipboard!', 'success');
        });
      } else {
        showToast(currentLang === 'hi' ? 'कोई पिन उपलब्ध नहीं' : 'No PIN available', 'warning');
      }
    });

    document.getElementById('copy-fid-btn').addEventListener('click', () => {
      if (currentFridge) {
        navigator.clipboard.writeText(currentFridge).then(() => {
          showToast(currentLang === 'hi' ? 'फ्रिज ID कॉपी किया गया!' : 'Fridge ID copied to clipboard!', 'success');
        });
      }
    });

    document.getElementById('share-fridge-btn').addEventListener('click', () => {
      if (currentFridge) {
        const text = `FridgeInventory Login Details:\nFridge ID: ${currentFridge}\nPIN / Password: ${currentFridgePin || '(Your Family PIN)'}\nURL: ${window.location.origin}`;
        navigator.clipboard.writeText(text).then(() => {
          showToast(currentLang === 'hi' ? 'फ्रिज विवरण शेयर करने के लिए कॉपी किया गया!' : 'Fridge details copied to share with family!', 'success');
        });
      }
    });

    async function handleLogout() {
      try {
        await fetch('/api/auth/logout', { method: 'POST' });
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

    loginForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fid = document.getElementById('login-fid').value.trim().toLowerCase();
      const pin = document.getElementById('login-pin').value.trim();
      const activeTz = getEffectiveTimezone();
      try {
        const res = await fetch('/api/auth/login', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Client-Timezone': activeTz,
          },
          body: JSON.stringify({ fridge_id: fid, pin: pin, timezone: activeTz })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Login failed');
        authModal.classList.add('hidden');
        setAuthenticatedState(data.fridge_id, pin);
        updateLiveClock();
        showToast(data.message || 'Login successful!', 'success');
      } catch (err) {
        showToast(err.message, 'error');
      }
    });

    createForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fid = document.getElementById('create-fid').value.trim().toLowerCase();
      const pin = document.getElementById('create-pin').value.trim();
      const activeTz = getEffectiveTimezone();
      try {
        const res = await fetch('/api/auth/create', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Client-Timezone': activeTz,
          },
          body: JSON.stringify({ fridge_id: fid, pin: pin, timezone: activeTz })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Fridge creation failed');
        authModal.classList.add('hidden');
        setAuthenticatedState(data.fridge_id, pin);
        updateLiveClock();
        showToast(data.message, 'success');
      } catch (err) {
        showToast(err.message, 'error');
      }
    });

    document.getElementById('logout-btn').addEventListener('click', handleLogout);

    // Load Inventory Items with Offline Caching & Timezone Sync
    async function loadInventory(silent = false) {
      const activeTz = getEffectiveTimezone();
      try {
        const url = currentFridge ? `/api/inventory?fridge=${encodeURIComponent(currentFridge)}` : '/api/inventory';
        const res = await fetch(url, {
          headers: { 'X-Client-Timezone': activeTz }
        });
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

      const elapsedText = item.days_elapsed === 0 ? 'Today' : `${item.days_elapsed}d ago`;

      const card = document.createElement('div');
      card.className = `bg-white dark:bg-stone-900 rounded-2xl p-4 border border-stone-200/80 dark:border-stone-800 shadow-sm hover:shadow-md transition-all flex flex-col justify-between ${
        item.days_left < 0 ? 'opacity-85' : ''
      }`;
      card.innerHTML = `
        <div>
          <div class="flex items-start justify-between gap-2">
            <div>
              <div class="flex items-center gap-1.5 flex-wrap">
                <h4 class="font-bold text-stone-900 dark:text-white text-sm leading-tight">${displayName}</h4>
                <span class="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-stone-100 dark:bg-stone-800 text-stone-500 uppercase">${item.category.replace(/_/g, ' ')}</span>
              </div>
              ${subName ? `<p class="text-xs text-stone-500 dark:text-stone-400 mt-0.5">${subName}</p>` : ''}
            </div>
            <span class="text-[11px] px-2.5 py-0.5 rounded-full border ${badgeClass} shrink-0">
              ${statusText}
            </span>
          </div>

          <div class="mt-3 text-[11px] text-stone-500 dark:text-stone-400 space-y-1.5">
            <div class="flex items-center justify-between">
              <span>Added: <b class="text-stone-700 dark:text-stone-300 font-medium">${item.added_date}</b> (${elapsedText})</span>
              <span>Expires: <b class="text-stone-700 dark:text-stone-300 font-medium">${item.expiry_date}</b></span>
            </div>
            ${item.storage_tip ? `<div class="text-stone-600 dark:text-stone-300 bg-stone-50 dark:bg-stone-800/80 p-2 rounded-xl border border-stone-100 dark:border-stone-700/80">${item.storage_tip}</div>` : ''}
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
      return card;
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
        try { localStorage.setItem('fi_cached_inventory', JSON.stringify(inventory)); } catch(e){}
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
          headers: {
            'Content-Type': 'application/json',
            'X-Client-Timezone': getEffectiveTimezone(),
          },
          body: JSON.stringify(payload)
        });

        if (!res.ok) throw new Error('Could not add item');
        const saved = await res.json();
        inventory.unshift(saved);
        try { localStorage.setItem('fi_cached_inventory', JSON.stringify(inventory)); } catch(e){}
        renderInventory();
        updateStats();
        modal.classList.add('hidden');
        document.getElementById('cmd-input').value = '';
        showToast(currentLang === 'hi' ? `${saved.name_hi || saved.name_en} जोड़ा गया!` : `Added ${saved.name_en}!`, 'success');
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
          headers: {
            'Content-Type': 'application/json',
            'X-Client-Timezone': getEffectiveTimezone(),
          },
          body: JSON.stringify({ command: text })
        });

        if (!res.ok) {
          const errData = await res.json();
          throw new Error(errData.detail || 'Command processing failed');
        }

        const data = await res.json();

        // 1. Clear action
        if (data.action === 'clear') {
          inventory = [];
          try { localStorage.setItem('fi_cached_inventory', JSON.stringify(inventory)); } catch(e){}
          renderInventory();
          updateStats();
          cmdInput.value = '';
          showToast(currentLang === 'hi' ? data.message_hi : data.message_en, 'success');
        }
        // 2. Consume / Remove action (e.g. "i ate lays")
        else if (data.action === 'removed') {
          inventory = inventory.filter(i => i.id !== data.item_id);
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
        // 4. Add / Classify action
        else if (data.action === 'classify') {
          const c = data.classification;
          if (!c.is_food) {
            showToast(c.message || 'Not recognized as a food item.', 'warning');
          } else if (c.requires_expiry_date) {
            // Packaged snack with printed date — show modal
            openModalWithClassification(c);
          } else {
            // Everyday groceries / produce — auto-add directly without modal
            autoAddFreshItem(c);
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
        li.className = 'flex items-start gap-2.5';
        li.innerHTML = `<span class="w-5 h-5 rounded-full bg-brand-100 dark:bg-stone-800 text-brand-800 dark:text-brand-300 font-bold text-xs flex items-center justify-center shrink-0 mt-0.5">${idx + 1}</span><span>${step}</span>`;
        stepsList.appendChild(li);
      });

      recipeContainer.classList.remove('hidden');
    }

    document.getElementById('generate-recipe-btn').addEventListener('click', () => {
      const customText = document.getElementById('rescue-items-input').value.trim();
      let items = customText ? customText.split(',').map(s => s.trim()).filter(Boolean) : Array.from(selectedItemsForRescue);
      if (items.length === 0) {
        items = inventory.filter(i => i.days_left <= 2).map(i => i.name_en);
      }
      triggerRecipeGeneration(items);
    });

    document.getElementById('copy-recipe-btn').addEventListener('click', () => {
      const name = document.getElementById('recipe-name').textContent;
      const steps = Array.from(document.querySelectorAll('#recipe-steps-list li')).map(li => li.innerText).join('\n');
      navigator.clipboard.writeText(`${name}\n\nInstructions:\n${steps}`).then(() => {
        showToast('Recipe copied to clipboard!', 'success');
      });
    });

    // Standalone Scanner
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

        resBox.innerHTML = `
          <div class="p-4 rounded-2xl bg-stone-50 dark:bg-stone-800 border border-stone-200 dark:border-stone-700 text-xs space-y-2">
            <div class="flex items-center justify-between">
              <span class="font-bold text-sm text-stone-900 dark:text-white">${data.name_en || text}</span>
              <span class="font-bold uppercase text-[10px] px-2 py-0.5 rounded bg-white dark:bg-stone-900 border border-stone-200 dark:border-stone-700">${data.category}</span>
            </div>
            ${data.name_hi ? `<div class="text-brand-600 font-semibold">${data.name_hi}</div>` : ''}
            <div>Estimated Shelf Life: <b>${data.shelf_life_days ? data.shelf_life_days + ' days' : 'Package Expiry Date Required'}</b></div>
            <div>Package Expiry Required: <b>${data.requires_expiry_date ? 'Yes' : 'No'}</b></div>
            <div>Storage Advice: <span class="text-stone-600 dark:text-stone-300">${data.storage_tip}</span></div>
          </div>
        `;
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
          headerBadge.textContent = 'ON';
          headerBadge.className = 'font-bold text-[10px] px-1.5 py-0.5 rounded-full bg-emerald-100 dark:bg-emerald-950 text-emerald-700 dark:text-emerald-300';
        }
        if (navBadge) {
          navBadge.textContent = 'ON';
          navBadge.className = 'text-[10px] px-2 py-0.5 rounded-full bg-emerald-100 dark:bg-emerald-950 text-emerald-700 dark:text-emerald-300 font-bold';
        }
        if (heroBadge) {
          heroBadge.textContent = 'ACTIVE';
          heroBadge.className = 'text-xs font-bold px-2.5 py-0.5 rounded-full bg-emerald-100 dark:bg-emerald-950 text-emerald-700 dark:text-emerald-300';
        }
        if (toggleText) toggleText.textContent = 'Disable Offline Expiry Alerts';
        if (toggleBtn) toggleBtn.className = 'px-6 py-3 rounded-2xl bg-stone-200 dark:bg-stone-800 hover:bg-stone-300 dark:hover:bg-stone-700 text-stone-800 dark:text-stone-200 font-bold text-sm transition-all flex items-center justify-center gap-2 shrink-0';
      } else {
        if (headerBadge) {
          headerBadge.textContent = 'OFF';
          headerBadge.className = 'font-bold text-[10px] px-1.5 py-0.5 rounded-full bg-stone-200 dark:bg-stone-700 text-stone-600 dark:text-stone-300';
        }
        if (navBadge) {
          navBadge.textContent = 'OFF';
          navBadge.className = 'text-[10px] px-2 py-0.5 rounded-full bg-stone-100 dark:bg-stone-800 text-stone-600 dark:text-stone-400 font-bold';
        }
        if (heroBadge) {
          heroBadge.textContent = 'OFF';
          heroBadge.className = 'text-xs font-bold px-2.5 py-0.5 rounded-full bg-stone-100 dark:bg-stone-800 text-stone-500';
        }
        if (toggleText) toggleText.textContent = 'Enable Offline Expiry Alerts';
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
        statusEl.textContent = `Live • ${timeStr} ${tzAbbr}`;
      }

      const syncTsEl = document.getElementById('sync-timestamp-display');
      if (syncTsEl) {
        syncTsEl.textContent = fullStr;
      }

      const engineHeadingEl = document.getElementById('sync-engine-heading');
      if (engineHeadingEl) {
        const countryOrCity = activeTz === 'auto' ? 'Auto Local' : activeTz.split('/').pop().replace('_', ' ');
        engineHeadingEl.textContent = `Live Freshness Engine (${countryOrCity} • ${tzAbbr}):`;
      }
    }

    // Local Offline Notification Engine (Works completely without Wi-Fi / Cellular)
    function checkLocalOfflineExpiryAlerts() {
      if (!swRegistration || !swRegistration.active) return;
      if (!inventory || inventory.length === 0) return;

      const expiringItems = inventory.filter(i => i.days_left <= 1);
      if (expiringItems.length === 0) return;

      // Throttle offline notifications so we don't spam on every 30s interval
      const lastAlert = parseInt(localStorage.getItem('fi_last_offline_alert') || '0', 10);
      const now = Date.now();
      if (now - lastAlert < 3 * 3600 * 1000) return;

      try { localStorage.setItem('fi_last_offline_alert', now.toString()); } catch(e){}
      const itemNames = expiringItems.map(i => currentLang === 'hi' ? (i.name_hi || i.name_en) : i.name_en).slice(0, 3).join(', ');
      const count = expiringItems.length;

      swRegistration.active.postMessage({
        type: 'SHOW_OFFLINE_NOTIFICATION',
        title: currentLang === 'hi' ? `🚨 खाद्य समाप्ति अलर्ट (${count} सामान)` : `🚨 Food Expiry Alert (${count} item${count > 1 ? 's' : ''})`,
        body: currentLang === 'hi' ? `${itemNames} जल्द समाप्त हो रहे हैं! फ्रिज खोलकर उपयोग करें।` : `${itemNames} expiring soon! Open your fridge to inspect.`,
        tag: 'offline-expiry-alert',
        url: '/'
      });
    }

    async function triggerLocalOfflineTest() {
      if (!swRegistration || !swRegistration.active) {
        showToast(currentLang === 'hi' ? 'सर्विस वर्कर सक्रिय नहीं है' : 'Service Worker not active yet', 'error');
        return;
      }
      swRegistration.active.postMessage({
        type: 'SHOW_OFFLINE_NOTIFICATION',
        title: currentLang === 'hi' ? '🔔 ऑफ़लाइन टेस्ट नोटिफिकेशन' : '🔔 Native Offline Notification',
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
// Service Worker for FridgeInventory Web Push & Offline Notifications
const CACHE_NAME = 'fridge-inventory-v2.3';

const OFFLINE_SHELL_ASSETS = [
  '/',
  '/favicon.ico',
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
      icon: event.data.icon || '/favicon.ico',
      badge: event.data.badge || '/favicon.ico',
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
    icon: '/favicon.ico',
    badge: '/favicon.ico',
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
    icon: data.icon || '/favicon.ico',
    badge: data.badge || '/favicon.ico',
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
