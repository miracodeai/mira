# Mira Features

Mira is a self-hostable, fully open-source AI code reviewer. Everything below is included — no paid tier, no license key, no upsell prompts. The project is licensed under [Apache 2.0](LICENSE).

## Review engine

- AI-powered inline PR comments with severity and confidence scoring
- PR walkthroughs / summaries with file coverage, comment breakdown, and per-severity stats
- Streaming walkthrough: placeholder posts within ~1s, narrative within ~10s, full review within a minute
- Multi-file reasoning across a diff
- Parallel chunk review (`asyncio.gather` with configurable concurrency)
- Cross-chunk and cross-file deduplication (Jaccard similarity on titles + bodies)
- Cross-PR overlap detection: flags other open PRs touching the same code (merge-conflict risk) or pursuing the same goal (duplicate effort) in the walkthrough
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
- Manifest-based package extraction (`package.json`, `requirements.txt`, `pyproject.toml`, `go.mod`, `composer.json`, `Dockerfile`) with version constraints — zero LLM cost

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

- `@miracodeai reject` thread resolution with feedback-event recording
- Deterministic rule synthesis from reject signals
- LLM-powered synthesis of human review patterns from merged PRs (extracts recurring themes from human reviewer comments)
- Feedback stats API for inspecting the learning loop
- Synthesized rules inject into future reviews automatically

## Platform integrations

- GitHub App with webhook support — works against github.com and GitHub Enterprise Server (set `MIRA_GITHUB_API_URL`)
- GitLab with full feature parity — merge-request reviews, `@mention` commands, thread auto-resolution, indexing — via a group or project access token (`MIRA_GITLAB_TOKEN`); self-managed instances via `MIRA_GITLAB_API_URL`
- Forgejo / Codeberg — pull-request reviews and `@mention` commands via an access token (`MIRA_FORGEJO_TOKEN`); self-hosted instances via `MIRA_FORGEJO_API_URL`
- One `mira serve` deployment reviews on any combination of platforms, each on its own webhook route
- Bot chat: mention the bot on any PR or MR to ask questions
- Cancel-in-progress indexing from the UI

## Bring your own LLM

- Any provider available through OpenRouter — Anthropic, OpenAI, Google Gemini, DeepSeek, and more — so you pay your provider directly with no Mira markup
- Any OpenAI-compatible endpoint via `llm.base_url` — vLLM, Ollama, LiteLLM proxy, LocalAI, llama.cpp, Together, Fireworks, Groq
- AWS Bedrock as a direct backend (Converse API, standard AWS credential chain)
- Separate model configuration for indexing (cheap) vs review (powerful)
- Fallback-model chain
- Adjustable review thinking mode (`llm.review_reasoning_effort`) for models with extended reasoning

## Dashboard and analytics

- Org-level stats: total reviews, comments, tokens, per-severity counts
- Period-based time-series (daily / weekly / monthly) with bar and line charts
- Issue-severity stacked breakdown per period
- Issue-category breakdown per period
- Per-repo views: files indexed, dependencies, blast radius, packages, last-indexed timestamp
- Indexing status dashboard with cost estimates
- Review event stream
- Threaded PR activity timeline: each review pass with its comments and the human replies nested under them
- Review page (admin): stale/waiting PRs, reviewer-responsiveness leaderboard, throughput trends, rubber-stamp detection, open-PR status board
- Contributor analytics: authoring stats, year-long contribution heatmap, and Mira's review-quality signal per contributor
- Pending-uninstall review queue

## Configuration

- Repo-level `.mira.yaml` configuration file
- Per-repo context entries (architecture docs, coding guidelines)
- Confidence thresholds (global and per-category), severity thresholds, comment caps
- Exclude patterns and per-language overrides
- PR author allow/deny lists (`filter.allowed_authors` / `filter.blocked_authors`) for muting bots or scoping auto-review
- Cross-PR overlap tuning (`review.overlap`): candidate cap, confidence floor, title-similarity threshold
- Opt-in ensemble mode (`review.ensemble_runs`): review each chunk N times and keep majority-vote findings
- Thread auto-resolution toggle (`review.auto_resolve_conversations`)
- `context_lines`, `max_concurrent_chunks`, and `index.max_file_size` tuning knobs

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
