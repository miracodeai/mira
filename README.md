# Mira

**The open-source AI code reviewer that's actually open.** Self-host every feature — full review engine, codebase indexing, vulnerability scanning, custom rules, org-wide package search, dashboard, learning loop. No paid tier, no license key, no SaaS upsell.

[![Sponsor](https://img.shields.io/github/sponsors/miracodeai?style=social)](https://github.com/sponsors/miracodeai)

Mira reviews your pull requests using any LLM (via [LiteLLM](https://github.com/BerriAI/litellm)) and posts concise, actionable feedback. The noise filter, confidence clamping, and learning loop ensure you only see comments that matter. See [`FEATURES.md`](FEATURES.md) for the full surface.

## Highlights

- **Any LLM**: OpenAI, Anthropic, Google, Azure, OpenRouter, Ollama — anything LiteLLM supports
- **Low noise**: Confidence thresholds, dedupe, severity sorting, per-PR comment caps
- **Indexed reviews**: full-repo code index gives the LLM real context, not just the diff
- **Learns your team**: synthesizes rules from rejected comments and human review patterns on merged PRs
- **Vulnerability scanning**: hourly OSV.dev poll surfaces CVEs across every package in every repo
- **Org-wide package search**: answer "which repos use `lodash@4.17.20`?" in seconds
- **Configurable**: `.mira.yml` for per-repo settings, custom + global rules in the dashboard
- **Self-host on day one**: Docker image, Railway / Fly.io / Render configs, SQLite or Postgres

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
