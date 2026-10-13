import os
import google.generativeai as genai
import streamlit as st

# Configure Gemini API key securely from Streamlit secrets or environment
if "google" in st.secrets and "api_key" in st.secrets["google"]:
  genai.configure(api_key=st.secrets["google"]["api_key"])
elif "GEMINI_API_KEY" in os.environ:
  genai.configure(api_key=os.environ["GEMINI_API_KEY"])


def get_ai_expiry_and_names(item_query):
  """Uses Gemini 3.8-flash to determine shelf life and bilingual names for unlisted items."""
  try:
    model = genai.GenerativeModel("gemini-3.8-flash")
    prompt = (
        f"You are a food science and inventory expert for an Indian kitchen"
        f" app.\nAnalyze the food item: '{item_query}'.\nProvide 3 values"
        f" separated by a vertical bar (|):\n1. Estimated shelf life in days"
        f" (integer only, e.g., 5).\n2. Clean English name (e.g.,"
        f" Dragonfruit).\n3. Clean Hindi name in Devanagari script (e.g.,"
        f" ड्रैगनफ्रूट).\nExample format: 5|Dragonfruit|ड्रैगनफ्रूट"
    )
    response = model.generate_content(prompt)
    parts = response.text.strip().split("|")
    if len(parts) >= 3:
      days = int(parts[0].strip())
      name_en = parts[1].strip().title()
      name_hi = parts[2].strip()
      return days, name_en, name_hi
  except Exception:
    pass
  return 7, item_query.title(), item_query
