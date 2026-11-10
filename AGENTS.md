# Project Rules & Guidelines

## Package & Dependency Management
- **Always use `uv add` and NEVER `pip` or `uv pip`.**
- **Dependency Workflow:**
  1. Always add new dependencies to `requirements.txt` first.
  2. Install and sync dependencies by running `uv add -r requirements.txt`.
  3. Never use `pip install`, `uv pip`, or bare `uv add <package>` without updating `requirements.txt`.

