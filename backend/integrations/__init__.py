"""
backend.integrations -- the agent's outbound integrations (ADR-001).

  gmail.py       Gmail DRAFTS only: create_draft() and
                 find_draft_by_message_id(). Nothing can send.
  gmail_auth.py  its OAuth (installed-app flow, gmail.compose scope) and
                 the token file; enabled only when GMAIL_CREDENTIALS_FILE
                 is set.
"""
