# Repository Guidelines

## Project Structure & Module Organization

`app.py` launches the active browser UI. Its local JSON/API coordination lives in `web_app.py`, and static interface files live in `web/`. `app_legacy.py` preserves the original Streamlit prototype and should remain isolated. Core scheduling logic lives in `scheduling/`, including CP-SAT generation, validation, repair, and shared types. Models and queries belong in `database/`; CSV/demo loading in `importers/`; appointment, absence, cancellation, and replacement workflows in `services/`. Tests are under `tests/`. Root CSV files and `eic_scheduler.db` are demo data.

## Build, Test, and Development Commands

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pytest -q
python app.py
```

Target one area with `python -m pytest tests/test_scheduler.py -q`. The final command starts the local browser UI at `http://127.0.0.1:8765`. The demo importer remains idempotent when demo records are needed.

## Coding Style & Naming Conventions

Follow PEP 8 with four-space indentation. Use `snake_case` for functions, variables, and modules; `PascalCase` for classes; and `UPPER_CASE` for constants. Add type hints to public functions and concise docstrings where intent is not obvious. Keep browser/API coordination in `web_app.py` and business rules in domain modules. No formatter or linter is configured; group imports as standard library, third-party, then local.

## Testing Guidelines

Tests use pytest, fixtures from `tests/conftest.py`, and in-memory SQLite for persistence. Name files `test_<feature>.py` and tests `test_<expected_behavior>`. Cover successful schedules and constraint failures; verify validation output and unmet-demand reasons. Keep solver inputs deterministic. Run the full suite before opening a pull request.

## Commit & Pull Request Guidelines

Git history is not available in this checkout, so use short, imperative commit subjects such as `Add absence conflict validation`. Keep each commit focused. Pull requests should explain the behavior change, identify affected rules or data migrations, list test commands and results, and link relevant issues. Include screenshots for browser UI changes and call out any changes to demo CSVs, SQLite schema, solver settings, or scheduling constraints.

## Data & Configuration Safety

Treat patient and therapist files as sensitive even when they contain demo data. Do not commit real personal or clinical information. Keep deterministic solver defaults in `config.py`, and document any local configuration or schema assumptions in the pull request.

## Business Rule Documentation

Every scheduling behavior change must update `rules.md` in the same change. Mark a rule implemented only when the behavior exists in code and is covered by an appropriate test. Put unresolved management decisions in `questions.md`; when a decision is answered, remove it from `questions.md`, add the confirmed rule to `rules.md`, and update the code and tests together. Never infer an unanswered clinical or operational policy merely to remove a question.
