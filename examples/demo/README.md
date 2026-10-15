# Synthetic service demonstration

After installing the checkout dependencies, run from the repository root:

```bash
.venv/bin/python examples/demo/run_demo.py
.venv/bin/python examples/demo/run_demo.py --apply
```

The first command generates and displays a persisted proposal without creating a
task. The second starts a fresh isolated run and explicitly applies its proposal.
The script verifies task counts and original-file immutability.

Each run uses a temporary SQLite database, source directory and knowledge root,
which are removed on exit. No personal settings, calendar, model download or
network connection is required. **The model client is a labelled fake**: this
demonstrates application boundaries, not model reasoning or benchmark performance.

The demo calls real services directly. HTTP/authentication and concurrent failure
behaviour are covered separately by the automated suite.
