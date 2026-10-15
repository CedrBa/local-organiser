"""Read-only iCloud CalDAV adapter and calendar-derived availability."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, field_validator, model_validator

from organiser.core.config import Settings
from organiser.services.scheduling import AvailabilityWindow, SchedulingPolicy


class CalendarUnavailableError(Exception):
    pass


class CalendarIncompleteError(CalendarUnavailableError):
    pass


class CalendarInfo(BaseModel):
    name: str


class CalendarBusyInterval(BaseModel):
    calendar_name: str
    external_event_id: str
    starts_at: datetime
    ends_at: datetime
    title: str | None = None

    @field_validator("starts_at", "ends_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        """Require timezone-aware input and normalise it to UTC."""
        if value.tzinfo is None:
            raise ValueError("Calendar-Zeitstempel muessen eine Zeitzone enthalten")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_interval(self):
        """Validate an aware, strictly increasing time interval before scheduling."""
        if self.ends_at <= self.starts_at:
            raise ValueError("Calendar-Event muss nach seinem Start enden")
        return self


class CalendarAdapter(Protocol):
    def list_calendars(self) -> list[CalendarInfo]:
        """Read calendar identifiers through the configured adapter; create nothing."""
        ...

    def busy_between(self, starts_at: datetime, ends_at: datetime) -> list[CalendarBusyInterval]:
        """Read selected calendars in the given horizon and report their busy intervals."""
        ...


class OrganiserCalendarWriter(CalendarAdapter, Protocol):
    def setup_organiser_calendar(self) -> CalendarInfo:
        """Explicitly find or create the configured Organiser calendar.

        This is a remote write operation when the calendar is absent."""
        ...

    def create_organiser_event(
        self, *, block_id: str, task_title: str, starts_at: datetime, ends_at: datetime
    ) -> tuple[str, str]:
        """Explicitly create or reconcile an event in the Organiser calendar only."""
        ...


def _as_datetime(value: datetime | date, timezone: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        return (
            value.replace(tzinfo=timezone) if value.tzinfo is None else value.astimezone(timezone)
        )
    return datetime.combine(value, time.min, tzinfo=timezone)


class ICloudCalDAVAdapter:
    """Read-only CalDAV implementation. It only performs PROPFIND/REPORT-style reads."""

    def __init__(
        self,
        *,
        url: str,
        username: str,
        app_password: str,
        calendar_names: tuple[str, ...],
        organiser_calendar: str,
        timezone: str,
        client_factory: Callable[..., object] | None = None,
    ):
        self._url = url
        self._username = username
        self._app_password = app_password
        self._calendar_names = calendar_names
        self._organiser_calendar = organiser_calendar
        self._timezone = ZoneInfo(timezone)
        self._client_factory = client_factory

    @classmethod
    def from_settings(cls, settings: Settings) -> "ICloudCalDAVAdapter":
        """Derive allowed roots and exclusions once from operator-owned settings."""
        if (
            not settings.caldav_url
            or not settings.caldav_username
            or not settings.caldav_app_password
        ):
            raise CalendarUnavailableError("iCloud CalDAV ist nicht vollstaendig konfiguriert")
        return cls(
            url=settings.caldav_url,
            username=settings.caldav_username,
            app_password=settings.caldav_app_password,
            calendar_names=settings.caldav_calendar_names,
            organiser_calendar=settings.caldav_organiser_calendar,
            timezone=settings.calendar_timezone,
        )

    def _client(self):
        if self._client_factory is not None:
            return self._client_factory(self._url, self._username, self._app_password)
        try:
            import caldav
        except ImportError as error:
            raise CalendarUnavailableError("CalDAV-Abhaengigkeit ist nicht installiert") from error
        try:
            return caldav.DAVClient(
                url=self._url, username=self._username, password=self._app_password
            )
        except Exception as error:
            raise CalendarUnavailableError(
                "iCloud CalDAV-Verbindung konnte nicht aufgebaut werden"
            ) from error

    def _all_calendars(self):
        try:
            calendars = self._client().principal().calendars()
        except CalendarUnavailableError:
            raise
        except Exception as error:
            raise CalendarUnavailableError(
                "iCloud-Kalender konnten nicht gelesen werden"
            ) from error
        return calendars

    def _selected_calendars(self):
        by_name = {calendar.name: calendar for calendar in self._all_calendars()}
        missing = [name for name in self._calendar_names if name not in by_name]
        if missing:
            raise CalendarIncompleteError(
                "Konfigurierte iCloud-Kalender fehlen oder sind nicht lesbar"
            )
        return [by_name[name] for name in self._calendar_names]

    def _organiser_calendar_resource(self):
        calendars = {calendar.name: calendar for calendar in self._all_calendars()}
        calendar = calendars.get(self._organiser_calendar)
        if calendar is None:
            raise CalendarUnavailableError("Organiser-Kalender ist nicht eingerichtet")
        return calendar

    def list_calendars(self) -> list[CalendarInfo]:
        """Read calendar identifiers through the configured adapter; create nothing."""
        return [CalendarInfo(name=calendar.name) for calendar in self._selected_calendars()]

    def _busy_interval(self, calendar_name: str, event) -> CalendarBusyInterval:
        try:
            component = event.vobject_instance.vevent
            starts_at = _as_datetime(component.dtstart.value, self._timezone)
            ends_at = _as_datetime(component.dtend.value, self._timezone)
            title = str(component.summary.value) if hasattr(component, "summary") else None
            external_event_id = str(getattr(event, "url", None) or component.uid.value)
            return CalendarBusyInterval(
                calendar_name=calendar_name,
                external_event_id=external_event_id,
                starts_at=starts_at,
                ends_at=ends_at,
                title=title,
            )
        except Exception as error:
            raise CalendarIncompleteError(
                "iCloud-Event konnte nicht vollstaendig gelesen werden"
            ) from error

    def busy_between(self, starts_at: datetime, ends_at: datetime) -> list[CalendarBusyInterval]:
        """Read selected calendars in the given horizon and report their busy intervals."""
        if starts_at.tzinfo is None or ends_at.tzinfo is None or ends_at <= starts_at:
            raise CalendarUnavailableError(
                "Calendar-Abfrage braucht einen gueltigen timezone-aware Zeitraum"
            )
        try:
            intervals = [
                self._busy_interval(calendar.name, event)
                for calendar in self._selected_calendars()
                for event in calendar.date_search(start=starts_at, end=ends_at, expand=True)
            ]
        except CalendarUnavailableError:
            raise
        except Exception as error:
            raise CalendarUnavailableError("iCloud-Termine konnten nicht gelesen werden") from error
        return sorted(
            intervals,
            key=lambda interval: (interval.starts_at, interval.ends_at, interval.external_event_id),
        )

    def setup_organiser_calendar(self) -> CalendarInfo:
        """Explicitly find or create the configured Organiser calendar.

        This is a remote write operation when the calendar is absent."""
        calendars = {calendar.name: calendar for calendar in self._all_calendars()}
        if self._organiser_calendar not in calendars:
            try:
                self._client().principal().make_calendar(name=self._organiser_calendar)
            except Exception as error:
                raise CalendarUnavailableError(
                    "Organiser-Kalender konnte nicht eingerichtet werden"
                ) from error
        return CalendarInfo(name=self._organiser_calendar)

    @staticmethod
    def _ical_escape(value: str) -> str:
        return (
            value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
        )

    @staticmethod
    def _ical_time(value: datetime) -> str:
        return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

    def create_organiser_event(
        self, *, block_id: str, task_title: str, starts_at: datetime, ends_at: datetime
    ) -> tuple[str, str]:
        """Explicitly create or reconcile an event in the Organiser calendar only."""
        if starts_at.tzinfo is None or ends_at.tzinfo is None:
            raise CalendarUnavailableError("Organiser-Event braucht timezone-aware Zeitstempel")
        calendar = self._organiser_calendar_resource()
        uid = f"organiser-{block_id}@local"
        try:
            from caldav.lib.error import NotFoundError

            existing = calendar.event_by_uid(uid)
            return str(getattr(calendar, "url", self._organiser_calendar)), str(existing.url)
        except NotFoundError:
            pass
        except Exception as error:
            raise CalendarUnavailableError(
                "Organiser-Event konnte nicht auf Idempotenz geprueft werden"
            ) from error
        event_data = "\r\n".join(
            [
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "PRODID:-//Organiser//EN",
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{self._ical_time(datetime.now(UTC))}",
                f"DTSTART:{self._ical_time(starts_at)}",
                f"DTEND:{self._ical_time(ends_at)}",
                f"SUMMARY:Organiser: {self._ical_escape(task_title)}",
                f"X-ORGANISER-BLOCK-ID:{block_id}",
                "END:VEVENT",
                "END:VCALENDAR",
                "",
            ]
        )
        try:
            event = calendar.save_event(event_data)
            return str(getattr(calendar, "url", self._organiser_calendar)), str(event.url)
        except Exception as error:
            raise CalendarUnavailableError(
                "Organiser-Event konnte nicht erstellt werden"
            ) from error


def availability_from_busy(
    busy_intervals: list[CalendarBusyInterval],
    *,
    starts_at: datetime,
    ends_at: datetime,
    policy: SchedulingPolicy,
) -> list[AvailabilityWindow]:
    """Subtract busy intervals from configured working windows deterministically."""
    if starts_at.tzinfo is None or ends_at.tzinfo is None or ends_at <= starts_at:
        raise CalendarUnavailableError(
            "Availability-Abfrage braucht einen gueltigen timezone-aware Zeitraum"
        )
    timezone = ZoneInfo(policy.calendar_timezone)
    local_start, local_end = starts_at.astimezone(timezone), ends_at.astimezone(timezone)
    windows: list[AvailabilityWindow] = []
    day = local_start.date()
    while day <= local_end.date():
        day_start = datetime.combine(day, time(policy.day_starts_hour), tzinfo=timezone).astimezone(
            UTC
        )
        day_end = datetime.combine(day, time(policy.day_ends_hour), tzinfo=timezone).astimezone(UTC)
        starts, ends = (
            max(day_start, starts_at.astimezone(UTC)),
            min(day_end, ends_at.astimezone(UTC)),
        )
        if starts < ends:
            windows.append(AvailabilityWindow(starts_at=starts, ends_at=ends))
        day += timedelta(days=1)
    for busy in busy_intervals:
        remaining: list[AvailabilityWindow] = []
        for window in windows:
            if window.starts_at < busy.ends_at and window.ends_at > busy.starts_at:
                if window.starts_at < busy.starts_at:
                    remaining.append(
                        AvailabilityWindow(starts_at=window.starts_at, ends_at=busy.starts_at)
                    )
                if busy.ends_at < window.ends_at:
                    remaining.append(
                        AvailabilityWindow(starts_at=busy.ends_at, ends_at=window.ends_at)
                    )
            else:
                remaining.append(window)
        windows = remaining
    return windows


class CalendarAvailabilityProvider:
    """AvailabilityProvider backed by a complete read-only calendar query."""

    def __init__(self, adapter: CalendarAdapter, policy: SchedulingPolicy):
        self._adapter = adapter
        self._policy = policy

    def availability_between(
        self, starts_at: datetime, ends_at: datetime
    ) -> list[AvailabilityWindow]:
        """Return availability within the requested interval using this provider contract."""
        return availability_from_busy(
            self._adapter.busy_between(starts_at, ends_at),
            starts_at=starts_at,
            ends_at=ends_at,
            policy=self._policy,
        )
