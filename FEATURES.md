# Mira Features

Mira is a self-hostable, fully open-source AI code reviewer. Everything below is included — no paid tier, no license key, no upsell prompts. The project is licensed under [Apache 2.0](LICENSE).

## Review engine

- AI-powered inline PR comments with severity and confidence scoring
- PR walkthroughs / summaries with file coverage, comment breakdown, and per-severity stats
- Streaming walkthrough: placeholder posts within ~1s, narrative within ~10s, full review within a minute
- Multi-file reasoning across a diff
- Parallel chunk review (`asyncio.gather` with configurable concurrency)
- Cross-chunk and cross-file deduplication (Jaccard similarity on titles + bodies)
- GitHub suggestion blocks with runtime fence sanitization
- Noise filtering: confidence thresholds, severity sorting, per-PR comment caps
- Per-language file-type support
- Confidence score auto-clamped to match findings (a blocker forces "Do not merge" regardless of the LLM's initial read)

## Codebase intelligence

- Full-repo code index with per-file summaries
- Dependency graph and relationships across files
- Cross-repo relationships and blast-radius analysis
- Blast-radius SVG rendering and interactive ReactFlow graph
- Relationship overrides and custom edges
- External reference tracking
- Manifest-based package extraction (`package.json`, `requirements.txt`, `pyproject.toml`, `go.mod`, `Dockerfile`) with version constraints — zero LLM cost

## Security

- **Vulnerability scanning** via OSV.dev — hourly background poll across every package in every repo. Surfaces critical/high/moderate/low CVEs with advisory links and fix versions.
- **Org-wide package search** — answer "which repos use lodash@4.17.20?" instantly. Built for incident response.
- Per-repo CVE badges inline with package listings
- Dashboard "Security alerts" widget showing org-wide open vulnerabilities by severity

## Custom rules

- Per-repo custom rules with full CRUD (unlimited)
- Global rules that apply to every repo in the org
- Both inject into the review prompt automatically
- Rules UI at `/rules` in the dashboard

## Learning from feedback

- `@mira-bot reject` thread resolution with feedback-event recording
- Deterministic rule synthesis from reject signals
- LLM-powered synthesis of human review patterns from merged PRs (extracts recurring themes from human reviewer comments)
- Feedback stats API for inspecting the learning loop
- Synthesized rules inject into future reviews automatically

## Platform integrations

- GitHub App with webhook support — works against github.com and GitHub Enterprise Server (set `MIRA_GITHUB_API_URL`)
- `mira serve` command for running the GitHub App server
- GitHub App chat: mention the bot on any PR to ask questions
- Cancel-in-progress indexing from the UI

## Bring your own LLM

- Any provider available through OpenRouter — Anthropic, OpenAI, Google Gemini, DeepSeek, and more — so you pay your provider directly with no Mira markup
- Separate model configuration for indexing (cheap) vs review (powerful)
- Fallback-model chain
- Direct (non-OpenRouter) provider integration is on the roadmap for users who'd rather hold their own API keys per provider

## Dashboard and analytics

- Org-level stats: total reviews, comments, tokens, per-severity counts
- Period-based time-series (daily / weekly / monthly) with bar and line charts
- Issue-severity stacked breakdown per period
- Issue-category breakdown per period
- Per-repo views: files indexed, dependencies, blast radius, packages, last-indexed timestamp
- Indexing status dashboard with cost estimates
- Review event stream
- Pending-uninstall review queue

## Configuration

- Repo-level `.mira.yml` configuration file
- Per-repo context entries (architecture docs, coding guidelines)
- Confidence thresholds, severity thresholds, comment caps
- Exclude patterns and per-language overrides
- `context_lines` and `max_concurrent_chunks` tuning knobs

## Storage and deployment

- SQLite backend (default, zero-config)
- PostgreSQL backend for horizontal scale
- Single-image Docker deployment
- Reference deploy configs: Railway, Fly.io, Render
- Self-hostable on any platform that runs Docker — no phone-home, no required telemetry

## Admin and setup

- Setup wizard for first-run GitHub App configuration
- Admin user management
- Model-selection UI for indexing and review models
- Background OSV vulnerability poller, indexing backfill, and webhook-driven re-indexing
