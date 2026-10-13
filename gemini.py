import json
import google.generativeai as genai
import streamlit as st

# Configure Gemini API key safely from st.secrets
if "google" in st.secrets and "api_key" in st.secrets["google"]:
  genai.configure(api_key=st.secrets["google"]["api_key"])


def get_ai_expiry_and_names(item_query):
  """Queries Gemini to estimate shelf life and bilingual names.

  Returns days = -1 if the input is not a food item.
  """
  try:
    model = genai.GenerativeModel("gemini-1.5-flash")
    prompt = f"""
        You are a smart kitchen inventory assistant. Analyze the user query: "{item_query}".
        1. Determine if this item is a FOOD item, vegetable, fruit, dairy, spice, beverage, or kitchen grocery ingredient.
        2. If it is NOT a food item (e.g., a building, vehicle, electronics, clothes, or random non-edible objects like a palace), you MUST return JSON with "days": -1.
        3. If it IS a food item, get the exact value of  its shelf life from trusted organizations in days in an Indian kitchen stored in a fridge, and provide its clean English name and Hindi name in Devanagari script.
     
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
