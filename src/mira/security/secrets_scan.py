"""Review-time deterministic secrets scan: flag hardcoded credentials in the diff.

The LLM security pass sweeps for known vulnerability classes (XSS, injection,
auth bypass, …) but has no key-format rules — a PR that commits a real
`ghp_…` token slips through if the critic isn't in the mood. This module
closes that gap with a pure-Python, in-memory, no-network regex+entropy pass
over the added lines of the diff, mirroring pr_scan.py (which scans added
manifest entries against OSV).
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter

from mira.models import FileDiff, ReviewComment, Severity
from mira.security.pr_scan import _added_line_hits

logger = logging.getLogger(__name__)

# Cap deterministic findings per file so a generated-file-heavy PR can't
# drown the review in noise. Stop scanning that file once the cap is hit.
_MAX_PER_FILE = 5

# Placeholder-ish literals the entropy gate must reject even if entropy is
# high (e.g. "your_super_secret_key" style scaffolding).
_PLACEHOLDER_RE = re.compile(r"xxx|your_|placeholder|example|changeme|dummy|<[^>]*>", re.IGNORECASE)

# (compiled regex, label, severity) over ONE added line (raw[1:]).
# Pattern 1 stays case-sensitive — its prefix set is uppercase by spec;
# the rest use re.IGNORECASE where the literal already mixes case.
_SECRET_PATTERNS: list[tuple[re.Pattern, str, Severity]] = [
    (
        re.compile(r"\b(AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}\b"),
        "AWS access key",
        Severity.BLOCKER,
    ),
    (
        re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36,255})\b", re.IGNORECASE),
        "GitHub token",
        Severity.BLOCKER,
    ),
    (
        re.compile(r"\bglpat-[A-Za-z0-9\-_]{20,}\b", re.IGNORECASE),
        "GitLab personal access token",
        Severity.BLOCKER,
    ),
    (
        re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b", re.IGNORECASE),
        "Google API key",
        Severity.BLOCKER,
    ),
    (
        re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b", re.IGNORECASE),
        "Slack token",
        Severity.BLOCKER,
    ),
    (
        re.compile(r"\bsk_live_[0-9a-zA-Z]{24,}\b", re.IGNORECASE),
        "Stripe secret key",
        Severity.BLOCKER,
    ),
    (
        re.compile(r"-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----"),
        "private key block",
        Severity.BLOCKER,
    ),
    (
        re.compile(
            r"(?:api[_-]?key|secret|token|password|passwd)\s*[:=]\s*[\"']([^\"']{12,})[\"']",
            re.IGNORECASE,
        ),
        "credential assignment",
        Severity.BLOCKER,
    ),
]

# The credential assignment pattern (last entry) captures the quoted literal
# in group 1 for the entropy gate.
_CREDENTIAL_ASSIGNMENT_RE = _SECRET_PATTERNS[-1][0]
_ENTROPY_THRESHOLD = 3.5


def shannon_entropy(s: str) -> float:
    """Shannon entropy (base 2), 0 for empty. Used to reject placeholder-ish
    low-entropy literals like 'aaaaaaaa' or 'examplepassword'."""
    if not s:
        return 0.0
    counts = Counter(s)
    n = float(len(s))
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _capture_literal(line: str) -> str | None:
    """Quoted literal from a credential assignment match, or None."""
    m = _CREDENTIAL_ASSIGNMENT_RE.search(line)
    if m is None:
        return None
    return m.group(1)


def _assignment_gate(line: str) -> bool:
    """Pattern 8 gate: entropy >= threshold and not a known placeholder."""
    literal = _capture_literal(line)
    if literal is None or shannon_entropy(literal) < _ENTROPY_THRESHOLD:
        return False
    return _PLACEHOLDER_RE.search(literal) is None


async def scan_secrets(files: list[FileDiff]) -> list[ReviewComment]:
    """Scan added diff lines for hardcoded credentials. Pure in-memory, fails open."""
    if not files:
        return []
    comments: list[ReviewComment] = []
    try:
        for f in files:
            per_file: list[ReviewComment] = []
            added = _added_line_hits(f, needle=None)
            for pat, label, sev in _SECRET_PATTERNS:
                if len(per_file) >= _MAX_PER_FILE:
                    break
                hit = None
                for line_no, text in added:
                    if pat.search(text) is None:
                        continue
                    if pat is _CREDENTIAL_ASSIGNMENT_RE and not _assignment_gate(text):
                        continue
                    hit = (line_no, text)
                    break
                if hit is None:
                    continue
                line_no, text = hit
                per_file.append(
                    ReviewComment(
                        path=f.path,
                        line=line_no,
                        end_line=None,
                        severity=sev,
                        category="security",
                        title=f"Hardcoded {label} in diff",
                        body=(
                            f"Deterministic scan found a {label} on the added line. "
                            f"Rotate/remove this key and move it to a secret store. "
                            f"Confidence 0.9."
                        ),
                        confidence=0.9,
                        suggestion=None,
                        agent_prompt=(
                            f"In {f.path}:{line_no}, remove the {label} and load it "
                            f"from the environment/secret manager instead of the source."
                        ),
                        existing_code=text,
                        source_pass="secrets",
                    )
                )
            comments.extend(per_file)
    except Exception as exc:
        logger.warning("Secrets scan failed: %s", exc)
        return []
    return comments
