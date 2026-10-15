# Getting started

Use a Linux source checkout with Python 3.11 or newer. CI targets 3.11 and 3.14.
PDF extraction is included in the development dependencies.
On Debian/Ubuntu, install the venv package matching the selected interpreter
(e.g. `python3.14-venv`) if `python -m venv` reports missing `ensurepip`.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
export LOCAL_ORGANISER_DEMO_ROOT="$(mktemp -d)"
export ORGANISER_DATABASE_PATH="$LOCAL_ORGANISER_DEMO_ROOT/inbox.sqlite3"
export ORGANISER_KNOWLEDGE_ROOT="$LOCAL_ORGANISER_DEMO_ROOT/knowledge"
export ORGANISER_API_TOKEN="$(openssl rand -hex 32)"
.venv/bin/python -m uvicorn organiser_api:app --host 127.0.0.1 --port 8765
```

Keep the token private. Health is public; business operations need the token:

```bash
curl http://127.0.0.1:8765/health
curl -H "Authorization: Bearer $ORGANISER_API_TOKEN" http://127.0.0.1:8765/tasks
```

Startup migrates the configured database. Never point a first-run experiment at
personal data. `.env.example` is not automatically loaded. Calendar and model
services are optional for domain CRUD and the [synthetic demo](../examples/demo/README.md).
The source allowlist is empty by default; registering a source grants no file access.

See `/docs` for current API contracts and [limitations](limitations.md) before enabling integrations.
