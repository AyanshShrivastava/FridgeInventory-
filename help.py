import streamlit as st


def render_expiry_helper(display_rows, T):
  """Checks active dashboard items and renders the expiration warning helper

  bubble at the bottom of the ledger.
  """
  urgent_items = [
      r[T["col_item"]]
      for r in display_rows
      if any(
          k in r[T["col_status"]]
          for k in [
              "EXPIRED",
              "CRITICAL",
              "WARNING",
              "खराब",
              "आपातकालीन",
              "चेतावनी",
          ]
      )
  ]
  if urgent_items:
    st.markdown(
        f'<div class="bubble-alert-note">{T["note_msg"].format(items=", ".join(urgent_items))}</div>',
        unsafe_allow_html=True,
    )
