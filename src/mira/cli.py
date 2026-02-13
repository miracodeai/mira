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
from mira.llm.provider import LLMProvider
from mira.models import ReviewResult, Severity


def _format_text(result: ReviewResult) -> str:
    """Format review result as human-readable text."""
    lines: list[str] = []

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
@click.option("--pr", "pr_url", default=None, help="PR URL (github.com/o/r/pull/N or o/r#N)")
@click.option("--stdin", "use_stdin", is_flag=True, help="Read diff from stdin")
@click.option("--model", envvar="MIRA_MODEL", default=None, help="LLM model to use")
@click.option("--max-comments", envvar="MIRA_MAX_COMMENTS", type=int, default=None)
@click.option("--confidence", envvar="MIRA_CONFIDENCE_THRESHOLD", type=float, default=None)
@click.option("--github-token", envvar="GITHUB_TOKEN", default=None, help="GitHub API token")
@click.option("--dry-run", is_flag=True, help="Don't post review, just print results")
@click.option("--output", "output_format", type=click.Choice(["text", "json"]), default="text")
@click.option("--verbose", is_flag=True, help="Enable verbose logging")
@click.option("--config", "config_path", default=None, help="Path to .mira.yml")
def review(
    pr_url: str | None,
    use_stdin: bool,
    model: str | None,
    max_comments: int | None,
    confidence: float | None,
    github_token: str | None,
    dry_run: bool,
    output_format: str,
    verbose: bool,
    config_path: str | None,
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

    try:
        config = load_config(config_path, overrides)
    except MiraError as e:
        raise click.ClickException(str(e)) from e

    llm = LLMProvider(config.llm)

    github_provider = None
    if pr_url:
        if not github_token:
            raise click.UsageError(
                "--github-token or GITHUB_TOKEN env var is required for PR review"
            )
        from mira.providers.github import GitHubProvider

        github_provider = GitHubProvider(github_token)

    engine = ReviewEngine(config=config, llm=llm, provider=None if dry_run else github_provider)

    try:
        if use_stdin:
            diff_text = sys.stdin.read()
            result = asyncio.run(engine.review_diff(diff_text))
        else:
            if dry_run and github_provider:

                async def _dry_run_review() -> ReviewResult:
                    pr_info = await github_provider.get_pr_info(pr_url)  # type: ignore[arg-type]
                    diff_text = await github_provider.get_pr_diff(pr_info)
                    return await engine._review_diff_internal(
                        diff_text,
                        pr_title=pr_info.title,
                        pr_description=pr_info.description,
                    )

                result = asyncio.run(_dry_run_review())
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
    required=True,
    help="GitHub App ID",
)
@click.option(
    "--private-key",
    envvar="MIRA_GITHUB_PRIVATE_KEY",
    required=True,
    help="PEM contents or @path/to/key.pem",
)
@click.option(
    "--webhook-secret",
    envvar="MIRA_WEBHOOK_SECRET",
    required=True,
    help="Webhook secret from GitHub App settings",
)
@click.option("--bot-name", envvar="MIRA_BOT_NAME", default="miracodeai", help="Bot @mention name")
@click.option(
    "--posthog-api-key",
    envvar="POSTHOG_API_KEY",
    default=None,
    help="PostHog API key for anonymous usage metrics (disabled if not set)",
)
@click.option(
    "--posthog-host",
    envvar="POSTHOG_HOST",
    default=None,
    help="PostHog host URL",
)
@click.option("--verbose", is_flag=True, help="Enable verbose logging")
def serve(
    host: str,
    port: int,
    app_id: str,
    private_key: str,
    webhook_secret: str,
    bot_name: str,
    posthog_api_key: str | None,
    posthog_host: str | None,
    verbose: bool,
) -> None:
    """Run the Mira GitHub App webhook server."""
    try:
        import uvicorn

        from mira.github_app.auth import GitHubAppAuth
        from mira.github_app.metrics import Metrics
        from mira.github_app.webhooks import create_app
    except ImportError as exc:
        raise click.ClickException(
            f"Missing dependency: {exc}. Install with: pip install mira-reviewer[serve]"
        ) from exc

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(name)s %(levelname)s: %(message)s",
        stream=sys.stdout,
    )

    # Support @path/to/key.pem syntax
    if private_key.startswith("@"):
        key_path = private_key[1:]
        try:
            with open(key_path) as f:
                private_key = f.read()
        except FileNotFoundError:
            raise click.ClickException(f"Private key file not found: {key_path}") from None

    app_auth = GitHubAppAuth(app_id=app_id, private_key=private_key)
    metrics = Metrics(api_key=posthog_api_key, host=posthog_host)
    app = create_app(
        app_auth=app_auth, webhook_secret=webhook_secret, bot_name=bot_name, metrics=metrics
    )

    click.echo(f"Starting Mira webhook server on {host}:{port}")
    uvicorn.run(app, host=host, port=port)
