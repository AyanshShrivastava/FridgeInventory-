#!/usr/bin/env python3
"""
Smart Rasoi (FridgeInventory) - Production-Ready Single-File FastAPI & Uvicorn Application
========================================================================================
Designed for Indian household food inventory management, zero-waste cooking, and 
automated expiry date tracking. Optimized for smooth zero-downtime deployment on Render.

Features:
- FastAPI & Uvicorn ASGI backend binding to 0.0.0.0:$PORT (default 10000).
- Firebase Firestore initialization supporting FIREBASE_CREDENTIALS_JSON with automatic private key formatting.
- Google GenAI (google-genai SDK) integration with structured JSON output via Pydantic models.
- Graceful offline / in-memory fallback for local development or delayed secret configuration.
- Comprehensive REST APIs: /health, /classify, /rescue, and /api/inventory CRUD endpoints.
- Embedded, responsive single-page frontend (HTML5 + Tailwind CSS + Vanilla JS) with Web Speech voice input.
"""

from __future__ import annotations

import os
import sys
import json
import logging
import re
import uuid
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
logger = logging.getLogger("smart_rasoi")

# ============================================================================
# 1. THIRD-PARTY & SDK IMPORTS (WITH SAFE FALLBACKS)
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
# 2. FIREBASE FIRESTORE INITIALIZATION
# ============================================================================

_firestore_client: Optional[Any] = None
_firestore_init_attempted: bool = False
_firestore_error_msg: str = ""

def init_firestore() -> Tuple[Optional[Any], str]:
    """
    Initialize Firebase Firestore client using environment variables.
    Handles FIREBASE_CREDENTIALS_JSON (raw JSON or file path), GOOGLE_APPLICATION_CREDENTIALS,
    and automatic formatting of escaped private key strings (\\n -> \\n).
    """
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
                # Attempt base64 decode if user passed base64 encoded JSON
                import base64
                try:
                    decoded = base64.b64decode(raw_str).decode("utf-8")
                    cert_dict = json.loads(decoded)
                except Exception:
                    pass

        if cert_dict and isinstance(cert_dict, dict):
            # Format escaped newlines in RSA private key if needed
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

        if os.path.isfile("firebase_credentials.json"):
            cred = credentials.Certificate("firebase_credentials.json")
            firebase_admin.initialize_app(cred)
            _firestore_client = firestore.client()
            logger.info("Firebase Firestore initialized via local firebase_credentials.json.")
            return _firestore_client, ""

        _firestore_error_msg = "FIREBASE_CREDENTIALS_JSON not provided or invalid. Operating in local in-memory storage mode."
        logger.warning(_firestore_error_msg)
        return None, _firestore_error_msg

    except Exception as exc:
        _firestore_error_msg = f"Firebase initialization failed: {exc}"
        logger.exception(_firestore_error_msg)
        return None, _firestore_error_msg


# ============================================================================
# 3. GOOGLE GENAI CLIENT CONFIGURATION
# ============================================================================

_gemini_client: Optional[Any] = None

# Model hierarchy for failover and low-latency responses
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
# 4. PYDANTIC SCHEMAS & DATA MODELS
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
    requires_expiry_date: bool = Field(default=False, description="True for branded, factory-packaged, sealed, or commercial dairy products where the printed label date must be checked; False for fresh produce, vegetables, fruits, or freshly cooked home meals.")
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


class RescueRequest(BaseModel):
    items: List[str] = Field(default_factory=list, description="List of expiring or leftover food ingredients")


class RescueRecipe(BaseModel):
    recipe_name: str = Field(description="Appetizing, authentic Indian recipe title in English")
    recipe_name_hi: str = Field(description="Recipe title in Hindi (Devanagari script)")
    prep_time_minutes: int = Field(default=20, description="Total preparation and cooking time in minutes")
    ingredients_used: List[str] = Field(default_factory=list, description="Expiring ingredients incorporated into the dish")
    pantry_items_needed: List[str] = Field(default_factory=list, description="Common Indian pantry spices/staples needed (e.g. mustard seeds, turmeric, jeera, green chillies, oil)")
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


# ============================================================================
# 5. STORAGE LAYER (FIRESTORE WITH IN-MEMORY BACKUP)
# ============================================================================

_IN_MEMORY_INVENTORY: Dict[str, dict] = {}

def _seed_initial_demo_items():
    """Seed demo items in-memory so the UI is immediately interactive on first spin-up."""
    now = datetime.now(IST).date()
    samples = [
        {
            "id": "demo-1",
            "name_en": "Fresh Paneer",
            "name_hi": "ताज़ा पनीर",
            "category": "dairy",
            "added_date": (now - timedelta(days=2)).isoformat(),
            "expiry_date": (now + timedelta(days=1)).isoformat(),
            "requires_expiry_date": False,
            "storage_tip": "Keep immersed in cold water inside an airtight container; change water daily.",
        },
        {
            "id": "demo-2",
            "name_en": "Spinach (Palak)",
            "name_hi": "पालक",
            "category": "vegetable",
            "added_date": (now - timedelta(days=1)).isoformat(),
            "expiry_date": (now + timedelta(days=2)).isoformat(),
            "requires_expiry_date": False,
            "storage_tip": "Wrap in a dry paper towel or cotton bag. Do not wash until ready to cook.",
        },
        {
            "id": "demo-3",
            "name_en": "Tomatoes",
            "name_hi": "टमाटर",
            "category": "vegetable",
            "added_date": (now - timedelta(days=1)).isoformat(),
            "expiry_date": (now + timedelta(days=5)).isoformat(),
            "requires_expiry_date": False,
            "storage_tip": "Store at cool room temperature stem-down, or refrigerate if fully ripe.",
        },
    ]
    for s in samples:
        _IN_MEMORY_INVENTORY[s["id"]] = s

_seed_initial_demo_items()


def get_all_inventory() -> List[InventoryItem]:
    """Retrieve all inventory items from Firestore or in-memory fallback."""
    db, _ = init_firestore()
    today = datetime.now(IST).date()
    items = []

    if db is not None:
        try:
            docs = db.collection("fridge_inventory").stream()
            for doc in docs:
                d = doc.to_dict()
                d["id"] = doc.id
                items.append(d)
        except Exception as exc:
            logger.error(f"Failed to fetch inventory from Firestore: {exc}")
            items = list(_IN_MEMORY_INVENTORY.values())
    else:
        items = list(_IN_MEMORY_INVENTORY.values())

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

    # Sort: Expired and expiring soon first, then ascending by days left
    results.sort(key=lambda x: x.days_left)
    return results


def save_inventory_item(item_data: InventoryItemCreate) -> InventoryItem:
    """Save an item to Firestore or in-memory fallback."""
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
            db.collection("fridge_inventory").document(doc_id).set(payload)
        except Exception as exc:
            logger.error(f"Error writing item to Firestore: {exc}")
            _IN_MEMORY_INVENTORY[doc_id] = payload
    else:
        _IN_MEMORY_INVENTORY[doc_id] = payload

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


