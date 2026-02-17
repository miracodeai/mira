# Mira

AI-powered PR reviewer with low-noise filtering.

Mira reviews your pull requests using any LLM (via [LiteLLM](https://github.com/BerriAI/litellm)) and posts concise, actionable feedback. Its noise filter ensures you only see comments that matter.

## Features

- **Any LLM**: Works with OpenAI, Anthropic, Google, Azure, and any LiteLLM-supported provider
- **Low noise**: Confidence thresholds, deduplication, severity sorting, and comment caps
- **GitHub Action**: Drop-in action for CI/CD pipelines
- **CLI**: Review PRs or diffs from the command line
- **Configurable**: `.mira.yml` for per-repo settings

## Quick Start

### GitHub App (self-hosted)

Run Mira as a GitHub App that auto-reviews every PR and responds to comments.

**1. Create a GitHub App** at [github.com/settings/apps/new](https://github.com/settings/apps/new):
- Webhook URL: `https://your-server.com/webhook`
- Permissions: Pull Requests (read+write), Contents (read), Issues (read+write)
- Events: Pull requests, Issue comments
- Generate a private key (.pem)

**2. Deploy:**

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/workspace/templates/05874bad-2d98-43f4-aa93-332f394e9ebd)

Or with Docker:

```bash
docker run -p 8000:8000 \
  -e MIRA_GITHUB_APP_ID=123456 \
  -e MIRA_GITHUB_PRIVATE_KEY="$(cat private-key.pem)" \
  -e MIRA_WEBHOOK_SECRET=your-secret \
  -e MIRA_MODEL=openai/gpt-4o \
  -e OPENAI_API_KEY=sk-... \
  ghcr.io/mira-reviewer/mira:latest
```

Mira uses [LiteLLM](https://docs.litellm.ai/docs/providers) under the hood, so you can use any supported provider. Just set the model and the matching API key:

| Provider | `MIRA_MODEL` | API key env var |
|----------|-------------|-----------------|
| OpenAI | `openai/gpt-4o` | `OPENAI_API_KEY` |
| Anthropic | `anthropic/claude-sonnet-4-5-20250929` | `ANTHROPIC_API_KEY` |
| OpenRouter | `anthropic/claude-sonnet-4-5` | `OPENROUTER_API_KEY` |
| Google Gemini | `gemini/gemini-2.5-pro` | `GEMINI_API_KEY` |
| Azure OpenAI | `azure/my-gpt4o-deployment` | `AZURE_API_KEY` |

**3. Install the app** on your repos — every PR gets auto-reviewed.

**Chat with Mira:** Comment `@mira-bot <question>` on any PR to ask about the code.

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

### CLI

```bash
pip install mira-reviewer
```

Review a PR:

```bash
export GITHUB_TOKEN="ghp_..."
export OPENAI_API_KEY="sk-..."  # or ANTHROPIC_API_KEY, OPENROUTER_API_KEY, etc.

mira review --pr https://github.com/owner/repo/pull/123 --model openai/gpt-4o
```

Review a diff from stdin:

```bash
git diff main | mira review --stdin --dry-run
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
pip install -e ".[dev,serve]"

# Run tests
pytest tests/ -v

# Lint
ruff check src/ tests/

# Type check
mypy src/mira/ --ignore-missing-imports
```

## License

Apache 2.0 — see [LICENSE](LICENSE).
