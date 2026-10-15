# Development

Install from checkout with `python -m pip install -e '.[dev]'` in a virtual environment.
Tests use synthetic data, temporary SQLite files and temporary knowledge/source roots.
Do not start the application with real settings to run tests: startup applies migrations.

```bash
python -m pytest
python -m coverage run -m pytest
python -m coverage report
ruff check .
ruff format --check .
```

CI targets Python 3.11 and 3.14. In plugin-heavy environments,
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` isolates pytest from unrelated installed plugins.
No real model or calendar account is required for the automated suite.

## Module and documentation rules

Prefer 150–350 total lines per Python module. At 500 lines review its responsibility;
up to about 600 is reasonable for a cohesive, well-documented module. Above that,
split by responsibility or record a specific exception. Never shorten useful
comments/docstrings simply to meet a line count.
Public operations document effect, inputs, errors and side effects. Writers also
explain transaction ownership, repeatability and partial failures. Classes document
state/lock/client ownership. Comments explain non-obvious decisions and invariants.

Keep domain values independent of API orchestration. Route factories receive explicit
application dependencies. Preserve failure/concurrency tests when moving code;
patch the actual dependency lookup location, not an unused reexport.
Historical migrations are immutable. New functionality and broad formatting changes
should remain distinguishable from behaviour-preserving extraction.

Support modules live under `tests/support`; executable test modules do not supply
fixtures or fake clients to other executable test modules. Public visibility does
not itself grant a licence; see [rights](../RIGHTS.md).
