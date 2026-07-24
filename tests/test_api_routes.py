"""Guard the dashboard's full route surface.

The dashboard endpoints have thin behavioural coverage, so this snapshot is the
safety net for refactors that move routes between modules: it fails if any
(path, method) is dropped, renamed, or accidentally added. Update the set
deliberately when you intend a route change.
"""

from __future__ import annotations

from mira.dashboard.api import router

EXPECTED_ROUTES = {
    ("/api/admin/settings", "GET"),
    ("/api/admin/settings", "PUT"),
    ("/api/admin/webhooks", "GET"),
    ("/api/admin/webhooks", "POST"),
    ("/api/admin/webhooks/{webhook_id}", "DELETE"),
    ("/api/admin/webhooks/{webhook_id}", "GET"),
    ("/api/admin/webhooks/{webhook_id}", "PUT"),
    ("/api/admin/webhooks/{webhook_id}/test", "POST"),
    ("/api/activity", "GET"),
    ("/api/activity/{owner}/{repo}/{pr_number}", "GET"),
    ("/api/contributors", "GET"),
    ("/api/contributors/summary", "GET"),
    ("/api/contributors/backfill/status", "GET"),
    ("/api/contributors/refresh", "POST"),
    ("/api/contributors/{owner}/{repo}/refresh", "POST"),
    ("/api/contributors/{login}", "GET"),
    ("/api/events", "GET"),
    ("/api/gitlab/repos", "POST"),
    ("/api/gitlab/sync", "POST"),
    ("/api/forgejo/repos", "POST"),
    ("/api/forgejo/sync", "POST"),
    ("/api/indexing/estimate", "GET"),
    ("/api/indexing/status", "GET"),
    ("/api/learned-rules", "GET"),
    ("/api/learned-rules/{owner}/{repo}", "POST"),
    ("/api/learned-rules/{owner}/{repo}/{rule_id}", "DELETE"),
    ("/api/learned-rules/{owner}/{repo}/{rule_id}", "GET"),
    ("/api/learned-rules/{owner}/{repo}/{rule_id}", "PUT"),
    ("/api/learned-rules/{owner}/{repo}/{rule_id}/active", "PATCH"),
    ("/api/learned-rules/{owner}/{repo}/{rule_id}/approve", "POST"),
    ("/api/learned-rules/{owner}/{repo}/{rule_id}/reject", "POST"),
    ("/api/packages/search", "GET"),
    ("/api/relationships", "GET"),
    ("/api/relationships/custom", "GET"),
    ("/api/relationships/custom", "POST"),
    ("/api/relationships/custom/{edge_id}", "DELETE"),
    ("/api/relationships/overrides", "DELETE"),
    ("/api/relationships/overrides", "GET"),
    ("/api/relationships/overrides", "POST"),
    ("/api/relationships/{owner}/{repo}", "GET"),
    ("/api/repos", "GET"),
    ("/api/review-insights/summary", "GET"),
    ("/api/review-insights/open-prs", "GET"),
    ("/api/review-insights/reviewers", "GET"),
    ("/api/repos/sync", "POST"),
    ("/api/repos/{owner}/{repo}", "GET"),
    ("/api/repos/{owner}/{repo}/blast-radius", "GET"),
    ("/api/repos/{owner}/{repo}/blast-radius.svg", "GET"),
    ("/api/repos/{owner}/{repo}/context", "GET"),
    ("/api/repos/{owner}/{repo}/context", "POST"),
    ("/api/repos/{owner}/{repo}/context/{context_id}", "DELETE"),
    ("/api/repos/{owner}/{repo}/context/{context_id}", "PUT"),
    ("/api/repos/{owner}/{repo}/dependencies", "GET"),
    ("/api/repos/{owner}/{repo}/external-refs", "GET"),
    ("/api/repos/{owner}/{repo}/files", "GET"),
    ("/api/repos/{owner}/{repo}/index", "DELETE"),
    ("/api/repos/{owner}/{repo}/index", "POST"),
    ("/api/repos/{owner}/{repo}/learned-rules", "GET"),
    ("/api/repos/{owner}/{repo}/packages", "GET"),
    ("/api/repos/{owner}/{repo}/reviews", "GET"),
    ("/api/repos/{owner}/{repo}/rules", "GET"),
    ("/api/repos/{owner}/{repo}/rules", "POST"),
    ("/api/repos/{owner}/{repo}/rules/{rule_id}", "DELETE"),
    ("/api/repos/{owner}/{repo}/rules/{rule_id}", "PUT"),
    ("/api/repos/{owner}/{repo}/vulnerabilities", "GET"),
    ("/api/rules/global", "GET"),
    ("/api/rules/global", "POST"),
    ("/api/rules/global/{rule_id}", "DELETE"),
    ("/api/rules/global/{rule_id}", "PUT"),
    ("/api/rules/global/{rule_id}/toggle", "PATCH"),
    ("/api/settings/models", "GET"),
    ("/api/settings/models", "PUT"),
    ("/api/setup/complete", "POST"),
    ("/api/setup/status", "GET"),
    ("/api/stats", "GET"),
    ("/api/stats/timeseries", "GET"),
    ("/api/uninstalls/pending", "GET"),
    ("/api/uninstalls/{installation_id}/delete", "POST"),
    ("/api/uninstalls/{installation_id}/keep", "POST"),
    ("/api/version", "GET"),
    ("/api/vulnerabilities", "GET"),
    ("/api/vulnerabilities/summary", "GET"),
}


def _actual_routes() -> set[tuple[str, str]]:
    out = set()
    for r in router.routes:
        for method in r.methods:
            if method != "HEAD":
                out.add((r.path, method))
    return out


def test_route_surface_unchanged():
    actual = _actual_routes()
    assert actual == EXPECTED_ROUTES, (
        f"missing: {EXPECTED_ROUTES - actual}\nunexpected: {actual - EXPECTED_ROUTES}"
    )
