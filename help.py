from datetime import datetime
import streamlit as st
from gemini import generate_rescue_recipe


def render_expiry_helper(active_rows, T, current_lang):
  """Renders expiry insights and triggers the top-tier Gemini rescue recipe

  generator strictly for items about to expire today or tomorrow.
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
        "Consulting top-tier model for precise rescue recipe..."
        if current_lang == "en"
        else "शीर्ष मॉडल द्वारा सुरक्षित रेसिपी तैयार की जा रही है..."
    ):
      try:
        recipe = generate_rescue_recipe(expiring_soon_items)

        if recipe:
          r_name = (
              recipe.get("recipe_name", "Recipe")
              if current_lang == "en"
              else recipe.get("recipe_name_hi", "रेसिपी")
          )
          st.success(f"### 🍽️ Rescue Dish: {r_name}")

          st.markdown(
              "**Ingredients Used:** "
              + ", ".join(recipe.get("ingredients", []))
          )
          st.markdown("**Cooking Steps:**")
          for idx, step in enumerate(recipe.get("steps", []), 1):
            st.markdown(f"{idx}. {step}")
        else:
          st.error(
              "Could not formulate a rescue recipe. Please try again later."
          )

      except Exception as e:
        st.error(
            f"Could not generate rescue recipe at the moment. Details: {e}"
        )
