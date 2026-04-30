# Contributing to Mira

Thanks for your interest in contributing to Mira. This guide covers setup, the
contribution flow, and the few quality gates we expect before a PR lands.

By contributing, you agree your contributions are licensed under the
[Apache 2.0 License](LICENSE) and you'll abide by our
[Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

Mira uses [uv](https://github.com/astral-sh/uv) for Python dependency
management. The lockfile is committed.

```bash
git clone https://github.com/miracodeai/mira.git
cd mira

# Install deps (uv reads pyproject.toml + uv.lock)
uv sync --extra dev --extra serve
```

For the dashboard UI:

```bash
cd ui/mira
npm install
```

## Running locally

The fastest path is the bundled launcher script, which loads `.env`, ensures
the GitHub App private key is in place, and starts both the API and the
dashboard UI:

```bash
cp .env.example .env   # then edit your secrets
bash scripts/start_local.sh
```

API: <http://localhost:8100>
Dashboard: <http://localhost:5173>

For the GitHub App webhook to reach a local server, point it at an
ngrok / cloudflared / smee tunnel terminating at port 8100.

## Quality gates

All four must be green before a PR can merge:

```bash
# Lint + format
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/

# Type check
uv run mypy src/mira/ --ignore-missing-imports

# Tests
uv run pytest tests/ --ignore=tests/evals --ignore=tests/test_integration.py

# UI type check (if you touched the UI)
cd ui/mira && npx tsc --noEmit
```

CI runs the same matrix on Python 3.11 and 3.12.

## PR flow

1. Fork and branch from `main`.
2. Make your change. Add tests for new behavior; update existing tests when
   you change behavior.
3. Run the quality gates above.
4. Open a PR using the template. Keep one concern per PR — split if it grows.

## Reporting bugs

Use the bug-report template at
<https://github.com/miracodeai/mira/issues/new/choose>. Include:

- Steps to reproduce (a minimal repro is best)
- Expected vs. actual behavior
- Mira version (or commit SHA) and the LLM provider/model you're running

**Security vulnerabilities should not be filed publicly.** See
[SECURITY.md](SECURITY.md) for the private disclosure process.

## Requesting features

Use the feature-request template. Lead with the problem, not the solution.
We're more likely to merge a feature when we understand what you're trying
to accomplish than when we get a fully-specified design with no context.

## Code style

- Follow existing patterns in the file you're editing — Mira values
  consistency over personal preference.
- Use type annotations on public functions.
- Default to writing no comments. Add them only when the *why* would
  surprise a future reader (subtle invariants, workarounds for specific
  bugs, hidden constraints).
- Don't add scaffolding for hypothetical future features. Three similar
  lines is better than a premature abstraction.

## License

By contributing, you agree your contributions are licensed under the
[Apache 2.0 License](LICENSE).
