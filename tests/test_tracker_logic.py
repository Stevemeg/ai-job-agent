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


class TestAgentEventTypes:
    """v2 agent decisions must slot into the derived status without
    disturbing any v1 behaviour above."""

    def test_approved_is_a_stage_between_save_and_applied(self):
        assert derive_status(["approved", "save"]) == "approved"
        assert derive_status(["approved", "applied"]) == "applied"

    def test_declined_is_terminal_when_newest(self):
        assert derive_status(["declined", "approved"]) == "declined"

    def test_reengagement_after_decline(self):
        assert derive_status(["applied", "declined"]) == "applied"

    def test_drafted_never_changes_status(self):
        assert derive_status(["drafted", "approved"]) == "approved"
        assert derive_status(["drafted", "rejected"]) == "rejected"
        assert derive_status(["drafted"]) == "save"

    def test_funnel_counts_include_agent_statuses(self):
        counts = get_funnel_counts([{"status": "approved"},
                                    {"status": "declined"},
                                    {"status": "expired"}])
        assert counts["approved"] == 1 and counts["declined"] == 1
        assert counts["expired"] == 1
        assert counts["save"] == 0

    def test_an_expiry_decline_reads_as_expired(self):
        from backend.database.tracker import effective_type
        assert effective_type("declined", "expiry") == "expired"
        assert effective_type("declined", "user") == "declined"
        assert effective_type("declined", None) == "declined"
        assert effective_type("approved", "user") == "approved"
        assert derive_status(["expired", "drafted"]) == "expired"     # terminal
        assert derive_status(["applied", "expired"]) == "applied"     # re-engaged


@pytest.mark.parametrize("agent_type", ["approved", "declined", "drafted"])
def test_outcome_cli_refuses_agent_only_types(agent_type, monkeypatch, capsys):
    from backend.database import log_outcome_cli

    answers = iter(["acme", "AI Engineer", agent_type])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    def _must_not_log(*a, **k):
        raise AssertionError("agent-only type reached log_outcome")
    monkeypatch.setattr(log_outcome_cli, "log_outcome", _must_not_log)
    monkeypatch.setattr(log_outcome_cli, "_get_or_create_demo_user",
                        lambda: 1)

    log_outcome_cli.main()
    out = capsys.readouterr().out
    assert f"'{agent_type}' is not valid" in out
    assert agent_type not in log_outcome_cli.OUTCOME_TYPES
