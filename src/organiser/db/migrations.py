from pathlib import Path

from alembic import command
from alembic.config import Config

from organiser.core.config import Settings


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def upgrade_database(settings: Settings) -> None:
    """Upgrade the configured SQLite database to Alembic head.

    Creates the target parent directory and applies real schema changes. Callers
    must select an isolated database for tests or back up personal data first."""
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "head")
