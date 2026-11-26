"""One PostgreSQL driver for application processes and migrations, preserving URL data."""

from contextlib import contextmanager

DATABASE_FAILURES = (
    (
        "name_resolution",
        (
            "could not translate host",
            "failed to resolve host",
            "name or service not known",
            "nodename nor servname",
            "temporary failure in name resolution",
        ),
    ),
    ("timeout", ("timeout", "timed out")),
    ("refused", ("refused",)),
    (
        "connection_lost",
        (
            "closed the connection",
            "connection reset",
            "broken pipe",
            "terminating connection",
        ),
    ),
    ("authentication", ("authentication", "password")),
    ("too_many_connections", ("too many",)),
)


def classify_database_error(exc):
    """Return only a fixed reason; upstream messages may contain connection credentials."""
    inner = getattr(exc, "orig", None) or exc
    detail = f"{type(inner).__name__} {inner}".lower()
    for label, needles in DATABASE_FAILURES:
        if any(needle in detail for needle in needles):
            return label
    return "other"


def database_url(value):
    if not isinstance(value, str):
        raise ValueError("DATABASE_URL must be a PostgreSQL URL")
    scheme, separator, rest = value.partition("://")
    if (
        not separator
        or not rest
        or scheme.lower() not in ("postgresql", "postgres", "postgresql+psycopg")
    ):
        raise ValueError("DATABASE_URL must be a PostgreSQL URL")
    return "postgresql+psycopg://" + rest


@contextmanager
def database_connection(value):
    """Bound startup connection time while respecting an explicitly configured timeout."""
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    url = make_url(database_url(value))
    options = {} if "connect_timeout" in url.query else {"connect_timeout": 10}
    engine = create_engine(url, connect_args=options)
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


def main():
    import os
    import sys

    from sqlalchemy.exc import SQLAlchemyError

    try:
        with database_connection(os.getenv("DATABASE_URL", "")):
            pass
    except ValueError:
        print(
            "Database check failed: configure a valid PostgreSQL DATABASE_URL.",
            file=sys.stderr,
        )
        return 1
    except SQLAlchemyError as exc:
        print(
            f"Database check failed: reason={classify_database_error(exc)}. "
            "Check DATABASE_URL, network access and credentials. No schema changes performed.",
            file=sys.stderr,
        )
        return 1
    print("Configured database is reachable. No schema changes performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
