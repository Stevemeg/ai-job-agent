import pytest

# tracker imports sqlalchemy via db.py; skip cleanly where it's absent
pytest.importorskip("sqlalchemy")

from backend.database.tracker import derive_status, get_funnel_counts


class TestDeriveStatus:
    def test_progression(self):
        assert derive_status(["applied", "save"]) == "applied"

    def test_save_after_apply_does_not_demote(self):
        assert derive_status(["save", "applied"]) == "applied"

    def test_terminal_newest_wins(self):
        assert derive_status(["rejected", "interview", "applied"]) == "rejected"

    def test_reengagement_after_rejection(self):
        assert derive_status(["interview", "rejected", "applied"]) == "interview"

    def test_offer_is_top(self):
        assert derive_status(["offer", "interview", "applied", "save"]) == "offer"

    def test_dismiss_terminal(self):
        assert derive_status(["dismiss"]) == "dismiss"

    def test_impressions_ignored(self):
        assert derive_status(["impression", "save"]) == "save"

    def test_no_deliberate_events_defaults_to_save(self):
        assert derive_status(["impression", "click"]) == "save"


def test_funnel_counts_pure():
    tracked = [{"status": "applied"}, {"status": "applied"},
               {"status": "offer"}, {"status": "rejected"}]
    counts = get_funnel_counts(tracked)
    assert counts["applied"] == 2
    assert counts["offer"] == 1
    assert counts["rejected"] == 1
    assert counts["save"] == 0
