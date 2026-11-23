"""
Small interactive CLI for manually logging outcome events (applied,
interview, rejected, offer) -- the "manual" half of Phase 2's logging.

Run from project root:
    python -m backend.database.log_outcome_cli
"""
from .events import log_outcome
from .db import get_session, User

DEMO_USER_EMAIL = "shanmuka@example.com"

# The ONLY types a person may log by hand. Validation is against this list,
# not VALID_EVENT_TYPES: the v2 agent types (approved/declined/drafted) are
# valid in the database but are written by the agent alone.
OUTCOME_TYPES = ("applied", "interview", "rejected", "offer", "save", "dismiss")


def _get_or_create_demo_user():
    with get_session() as session:
        user = session.query(User).filter_by(email=DEMO_USER_EMAIL).first()
        if user:
            return user.id
        user = User(email=DEMO_USER_EMAIL, display_name="Shanmuka")
        session.add(user)
        session.flush()
        return user.id


def main():
    print("Log a job outcome event. Press Ctrl+C anytime to cancel.\n")
    company = input("Company (must match how it was stored, e.g. 'databricks'): ").strip()
    title = input("Job title: ").strip()

    print(f"\nEvent type options: {OUTCOME_TYPES}")
    event_type = input("Event type: ").strip().lower()

    if event_type not in OUTCOME_TYPES:
        print(f"\n'{event_type}' is not valid. Must be one of: {OUTCOME_TYPES}")
        return

    user_id = _get_or_create_demo_user()
    event_id = log_outcome(user_id, company, title, event_type)
    print(f"\nLogged: {event_type} -> {title} @ {company} (event id {event_id})")


if __name__ == "__main__":
    main()