import json
import os
import google.generativeai as genai
import streamlit as st

# Force the SDK to bypass Google Cloud Vertex AI and use standard Google AI Studio
os.environ.pop("GOOGLE_GENAI_USE_VERTEXAI", None)
os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)

# Configure Gemini API key safely from st.secrets
if "google" in st.secrets and "api_key" in st.secrets["google"]:
  api_key = st.secrets["google"]["api_key"]
  genai.configure(api_key=api_key)


def get_ai_expiry_and_names(item_query):
  """Queries the top-tier Gemini model to find exact shelf life based on

  trusted food safety standards and bilingual names.
  Returns days = -1 if the input is not a food item.
  """
  try:
    model = genai.GenerativeModel("gemini-3.8-flash")
    prompt = f"""
        You are a strict food safety and inventory management AI for Indian kitchens, cross-referencing trusted standards (such as FSSAI guidelines and standard culinary preservation data). Analyze the user query: "{item_query}".
        1. Determine if this item is a FOOD item, vegetable, fruit, dairy, spice, beverage, or kitchen grocery ingredient.
        2. If it is NOT a food item (e.g., a building, vehicle, electronics, clothes, or random non-edible objects like a palace), you MUST return JSON with "days": -1.
        3. If it IS a food item, provide its exact typical shelf life in days based on trusted storage standards in an Indian kitchen, along with its clean English name and Hindi name in Devanagari script.
        
        Return ONLY a valid JSON object with these exact keys:
        {{"days": <integer number of days, or -1 if not a food item>, "en": "<English name>", "hi": "<Hindi name in Devanagari>"}}
        """
    response = model.generate_content(prompt)
    text = response.text.strip()

    if text.startswith("```json"):
      text = text[7:-3].strip()
    elif text.startswith("```"):
      text = text[3:-3].strip()

    data = json.loads(text)
    return int(data.get("days", -1)), data.get("en", ""), data.get("hi", "")
  except Exception as e:
    return 7, item_query, item_query


def generate_rescue_recipe(expiring_soon_items):
  """Generates an anti-waste rescue recipe using the top-tier Gemini model."""
  try:
    model = genai.GenerativeModel("gemini-3.8-flash")
    prompt = f"""
        You are an expert Indian chef and food-waste reduction specialist. 
        The user has these specific kitchen ingredients that are ABOUT TO EXPIRE TODAY OR TOMORROW and must be used immediately to prevent waste: {expiring_soon_items}.
        Create a delicious, practical Indian rescue recipe utilizing these expiring items, based on trusted culinary practices.
        
        Return ONLY a valid JSON object with these exact keys:
        {{"recipe_name": "<English name>", "recipe_name_hi": "<Hindi name in Devanagari>", "ingredients": ["<item1>", "<item2>"], "steps": ["<step1>", "<step2>"]}}
        """
    response = model.generate_content(prompt)
    text = response.text.strip()
    if text.startswith("```json"):
      text = text[7:-3].strip()
    elif text.startswith("```"):
      text = text[3:-3].strip()

    return json.loads(text)
  except Exception as e:
    return None
