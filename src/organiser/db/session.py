from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from organiser.core.config import Settings


def create_engine_for_settings(settings: Settings) -> Engine:
    """Create a SQLite engine with foreign keys and WAL enabled per connection.

    Creates the configured database parent directory. Does not run migrations."""
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _):
        """Enable foreign keys and WAL for this newly opened SQLite connection."""
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")

    return engine


def create_session_factory(settings: Settings):
    """Create sessions without autoflush or expiration on commit.

    Services retain responsibility for their explicit transaction boundaries."""
    return sessionmaker(
        bind=create_engine_for_settings(settings), autoflush=False, expire_on_commit=False
    )
