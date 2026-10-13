import streamlit as st

try:
  import google.generativeai as genai

  GEMINI_AVAILABLE = True
except ImportError:
  GEMINI_AVAILABLE = False


def render_expiry_helper(display_rows, T, current_lang):
  """Checks active dashboard items and renders the zero-waste recipe generator

  safely.
  """
  urgent_items = [
      r[T["col_item"]]
      for r in display_rows
      if any(
          k in r[T["col_status"]] for k in ["CRITICAL", "WARNING", "आपातकालीन", "चेतावनी"]
      )
  ]

  if urgent_items:
    items_str = ", ".join(urgent_items)

    st.markdown(
        f'<div class="bubble-alert-note">{T["note_msg"].format(items=items_str)}</div>',
        unsafe_allow_html=True,
    )

    st.markdown("<br>", unsafe_allow_html=True)

    btn_label = (
        "Generate Zero-Waste Recipe"
        if current_lang == "en"
        else "जीरो-वेस्ट रेसिपी बनाएं"
    )
    recipe_btn_key = f"recipe_btn_{hash(items_str)}"

    if st.button(btn_label, key=recipe_btn_key, use_container_width=True):
      if not GEMINI_AVAILABLE:
        err_msg = (
            "Gemini library is not installed in this environment."
            if current_lang == "en"
            else "जेमिनी लाइब्रेरी इस वातावरण में इंस्टॉल नहीं है।"
        )
        st.error(err_msg)
        return

      if "GEMINI_API_KEY" not in st.secrets:
        err_msg = (
            "Gemini API key missing in Streamlit secrets."
            if current_lang == "en"
            else "स्ट्रीमलाइट सीक्रेट्स में जेमिनी एपीआई कुंजी गायब है।"
        )
        st.error(err_msg)
        return

      spinner_text = (
          "Generating quick kitchen recipe..."
          if current_lang == "en"
          else "रसोई की झटपट रेसिपी तैयार की जा रही है..."
      )
      with st.spinner(spinner_text):
        try:
          genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
          model = genai.GenerativeModel("gemini-2.5-flash")
          lang_instruction = "in English" if current_lang == "en" else "in Hindi"
          prompt = (
              f"You are an expert Indian home chef. The following items are"
              f" expiring soon or tomorrow in the household kitchen:"
              f" {items_str}. Provide a quick, simple, and delicious recipe"
              f" using these items (and basic pantry staples like oil, salt,"
              f" and common spices) {lang_instruction} to prevent food waste."
              " Keep it concise, practical, and formatted professionally."
          )
          response = model.generate_content(prompt)
          recipe_text = response.text

          box_title = (
              "Zero-Waste Kitchen Recipe"
              if current_lang == "en"
              else "जीरो-वेस्ट किचन रेसिपी"
          )
          st.markdown(
              f"""
                        <div style="background-color: #1E293B; border: 3px solid #38BDF8; padding: 22px; border-radius: 18px; margin-top: 15px; color: #F3F4F6; box-shadow: 0px 6px 0px #0369A1;">
                            <h4 style="color: #38BDF8; margin-top: 0; font-family: 'Fredoka One', cursive;">{box_title}</h4>
                            <p style="white-space: pre-wrap; font-size: 15px; line-height: 1.6;">{recipe_text}</p>
                        </div>
                    """,
              unsafe_allow_html=True,
          )
        except Exception:
          err_msg = (
              "Could not generate recipe at the moment. Please verify API"
              " configuration."
              if current_lang == "en"
              else "इस समय रेसिपी नहीं बन सकी। कृपया एपीआई जांचें।"
          )
          st.error(err_msg)
