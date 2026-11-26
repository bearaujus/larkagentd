import os

from alembic import context
from alembic.util import CommandError
from sqlalchemy.exc import SQLAlchemyError

from larkagentd.core import group_actions  # noqa: F401
from larkagentd.core.database import classify_database_error, database_connection
from larkagentd.core.store import metadata
from larkagentd.knowledge import library  # noqa: F401

try:
    with database_connection(os.environ["DATABASE_URL"]) as connection:
        context.configure(connection=connection, target_metadata=metadata)
        with context.begin_transaction():
            context.run_migrations()
except SQLAlchemyError as exc:
    raise CommandError(
        f"Database migration failed: reason={classify_database_error(exc)}. "
        "Check DATABASE_URL, network access, credentials and pending migration permissions."
    ) from None
