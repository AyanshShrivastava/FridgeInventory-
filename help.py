from datetime import datetime
import json
import google.generativeai as genai
import streamlit as st


def render_expiry_helper(active_rows, T, current_lang):
  """Renders expiry insights and Gemini-powered smart rescue recipes

  strictly for items about to expire today or tomorrow.
  """
  st.markdown("---")
  st.subheader(
      "🍲 Anti-Waste Rescue Recipe Assistant"
      if current_lang == "en"
      else "🍲 एंटी-वेस्ट रेस्क्यू रेसिपी सहायक"
  )

  # Filter active rows to find ONLY items expiring today (0 days) or tomorrow (1 day)
  today = datetime.now().date()
  expiring_soon_items = []

  for row in active_rows:
    target_date_str = row[T["col_target"]]
    try:
      expiry_dt = datetime.strptime(target_date_str, "%Y-%m-%d").date()
      days_left = (expiry_dt - today).days

      # Strictly target items expiring today or tomorrow
      if days_left <= 1:
        expiring_soon_items.append(row[T["col_item"]])
    except Exception:
      continue

  if not expiring_soon_items:
    safe_msg = (
        "🎉 **All clear!** No items are about to expire right now. Your active"
        " storage is fresh and safe."
        if current_lang == "en"
        else "🎉 **सब कुछ सुरक्षित है!** अभी कोई भी सामान खराब नहीं होने वाला है।"
        " आपका सारा सामान ताजा है।"
    )
    st.info(safe_msg)
    return

  st.markdown(
      f"<p style='color: #F87171; font-weight: 500;'>⚠️ Items needing"
      f" immediate rescue: <b>{', '.join(expiring_soon_items)}</b></p>",
      unsafe_allow_html=True,
  )

  if st.button(
      "✨ Cook Rescue Recipe with Expiring Items",
      use_container_width=True,
      key="gen_rescue_recipe_btn",
  ):
    with st.spinner(
        "Formulating rescue recipe from trusted culinary standards..."
        if current_lang == "en"
        else "खराब होने वाले सामान से सुरक्षित रेसिपी तैयार की जा रही है..."
    ):
      try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = f"""
                You are an expert Indian chef and food-waste reduction specialist. 
                The user has these specific kitchen ingredients that are ABOUT TO EXPIRE TODAY OR TOMORROW and must be used immediately to prevent waste: {expiring_soon_items}.
                Create a delicious, practical Indian rescue recipe utilizing these expiring items.
                
                Return ONLY a valid JSON object with these exact keys:
                {{"recipe_name": "<English name>", "recipe_name_hi": "<Hindi name in Devanagari>", "ingredients": ["<item1>", "<item2>"], "steps": ["<step1>", "<step2>"]}}
                """
        response = model.generate_content(prompt)
        text = response.text.strip()
        if text.startswith("```json"):
          text = text[7:-3].strip()
        elif text.startswith("```"):
          text = text[3:-3].strip()

        recipe = json.loads(text)

        r_name = (
            recipe["recipe_name"]
            if current_lang == "en"
            else recipe["recipe_name_hi"]
        )
        st.success(f"### 🍽️ Rescue Dish: {r_name}")

        st.markdown(
            "**Ingredients Used:** " + ", ".join(recipe["ingredients"])
        )
        st.markdown("**Cooking Steps:**")
        for idx, step in enumerate(recipe["steps"], 1):
          st.markdown(f"{idx}. {step}")

      except Exception as e:
        st.error(
            f"Could not generate rescue recipe at the moment. Details: {e}"
        )
