# Mira

**The open-source AI code reviewer that's actually open.** Self-host every feature — full review engine, codebase indexing, vulnerability scanning, custom rules, org-wide package search, dashboard, learning loop. No paid tier, no license key, no SaaS upsell.

[![Sponsor](https://img.shields.io/github/sponsors/miracodeai?style=social)](https://github.com/sponsors/miracodeai)

Mira reviews your pull requests using your choice of LLM (via [OpenRouter](https://openrouter.ai), which fronts Anthropic, OpenAI, Google, DeepSeek, and more) and posts concise, actionable feedback. The noise filter, confidence clamping, and learning loop ensure you only see comments that matter. See [`FEATURES.md`](FEATURES.md) for the full surface.

## Highlights

- **Any provider via OpenRouter**: Anthropic, OpenAI, Google Gemini, DeepSeek, and more — pay your provider directly, no Mira markup
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
- Webhook URL: `https://your-server.com/github/webhook`
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
  -e MIRA_MODEL=anthropic/claude-sonnet-4-6 \
  -e OPENROUTER_API_KEY=sk-or-... \
  ghcr.io/miracodeai/mira:latest
```

Mira talks to OpenRouter under the hood, so any model OpenRouter supports works. The `MIRA_MODEL` value is whatever the [OpenRouter Models page](https://openrouter.ai/models) lists — examples:

| Provider | `MIRA_MODEL` |
|----------|--------------|
| Anthropic | `anthropic/claude-sonnet-4-6` |
| Anthropic (cheap, indexing) | `anthropic/claude-haiku-4-5` |
| OpenAI | `openai/gpt-4o` |
| OpenAI (cheap) | `openai/gpt-4o-mini` |
| Google | `google/gemini-2.5-pro` |

Set `OPENROUTER_API_KEY` once; one key works across every provider. See [`src/mira/llm/models.json`](src/mira/llm/models.json) for the full registry of models Mira recognises (with pricing and per-purpose recommendations).

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
