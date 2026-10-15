from dataclasses import dataclass, field
import json
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def parse_source_allowed_roots(raw: str | None) -> tuple[Path, ...]:
    """Parse ``ORGANISER_SOURCE_ALLOWED_ROOTS`` into absolute, canonical roots.

    The value is a JSON list of absolute directory paths, e.g.
    ``["/home/user/Documents/Studium"]``. Everything that is not exactly that
    shape is refused: an unparsable value, a JSON object, a relative entry, an
    empty entry or a non-string entry yields *no* root at all. Default deny is
    therefore also the behaviour for a broken configuration -- a typo can never
    widen the readable filesystem, it can only narrow it to nothing.

    Each accepted entry is resolved once, so a symlinked root is compared by its
    real path later. Roots that do not exist are kept: the operator may configure
    a mount point before it is present, and existence is checked per scan.
    """
    if raw is None:
        return ()
    text = raw.strip()
    if not text:
        return ()
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, list) or not parsed:
        return ()
    roots: list[Path] = []
    for entry in parsed:
        if not isinstance(entry, str):
            return ()
        candidate = entry.strip()
        if not candidate or not candidate.startswith("/") or "\x00" in candidate:
            return ()
        try:
            resolved = Path(candidate).resolve()
        except (OSError, ValueError):
            return ()
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


@dataclass(frozen=True)
class Settings:
    database_path: Path
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_timeout_seconds: float = 60.0
    api_token: str | None = field(default=None, repr=False)
    calendar_timezone: str = "Europe/Berlin"
    caldav_url: str | None = None
    caldav_username: str | None = None
    caldav_app_password: str | None = field(default=None, repr=False)
    caldav_calendar_names: tuple[str, ...] = ("Privat", "Uni")
    caldav_organiser_calendar: str = "Organiser"
    knowledge_root: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "knowledge")
    # Operator-side read allowlist for ContextSource scanning. Empty means deny.
    source_allowed_roots: tuple[Path, ...] = ()

    @property
    def database_url(self) -> str:
        return f"sqlite+pysqlite:///{self.database_path}"


def get_settings() -> Settings:
    """Read explicit environment settings; do not load a dotenv file automatically."""
    configured_path = os.environ.get("ORGANISER_DATABASE_PATH")
    database_path = (
        Path(configured_path) if configured_path else PROJECT_ROOT / "data" / "inbox.sqlite3"
    )
    configured_root = os.environ.get("ORGANISER_KNOWLEDGE_ROOT")
    knowledge_root = (
        Path(configured_root).expanduser()
        if configured_root
        else PROJECT_ROOT / "data" / "knowledge"
    )
    return Settings(
        database_path=database_path.resolve(),
        llm_base_url=os.environ.get("ORGANISER_LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
        llm_model=os.environ.get("ORGANISER_LLM_MODEL", "local-general"),
        llm_timeout_seconds=float(os.environ.get("ORGANISER_LLM_TIMEOUT_SECONDS", "60")),
        api_token=os.environ.get("ORGANISER_API_TOKEN", "").strip() or None,
        calendar_timezone=os.environ.get("ORGANISER_CALENDAR_TIMEZONE", "Europe/Berlin"),
        caldav_url=os.environ.get("ORGANISER_CALDAV_URL"),
        caldav_username=os.environ.get("ORGANISER_CALDAV_USERNAME"),
        caldav_app_password=os.environ.get("ORGANISER_CALDAV_APP_PASSWORD"),
        caldav_calendar_names=tuple(
            name.strip()
            for name in os.environ.get("ORGANISER_CALDAV_CALENDARS", "Privat,Uni").split(",")
            if name.strip()
        ),
        caldav_organiser_calendar=os.environ.get(
            "ORGANISER_CALDAV_ORGANISER_CALENDAR", "Organiser"
        ),
        knowledge_root=knowledge_root.resolve(),
        source_allowed_roots=parse_source_allowed_roots(
            os.environ.get("ORGANISER_SOURCE_ALLOWED_ROOTS")
        ),
    )
