"""Hardening for LLM-generated Mermaid diagrams.

The walkthrough's ``sequence_diagram`` is posted inside a ```` ```mermaid ````
fence on GitHub, which runs it through Mermaid's parser. LLMs produce
diagrams that fail there ("Unable to render rich display") in ways no prompt
warning prevents:

- Double-escaped newlines: the JSON string ``"graph LR\\n a --> b"`` arrives
  as a *literal* backslash-n, so the whole diagram is one line. Mermaid's
  lexer hits the backslash and emits a ``NODE_STRING`` token where the
  parser expects ``SEMI``/``NEWLINE``/``SPACE`` — the exact error GitHub
  surfaces.
- Nested quotes in labels (``engine["core/"engine.py""]``), stray
  backslashes, unbalanced brackets, invented syntax — anything the model
  improvises.

Rather than chase each failure mode with a repair, :func:`harden_mermaid`
validates the diagram against a strict subset of the flowchart / sequence
grammars and, only when *every* line fits, re-emits it in canonical form.
The re-emitted shapes (one statement per line, quoted labels, plain arrows)
are verified against the real Mermaid parser. If the input is outside the
subset we return ``None`` and the caller drops the diagram from the comment
instead of shipping something that cannot render.
"""

from __future__ import annotations

import re

# A runaway LLM must not be able to produce an unbounded comment.
_MAX_LINES = 50
_MAX_LEN = 4000

# ── normalization ────────────────────────────────────────────────────

# A wrapping ```mermaid fence the model sometimes includes.
_FENCE_OPEN_RE = re.compile(r"^```[A-Za-z]*\s*\n?")


