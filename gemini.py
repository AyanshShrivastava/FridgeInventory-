import google.generativeai as genai
import streamlit as st

# Configure Gemini securely via Streamlit secrets
if "GEMINI_API_KEY" in st.secrets:
  genai.configure(api_key=st.secrets["GEMINI_API_KEY"])


def get_ai_expiry_and_names(raw_item: str):
  """Quietly queries Gemini for unlisted grocery items to return shelf-life

  in days, clean English name, and Hindi translation.
  """
  try:
    model = genai.GenerativeModel("gemini-2.5-flash")
    prompt = (
        f"A user added an unlisted grocery item '{raw_item}' to their kitchen"
        " inventory in India. Provide three things separated by a comma: 1."
        " Standard English name, 2. Standard Hindi name in Devanagari script,"
        " 3. Scientific shelf-life in days when stored in an Indian"
        " refrigerator or pantry. Format strictly as: EnglishName, HindiName,"
        " Days (e.g., Dragonfruit, ड्रैगनफ्रूट, 5). Do not include any extra"
        " text or explanation."
    )
    response = model.generate_content(prompt)
    parts = [p.strip() for p in response.text.split(",")]
    if len(parts) >= 3:
      name_en = parts[0].lower()
      name_hi = parts[1]
      days = int(parts[2])
      return days, name_en, name_hi
  except Exception:
    pass

  # Safe fallback if API quota or connection fails
  return 7, raw_item.lower(), raw_item
