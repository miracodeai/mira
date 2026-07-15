"""Click CLI for Mira."""

from __future__ import annotations

import asyncio
import json
import logging
import sys

import click

from mira import __version__
from mira.config import load_config
from mira.core.engine import ReviewEngine
from mira.exceptions import MiraError
from mira.llm import create_llm
from mira.models import ReviewResult, Severity


def _format_text(result: ReviewResult) -> str:
    """Format review result as human-readable text."""
    lines: list[str] = []

    if result.thread_decisions:
        from mira.llm.prompts.verify_fixes import _extract_issue_description

        lines.append("Thread resolution:")
        for d in result.thread_decisions:
            status = "RESOLVE" if d.fixed else "KEEP"
            desc = _extract_issue_description(d.body)
            if len(desc) > 80:
                desc = desc[:77] + "..."
            lines.append(f"  [{status}] {d.path}:{d.line} — {desc}")
        fixed = sum(1 for d in result.thread_decisions if d.fixed)
        lines.append(f"  {fixed}/{len(result.thread_decisions)} thread(s) would be resolved.")
        lines.append("")

    if result.walkthrough:
        lines.append(result.walkthrough.to_markdown())
        lines.append("")
        lines.append("---")
        lines.append("")

    if result.summary:
        lines.append(result.summary)
        lines.append("")

    if not result.comments:
        lines.append("No issues found.")
        return "\n".join(lines)

    for i, c in enumerate(result.comments, 1):
        lines.append(f"{i}. [{c.severity.name}] {c.path}:{c.line} — {c.title}")
        lines.append(f"   {c.body}")
        if c.suggestion:
            lines.append(f"   Suggestion: {c.suggestion}")
        lines.append("")

    lines.append(f"Reviewed {result.reviewed_files} files, {len(result.comments)} comments.")
    if result.token_usage:
        lines.append(f"Tokens used: {result.token_usage.get('total_tokens', 0)}")

    return "\n".join(lines)


def _format_json(result: ReviewResult) -> str:
    """Format review result as JSON."""
    walkthrough_data = None
    if result.walkthrough:
        # Group file changes by their group label for JSON output
        groups: dict[str, list[dict[str, str]]] = {}
        for fc in result.walkthrough.file_changes:
            label = fc.group or "Other"
            groups.setdefault(label, []).append(
                {
                    "path": fc.path,
                    "change_type": fc.change_type.value,
                    "description": fc.description,
                }
            )
        effort_data = None
        if result.walkthrough.effort:
            effort_data = {
                "level": result.walkthrough.effort.level,
                "label": result.walkthrough.effort.label,
                "minutes": result.walkthrough.effort.minutes,
            }
        walkthrough_data = {
            "summary": result.walkthrough.summary,
            "change_groups": [{"label": label, "files": files} for label, files in groups.items()],
            "effort": effort_data,
            "sequence_diagram": result.walkthrough.sequence_diagram,
        }

    data = {
        "summary": result.summary,
        "walkthrough": walkthrough_data,
        "comments": [
            {
                "path": c.path,
                "line": c.line,
                "end_line": c.end_line,
                "severity": c.severity.name.lower(),
                "category": c.category,
                "title": c.title,
                "body": c.body,
                "confidence": c.confidence,
                "suggestion": c.suggestion,
            }
            for c in result.comments
        ],
        "reviewed_files": result.reviewed_files,
        "token_usage": result.token_usage,
    }
    return json.dumps(data, indent=2)


@click.group()
@click.version_option(version=__version__, prog_name="mira")
def main() -> None:
    """Mira — AI-powered PR reviewer."""


