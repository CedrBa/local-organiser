# Configuration

Settings are read from environment variables, not automatically from `.env`.
Keep real credentials outside version control, in privately readable files.

| Variable | Purpose / default |
|---|---|
| `ORGANISER_API_TOKEN` | Required for business routes; absent configuration returns 503 |
| `ORGANISER_DATABASE_PATH` | SQLite file; default `data/inbox.sqlite3` in checkout |
| `ORGANISER_KNOWLEDGE_ROOT` | Knowledge root; default `data/knowledge` in checkout |
| `ORGANISER_SOURCE_ALLOWED_ROOTS` | JSON array of absolute allowed roots; absent/invalid denies access |
| `ORGANISER_LLM_BASE_URL` | Compatible endpoint; default `http://127.0.0.1:8080/v1` |
| `ORGANISER_LLM_MODEL` | Default `local-general` |
| `ORGANISER_LLM_TIMEOUT_SECONDS` | Default 60 seconds |
| `ORGANISER_CALENDAR_TIMEZONE` | Default `Europe/Berlin` |
| `ORGANISER_CALDAV_URL` | Optional CalDAV endpoint |
| `ORGANISER_CALDAV_USERNAME` | Optional account |
| `ORGANISER_CALDAV_APP_PASSWORD` | Optional credential; never commit |
| `ORGANISER_CALDAV_CALENDARS` | Comma-separated; default `Privat,Uni` |
| `ORGANISER_CALDAV_ORGANISER_CALENDAR` | Explicit sync target; default `Organiser` |

Business routes require `Authorization: Bearer <token>`, not a query-string token.
Allowed sources must also be registered and active. Project, knowledge, VCS,
virtual environment and hidden paths are restricted by ingestion policy.
For personal operation, keep data and knowledge outside the checkout.
