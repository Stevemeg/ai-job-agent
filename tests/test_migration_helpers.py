"""Pure helpers behind the idempotent migrations -- no database needed."""
import pytest

pytest.importorskip("sqlalchemy")

from backend.database import db                   # noqa: E402


def _deparsed(values, column_type):
    """What pg_get_constraintdef() prints for `event_type IN (...)` on a
    TEXT column (schema.sql) and on a VARCHAR column (the ORM's String)."""
    if column_type == "text":
        arr = ", ".join(f"'{v}'::text" for v in values)
        return f"CHECK ((event_type = ANY (ARRAY[{arr}])))"
    arr = ", ".join(f"'{v}'::character varying" for v in values)
    return f"CHECK (((event_type)::text = ANY ((ARRAY[{arr}])::text[])))"


@pytest.mark.parametrize("column_type", ["text", "varchar"])
def test_current_constraint_is_recognized_in_both_deparse_forms(column_type):
    assert db.event_type_check_is_current(
        _deparsed(db.VALID_EVENT_TYPES, column_type))
    # order is irrelevant
    assert db.event_type_check_is_current(
        _deparsed(tuple(reversed(db.VALID_EVENT_TYPES)), column_type))


@pytest.mark.parametrize("column_type", ["text", "varchar"])
def test_stale_or_missing_constraint_is_not_current(column_type):
    assert not db.event_type_check_is_current(None)
    assert not db.event_type_check_is_current(
        _deparsed(db.CORE_EVENT_TYPES, column_type))            # v1 set
    assert not db.event_type_check_is_current(
        _deparsed(db.VALID_EVENT_TYPES + ("ghosted",), column_type))
    assert not db.event_type_check_is_current(
        "CHECK ((other_col = ANY (ARRAY['x'::text])))")
