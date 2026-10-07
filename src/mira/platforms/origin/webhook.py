"""Cursor Origin webhook handling: Ed25519 JWKS signature verification, event
normalization, handlers.

Origin wraps each delivery in an envelope ``{deliveryId, appId, installationId,
event: {id, type, eventTime, payload}}``. Signature headers use Standard
Webhooks names but Origin signs ``SHA-256(id.timestamp.body)`` hex with Ed25519
(``v1ed,...``), verified against ``GET /v1/origin/keys``.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import time
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from mira.config import load_config
from mira.platforms import profiles
from mira.platforms.auth import PlatformAuth
from mira.platforms.fetch import make_fetcher
from mira.platforms.mentions import (
    author_is_filtered,
    command_after_mention,
    has_mention,
    mention_names,
    strip_mentions,
)
from mira.providers import create_provider

logger = logging.getLogger(__name__)

# Cache Origin JWKS for 10 minutes (matches Cache-Control on /keys).
_JWKS_CACHE: tuple[float, list[Ed25519PublicKey]] | None = None
_JWKS_TTL = 600


def _api_base() -> str:
    return (profiles.resolve("origin").get("api_url") or "https://api.cursor.com/v1/origin").rstrip(
        "/"
    )


def _jwk_to_public_key(jwk: dict[str, Any]) -> Ed25519PublicKey | None:
    """Convert an OKP/Ed25519 JWK to a cryptography public key."""
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        return None
    x_b64 = jwk.get("x")
    if not isinstance(x_b64, str) or not x_b64:
        return None
    # JWK uses URL-safe base64 without padding.
    pad = "=" * (-len(x_b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(x_b64 + pad)
    except Exception:
        return None
    if len(raw) != 32:
        return None
    # cryptography accepts raw Ed25519 public bytes via from_public_bytes.
    return Ed25519PublicKey.from_public_bytes(raw)


async def _load_jwks(force: bool = False) -> list[Ed25519PublicKey]:
    global _JWKS_CACHE
    now = time.time()
    if not force and _JWKS_CACHE and now - _JWKS_CACHE[0] < _JWKS_TTL:
        return _JWKS_CACHE[1]
    keys: list[Ed25519PublicKey] = []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{_api_base()}/keys")
            resp.raise_for_status()
            for jwk in (resp.json() or {}).get("keys") or []:
                pk = _jwk_to_public_key(jwk)
                if pk is not None:
                    keys.append(pk)
    except Exception as exc:
        logger.warning("Failed to fetch Origin JWKS: %s", exc)
        if _JWKS_CACHE:
            return _JWKS_CACHE[1]
        return []
    _JWKS_CACHE = (now, keys)
    return keys


async def verify_origin_signature(
    body: bytes,
    webhook_id: str,
    webhook_timestamp: str,
    webhook_signature: str,
) -> bool:
    """Verify an Origin webhook delivery signature.

    Construction: ``lowercaseHex(SHA-256("<id>.<timestamp>.<raw-body>"))``,
    verified as Ed25519 over the UTF-8 hex digest against active JWKS keys.
    Rejects timestamps more than five minutes from now.
    """
    if not webhook_id or not webhook_timestamp or not webhook_signature:
        return False
    try:
        ts = int(webhook_timestamp)
    except ValueError:
        return False
    if abs(int(time.time()) - ts) > 300:
        return False

    sig_b64 = None
    for part in webhook_signature.split():
        if part.startswith("v1ed,"):
            sig_b64 = part[5:]
            break
    if not sig_b64:
        return False
    try:
        signature = base64.b64decode(sig_b64)
    except Exception:
        return False

    digest = hashlib.sha256(f"{webhook_id}.{webhook_timestamp}.".encode() + body).hexdigest()
    digest_bytes = digest.encode("utf-8")

    keys = await _load_jwks()
    for pk in keys:
        try:
            pk.verify(signature, digest_bytes)
            return True
        except Exception:
            continue
    # One refresh in case of key rotation mid-delivery.
    keys = await _load_jwks(force=True)
    for pk in keys:
        try:
            pk.verify(signature, digest_bytes)
            return True
        except Exception:
            continue
    return False


def unwrap_envelope(body: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Return ``(event_type, installation_id, payload)`` from an Origin delivery."""
    event = body.get("event") or {}
    event_type = str(event.get("type") or "")
    installation_id = str(body.get("installationId") or "")
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    return event_type, installation_id, payload


def _actor_handle(actor: dict[str, Any] | None) -> str:
    if not actor:
        return ""
    user = actor.get("user") or {}
    if user.get("handle"):
        return str(user["handle"])
    if user.get("displayName"):
        return str(user["displayName"])
    app = actor.get("app") or {}
    if app.get("displayName"):
        return str(app["displayName"])
    if app.get("id"):
        return str(app["id"])
    return ""


