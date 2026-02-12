# Mira

AI-powered PR reviewer with low-noise filtering.

Mira reviews your pull requests using any LLM (via [LiteLLM](https://github.com/BerriAI/litellm)) and posts concise, actionable feedback. Its noise filter ensures you only see comments that matter.

## Features

- **Any LLM**: Works with OpenAI, Anthropic, Google, Azure, and any LiteLLM-supported provider
- **Low noise**: Confidence thresholds, deduplication, severity sorting, and comment caps
- **GitHub Action**: Drop-in action for CI/CD pipelines
- **CLI**: Review PRs or diffs from the command line
- **Configurable**: `.mira.yml` for per-repo settings

## Installation

```bash
pip install mira-reviewer
```

## Quick Start

### CLI

Review a PR:

```bash
export GITHUB_TOKEN="ghp_..."
export OPENAI_API_KEY="sk-..."

mira review --pr https://github.com/owner/repo/pull/123
```

Review a diff from stdin:

```bash
git diff main | mira review --stdin --dry-run
```

### GitHub Action

Add to `.github/workflows/mira.yml`:

```yaml
name: Mira Review
on:
  pull_request:
    types: [opened, synchronize]

permissions:
  pull-requests: write

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: mira-reviewer/mira/.github/actions/mira-review@main
        with:
          api_key: ${{ secrets.OPENAI_API_KEY }}
```

## Configuration

Create a `.mira.yml` in your repo root (see [`.mira.yml.example`](.mira.yml.example)):

```yaml
llm:
  model: "openai/gpt-4o"
  fallback_model: "openai/gpt-4o-mini"

filter:
  confidence_threshold: 0.7
  max_comments: 5

review:
  context_lines: 3
```

## CLI Options

```
mira review [OPTIONS]

Options:
  --pr TEXT                PR URL or shorthand (owner/repo#N)
  --stdin                  Read diff from stdin
  --model TEXT             LLM model (env: MIRA_MODEL)
  --max-comments INT       Max comments (env: MIRA_MAX_COMMENTS)
  --confidence FLOAT       Min confidence (env: MIRA_CONFIDENCE_THRESHOLD)
  --github-token TEXT      GitHub token (env: GITHUB_TOKEN)
  --dry-run                Print results without posting
  --output [text|json]     Output format
  --verbose                Enable debug logging
  --config PATH            Path to .mira.yml
```

## Development

```bash
git clone https://github.com/mira-reviewer/mira.git
cd mira
pip install -e .
pip install pytest pytest-asyncio pytest-cov pytest-mock ruff mypy

# Run tests
pytest tests/ -v

# Lint
ruff check src/ tests/

# Type check
mypy src/mira/
```

## License

Apache 2.0 — see [LICENSE](LICENSE).
