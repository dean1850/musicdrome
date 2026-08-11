# Tests

## Unit and API tests

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

No network, no browser. Every test runs against a throwaway SQLite database
created in a temp directory by `conftest.py`, which sets the environment before
`app.config` is first imported.

## Browser tests

```bash
cd tests/e2e
npm install
npx playwright install chromium   # or set CHROMIUM_PATH, below
npx playwright test
```

Playwright starts its own server: `seed.py` writes a fixed set of plays and
suggestions into a temp database, then uvicorn serves against it with
`MUSICDROME_TESTING=1`, which keeps the download workers and the scan scheduler
switched off. The suite makes no outbound requests, and the database is
re-seeded before each test so the specs do not depend on each other's state.

Useful environment variables:

| | |
|---|---|
| `CHROMIUM_PATH` | Use a Chromium already on the machine instead of Playwright's own download. |
| `PYTHON` | Interpreter to run the server with. Defaults to `.venv/bin/python`. |
| `PORT` | Defaults to 3047, so it does not collide with a running instance on 3046. |