@main.command()
@click.option("--pr", "pr_url", default=None, help="PR/MR URL (GitHub PR or GitLab MR)")
@click.option("--stdin", "use_stdin", is_flag=True, help="Read diff from stdin")
@click.option("--model", envvar="MIRA_MODEL", default=None, help="LLM model to use")
@click.option("--max-comments", envvar="MIRA_MAX_COMMENTS", type=int, default=None)
@click.option("--confidence", envvar="MIRA_CONFIDENCE_THRESHOLD", type=float, default=None)
@click.option("--token", envvar="MIRA_GIT_TOKEN", default=None, help="Git platform API token")
@click.option(
    "--github-token",
    envvar="GITHUB_TOKEN",
    default=None,
    help="GitHub API token (alias for --token)",
)
@click.option("--dry-run", is_flag=True, help="Don't post review, just print results")
@click.option("--output", "output_format", type=click.Choice(["text", "json"]), default="text")
@click.option("--verbose", is_flag=True, help="Enable verbose logging")
@click.option("--config", "config_path", default=None, help="Path to .mira.yaml")
@click.option(
    "--no-walkthrough",
    is_flag=True,
    help="Skip walkthrough generation. Useful in dry-run loops where only the "
    "inline review is needed and the extra LLM call should be saved.",
)
def review(
    pr_url: str | None,
    use_stdin: bool,
    model: str | None,
    max_comments: int | None,
    confidence: float | None,
    token: str | None,
    github_token: str | None,
    dry_run: bool,
    output_format: str,
    verbose: bool,
    config_path: str | None,
    no_walkthrough: bool,
) -> None:
    """Review a pull request or diff."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(name)s %(levelname)s: %(message)s",
        stream=sys.stdout,
    )

    if not pr_url and not use_stdin:
        raise click.UsageError("Provide --pr <url> or --stdin")

    overrides: dict[str, object] = {}
    if model:
        overrides["llm.model"] = model
    if max_comments is not None:
        overrides["filter.max_comments"] = max_comments
    if confidence is not None:
        overrides["filter.confidence_threshold"] = confidence
    if no_walkthrough:
        overrides["review.walkthrough"] = False

    try:
        config = load_config(config_path, overrides)
    except MiraError as e:
        raise click.ClickException(str(e)) from e

    from mira.dashboard.models_config import llm_config_for

    llm = create_llm(llm_config_for("review", config.llm))
    indexing_llm = create_llm(llm_config_for("indexing", config.llm))

    git_token = token or github_token
    github_provider = None
    if pr_url:
        if not git_token:
            raise click.UsageError(
                "--token (or --github-token / GITHUB_TOKEN / MIRA_GIT_TOKEN) is required for PR review"
            )
        # Infer the platform from the URL shape; fall back to the configured default.
        if "/-/merge_requests/" in pr_url or "gitlab" in pr_url:
            provider_type = "gitlab"
        elif "/pulls/" in pr_url or "forgejo" in pr_url:
            provider_type = "forgejo"
        elif "/pull/" in pr_url or "github" in pr_url:
            provider_type = "github"
        else:
            provider_type = config.provider.type
        from mira.providers import create_provider, get_available_providers

        try:
            github_provider = create_provider(provider_type, git_token)
        except ValueError as err:
            available = ", ".join(get_available_providers()) or "(none)"
            raise click.UsageError(
                f"Unknown provider type {provider_type!r}. Available providers: {available}"
            ) from err

    engine = ReviewEngine(
        config=config, llm=llm, provider=github_provider, dry_run=dry_run, indexing_llm=indexing_llm
    )

    try:
        if use_stdin:
            diff_text = sys.stdin.read()
            result = asyncio.run(engine.review_diff(diff_text))
        else:
            result = asyncio.run(engine.review_pr(pr_url))  # type: ignore[arg-type]
    except MiraError as e:
        raise click.ClickException(str(e)) from e

    if output_format == "json":
        click.echo(_format_json(result))
    else:
        click.echo(_format_text(result))

    # Exit with non-zero if blockers found
    if any(c.severity >= Severity.BLOCKER for c in result.comments):
        sys.exit(1)


@main.command()
@click.option("--host", default="0.0.0.0", help="Host to bind to")
@click.option("--port", envvar="PORT", default=8000, type=int, help="Port to bind to")
@click.option(
    "--app-id",
    envvar="MIRA_GITHUB_APP_ID",
    default=None,
    help="GitHub App ID (enables the GitHub webhook route)",
)
@click.option(
    "--private-key",
    envvar="MIRA_GITHUB_PRIVATE_KEY",
    default=None,
    help="PEM contents or @path/to/key.pem",
)
@click.option(
    "--webhook-secret",
    envvar="MIRA_WEBHOOK_SECRET",
    default=None,
    help="Webhook secret from GitHub App settings",
)
@click.option(
    "--gitlab-token",
    envvar="MIRA_GITLAB_TOKEN",
    default=None,
    help="GitLab group/project access token (enables the GitLab webhook route)",
)
@click.option(
    "--gitlab-webhook-secret",
    envvar="MIRA_GITLAB_WEBHOOK_SECRET",
    default=None,
    help="Secret string configured on the GitLab project webhook (X-Gitlab-Token)",
)
@click.option(
    "--gitlab-base-url",
    envvar="MIRA_GITLAB_API_URL",
    default=None,
    help="GitLab API base for self-managed instances, e.g. https://gitlab.acme.com/api/v4",
)
@click.option(
    "--forgejo-token",
    envvar="MIRA_FORGEJO_TOKEN",
    default=None,
    help="Forgejo access token (enables the Forgejo webhook route)",
)
@click.option(
    "--forgejo-webhook-secret",
    envvar="MIRA_FORGEJO_WEBHOOK_SECRET",
    default=None,
    help="HMAC-SHA256 secret for verifying Forgejo webhook signatures (X-Forgejo-Signature header)",
)
@click.option(
    "--forgejo-base-url",
    envvar="MIRA_FORGEJO_API_URL",
    default=None,
    help="Forgejo API base, e.g. https://forgejo.example.com/api/v1",
)
@click.option(
    "--bot-name",
    envvar="MIRA_BOT_NAME",
    default=None,
    help="Bot @mention name. If unset, auto-detected from the platform's own identity.",
)
@click.option(
    "--config",
    "config_path",
    envvar="MIRA_CONFIG",
    type=click.Path(dir_okay=False),
    default=None,
    help=(
        "Path to a deployment-wide config YAML (model defaults, filter, review). "
        "Per-repo `.mira.yaml` files, when present, deep-merge over these defaults."
    ),
)
@click.option("--verbose", is_flag=True, help="Enable verbose logging")
def serve(
    host: str,
    port: int,
    app_id: str | None,
    private_key: str | None,
    webhook_secret: str | None,
    gitlab_token: str | None,
    gitlab_webhook_secret: str | None,
    gitlab_base_url: str | None,
    forgejo_token: str | None,
    forgejo_webhook_secret: str | None,
    forgejo_base_url: str | None,
    bot_name: str | None,
    config_path: str | None,
    verbose: bool,
) -> None:
    """Run the Mira webhook server for GitHub, GitLab, and/or Forgejo."""
    try:
        import asyncio

        import uvicorn

        from mira.config import set_global_defaults
        from mira.platforms.forgejo.auth import ForgejoTokenAuth
        from mira.platforms.github.auth import GitHubAppAuth
        from mira.platforms.gitlab.auth import GitLabTokenAuth
        from mira.platforms.server import create_app
    except ImportError as exc:
        raise click.ClickException(
            f"Missing dependency: {exc}. Install with: pip install mira-reviewer[serve]"
        ) from exc

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(name)s %(levelname)s: %(message)s",
        stream=sys.stdout,
    )

    if config_path:
        try:
            set_global_defaults(config_path)
            click.echo(f"Loaded deployment config: {config_path}")
        except Exception as exc:
            raise click.ClickException(f"Invalid --config file: {exc}") from exc

    github_configured = bool(app_id and private_key and webhook_secret)
    gitlab_configured = bool(gitlab_token and gitlab_webhook_secret)
    forgejo_configured = bool(forgejo_token and forgejo_webhook_secret)
    if not github_configured and not gitlab_configured and not forgejo_configured:
        raise click.ClickException(
            "No platform configured. Provide GitHub App creds (--app-id, --private-key, "
            "--webhook-secret) and/or GitLab creds (--gitlab-token, --gitlab-webhook-secret)."
        )

    app_auth = None
    gitlab_auth = None
    forgejo_auth = None

    if github_configured:
        assert private_key is not None
        assert app_id is not None
        if private_key.startswith("@"):
            key_path = private_key[1:]
            try:
                with open(key_path) as f:
                    private_key = f.read()
            except FileNotFoundError:
                raise click.ClickException(f"Private key file not found: {key_path}") from None
        app_auth = GitHubAppAuth(app_id=app_id, private_key=private_key)

    if gitlab_configured:
        assert gitlab_token is not None
        gitlab_auth = GitLabTokenAuth(gitlab_token, gitlab_base_url or "https://gitlab.com/api/v4")

    if forgejo_configured:
        assert forgejo_token is not None
        forgejo_auth = ForgejoTokenAuth(
            forgejo_token, forgejo_base_url or "https://codeberg.org/api/v1"
        )

    # Auto-detect the bot @mention from whichever platform's own identity when
    # the user didn't override it. Falls back to "miracodeai" on a lookup blip.
    if not bot_name:
        identity_auth = app_auth or gitlab_auth or forgejo_auth
        if identity_auth is not None:
            bot_name = asyncio.run(identity_auth.get_bot_identity()) or "miracodeai"
            click.echo(f"Detected bot @mention: @{bot_name}")
        else:
            bot_name = "miracodeai"

    # Persist the resolved name so the dashboard UI can show the real handle.
    try:
        from mira.dashboard.api import _app_db

        _app_db.set_setting("bot_name", bot_name)
    except Exception as exc:
        click.echo(f"Warning: could not persist bot_name for the dashboard: {exc}")

    app = create_app(
        app_auth=app_auth,
        webhook_secret=webhook_secret,
        bot_name=bot_name,
        gitlab_auth=gitlab_auth,
        gitlab_webhook_secret=gitlab_webhook_secret,
        forgejo_auth=forgejo_auth,
        forgejo_webhook_secret=forgejo_webhook_secret,
    )

    platforms = ", ".join(
        p
        for p, on in [
            ("GitHub", github_configured),
            ("GitLab", gitlab_configured),
            ("Forgejo", forgejo_configured),
        ]
        if on
    )
    click.echo(f"Starting Mira webhook server ({platforms}) on {host}:{port}")
    uvicorn.run(app, host=host, port=port)