def remove_inventory_item(item_id: str) -> bool:
    """Remove item by ID from Firestore or in-memory storage."""
    db, _ = init_firestore()
    success = False

    if db is not None:
        try:
            db.collection("fridge_inventory").document(item_id).delete()
            success = True
        except Exception as exc:
            logger.error(f"Error deleting item {item_id} from Firestore: {exc}")

    if item_id in _IN_MEMORY_INVENTORY:
        del _IN_MEMORY_INVENTORY[item_id]
        success = True

    return success


# ============================================================================
# 6. LOCAL DATABASE HEURISTIC LOOKUP
# ============================================================================

def lookup_local_database(phrase: str) -> Optional[Tuple[int, str, str, bool]]:
    """
    Check EXPIRY_DATABASE (from database.py) for instantaneous zero-latency local match.
    Returns (shelf_life_days, English_name, Hindi_name, requires_expiry_date) or None.
    """
    if not EXPIRY_DATABASE:
        return None

    clean = phrase.strip().lower()
    if clean in EXPIRY_DATABASE:
        days, en, hi = EXPIRY_DATABASE[clean]
        return days, en, hi, (clean in CHEESE_DATE_REQUIRED_ITEMS)

    # Clean punctuation and check normalized tokens
    norm = re.sub(r"[^a-zA-Z\u0900-\u097f]+", " ", clean).strip()
    if norm in EXPIRY_DATABASE:
        days, en, hi = EXPIRY_DATABASE[norm]
        return days, en, hi, (norm in CHEESE_DATE_REQUIRED_ITEMS)

    # Word-by-word scan for compound inputs (e.g., '1kg palak', 'amul paneer block')
    for word in norm.split():
        if len(word) >= 3 and word in EXPIRY_DATABASE:
            days, en, hi = EXPIRY_DATABASE[word]
            return days, en, hi, (word in CHEESE_DATE_REQUIRED_ITEMS)

    return None


# ============================================================================
# 7. AI CLASSIFICATION & RECIPE RESCUE CORE
# ============================================================================

CLASSIFY_PROMPT_TEMPLATE = """You are an expert culinary AI for an Indian household kitchen and fridge inventory app ("Smart Rasoi").
Analyze the food input phrase: "{query}"

Classification Rules:
1. Spelling & Slang: Correct common speech-to-text slips or Hinglish transliterations (e.g. 'tamatar' -> 'Tomato', 'panir'/'paneer' -> 'Paneer', 'palak' -> 'Spinach', 'dahi' -> 'Curd / Dahi', 'bhindee' -> 'Okra (Bhindi)', 'amul makkhan' -> 'Amul Butter', 'bournvita' -> 'Bournvita', 'chawal' -> 'Cooked Rice', 'aloo' -> 'Potato').
2. is_food check:
   - Return is_food=true ONLY for edible/drinkable food, produce, dairy, meat/fish/poultry, eggs, grains, lentils, flours, spices, snacks, beverages, and cooked meals.
   - Return is_food=false for objects, electronics, medicines, cleaners, cosmetics, people, vehicles, furniture, or non-food words.
3. If is_food is false:
   - category=not_food, shelf_life_days=0, requires_expiry_date=false, name_en="", name_hi="", storage_tip="".
4. Expiry Date Requirement (requires_expiry_date):
   - Set requires_expiry_date=true for factory-packaged, sealed, branded items (e.g. packaged milk cartons, cheese blocks/slices, butter packs, sliced bread, biscuits, ketchup bottles, packaged paneer, canned items, snacks).
   - Set requires_expiry_date=false for loose fresh fruits, fresh vegetables, fresh unsealed bakery items, raw whole grains/pulses, or freshly cooked home meals.
5. Shelf Life:
   - For fresh produce or cooked dishes (requires_expiry_date=false), provide a realistic estimated shelf life in days in an Indian refrigerator (e.g., Spinach: 3 days, Okra: 4 days, Tomatoes: 6 days, Cooked Dal: 3 days, Loose Milk: 2 days).
   - For items with requires_expiry_date=true, set shelf_life_days=0 since the user must enter the printed package date.
6. Provide name_en (English), name_hi (Hindi in Devanagari script), category, and a practical storage_tip for Indian climates.
"""

def classify_food(phrase: str) -> ClassifyResponse:
    """Classify food phrase using Google GenAI with fallback to local database heuristic."""
    clean_phrase = phrase.strip()
    if not clean_phrase:
        raise HTTPException(status_code=400, detail="Food item phrase cannot be empty.")

    client = get_gemini_client()

    # 1. Attempt Gemini classification with structured output
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
                    )

                return ClassifyResponse(
                    is_food=True,
                    name_en=verdict.name_en,
                    name_hi=verdict.name_hi or verdict.name_en,
                    category=verdict.category.value,
                    shelf_life_days=None if verdict.requires_expiry_date else verdict.shelf_life_days,
                    requires_expiry_date=verdict.requires_expiry_date,
                    storage_tip=verdict.storage_tip or "Keep refrigerated or in a cool dry space.",
                )
            except Exception as exc:
                logger.warning(f"Gemini model {model_name} failed on '{clean_phrase}': {exc}")
                continue

    # 2. Local Database match if Gemini failed or is unconfigured
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
            message="Classified via local rasoi database.",
        )

    # 3. Clean fallback heuristic for offline operation
    return ClassifyResponse(
        is_food=True,
        name_en=clean_phrase.title(),
        name_hi=clean_phrase,
        category="vegetable",
        shelf_life_days=4,
        requires_expiry_date=False,
        storage_tip="Refrigerate in a clean container to maximize freshness.",
        message="Classified using default rasoi estimate (Gemini API offline).",
    )


