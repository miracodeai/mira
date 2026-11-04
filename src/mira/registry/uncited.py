"""Advisory findings for decisions stated without a registry ID (HAI-12775).

A pull request body, or the description of a Linear ticket it links, can state
a decision ("we decided ...", or an item under "Decisions I made") without
citing the registry object that records it. This module finds those
statements and returns one ``suggestion`` comment per decision.

The check is advisory. It does not block a merge, call a model, or write a
registry object. It reads the PR metadata from the frozen snapshot and, for
linked tickets, only the Linear work the registry has already mirrored. A
missing, retired, or unreadable mirror yields no finding. Mirrors are as fresh
as the last Linear import.

A citation is syntax only: ``reg_`` followed by twelve hex digits, next to the
decision. This module does not check that the id exists or is approved.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from mira.models import ReviewComment, Severity

logger = logging.getLogger(__name__)

SOURCE_PASS = "registry_uncited_decision"
TITLE = "Decision lacks a registry citation"
# Bounds on one review: tickets read from the mirror, and findings returned.
MAX_TICKETS = 5
MAX_FINDINGS = 10
_MAX_QUOTE = 300

_CITATION = re.compile(r"(?<![A-Za-z0-9_])reg_[0-9a-f]{12}(?![A-Za-z0-9_])")
_TICKET = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
_BOLD_LABEL = re.compile(r"^\s*(\*\*|__)[^*_]+(\*\*|__)\s*:?\s*$")
_SECTION = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?(?:\*\*|__)?\s*decisions\s+i\s+made\b"
    r"\s*(?:\*\*|__)?\s*:?\s*(?:\*\*|__)?\s*(?P<rest>.*)$",
    re.I,
)
_BULLET = re.compile(r"^(?P<indent>\s*)(?:[-*+]|\d+[.)])\s+(?P<text>.*)$")
_EMPTY = re.compile(
    r"^(?:none|n/?a|nothing(?:\s+notable)?|no(?:\s+new)?\s+decisions?(?:\s+made)?)?$", re.I
)
# "No new product behavior is ratified here", "No product behavior changed":
# an explicit statement that there is no decision. "No retries on 4xx" is one.
_NO_DECISION = re.compile(
    r"^(?:no|none|nothing)\b.*\b(?:chang(?:e|es|ed)|ratif(?:y|ies|ied)|decided|decisions?|"
    r"introduc(?:e|es|ed)|alter(?:s|ed)?|affect(?:s|ed)?)\b"
    r"|\bno\s+(?:new\s+)?(?:product\s+)?(?:behaviou?r(?:al)?|decisions?)\b",
    re.I,
)
_COMMIT = re.compile(
    r"\b(?:we|i)(?:\s+have|\s+had|'ve|'d)?\s+(?:decided|chose|chosen|opted|settled\s+on|agreed)\b"
    r"|\bthe\s+decision\s+(?:is|was)\s+to\b"
    r"|^\s*(?:[-*+]\s+)?(?:\*\*|__)?decision(?:\*\*|__)?\s*:",
    re.I,
)
_NEGATED = re.compile(r"\b(?:not|never|no\s+longer|yet\s+to)\b|n't\b|\bundecided\b", re.I)
_HYPOTHETICAL = re.compile(
    r"\b(?:if|whether|unless|suppose|imagine|hypothetically|alternatively|maybe|perhaps|"
    r"possibly|optionally)\b",
    re.I,
)
_ROUTINE = re.compile(
    r"^(?:ran|run|added|add|updated|update|wrote|write|fixed|fix)\b.*"
    r"\b(?:tests?|lint(?:ing)?|ruff|mypy|pytest|format(?:ting)?|typos?|changelog|ci)\b",
    re.I,
)
_QUESTION = re.compile(r"^[^.!?]*\?")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[`\"*_])")
_CODE = re.compile(r"`[^`]*`")
_QUOTED = re.compile(r"\"[^\"]*\"|“[^”]*”")
_MARKUP = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+|\*\*|__")


@dataclass(frozen=True)
class Decision:
    """One uncited decision: its 1-based line in the source text and its words."""

    line: int
    quote: str


def _mask(text: str) -> str:
    """Blank out inline code and quoted examples so they cannot match."""
    return _QUOTED.sub(" ", _CODE.sub(" ", text))


def _visible(text: str) -> list[tuple[int, str | None]]:
    """Each line with its number. Fenced code and blockquotes come back as None."""
    text = _COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text.replace("\r\n", "\n"))
    out: list[tuple[int, str | None]] = []
    fenced = False
    for no, line in enumerate(text.split("\n"), start=1):
        if _FENCE.match(line):
            fenced = not fenced
            out.append((no, None))
        elif fenced or line.lstrip().startswith(">"):
            out.append((no, None))
        else:
            out.append((no, line))
    return out


def _clean(text: str) -> str:
    text = " ".join(_MARKUP.sub("", text).split())
    return text if len(text) <= _MAX_QUOTE else text[: _MAX_QUOTE - 1].rstrip() + "…"


def _section_item(text: str) -> bool:
    """True when an item under "Decisions I made" states an uncited decision."""
    if _CITATION.search(text):
        return False
    masked = _mask(_clean(text)).strip()
    words = masked.rstrip(" .!;:-")
    if _EMPTY.match(words) or _NO_DECISION.search(words) or _QUESTION.match(masked):
        return False
    first = words.split(" ", 1)[0]
    if _HYPOTHETICAL.fullmatch(first):
        return False
    return not (_ROUTINE.match(words) and not _COMMIT.search(masked))


def _prose(line: str) -> list[str]:
    """Uncited explicit commitments in one line of prose, as sentences."""
    sentences = _SENTENCE.split(line.strip())
    found: list[str] = []
    for i, sentence in enumerate(sentences):
        masked = _mask(sentence)
        match = _COMMIT.search(masked)
        if match is None or masked.rstrip().endswith("?"):
            continue
        lead = masked[: match.end()]
        if _NEGATED.search(lead) or _HYPOTHETICAL.search(masked[: match.start()]):
            continue
        if _EMPTY.match(masked[match.end() :].strip(" .!;:-*_")):
            continue
        if _CITATION.search(sentence):
            continue
        # "We chose X. Recorded as reg_..." cites X; a second decision does not.
        nxt = sentences[i + 1] if i + 1 < len(sentences) else ""
        if nxt and _CITATION.search(nxt) and not _COMMIT.search(_mask(nxt)):
            continue
        found.append(sentence)
    return found


def find_uncited(text: str) -> list[Decision]:
    """Decisions in ``text`` with no registry citation, in order, deduplicated."""
    units: list[tuple[int, str, bool]] = []  # (line, text, from the section)
    current: list[Any] | None = None  # [line, text, is_bullet]
    in_section = False
    content_seen = False
    blank_after = False

    def flush() -> None:
        nonlocal current
        if current is not None:
            units.append((current[0], current[1], True))
            current = None

    for no, line in _visible(text):
        if line is None:
            flush()
            continue
        head = _SECTION.match(line)
        if head:
            flush()
            rest = head.group("rest").strip()
            in_section, content_seen, blank_after = not rest, False, False
            if rest:
                units.append((no, rest, True))
            continue
        if in_section:
            if not line.strip():
                flush()
                blank_after = content_seen
                continue
            bullet = _BULLET.match(line)
            # A heading, a bold label, or unindented prose after a blank line.
            ended = bool(_HEADING.match(line) or _BOLD_LABEL.match(line)) or (
                not bullet and blank_after and not line[:1].isspace()
            )
            if ended:
                flush()
                in_section = False
            elif bullet:
                flush()
                current = [no, bullet.group("text"), True]
                content_seen, blank_after = True, False
                continue
            elif current is not None and (line[:1].isspace() or not current[2]):
                current[1] += " " + line.strip()
                continue
            else:
                flush()
                current = [no, line.strip(), False]
                content_seen = True
                continue
        for sentence in _prose(line):
            units.append((no, sentence, False))
    flush()

    seen: set[str] = set()
    out: list[Decision] = []
    for no, unit, sectioned in units:
        if sectioned and not _section_item(unit):
            continue
        quote = _clean(unit)
        key = quote.lower()
        if not quote or key in seen:
            continue
        seen.add(key)
        out.append(Decision(no, quote))
    return out


def linked_tickets(title: str, body: str) -> list[str]:
    """Ticket keys the PR title and body name, first mention first."""
    return list(dict.fromkeys(_TICKET.findall(f"{title}\n{body}")))[:MAX_TICKETS]


def _mirror(registry: Any, ticket: str) -> dict | None:
    """The mirrored Linear work for ``ticket``, or None. Reads only."""
    try:
        work = registry.work_by_ticket(ticket)
    except Exception as exc:
        logger.debug("No readable Linear mirror for %s: %s", ticket, exc)
        return None
    if (
        work.get("source_system") != "linear"
        or work.get("kind") != "work"
        or work.get("ticket") != ticket
        or work.get("status") == "retired"
    ):
        return None
    return work


def finding(source: str, decision: Decision) -> ReviewComment:
    """A ``suggestion`` on the PR as a whole: ``path`` is empty."""
    body = "\n".join(
        [
            f"{source}, line {decision.line}, states a decision without a registry ID:",
            f'"{decision.quote}"',
            "If this is a durable decision, record it in the registry and cite its "
            "`reg_` ID next to it. This is advisory. It does not block the merge, "
            "and nothing was written to the registry.",
        ]
    )
    return ReviewComment(
        path="",
        line=1,
        end_line=None,
        severity=Severity.SUGGESTION,
        category="registry",
        title=TITLE,
        body=body,
        confidence=0.7,
        source_pass=SOURCE_PASS,
    )


def uncited_decisions(pr_info: Any, registry: Any = None) -> list[tuple[str, Decision]]:
    """``(source, decision)`` for the PR body and each linked, mirrored Linear ticket.

    The PR body is checked even when no registry is configured. Any failure
    returns what was found so far, so this check never fails a review.
    """
    out: list[tuple[str, Decision]] = []
    try:
        title = getattr(pr_info, "title", "") or ""
        body = getattr(pr_info, "description", "") or ""
        out.extend(("The pull request body", d) for d in find_uncited(body))
        if registry is None:
            from mira.registry import get_registry

            registry = get_registry(create=False)
        if registry is not None:
            for ticket in linked_tickets(title, body):
                work = _mirror(registry, ticket)
                if work is None:
                    continue
                url = work.get("source_url") or ""
                source = f"The Linear {ticket} description" + (f" ({url})" if url else "")
                out.extend((source, d) for d in find_uncited(work.get("body") or ""))
    except Exception as exc:
        logger.debug("Uncited-decision check stopped early: %s", exc)
    return out[:MAX_FINDINGS]


def advisory_findings(pr_info: Any, registry: Any = None) -> list[ReviewComment]:
    """One ``suggestion`` finding per uncited decision (the review API's shape)."""
    return [finding(source, d) for source, d in uncited_decisions(pr_info, registry)]


# A hidden marker so a re-review edits its own comment instead of adding one.
COMMENT_MARKER = "<!-- kory-source:registry-uncited-decisions -->"


def _inert(text: str) -> str:
    """Quoted PR text must not @-mention anyone or close the quote early."""
    return text.replace("@", "@\u200b").replace("\n", " ")


def general_comment(items: list[tuple[str, Decision]]) -> str:
    """The advisories as one PR conversation comment, for reviews posted to GitHub.

    An inline review comment needs a path and a diff line. These findings are
    about the PR description or a ticket, so they go in a general comment.
    """
    lines = [COMMENT_MARKER, f"### {TITLE}", ""]
    if not items:
        lines.append("Every decision this pull request states now cites a registry ID.")
        return "\n".join(lines) + "\n"
    lines.append(
        "These decisions have no registry ID next to them. If one is durable, record it "
        "in the registry and cite its `reg_` ID. This is advisory: it does not block the "
        "merge, and nothing was written to the registry."
    )
    lines.append("")
    for source, d in items:
        lines.append(f'- {source}, line {d.line}: "{_inert(d.quote)}"')
    return "\n".join(lines) + "\n"


async def post_general_comment(
    provider: Any, pr_info: Any, items: list[tuple[str, Decision]]
) -> None:
    """Create, or edit in place, the advisory comment. Never raises.

    With nothing to report and no earlier comment, this posts nothing. With an
    earlier comment, it is edited to say the decisions are now cited.
    """
    try:
        existing = await provider.find_bot_comment(pr_info, COMMENT_MARKER)
        if existing is not None:
            await provider.update_comment(pr_info, existing, general_comment(items))
        elif items:
            await provider.post_comment(pr_info, general_comment(items))
    except Exception as exc:
        logger.warning("Uncited-decision comment was not posted: %s", exc)
