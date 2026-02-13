# Contributing to Mira

Thanks for your interest in contributing to Mira! This guide will help you get started.

## Development Setup

```bash
git clone https://github.com/mira-reviewer/mira.git
cd mira
pip install -e ".[dev,serve]"
```

## Running Tests

```bash
# All tests
uv run pytest tests/ -v

# Specific test file
uv run pytest tests/test_engine.py -v

# With coverage
uv run pytest tests/ --cov=src/mira
```

## Code Quality

Before submitting a PR, make sure all checks pass:

```bash
# Lint
uv run ruff check src/ tests/

# Format
uv run ruff format src/ tests/

# Type check
uv run mypy src/mira/ --ignore-missing-imports
```

## Submitting Changes

1. Fork the repository and create a branch from `main`.
2. Make your changes and add tests for any new functionality.
3. Ensure all tests pass and linting is clean.
4. Write a clear PR description explaining what changed and why.
5. Submit a pull request to `main`.

## Reporting Bugs

Use the [bug report template](https://github.com/mira-reviewer/mira/issues/new?template=bug_report.md) and include:

- Steps to reproduce
- Expected vs actual behavior
- Mira version and LLM provider/model used

## Requesting Features

Use the [feature request template](https://github.com/mira-reviewer/mira/issues/new?template=feature_request.md) and describe:

- The problem you're trying to solve
- Your proposed solution
- Any alternatives you considered

## Code Style

- Follow existing patterns in the codebase.
- Use type annotations for function signatures.
- Keep changes focused — one concern per PR.

## License

By contributing, you agree that your contributions will be licensed under the [Apache 2.0 License](LICENSE).