def _normalize(raw: str) -> str:
    """Fix double-escaped control chars and strip unrenderable backslashes.

    An LLM that double-escapes newlines inside its JSON string emits
    ``graph LR\\n a --> b`` — a *literal* backslash-n — which is what trips
    Mermaid's lexer with the ``NODE_STRING`` error. Real newlines (and CRLF)
    are fine, so we convert the literal forms and drop any backslashes that
    remain (a bare backslash is invalid in every label form we re-emit).
    """
    text = raw.strip()
    fence = _FENCE_OPEN_RE.match(text)
    if fence:
        text = text[fence.end() :]
        end = text.rfind("```")
        if end != -1:
            text = text[:end]
    text = text.replace("\\n", "\n").replace("\\r", "\n").replace("\\t", " ")
    text = re.sub(r"\\", "", text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _clean_label(content: str) -> str:
    """Reduce label content to one clean, quote-safe string.

    Nested quotes (``core/"engine.py"``) are the classic breakage: strip
    every ``"`` and re-quote the result with a single pair. ``|`` is
    reserved for edge labels, so it goes too.
    """
    if content.startswith('"') and content.endswith('"') and len(content) >= 2:
        content = content[1:-1]
    cleaned = content.replace('"', "").replace("|", "").replace("\n", " ")
    return " ".join(cleaned.split())


# ── flowcharts ───────────────────────────────────────────────────────

# Node id: letters/digits/underscore, plus . / $ (path-ish names are the
# norm in LLM output), plus internal dashes (`a-b`). A dash only counts as
# part of the id when followed by word characters (same guard as _SEQ_ID),
# so a trailing dash never swallows the start of an arrow (`A-->B`,
# `a-b-->c`). Must contain at least one letter.
_NODE_ID = r"(?=[A-Za-z0-9_./$-]*[A-Za-z])[A-Za-z0-9_./$]+(?:-[A-Za-z0-9_]+)*"

# Bare label content: no quotes, brackets, parens, braces, pipe.
_BARE = r'(?!")[^"\\[\](){}|]+'

# (name, opener, closer) for each node shape. Multi-char openers come
# before the single-char shapes they start with.
_SHAPES: tuple[tuple[str, str, str], ...] = (
    ("stadium", "[[", "]]"),
    ("cylinder", "[(", ")]"),
    ("parallelogram", "[//", "//]"),
    ("trapezoid", "[/", "/]"),
    ("diamond", "{", "}"),
    ("rounded", "(", ")"),
    ("rectangle", "[", "]"),
)


# Quoted label content for a given shape closer: a run of anything (inner
# quotes allowed) up to the last `"` that the closer follows. The tempered
# token stops at the first `"` that is directly followed by the closer,
# which makes `["core/"engine.py""]` capture as ONE label so _clean_label
# can repair it, and stops a greedy match from swallowing the next node.
def _quoted_for(close: str) -> str:
    return rf'"(?:(?!{re.escape(close)}).)*"'


_SHAPE_PARTS = "|".join(
    rf"(?P<{name}_l>{re.escape(open_)})(?P<{name}_c>(?:{_quoted_for(close_)}|{_BARE}))"
    rf"(?P<{name}_r>{re.escape(close_)})"
    for name, open_, close_ in _SHAPES
)
# No ``^`` (matched at a ``pos`` offset mid-line) and no trailing ``$``
# (an edge + next node may follow the node).
_NODE_RE = re.compile(
    rf"\s*(?P<id>{_NODE_ID})\s*"
    rf"(?:{_SHAPE_PARTS})?"
    rf"(?P<cls>:::(?P<class_name>[A-Za-z_][A-Za-z0-9_.]*))?"
    r"\s*"
)

# Edge between two nodes.
#   text-form arrow first: `-- imports -->`. The opening `--` must be
#   followed by whitespace (a compact `--x`/`-->` is a plain arrow, never a
#   text edge), and the label is a run of non-pipe, non-newline chars where
#   a dash only counts as text when NOT followed by another dash (a
#   dash-run is always the CLOSING `--`/`-->`/`--o`/`--x`, mirroring
#   Mermaid's own lexer: EDGE_TEXT is `[^-]|\-(?!\-)+`). That keeps a plain
#   link (`---`  `----`) and the compact `-->` out of the branch.
#   The closing is `--`, `-->`, `--o`, or `--x` (at most one arrowhead),
#   so `-- x -->o B` (a head after `>`) is not a valid closing and is
#   rejected.
#   plain arrow, optional arrowhead, optional |label|: -->  ==>  -.->  ~~~
_EDGE_RE = re.compile(
    r"\s*(?:"
    r"--\s+(?P<tlabel>(?:[^|\-\n]|\-(?!-))+)\s*-{2,}(?P<ttail>[xo>]?)"
    r"|"
    r"(?P<arrow>-\.+-+|~~+|=+|---+|--+)(?P<tail>[o>x])?"
    r"(?P<label>\s*\|\s*(?P<lab>\"[^\"]*\"|[^|\n]+?)\s*\|)?"
    r")"
)

_FLOW_HEAD_RE = re.compile(r"^\s*(?:graph|flowchart)\s+(LR|RL|TB|BT|TD|BR)\s*;?\s*$")
_SUBGRAPH_RE = re.compile(rf"^\s*subgraph\s+(?P<name>{_NODE_ID}|\"[^\"]*\")\s*;?\s*$")
_DIRECTION_RE = re.compile(r"^\s*direction\s+(LR|RL|TB|BT|TD|BR)\s*;?\s*$")
_CLASSDEF_RE = re.compile(r"^\s*classDef\s+[A-Za-z_][A-Za-z0-9_.]*\s+\S.*$")


def _reemit_node(match: re.Match[str]) -> str:
    id_ = match.group("id")
    out = id_
    for name, open_, close_ in _SHAPES:
        if match.group(f"{name}_l"):
            label = _clean_label(match.group(f"{name}_c"))
            if label:
                out = f'{id_}{open_}"{label}"{close_}'
            break
    if match.group("cls"):
        out += f":::{match.group('class_name')}"
    return out


def _reemit_edge(match: re.Match[str]) -> str:
    if match.group("tlabel") is not None:
        label = " ".join(match.group("tlabel").replace('"', "").split())
        return f"-- {label} --{match.group('ttail') or ''}"
    arrow = f"{match.group('arrow')}{match.group('tail') or ''}"
    if not match.group("label"):
        return arrow
    lab = _clean_label(match.group("lab"))
    return f"{arrow}|{lab}|" if lab else arrow


def _parse_flow_line(line: str) -> str | None:
    """Re-emit one ``node (edge node)*`` line, or None if outside the subset."""
    parts: list[str] = []
    pos = 0
    while pos < len(line):
        m = _NODE_RE.match(line, pos)
        if not m:
            return None
        # `end` is a reserved word (the subgraph terminator), not a valid
        # node id: re-emitting it unquoted yields a diagram Mermaid's own
        # parser rejects. A plain node named `end` carries no label to
        # quote it as, so reject the diagram rather than guess.
        if m.group("id") == "end":
            return None
        parts.append(_reemit_node(m))
        rest = line[m.end() :]
        if not rest.strip():
            return " ".join(parts)
        e = _EDGE_RE.match(rest)
        if not e:
            return None
        parts.append(_reemit_edge(e))
        pos = m.end() + e.end()
    return None  # line ended on an arrow — dangling edge


def _harden_flowchart(text: str) -> str | None:
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln and not ln.startswith("%%")]
    if len(lines) < 2 or len(lines) > _MAX_LINES:
        return None
    out: list[str] = []
    depth = 0
    for i, line in enumerate(lines):
        if i == 0:
            head = _FLOW_HEAD_RE.match(line)
            if not head:
                return None
            out.append(f"graph {head.group(1)}")
            continue
        if line == "end":
            if depth == 0:
                return None
            depth -= 1
            out.append("end")
            continue
        if sub := _SUBGRAPH_RE.match(line):
            name = sub.group("name")
            if not name.startswith('"'):
                name = f'"{name}"'
            out.append(f"subgraph {name}")
            depth += 1
            continue
        if _DIRECTION_RE.match(line) or _CLASSDEF_RE.match(line):
            out.append(line)
            continue
        reemitted = _parse_flow_line(line)
        if reemitted is None:
            return None
        out.append(f"  {reemitted}")
    if depth != 0:
        return None
    return "\n".join(out)