def _repo_owner_name(repo: dict[str, Any]) -> tuple[str, str] | None:
    owner = (repo.get("owner") or {}).get("slug") or ""
    name = repo.get("name") or ""
    if not owner or not name:
        full = repo.get("fullName") or ""
        if "/" in full:
            owner, name = full.split("/", 1)
    if not owner or not name:
        return None
    return owner, name


def _pr_web_url(owner: str, repo: str, number: int | str, pr: dict[str, Any]) -> str:
    return pr.get("webUrl") or f"https://cursor.com/codebase/{owner}/{repo}/pull/{number}"


async def list_origin_installation_repos(token: str, base_url: str) -> list[dict]:
    """Repos accessible to an installation token (paginated)."""
    out: list[dict] = []
    page_token: str | None = None
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            params: dict[str, Any] = {"pageSize": 100}
            if page_token:
                params["pageToken"] = page_token
            resp = await client.get(
                f"{base_url.rstrip('/')}/installation/repos",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
            if resp.status_code != 200:
                logger.warning(
                    "Origin installation repo list failed: %d %s",
                    resp.status_code,
                    resp.text[:200],
                )
                break
            data = resp.json() or {}
            repos = data.get("repositories") or []
            out.extend(repos)
            page_token = data.get("nextPageToken") or None
            if not page_token or not repos:
                break
    return out


async def backfill_origin_repos(auth: PlatformAuth, installation_id: str | None = None) -> int:
    """Register Origin repos accessible to the auth token / installation."""
    from mira.config import load_config
    from mira.platforms.index_handlers import _get_app_db

    token = await auth.get_token(installation_id)
    base_url = _api_base()
    repos = await list_origin_installation_repos(token, base_url)
    db = _get_app_db()
    exclude_patterns = load_config().filter.exclude_patterns
    fetcher = make_fetcher("origin", token)
    n = 0
    for r in repos:
        owner_name = _repo_owner_name(r)
        if not owner_name:
            continue
        owner, repo = owner_name
        db.register_repo(owner, repo, platform="origin")
        # Origin sparse summaries don't always carry visibility; treat as private.
        db.set_repo_visibility(owner, repo, True, platform="origin")
        try:
            from mira.index.indexer import _should_index

            branch = await fetcher.default_branch(owner, repo)
            tree_paths = await fetcher.repo_tree(owner, repo, branch)
            indexable = [p for p in tree_paths if _should_index(p, exclude_patterns)]
            db.set_repo_file_count(owner, repo, len(indexable), platform="origin")
        except Exception as exc:
            logger.warning("Failed to count Origin files for %s/%s: %s", owner, repo, exc)
        n += 1
    logger.info("Origin: discovered + registered %d accessible repo(s)", n)
    return n


async def handle_origin_pr(
    payload: dict[str, Any],
    auth: PlatformAuth,
    bot_name: str,
    installation_id: str,
    event_type: str,
) -> None:
    """Review a pull request on create / reopen / head push."""
    from mira.platforms.handlers import run_pr_review
    from mira.platforms.index_handlers import _get_app_db

    if event_type == "pull_request.head_ref.pushed" and not load_config().review.review_on_synchronize:
        logger.info("Origin PR head push skipped — review.review_on_synchronize is off")
        return

    pr = payload.get("pullRequest") or payload.get("pull_request") or {}
    repo = pr.get("repository") or payload.get("repository") or {}
    owner_name = _repo_owner_name(repo)
    if not owner_name:
        return
    owner, repo_name = owner_name
    number_raw = pr.get("number")
    if number_raw is None:
        return
    number = int(number_raw)
    pr_url = _pr_web_url(owner, repo_name, number, pr)

    names = mention_names(bot_name, await auth.get_bot_identity())
    description = pr.get("body", "") or ""
    if any(re.search(rf"@{re.escape(n)}[ \t]+ignore\b", description, re.IGNORECASE) for n in names):
        logger.info(
            "PR %s/%s#%d ignored via @%s ignore in description", owner, repo_name, number, bot_name
        )
        return

    try:
        _get_app_db().register_repo(owner, repo_name, platform="origin")
        token = await auth.get_token(installation_id)
        provider = create_provider("origin", token)
        # Labels aren't on the webhook snapshot — fetch when needed.
        await run_pr_review(
            provider,
            owner,
            repo_name,
            number,
            pr_url,
            True,  # Origin repos treated private by default
            bot_name,
            platform="origin",
            pr_title=pr.get("title", "") or "",
        )
    except Exception:
        logger.exception(
            "Error handling Origin %s for %s/%s#%d", event_type, owner, repo_name, number
        )


async def handle_origin_push(
    payload: dict[str, Any], auth: PlatformAuth, bot_name: str, installation_id: str
) -> None:
    """Incrementally index a push to the default branch."""
    from mira.platforms.index_handlers import _get_app_db, run_incremental_index

    repo = payload.get("repository") or {}
    owner_name = _repo_owner_name(repo)
    if not owner_name:
        return
    owner, repo_name = owner_name

    repo_record = _get_app_db().get_repo(owner, repo_name, platform="origin")
    if not repo_record or repo_record.status not in ("ready", "indexing"):
        logger.debug("Origin push to %s/%s skipped — not indexed", owner, repo_name)
        return

    try:
        token = await auth.get_token(installation_id)
        fetcher = make_fetcher("origin", token)
        default_branch = await fetcher.default_branch(owner, repo_name)
    except Exception:
        logger.exception("Origin push: failed to resolve default branch for %s/%s", owner, repo_name)
        return

    # Origin push payloads have refUpdates, not a commits array. Without a
    # changed-file list we re-index the whole tree tip by treating the push as
    # a full refresh of touched refs on the default branch.
    for update in payload.get("refUpdates") or []:
        ref = update.get("ref") or ""
        if not ref.startswith("refs/heads/"):
            continue
        branch = ref[len("refs/heads/") :]
        if branch != default_branch:
            continue
        if update.get("deleted"):
            continue
        try:
            # Compare before→after for changed files when both SHAs are present.
            before = update.get("before") or ""
            after = update.get("after") or ""
            changed: list[str] = []
            removed: list[str] = []
            if before and after and set(before) != {"0"} and set(after) != {"0"}:
                provider = create_provider("origin", token)
                # Reuse compare files via a synthetic PRInfo.
                from mira.models import PRInfo

                pr_info = PRInfo(
                    title="",
                    description="",
                    base_branch=default_branch,
                    head_branch=default_branch,
                    url="",
                    number=0,
                    owner=owner,
                    repo=repo_name,
                    head_sha=after,
                    platform="origin",
                )
                # Parse filenames from the compare diff header lines.
                diff = await provider.get_compare_diff(pr_info, before, after)
                for line in diff.splitlines():
                    if line.startswith("+++ b/"):
                        changed.append(line[6:])
                    elif line.startswith("--- a/") and "dev/null" not in line:
                        # Track removals when status is deleted (no +++ b/).
                        pass
                    if line.startswith("deleted file mode"):
                        # previous --- a/ path is the removed file; handled below loosely
                        pass
                # Also pick up deleted paths from diff headers.
                pending_del: str | None = None
                for line in diff.splitlines():
                    if line.startswith("deleted file mode"):
                        pending_del = "yes"
                    elif pending_del and line.startswith("--- a/"):
                        removed.append(line[6:])
                        pending_del = None
                changed = [p for p in changed if p and p not in removed]

            await run_incremental_index(
                owner,
                repo_name,
                fetcher,
                changed,
                removed,
                default_branch,
                platform="origin",
            )
        except Exception:
            logger.exception("Error handling Origin push for %s/%s", owner, repo_name)


async def handle_origin_comment(
    payload: dict[str, Any],
    auth: PlatformAuth,
    bot_name: str,
    installation_id: str,
) -> None:
    """An @-mention in a PR comment: command, pause/resume, or thread reply."""
    from mira.platforms.handlers import (
        _PAUSE_KEYWORDS,
        _REJECT_KEYWORDS,
        _RESUME_KEYWORDS,
        PAUSE_LABEL,
        _open_store,
        run_pr_command,
        run_thread_reply,
    )

    comment = payload.get("comment") or {}
    comment_body = comment.get("body", "") or ""
    pr = payload.get("pullRequest") or {}
    repo = pr.get("repository") or payload.get("repository") or {}
    owner_name = _repo_owner_name(repo)
    if not owner_name:
        return
    owner, repo_name = owner_name
    number_raw = pr.get("number")
    if number_raw is None:
        return
    number = int(number_raw)
    pr_url = _pr_web_url(owner, repo_name, number, pr)

    actor = _actor_handle(comment.get("author"))
    thread = comment.get("thread") or {}
    comment_path = thread.get("path") or ""
    comment_line = int(thread.get("startLine") or thread.get("endLine") or 0)
    thread_id = str(thread.get("id") or "")

    try:
        token = await auth.get_token(installation_id)
        provider = create_provider("origin", token)
        pr_info = await provider.get_pr_info(pr_url)

        names = mention_names(bot_name, await auth.get_bot_identity())
        question = strip_mentions(comment_body, names)
        first_word = question.split()[0].lower() if question.split() else ""

        if first_word in _PAUSE_KEYWORDS:
            await provider.add_label(pr_info, PAUSE_LABEL)
            await provider.post_comment(
                pr_info,
                f"Automatic reviews paused. Request a manual review with `@{bot_name} review`.",
            )
            return
        if first_word in _RESUME_KEYWORDS:
            await provider.remove_label(pr_info, PAUSE_LABEL)
            await provider.post_comment(pr_info, "Automatic reviews resumed.")
            return

        if first_word in _REJECT_KEYWORDS and comment_path:
            try:
                store = _open_store(owner, repo_name, "origin")
                store.record_feedback(
                    pr_number=number,
                    pr_url=pr_url,
                    comment_path=comment_path,
                    comment_line=comment_line,
                    comment_category="",
                    comment_severity="",
                    comment_title="",
                    signal="rejected",
                    actor=actor,
                )
            except Exception as exc:
                logger.debug("Failed to record Origin reject feedback: %s", exc)
            finally:
                if "store" in locals():
                    store.close()
            return

        if comment_path:
            original = ""
            cid = comment.get("id")
            if cid:
                original = await provider.get_comment_body(pr_info, str(cid))  # type: ignore[arg-type]
            await run_thread_reply(
                provider,
                pr_info,
                question,
                0,  # numeric id unused; reply uses thread_id
                original_suggestion=original,
                thread_id=thread_id or str(cid or ""),
                comment_path=comment_path,
                comment_line=comment_line,
                actor=actor,
                bot_name=bot_name,
                platform="origin",
            )
            return

        await run_pr_command(
            provider,
            owner,
            repo_name,
            number,
            pr_url,
            question,
            actor,
            bot_name,
            platform="origin",
        )
    except Exception:
        logger.exception(
            "Error handling Origin comment on %s/%s#%d", owner, repo_name, number
        )


async def handle_origin_installation(
    payload: dict[str, Any], auth: PlatformAuth, installation_id: str
) -> None:
    """Register repos from an installation.created / updated event."""
    from mira.platforms.index_handlers import _get_app_db

    installation = payload.get("installation") or {}
    repos = installation.get("repositories") or []
    db = _get_app_db()
    for r in repos:
        owner_name = _repo_owner_name(r)
        if not owner_name:
            continue
        owner, repo = owner_name
        db.register_repo(owner, repo, platform="origin")
    if not repos and installation_id:
        # Selection mode "all" — discover via API.
        try:
            await backfill_origin_repos(auth, installation_id)
        except Exception:
            logger.exception("Origin installation backfill failed for %s", installation_id)


async def dispatch_origin_event(
    event_type: str,
    envelope: dict[str, Any],
    auth: PlatformAuth,
    bot_name: str,
    background_tasks: Any,
) -> str:
    """Route a verified Origin webhook to a handler. Returns a status string."""
    _type, installation_id, payload = unwrap_envelope(envelope)
    event_type = event_type or _type
    if not event_type:
        return "ignored"

    cfg = load_config()

    # Ignore our own app's comments when we can identify the actor.
    if event_type.startswith("pull_request.comment."):
        actor = _actor_handle((payload.get("comment") or {}).get("author"))
        bot_identity = await auth.get_bot_identity()
        if actor and bot_identity and actor == bot_identity:
            return "ignored"

    pr_review_events = {
        "pull_request.created",
        "pull_request.published",
        "pull_request.reopened",
        "pull_request.head_ref.pushed",
    }
    if event_type in pr_review_events:
        pr = payload.get("pullRequest") or {}
        actor = _actor_handle(pr.get("author"))
        if author_is_filtered(actor, cfg.filter.allowed_authors, cfg.filter.blocked_authors):
            logger.debug("Origin PR skipped — author %s filtered", actor)
            return "ignored"
        background_tasks.add_task(
            handle_origin_pr, payload, auth, bot_name, installation_id, event_type
        )
        return "processing"

    if event_type == "repository.pushed":
        background_tasks.add_task(
            handle_origin_push, payload, auth, bot_name, installation_id
        )
        return "processing"

    if event_type == "pull_request.comment.created":
        comment_body = (payload.get("comment") or {}).get("body", "") or ""
        names = mention_names(bot_name, await auth.get_bot_identity())
        if has_mention(comment_body, names):
            cmd_word = command_after_mention(comment_body, names)
            actor = _actor_handle((payload.get("comment") or {}).get("author"))
            if cmd_word != "review" and author_is_filtered(
                actor, cfg.filter.allowed_authors, cfg.filter.blocked_authors
            ):
                logger.debug("Origin comment skipped — author %s filtered", actor)
                return "ignored"
            background_tasks.add_task(
                handle_origin_comment, payload, auth, bot_name, installation_id
            )
            return "processing"
        return "ignored"

    if event_type in ("installation.created", "installation.updated"):
        background_tasks.add_task(handle_origin_installation, payload, auth, installation_id)
        return "processing"

    return "ignored"


# Silence unused-import lint for load_pem_public_key (kept for PEM JWKS variants).
_ = load_pem_public_key
