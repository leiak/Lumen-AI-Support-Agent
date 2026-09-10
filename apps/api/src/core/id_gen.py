"""ULID generator for primary keys. ULIDs are 26-char, lexicographically sortable,
time-encoded — a good default for multi-tenant distributed IDs."""
import ulid


def new_id() -> str:
    """Return a fresh ULID as a 26-character string."""
    return str(ulid.new())