RESCUE_PROMPT_TEMPLATE = """You are an expert Indian home cook and zero-waste kitchen master for 'Smart Rasoi'.
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

    # Resilient fallback Indian rescue dish if Gemini is not available
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
# 8. FASTAPI APPLICATION SETUP
# ============================================================================

app = FastAPI(
    title="Smart Rasoi (FridgeInventory) API",
    description="Intelligent Indian Fridge & Pantry Inventory Management System with Gemini AI",
    version="2.0.0",
)

# Enable CORS for unrestricted frontend and microservice access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================================
# 9. REST API ENDPOINTS
# ============================================================================

@app.get("/health", summary="Health and Service Status Check")
def health_check():
    """Returns application status, time in IST, and configuration flags for Gemini and Firestore."""
    db, db_err = init_firestore()
    gemini_ready = get_gemini_client() is not None
    now_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z")
    
    return {
        "status": "healthy",
        "service": "Smart Rasoi (FridgeInventory)",
        "timestamp_ist": now_ist,
        "gemini_configured": gemini_ready,
        "firebase_configured": db is not None,
        "firestore_status": "connected" if db is not None else db_err,
        "local_database_cached_items": len(EXPIRY_DATABASE),
    }


@app.post("/classify", response_model=ClassifyResponse, summary="Classify Food & Shelf Life")
def api_classify(payload: ClassifyRequest):
    """
    Takes a food phrase (e.g. 'bhindee 500g', 'amul butter', 'palak', 'paneer'),
    returns standardized English and Hindi names, culinary category, shelf life days,
    and whether a printed package expiry date is mandatory.
    """
    query = payload.get_query()
    if not query:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Item phrase cannot be empty. Please provide 'item' or 'phrase'.",
        )
    return classify_food(query)


@app.post("/rescue", response_model=RescueRecipe, summary="Generate Zero-Waste Rescue Recipe")
def api_rescue(payload: RescueRequest):
    """
    Takes a list of expiring item names and generates an authentic, practical
    Indian rescue recipe using Gemini AI to avoid household food waste.
    """
    if not payload.items:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Please provide at least one expiring food item in the 'items' list.",
        )
    return generate_rescue_recipe(payload.items)


@app.get("/api/inventory", response_model=List[InventoryItem], summary="Get All Inventory Items")
def api_get_inventory():
    """Retrieve all tracked items with calculated days left and expiry alert flags."""
    return get_all_inventory()


@app.post("/api/inventory", response_model=InventoryItem, summary="Add Item to Inventory")
def api_add_inventory(item: InventoryItemCreate):
    """Add a verified food item into fridge inventory."""
    return save_inventory_item(item)


@app.delete("/api/inventory/{item_id}", summary="Delete / Consume Inventory Item")
def api_delete_inventory(item_id: str):
    """Remove an item from inventory when consumed or cleared."""
    removed = remove_inventory_item(item_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not found.")
    return {"ok": True, "message": f"Item {item_id} removed successfully."}


# ============================================================================
# 10. EMBEDDED FRONTEND UI (FASTAPI HTMLRESPONSE)
# ============================================================================

HTML_CONTENT = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Smart Rasoi (स्मार्ट रसोई) | FridgeInventory</title>
  <!-- Tailwind CSS CDN -->
  <script src="https://cdn.tailwindcss.com"></script>
  <!-- Google Fonts: Inter, Poppins, Noto Sans Devanagari -->
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Devanagari:wght@400;600;700&family=Poppins:wght@400;500;600;700&display=swap" rel="stylesheet">
  <script>
    tailwind.config = {
      theme: {
        extend: {
          fontFamily: {
            sans: ['Poppins', 'Noto Sans Devanagari', 'sans-serif'],
          },
          colors: {
            saffron: { 50: '#fffbeb', 100: '#fef3c7', 500: '#f59e0b', 600: '#d97706', 700: '#b45309' },
            coriander: { 50: '#ecfdf5', 100: '#d1fae5', 500: '#10b981', 600: '#059669', 700: '#047857' },
            clay: { 800: '#1f2937', 900: '#111827' }
          }
        }
      }
    }
  </script>
  <style>
    .glass { background: rgba(255, 255, 255, 0.95); backdrop-filter: blur(8px); }
    .badge-pulse { animation: pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite; }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .6; } }
  </style>
</head>
<body class="bg-amber-50/40 text-stone-800 min-h-screen flex flex-col antialiased selection:bg-saffron-500 selection:text-white">

  <!-- TOP APP HEADER -->
  <header class="bg-white border-b border-amber-100 shadow-sm sticky top-0 z-40">
    <div class="max-w-6xl mx-auto px-4 py-3 sm:py-4 flex flex-col sm:flex-row items-center justify-between gap-3">
      <div class="flex items-center gap-3">
        <div class="w-11 h-11 rounded-2xl bg-gradient-to-tr from-saffron-600 to-amber-400 flex items-center justify-center text-2xl shadow-md shadow-saffron-500/20 text-white">
          🍲
        </div>
        <div>
          <div class="flex items-center gap-2">
            <h1 class="font-bold text-xl sm:text-2xl tracking-tight text-stone-900">Smart Rasoi</h1>
            <span class="text-xs bg-saffron-100 text-saffron-700 font-semibold px-2 py-0.5 rounded-full">स्मार्ट रसोई</span>
          </div>
          <p class="text-xs text-stone-500">Smart Indian Household Fridge & Zero-Waste Assistant</p>
        </div>
      </div>

      <!-- SYSTEM STATUS PILLS -->
      <div class="flex items-center gap-2 text-xs">
        <div id="gemini-status" class="flex items-center gap-1.5 px-3 py-1 rounded-full bg-stone-100 text-stone-600 font-medium">
          <span class="w-2 h-2 rounded-full bg-stone-400"></span> Gemini AI Checking...
        </div>
        <div id="firebase-status" class="flex items-center gap-1.5 px-3 py-1 rounded-full bg-stone-100 text-stone-600 font-medium">
          <span class="w-2 h-2 rounded-full bg-stone-400"></span> Cloud Sync
        </div>
      </div>
    </div>
  </header>

  <!-- MAIN CONTAINER -->
  <main class="flex-grow max-w-6xl w-full mx-auto px-4 py-6 space-y-6">

    <!-- METRIC STATS STRIP -->
    <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 sm:gap-4">
      <div class="bg-white p-4 rounded-2xl border border-amber-100/80 shadow-sm flex items-center gap-3.5">
        <div class="w-10 h-10 rounded-xl bg-amber-50 text-saffron-600 flex items-center justify-center text-xl">📦</div>
        <div>
          <div class="text-2xl font-bold text-stone-900" id="stat-total">0</div>
          <div class="text-xs font-medium text-stone-500">Total Items</div>
        </div>
      </div>
      <div class="bg-white p-4 rounded-2xl border border-amber-100/80 shadow-sm flex items-center gap-3.5">
        <div class="w-10 h-10 rounded-xl bg-emerald-50 text-coriander-600 flex items-center justify-center text-xl">🥬</div>
        <div>
          <div class="text-2xl font-bold text-coriander-600" id="stat-fresh">0</div>
          <div class="text-xs font-medium text-stone-500">Fresh (3+ Days)</div>
        </div>
      </div>
      <div class="bg-white p-4 rounded-2xl border border-amber-100/80 shadow-sm flex items-center gap-3.5">
        <div class="w-10 h-10 rounded-xl bg-orange-50 text-orange-600 flex items-center justify-center text-xl">⚠️</div>
        <div>
          <div class="text-2xl font-bold text-orange-600" id="stat-urgent">0</div>
          <div class="text-xs font-medium text-stone-500">Expiring Soon</div>
        </div>
      </div>
      <div class="bg-white p-4 rounded-2xl border border-amber-100/80 shadow-sm flex items-center gap-3.5">
        <div class="w-10 h-10 rounded-xl bg-rose-50 text-rose-600 flex items-center justify-center text-xl">🚨</div>
        <div>
          <div class="text-2xl font-bold text-rose-600" id="stat-expired">0</div>
          <div class="text-xs font-medium text-stone-500">Expired</div>
        </div>
      </div>
    </div>

    <!-- QUICK ADD FOOD ITEM BANNER -->
    <section class="bg-white rounded-3xl p-5 sm:p-6 border border-amber-200/60 shadow-md shadow-saffron-500/5 relative overflow-hidden">
      <div class="max-w-2xl">
        <h2 class="text-lg font-bold text-stone-900 flex items-center gap-2">
          <span>✨ Add Item by Voice or Text</span>
          <span class="text-xs font-normal text-stone-500">(Auto-calculates shelf life or asks for package date)</span>
        </h2>
        <p class="text-xs text-stone-500 mt-1 mb-4">
          Type or speak anything: <i>"bhindee 500g", "amul paneer", "palak", "dahi", "fresh milk"</i>
        </p>
      </div>

      <form id="add-form" class="flex flex-col sm:flex-row gap-2.5">
        <div class="relative flex-grow">
          <input 
            type="text" 
            id="item-input" 
            placeholder="Enter food or grocery name (e.g. Bhindi, Amul Cheese, Palak)..." 
            class="w-full pl-4 pr-12 py-3 rounded-2xl border border-stone-200 focus:outline-none focus:ring-2 focus:ring-saffron-500 focus:border-transparent text-sm text-stone-800 bg-stone-50/50"
            required
          />
          <button 
            type="button" 
            id="mic-btn" 
            title="Speak Food Name (Hindi/English)"
            class="absolute right-2 top-2 p-1.5 rounded-xl text-stone-400 hover:text-saffron-600 hover:bg-stone-100 transition-colors"
          >
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 003-3V5a3 3 0 00-6 0v6a3 3 0 003 3z"></path>
            </svg>
          </button>
        </div>
        <button 
          type="submit" 
          id="classify-btn"
          class="px-6 py-3 bg-gradient-to-r from-saffron-600 to-amber-500 hover:from-saffron-700 hover:to-amber-600 text-white font-semibold rounded-2xl shadow-sm text-sm flex items-center justify-center gap-2 transition-all transform active:scale-95"
        >
          <span>Classify & Add</span>
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>
        </button>
      </form>

      <!-- QUICK SUGGESTION CHIPS -->
      <div class="mt-3 flex items-center gap-1.5 flex-wrap text-xs text-stone-500">
        <span class="font-medium">Quick Suggestions:</span>
        <button type="button" class="chip hover:bg-amber-100 bg-amber-50 text-amber-800 px-2.5 py-1 rounded-lg">🥬 पालक (Palak)</button>
        <button type="button" class="chip hover:bg-amber-100 bg-amber-50 text-amber-800 px-2.5 py-1 rounded-lg">🧀 पनीर (Paneer)</button>
        <button type="button" class="chip hover:bg-amber-100 bg-amber-50 text-amber-800 px-2.5 py-1 rounded-lg">🍅 टमाटर (Tomato)</button>
        <button type="button" class="chip hover:bg-amber-100 bg-amber-50 text-amber-800 px-2.5 py-1 rounded-lg">🥛 दही (Curd)</button>
        <button type="button" class="chip hover:bg-amber-100 bg-amber-50 text-amber-800 px-2.5 py-1 rounded-lg">🍞 ब्रेड (Bread)</button>
        <button type="button" class="chip hover:bg-amber-100 bg-amber-50 text-amber-800 px-2.5 py-1 rounded-lg">🧈 मक्खन (Butter)</button>
      </div>
    </section>

    <!-- TAB NAVIGATION BAR -->
    <div class="flex items-center justify-between border-b border-stone-200">
      <nav class="flex space-x-6 text-sm font-semibold">
        <button id="tab-inventory-btn" class="tab-btn py-3 border-b-2 border-saffron-600 text-saffron-700 flex items-center gap-2">
          <span>📦 Fridge Inventory</span>
          <span id="tab-inventory-count" class="bg-saffron-100 text-saffron-700 text-xs px-2 py-0.5 rounded-full font-bold">0</span>
        </button>
        <button id="tab-rescue-btn" class="tab-btn py-3 border-b-2 border-transparent text-stone-500 hover:text-stone-800 flex items-center gap-2">
          <span>🍲 Rasoi Rescue Chef</span>
          <span class="bg-orange-100 text-orange-700 text-xs px-2 py-0.5 rounded-full font-bold">AI Zero-Waste</span>
        </button>
        <button id="tab-test-btn" class="tab-btn py-3 border-b-2 border-transparent text-stone-500 hover:text-stone-800 flex items-center gap-2">
          <span>🔍 AI Food Scanner</span>
        </button>
      </nav>

      <!-- INVENTORY FILTER PILLS -->
      <div id="filter-controls" class="hidden sm:flex items-center gap-1.5 text-xs">
        <button class="filter-btn active bg-stone-900 text-white px-3 py-1 rounded-full font-medium" data-filter="all">All</button>
        <button class="filter-btn bg-stone-100 text-stone-600 hover:bg-stone-200 px-3 py-1 rounded-full font-medium" data-filter="urgent">⚠️ Urgent</button>
        <button class="filter-btn bg-stone-100 text-stone-600 hover:bg-stone-200 px-3 py-1 rounded-full font-medium" data-filter="fresh">🟢 Fresh</button>
        <button class="filter-btn bg-stone-100 text-stone-600 hover:bg-stone-200 px-3 py-1 rounded-full font-medium" data-filter="expired">🚨 Expired</button>
      </div>
    </div>

    <!-- TAB 1: INVENTORY SECTION -->
    <section id="section-inventory" class="tab-section space-y-4">
      <!-- MULTI-SELECT RESCUE BAR -->
      <div id="rescue-action-bar" class="hidden bg-gradient-to-r from-orange-500 to-saffron-600 text-white p-3 sm:p-4 rounded-2xl flex flex-col sm:flex-row items-center justify-between gap-3 shadow-lg shadow-orange-500/20">
        <div class="flex items-center gap-2 text-sm font-medium">
          <span class="text-xl">🧑‍🍳</span>
          <span>Selected <b id="selected-count">0</b> expiring items for recipe rescue</span>
        </div>
        <button id="trigger-rescue-btn" class="px-5 py-2 bg-white text-orange-700 hover:bg-orange-50 font-bold rounded-xl text-xs sm:text-sm shadow transition transform active:scale-95">
          Generate Indian Rescue Recipe ✨
        </button>
      </div>

      <!-- INVENTORY GRID -->
      <div id="inventory-grid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        <!-- Rendered dynamically by JavaScript -->
      </div>

      <!-- EMPTY STATE -->
      <div id="empty-state" class="hidden text-center py-16 bg-white rounded-3xl border border-dashed border-stone-200 p-6">
        <div class="text-5xl mb-3">🧺</div>
        <h3 class="text-base font-bold text-stone-800">Your Fridge is Empty!</h3>
        <p class="text-xs text-stone-500 mt-1 max-w-sm mx-auto">
          Add fresh vegetables, dairy, or groceries using the input above or your microphone.
        </p>
      </div>
    </section>

    <!-- TAB 2: RASOI RESCUE RECIPE SECTION -->
    <section id="section-rescue" class="tab-section hidden space-y-6">
      <div class="bg-white rounded-3xl p-6 border border-amber-200/70 shadow-sm">
        <div class="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 border-b border-stone-100 pb-5">
          <div>
            <h2 class="text-xl font-bold text-stone-900 flex items-center gap-2">
              <span>🍲 Rasoi Rescue Chef</span>
              <span class="text-xs font-normal bg-orange-100 text-orange-700 px-2.5 py-0.5 rounded-full font-semibold">Zero Food Waste</span>
            </h2>
            <p class="text-xs text-stone-500 mt-1">
              Select expiring ingredients from your fridge or type custom leftovers to create an authentic Indian meal.
            </p>
          </div>
          <button id="generate-recipe-btn" class="px-6 py-2.5 bg-gradient-to-r from-coriander-600 to-emerald-500 hover:from-coriander-700 hover:to-emerald-600 text-white font-semibold rounded-xl text-sm shadow transition flex items-center gap-2">
            <span>Rescue My Expiring Items</span>
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg>
          </button>
        </div>

        <!-- CUSTOM INGREDIENTS INPUT -->
        <div class="mt-4">
          <label class="block text-xs font-semibold text-stone-700 mb-1">Custom Ingredients / Leftovers to Rescue (Comma-separated):</label>
          <input 
            type="text" 
            id="rescue-items-input" 
            placeholder="e.g. leftover boiled potatoes, sour curd, coriander stems, capsicum..."
            class="w-full px-4 py-2.5 rounded-xl border border-stone-200 text-sm focus:outline-none focus:ring-2 focus:ring-saffron-500 bg-stone-50/50"
          />
        </div>

        <!-- RECIPE RESULT CARD -->
        <div id="recipe-container" class="mt-6 hidden space-y-6">
          <div class="bg-amber-50/50 border border-amber-200/80 rounded-2xl p-6">
            <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-amber-200/60 pb-4">
              <div>
                <span class="text-xs font-bold text-saffron-700 uppercase tracking-wider">AI Rescue Recipe</span>
                <h3 id="recipe-name" class="text-2xl font-bold text-stone-900"></h3>
                <h4 id="recipe-name-hi" class="text-lg font-semibold text-saffron-700 font-sans"></h4>
              </div>
              <div class="flex items-center gap-2">
                <span id="recipe-time" class="bg-white border border-amber-200 text-stone-700 text-xs font-semibold px-3 py-1.5 rounded-full flex items-center gap-1">
                  ⏱️ 20 mins
                </span>
                <button id="copy-recipe-btn" class="p-2 bg-white hover:bg-stone-50 border border-amber-200 rounded-xl text-stone-600 text-xs font-medium" title="Copy Recipe">
                  📋 Copy
                </button>
              </div>
            </div>

            <!-- INGREDIENTS DUAL LIST -->
            <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mt-4">
              <div class="bg-white p-4 rounded-xl border border-amber-100">
                <h5 class="text-xs font-bold text-stone-700 uppercase mb-2 flex items-center gap-1.5">
                  <span class="text-coriander-600">✔</span> Rescued Ingredients
                </h5>
                <ul id="recipe-rescued-list" class="text-xs text-stone-600 space-y-1.5 list-disc list-inside"></ul>
              </div>
              <div class="bg-white p-4 rounded-xl border border-amber-100">
                <h5 class="text-xs font-bold text-stone-700 uppercase mb-2 flex items-center gap-1.5">
                  <span class="text-saffron-600">🧂</span> Pantry Spices & Staples
                </h5>
                <ul id="recipe-pantry-list" class="text-xs text-stone-600 space-y-1.5 list-disc list-inside"></ul>
              </div>
            </div>

            <!-- STEP BY STEP -->
            <div class="mt-5">
              <h5 class="text-xs font-bold text-stone-800 uppercase tracking-wider mb-3">Cooking Instructions (विधि):</h5>
              <ol id="recipe-steps-list" class="space-y-3 text-xs sm:text-sm text-stone-700 leading-relaxed"></ol>
            </div>

            <!-- CHEF TIP -->
            <div class="mt-5 p-3.5 bg-saffron-100/60 rounded-xl border border-saffron-200/80 flex items-start gap-3">
              <span class="text-xl">💡</span>
              <div>
                <h6 class="text-xs font-bold text-saffron-900">दादी माँ का नुस्खा (Zero-Waste Chef Tip):</h6>
                <p id="recipe-tip" class="text-xs text-saffron-800 mt-0.5"></p>
              </div>
            </div>
          </div>
        </div>

        <!-- RECIPE LOADING SKELETON -->
        <div id="recipe-loader" class="hidden text-center py-12">
          <div class="inline-block animate-spin text-4xl mb-3">🍲</div>
          <p class="text-sm font-semibold text-stone-800">Consulting Indian Culinary Intelligence...</p>
          <p class="text-xs text-stone-500 mt-1">Crafting a zero-waste homestyle Indian recipe with your ingredients.</p>
        </div>
      </div>
    </section>

    <!-- TAB 3: STANDALONE FOOD SCANNER SECTION -->
    <section id="section-test" class="tab-section hidden space-y-4">
      <div class="bg-white rounded-3xl p-6 border border-stone-200 shadow-sm max-w-2xl mx-auto">
        <h2 class="text-lg font-bold text-stone-900 mb-1">🔍 AI Food Inspector & Storage Advisor</h2>
        <p class="text-xs text-stone-500 mb-4">
          Test any Indian food name, brand, or dish to inspect its culinary category, Hindi name, standard shelf life, and whether package expiry is mandatory.
        </p>

        <div class="flex gap-2">
          <input 
            type="text" 
            id="scanner-input" 
            placeholder="Type any food (e.g. Gajar, Haldiram Bhujia, Amul Butter, Cooked Dal)..." 
            class="flex-grow px-4 py-2.5 rounded-xl border border-stone-200 text-sm focus:outline-none focus:ring-2 focus:ring-saffron-500"
          />
          <button 
            type="button" 
            id="scanner-btn" 
            class="px-5 py-2.5 bg-stone-900 hover:bg-stone-800 text-white font-semibold rounded-xl text-sm transition"
          >
            Inspect
          </button>
        </div>

        <div id="scanner-result" class="mt-5 hidden">
          <!-- Populated by JavaScript -->
        </div>
      </div>
    </section>

  </main>

  <!-- CLASSIFICATION VERIFICATION MODAL -->
  <div id="classify-modal" class="fixed inset-0 bg-stone-900/40 backdrop-blur-sm z-50 flex items-center justify-center p-4 hidden">
    <div class="bg-white rounded-3xl max-w-md w-full p-6 shadow-2xl border border-stone-100 transform transition-all">
      <div class="flex items-center justify-between border-b border-stone-100 pb-3">
        <h3 class="font-bold text-stone-900 text-base flex items-center gap-2">
          <span>✨ Confirm Rasoi Entry</span>
        </h3>
        <button id="close-modal-btn" class="text-stone-400 hover:text-stone-600 text-xl font-bold">&times;</button>
      </div>

      <div class="mt-4 space-y-4">
        <!-- AI RESULT SUMMARY -->
        <div class="p-3.5 bg-amber-50/70 rounded-2xl border border-amber-200/50 flex items-center gap-3">
          <div id="modal-category-icon" class="text-3xl">🥬</div>
          <div class="flex-grow">
            <h4 id="modal-name-en" class="font-bold text-stone-900 text-base leading-tight"></h4>
            <div id="modal-name-hi" class="text-xs font-semibold text-saffron-700"></div>
            <span id="modal-category-badge" class="inline-block mt-1 text-[11px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-md bg-white border border-amber-200 text-stone-700"></span>
          </div>
        </div>

        <!-- SPECIAL ALERT FOR PACKAGED ITEMS -->
        <div id="modal-packaged-alert" class="p-3 bg-amber-100/70 rounded-xl border border-amber-300/80 text-xs text-amber-900 hidden">
          <div class="font-bold flex items-center gap-1.5 mb-1">
            <span>🏷️ Printed Expiry Required</span>
          </div>
          This is a packaged, branded, or dairy item. Enter the expiry date printed on the package label.
        </div>

        <!-- EXPIRY DATE SELECTION -->
        <div>
          <label class="block text-xs font-semibold text-stone-700 mb-1" id="modal-date-label">
            Calculated Expiry Date:
          </label>
          <input 
            type="date" 
            id="modal-date-input" 
            class="w-full px-4 py-2.5 rounded-xl border border-stone-200 text-sm focus:outline-none focus:ring-2 focus:ring-saffron-500 bg-stone-50"
          />
          <p id="modal-date-help" class="text-[11px] text-stone-500 mt-1"></p>
        </div>

        <!-- STORAGE TIP -->
        <div class="p-3 bg-coriander-50/70 rounded-xl border border-coriander-100 text-xs text-coriander-900">
          <div class="font-bold mb-0.5">🌿 Kitchen Storage Tip:</div>
          <div id="modal-storage-tip"></div>
        </div>

        <!-- ACTIONS -->
        <div class="flex items-center gap-2 pt-2">
          <button id="modal-cancel-btn" class="w-1/2 py-2.5 rounded-xl border border-stone-200 text-stone-600 text-sm font-semibold hover:bg-stone-50">
            Cancel
          </button>
          <button id="modal-confirm-btn" class="w-1/2 py-2.5 rounded-xl bg-gradient-to-r from-saffron-600 to-amber-500 text-white text-sm font-semibold hover:from-saffron-700 shadow-sm">
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
    // State Management
    let inventory = [];
    let currentFilter = 'all';
    let selectedItemsForRescue = new Set();
    let pendingModalItem = null;

    const categoryIcons = {
      fruit: '🍎',
      vegetable: '🥬',
      dairy: '🥛',
      meat_fish_egg: '🍗',
      grain_pulse_flour: '🌾',
      spice_condiment: '🧂',
      beverage: '🧃',
      health_drink_powder: '☕',
      packaged_grocery: '🥫',
      snack_bakery: '🍞',
      oil_sugar_fat: '🛢️',
      cooked_dish: '🍛',
      not_food: '📦'
    };

    // DOM Elements
    const addForm = document.getElementById('add-form');
    const itemInput = document.getElementById('item-input');
    const micBtn = document.getElementById('mic-btn');
    const classifyBtn = document.getElementById('classify-btn');
    const inventoryGrid = document.getElementById('inventory-grid');
    const emptyState = document.getElementById('empty-state');
    const toastContainer = document.getElementById('toast-container');
    const rescueActionBar = document.getElementById('rescue-action-bar');
    const selectedCountEl = document.getElementById('selected-count');

    // Toasts
    function showToast(message, type = 'info') {
      const el = document.createElement('div');
      el.className = `pointer-events-auto px-4 py-2.5 rounded-xl shadow-lg text-xs font-semibold flex items-center gap-2 transition-all transform translate-y-2 opacity-0 ${
        type === 'success' ? 'bg-coriander-600 text-white' :
        type === 'error' ? 'bg-rose-600 text-white' :
        type === 'warning' ? 'bg-orange-600 text-white' : 'bg-stone-800 text-white'
      }`;
      el.innerHTML = `<span>${type === 'success' ? '✔' : type === 'error' ? '✖' : 'ℹ'}</span> <span>${message}</span>`;
      toastContainer.appendChild(el);
      requestAnimationFrame(() => {
        el.classList.remove('translate-y-2', 'opacity-0');
      });
      setTimeout(() => {
        el.classList.add('opacity-0', 'translate-y-2');
        setTimeout(() => el.remove(), 300);
      }, 3500);
    }

    // Health Status Check
    async function checkHealth() {
      try {
        const res = await fetch('/health');
        if (!res.ok) throw new Error('Health check error');
        const data = await res.json();
        
        const gEl = document.getElementById('gemini-status');
        if (data.gemini_configured) {
          gEl.className = 'flex items-center gap-1.5 px-3 py-1 rounded-full bg-emerald-100 text-emerald-800 font-medium';
          gEl.innerHTML = '<span class="w-2 h-2 rounded-full bg-emerald-500 badge-pulse"></span> Gemini Active';
        } else {
          gEl.className = 'flex items-center gap-1.5 px-3 py-1 rounded-full bg-amber-100 text-amber-800 font-medium';
          gEl.innerHTML = '<span class="w-2 h-2 rounded-full bg-amber-500"></span> Gemini Offline';
        }

        const fEl = document.getElementById('firebase-status');
        if (data.firebase_configured) {
          fEl.className = 'flex items-center gap-1.5 px-3 py-1 rounded-full bg-emerald-100 text-emerald-800 font-medium';
          fEl.innerHTML = '<span class="w-2 h-2 rounded-full bg-emerald-500"></span> Firestore Sync';
        } else {
          fEl.className = 'flex items-center gap-1.5 px-3 py-1 rounded-full bg-stone-100 text-stone-600 font-medium';
          fEl.innerHTML = '<span class="w-2 h-2 rounded-full bg-stone-400"></span> Local Memory';
        }
      } catch (e) {
        console.warn('Could not contact /health:', e);
      }
    }

    // Load Inventory Items
    async function loadInventory() {
      try {
        const res = await fetch('/api/inventory');
        if (!res.ok) throw new Error('Failed to load inventory');
        inventory = await res.json();
        renderInventory();
        updateStats();
      } catch (err) {
        showToast('Error loading fridge inventory', 'error');
      }
    }

    // Update Header Metric Statistics
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

    // Render Inventory Cards
    function renderInventory() {
      inventoryGrid.innerHTML = '';
      
      const filtered = inventory.filter(item => {
        if (currentFilter === 'urgent') return item.days_left >= 0 && item.days_left <= 2;
        if (currentFilter === 'fresh') return item.days_left >= 3;
        if (currentFilter === 'expired') return item.days_left < 0;
        return true;
      });

      if (filtered.length === 0) {
        emptyState.classList.remove('hidden');
      } else {
        emptyState.classList.add('hidden');
      }

      filtered.forEach(item => {
        const icon = categoryIcons[item.category] || '🥬';
        let badgeClass = 'bg-coriander-50 text-coriander-700 border-coriander-200';
        let statusText = `${item.days_left} days left`;
        
        if (item.days_left < 0) {
          badgeClass = 'bg-rose-50 text-rose-700 border-rose-200';
          statusText = `Expired ${Math.abs(item.days_left)}d ago`;
        } else if (item.days_left === 0) {
          badgeClass = 'bg-orange-100 text-orange-800 border-orange-300 font-bold';
          statusText = 'Expires Today!';
        } else if (item.days_left <= 2) {
          badgeClass = 'bg-orange-50 text-orange-700 border-orange-200 font-semibold';
          statusText = `Urgent: ${item.days_left}d left`;
        }

        const isChecked = selectedItemsForRescue.has(item.name_en);

        const card = document.createElement('div');
        card.className = `bg-white rounded-2xl p-4 border border-stone-200/80 shadow-sm hover:shadow-md transition-all flex flex-col justify-between ${
          item.days_left < 0 ? 'opacity-85' : ''
        }`;
        card.innerHTML = `
          <div>
            <div class="flex items-start justify-between gap-2">
              <div class="flex items-center gap-2.5">
                <span class="text-2xl">${icon}</span>
                <div>
                  <h4 class="font-bold text-stone-900 text-sm leading-tight">${item.name_en}</h4>
                  <p class="text-xs font-semibold text-saffron-700 font-sans">${item.name_hi || ''}</p>
                </div>
              </div>
              <span class="text-[11px] px-2 py-0.5 rounded-full border ${badgeClass}">
                ${statusText}
              </span>
            </div>

            <div class="mt-3 text-[11px] text-stone-500 space-y-1">
              <div>Added: <span class="font-medium text-stone-700">${item.added_date}</span> | Expiry: <span class="font-medium text-stone-700">${item.expiry_date}</span></div>
              ${item.storage_tip ? `<div class="text-stone-600 bg-stone-50 p-2 rounded-lg border border-stone-100 mt-2">💡 ${item.storage_tip}</div>` : ''}
            </div>
          </div>

          <div class="mt-4 pt-3 border-t border-stone-100 flex items-center justify-between">
            <label class="flex items-center gap-1.5 text-xs text-stone-600 cursor-pointer select-none">
              <input type="checkbox" class="rescue-check rounded text-saffron-600 focus:ring-saffron-500" data-name="${item.name_en}" ${isChecked ? 'checked' : ''}>
              <span>Rescue Chef</span>
            </label>
            <button class="delete-btn text-xs text-stone-400 hover:text-rose-600 font-medium transition" data-id="${item.id}">
              Consume / Clear
            </button>
          </div>
        `;
        inventoryGrid.appendChild(card);
      });

      // Bind dynamic actions
      document.querySelectorAll('.rescue-check').forEach(box => {
        box.addEventListener('change', (e) => {
          const name = e.target.dataset.name;
          if (e.target.checked) {
            selectedItemsForRescue.add(name);
          } else {
            selectedItemsForRescue.delete(name);
          }
          updateRescueBar();
        });
      });

      document.querySelectorAll('.delete-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
          const id = e.target.dataset.id;
          await deleteItem(id);
        });
      });
    }

    function updateRescueBar() {
      const count = selectedItemsForRescue.size;
      selectedCountEl.textContent = count;
      if (count > 0) {
        rescueActionBar.classList.remove('hidden');
      } else {
        rescueActionBar.classList.add('hidden');
      }
    }

    async function deleteItem(id) {
      try {
        const res = await fetch(`/api/inventory/${id}`, { method: 'DELETE' });
        if (!res.ok) throw new Error('Delete failed');
        inventory = inventory.filter(i => i.id !== id);
        renderInventory();
        updateStats();
        showToast('Item cleared from fridge', 'info');
      } catch (err) {
        showToast('Failed to delete item', 'error');
      }
    }

    // Modal Handling
    const modal = document.getElementById('classify-modal');
    const modalNameEn = document.getElementById('modal-name-en');
    const modalNameHi = document.getElementById('modal-name-hi');
    const modalCatBadge = document.getElementById('modal-category-badge');
    const modalCatIcon = document.getElementById('modal-category-icon');
    const modalPackagedAlert = document.getElementById('modal-packaged-alert');
    const modalDateInput = document.getElementById('modal-date-input');
    const modalDateLabel = document.getElementById('modal-date-label');
    const modalDateHelp = document.getElementById('modal-date-help');
    const modalStorageTip = document.getElementById('modal-storage-tip');

    function openModalWithClassification(result, rawPhrase) {
      pendingModalItem = result;
      modalNameEn.textContent = result.name_en;
      modalNameHi.textContent = result.name_hi;
      modalCatBadge.textContent = result.category.replace(/_/g, ' ');
      modalCatIcon.textContent = categoryIcons[result.category] || '🥬';
      modalStorageTip.textContent = result.storage_tip || 'Keep in a cool dry spot or refrigerated.';

      const today = new Date();
      let defaultDate = new Date();

      if (result.requires_expiry_date) {
        modalPackagedAlert.classList.remove('hidden');
        modalDateLabel.textContent = 'Package Printed Expiry Date (DD/MM/YYYY):';
        modalDateHelp.textContent = 'Check the label on the box/packet and select the printed date.';
        // Default to +7 days for convenience if user has not set
        defaultDate.setDate(today.getDate() + 7);
      } else {
        modalPackagedAlert.classList.add('hidden');
        modalDateLabel.textContent = 'AI Estimated Shelf Life:';
        const days = (result.shelf_life_days && result.shelf_life_days > 0) ? result.shelf_life_days : 4;
        defaultDate.setDate(today.getDate() + days);
        modalDateHelp.textContent = `Estimated ${days} days based on Indian kitchen storage conditions.`;
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

        const res = await fetch('/api/inventory', {
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
        itemInput.value = '';
        showToast(`Added ${saved.name_en} to fridge!`, 'success');
      } catch (err) {
        showToast('Error saving item to inventory', 'error');
      }
    });

    // Form Submission: Classify
    addForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const phrase = itemInput.value.trim();
      if (!phrase) return;

      classifyBtn.disabled = true;
      classifyBtn.innerHTML = '<span>Checking...</span>';

      try {
        const res = await fetch('/classify', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ item: phrase })
        });

        if (!res.ok) {
          const errData = await res.json();
          throw new Error(errData.detail || 'Classification failed');
        }

        const data = await res.json();
        if (!data.is_food) {
          showToast(data.message || 'This does not look like food or groceries.', 'warning');
        } else {
          openModalWithClassification(data, phrase);
        }
      } catch (err) {
        showToast(err.message, 'error');
      } finally {
        classifyBtn.disabled = false;
        classifyBtn.innerHTML = '<span>Classify & Add</span><svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>';
      }
    });

    // Web Speech API Voice Input
    if ('webkitSpeechRecognition' in window || 'SpeechRecognition' in window) {
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      const recognition = new SpeechRecognition();
      recognition.continuous = false;
      recognition.interimResults = false;
      recognition.lang = 'en-IN';

      micBtn.addEventListener('click', () => {
        try {
          recognition.start();
          micBtn.classList.add('text-rose-600', 'animate-pulse');
          showToast('Listening in Hindi/English... Speak food name now', 'info');
        } catch (err) {
          recognition.stop();
        }
      });

      recognition.onresult = (e) => {
        const transcript = e.results[0][0].transcript;
        itemInput.value = transcript;
        micBtn.classList.remove('text-rose-600', 'animate-pulse');
        showToast(`Heard: "${transcript}"`, 'success');
        addForm.dispatchEvent(new Event('submit'));
      };

      recognition.onerror = () => {
        micBtn.classList.remove('text-rose-600', 'animate-pulse');
      };

      recognition.onend = () => {
        micBtn.classList.remove('text-rose-600', 'animate-pulse');
      };
    } else {
      micBtn.style.display = 'none';
    }

    // Quick Chips
    document.querySelectorAll('.chip').forEach(chip => {
      chip.addEventListener('click', () => {
        const text = chip.textContent.split(' ')[1] || chip.textContent;
        itemInput.value = text;
        addForm.dispatchEvent(new Event('submit'));
      });
    });

    // Tab Switching
    const tabInvBtn = document.getElementById('tab-inventory-btn');
    const tabRescBtn = document.getElementById('tab-rescue-btn');
    const tabTestBtn = document.getElementById('tab-test-btn');
    const secInv = document.getElementById('section-inventory');
    const secResc = document.getElementById('section-rescue');
    const secTest = document.getElementById('section-test');
    const filterControls = document.getElementById('filter-controls');

    function switchTab(activeBtn, activeSec) {
      [tabInvBtn, tabRescBtn, tabTestBtn].forEach(b => {
        b.className = 'tab-btn py-3 border-b-2 border-transparent text-stone-500 hover:text-stone-800 flex items-center gap-2';
      });
      [secInv, secResc, secTest].forEach(s => s.classList.add('hidden'));

      activeBtn.className = 'tab-btn py-3 border-b-2 border-saffron-600 text-saffron-700 flex items-center gap-2 font-bold';
      activeSec.classList.remove('hidden');

      if (activeSec === secInv) {
        filterControls.classList.remove('hidden');
      } else {
        filterControls.classList.add('hidden');
      }
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

    // Filter Buttons
    document.querySelectorAll('.filter-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        document.querySelectorAll('.filter-btn').forEach(b => {
          b.className = 'filter-btn bg-stone-100 text-stone-600 hover:bg-stone-200 px-3 py-1 rounded-full font-medium';
        });
        e.target.className = 'filter-btn active bg-stone-900 text-white px-3 py-1 rounded-full font-medium';
        currentFilter = e.target.dataset.filter;
        renderInventory();
      });
    });

    // Rasoi Rescue Generation
    const generateRecipeBtn = document.getElementById('generate-recipe-btn');
    const rescueInput = document.getElementById('rescue-items-input');
    const recipeContainer = document.getElementById('recipe-container');
    const recipeLoader = document.getElementById('recipe-loader');

    async function triggerRecipeGeneration(itemsList) {
      if (!itemsList || itemsList.length === 0) {
        showToast('Please select or specify at least one expiring ingredient', 'warning');
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
        li.innerHTML = `<span class="w-5 h-5 rounded-full bg-saffron-500 text-white flex items-center justify-center text-xs font-bold shrink-0 mt-0.5">${idx + 1}</span><span>${step}</span>`;
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
        // Collect all items expiring within 2 days automatically
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
        showToast('Recipe title and tips copied to clipboard!', 'success');
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
            <div class="p-4 bg-rose-50 border border-rose-200 rounded-2xl text-rose-800 text-xs">
              <b>Not a recognized food item:</b> ${data.message || 'Please check spelling.'}
            </div>
          `;
        } else {
          scannerResult.innerHTML = `
            <div class="p-5 bg-amber-50/70 border border-amber-200 rounded-2xl space-y-3">
              <div class="flex items-center justify-between">
                <div>
                  <h4 class="font-bold text-stone-900 text-base">${data.name_en}</h4>
                  <div class="text-xs font-semibold text-saffron-700">${data.name_hi}</div>
                </div>
                <span class="text-xs font-bold px-2.5 py-1 rounded-lg bg-white border border-amber-200 text-stone-700 uppercase">
                  ${data.category.replace(/_/g, ' ')}
                </span>
              </div>
              <div class="text-xs text-stone-600 space-y-1">
                <div><b>Printed Expiry Date Required:</b> ${data.requires_expiry_date ? '<span class="text-rose-600 font-bold">YES (Packaged/Branded)</span>' : '<span class="text-coriander-600 font-bold">NO (Fresh produce/cooked)</span>'}</div>
                <div><b>Estimated Shelf Life:</b> ${data.requires_expiry_date ? 'Check package date' : (data.shelf_life_days + ' Days in Fridge')}</div>
              </div>
              <div class="p-3 bg-white rounded-xl border border-amber-100 text-xs text-stone-700">
                <b>Storage Advice:</b> ${data.storage_tip || 'Refrigerate in a clean container.'}
              </div>
            </div>
          `;
        }
      } catch (err) {
        showToast('Inspection failed', 'error');
      } finally {
        scannerBtn.disabled = false;
        scannerBtn.textContent = 'Inspect';
      }
    });

    // Initialize on page load
    window.addEventListener('DOMContentLoaded', () => {
      checkHealth();
      loadInventory();
    });
  </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse, summary="Serve Smart Rasoi Frontend UI")
def serve_ui():
    """Returns the single-page application dashboard for Smart Rasoi."""
    return HTMLResponse(content=HTML_CONTENT, status_code=200)


# ============================================================================
# 11. PRODUCTION EXECUTION ENTRYPOINT
# ============================================================================

if __name__ == "__main__":
    # Render assigns the listening port via the $PORT environment variable (default 10000)
    server_port = int(os.getenv("PORT", 10000))
    server_host = "0.0.0.0"

    logger.info("=" * 70)
    logger.info("Starting Smart Rasoi (FridgeInventory) Production Server")
    logger.info(f"Binding strictly to http://{server_host}:{server_port}")
    logger.info(f"Target Environment: Render / Production ASGI")
    logger.info("=" * 70)

    # Initialize Firestore connection on startup
    init_firestore()

    # Run production Uvicorn server
    uvicorn.run(
        app,
        host=server_host,
        port=server_port,
        log_level="info",
        access_log=True,
    )