# ── sequence diagrams ────────────────────────────────────────────────

# Bare or double-quoted participant id. A dash only counts as part of the
# id when followed by word characters, so a trailing dash never swallows
# the start of an arrow (`A-.->B`, `A--B`).
_SEQ_ID = r'(?:"[^"\n]+"|[A-Za-z0-9_][A-Za-z0-9_.]*(?:-[A-Za-z0-9_]+)*)'
# Message arrows with optional +/- activation markers:
#   ->  -->  ->>  -->>  and the dotted link --
_SEQ_MSG_RE = re.compile(
    rf"^\s*(?P<a>{_SEQ_ID})\s*(?P<arrow>-->>|-->|->>|->|-.->)"
    rf"(?P<marker>[+-])?(?P<b>{_SEQ_ID})\s*:\s*(?P<text>\S.*?)\s*$"
)
_SEQ_NOTE_RE = re.compile(
    rf"^\s*note\s+(?:over|above|below|left\s+of|right\s+of)\s+"
    rf"(?P<ids>{_SEQ_ID}(?:\s*,\s*{_SEQ_ID})*)\s*:\s*(?P<text>\S.*?)\s*$"
)
_SEQ_PARTICIPANT_RE = re.compile(
    rf"^\s*(?:participant|actor)\s+(?P<id>{_SEQ_ID})"
    rf"(?:\s+as\s+(?P<name>[^\n]+?))?\s*$"
)
_SEQ_BLOCK_RE = re.compile(r"^\s*(loop|alt|opt|par)\b\s*(.*?)\s*$")
_SEQ_ELSE_RE = re.compile(r"^\s*(else|and)\b\s*(.*?)\s*$")
_SEQ_AUTONUM_RE = re.compile(r"^\s*autonumber(?:\s+(\d+)(?:\s*,\s*(\d+))?)?\s*$")
_SEQ_ACTIVATE_RE = re.compile(r"^\s*(?:activate|deactivate)\s+[^\s\n]+\s*$")


def _harden_sequence(text: str) -> str | None:
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln and not ln.startswith("%%")]
    if not lines or lines[0].lower() != "sequencediagram" or len(lines) < 2:
        return None
    if len(lines) > _MAX_LINES:
        return None
    out = ["sequenceDiagram"]
    depth = 0
    # Activation bookkeeping. Mermaid's semantics (verified against the
    # real parser): `A->>+B` activates the *receiver* B, while `A-->>-B`
    # deactivates the *sender* A — a stray `-` on an inactive participant
    # throws at render time ("Trying to inactivate an inactive
    # participant"), so we track state and drop markers we cannot prove
    # safe.
    active: set[str] = set()
    for line in lines[1:]:
        if line == "end":
            if depth == 0:
                return None
            depth -= 1
            out.append(line)
            continue
        # activate/deactivate statements are decorative and error-prone
        # (double-activate / deactivate-never both blow up at render time);
        # drop them and let message markers handle activation instead.
        if _SEQ_ACTIVATE_RE.match(line):
            continue
        if m := _SEQ_BLOCK_RE.match(line):
            out.append(f"{m.group(1)} {m.group(2)}".strip())
            depth += 1
            continue
        if m := _SEQ_ELSE_RE.match(line):
            if depth == 0:
                return None
            out.append(f"{m.group(1)} {m.group(2)}".strip())
            continue
        if m := _SEQ_MSG_RE.match(line):
            sender, receiver = m.group("a"), m.group("b")
            marker = m.group("marker")
            if marker == "+":
                active.add(receiver)
            elif marker == "-":
                if sender not in active:
                    marker = None  # stray deactivate — drop, keep the message
                else:
                    active.discard(sender)
            out.append(f"{sender}{m.group('arrow')}{marker or ''}{receiver}: {m.group('text')}")
            continue
        if _SEQ_PARTICIPANT_RE.match(line) or _SEQ_NOTE_RE.match(line):
            out.append(line)
            continue
        if m := _SEQ_AUTONUM_RE.match(line):
            nums = " ".join(g for g in (m.group(1), m.group(2)) if g)
            out.append(f"autonumber {nums}".rstrip())
            continue
        return None
    if depth != 0:
        return None
    return "\n".join(out)


# ── entry point ──────────────────────────────────────────────────────


def harden_mermaid(raw: str | None) -> str | None:
    """Return a canonical, parser-safe Mermaid diagram, or ``None``.

    ``None`` means the input is empty or outside the supported subset — the
    caller should drop the diagram from the comment rather than risk a
    render failure on GitHub.
    """
    if not raw or not raw.strip():
        return None
    text = _normalize(raw)
    if len(text) > _MAX_LEN:
        return None
    first = next((ln for ln in text.split("\n") if ln.strip()), "")
    if re.match(r"^\s*(?:graph|flowchart)\b", first):
        return _harden_flowchart(text)
    if first.lower() == "sequencediagram":
        return _harden_sequence(text)
    return None
