# Contributing to Ziva

Thanks for your interest in Ziva! This document covers how to set up a
development environment, run tests, and submit changes.

## Development Environment

1. **Clone the repository**

   ```bash
   git clone https://github.com/ziva-ai/ziva.git
   cd ziva
   ```

2. **Install Python dependencies**

   We use [`uv`](https://docs.astral.sh/uv/):

   ```bash
   uv pip install -e ".[dev]"
   ```

   This installs the core SDK plus the desktop/CLI extras and test tools.

3. **Install Node dependencies**

   ```bash
   cd web && npm install
   cd ../electron && npm install
   cd ..
   ```

## Running Tests

```bash
uv run pytest
```

Integration tests may require a configured model API key. Unit tests should run
offline.

## Code Style

- Python: follow PEP 8; run type checks with `mypy` if available.
- TypeScript: run `npx tsc --noEmit` in `web/` and `electron/` before committing.
- Keep changes focused; avoid unrelated refactoring in the same PR.

## Submitting Changes

1. Open an issue to discuss large features or ambiguous bugs.
2. Create a feature branch from `main`.
3. Add tests when possible.
4. Update `CHANGELOG.md` with a short summary under the `Unreleased` section.
5. Open a pull request with a clear description and reproduction steps.

## Questions?

Open a [GitHub Discussion](https://github.com/ziva-ai/ziva/discussions) or
join the community channels linked in the README.
