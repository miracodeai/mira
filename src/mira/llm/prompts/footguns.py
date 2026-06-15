"""Language-keyed framework footgun knowledge injected into the review prompt.

Static knowledge that an LLM trained on a snapshot of the world reliably
benefits from being reminded of: subtle bugs that are easy to miss because
they look correct at a glance. Keep entries terse and copy-paste-able into
a single prompt section.

To extend: add an entry under the relevant language. Each rule should
describe the bug, not the fix — the model is good at fixes once it
recognises the pattern.
"""

from __future__ import annotations

from mira.models import FileDiff

_EXT_TO_LANG = {
    "py": "python",
    "js": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "go": "go",
    "rb": "ruby",
    "java": "java",
    "kt": "kotlin",
    "rs": "rust",
}

# Each language's footguns is a list of bullet rules. Keep concise — the
# model just needs the recognition trigger, not paragraphs of context.
_FOOTGUNS: dict[str, list[str]] = {
    "python": [
        "**Negative slicing on Django QuerySets** raises `AssertionError` — `qs[-1]` and "
        "`qs[:-1]` are not supported. Order with `.order_by()` and use `.first()` / `.last()`.",
        "**`is` for value comparisons** — use `==` for ints/strs. `is` only works on identity "
        "(small-int caching is an implementation detail).",
        "**`{}.fromkeys(keys, [])`** — every key shares the same list. Use a comprehension.",
        "**Mutable default arguments** (`def f(x=[]):`) — the list is shared across calls.",
        "**`async`/`await` with sync code in `forEach`-style helpers** — `for x in items: await ...` "
        "is correct; passing an async function to a sync helper is not.",
        "**`hash()` for cache keys** is non-deterministic across processes (PYTHONHASHSEED). "
        "Use `hashlib` for cache keys that must be stable.",
        "**Blanket `except Exception`** — swallows `KeyboardInterrupt`/`SystemExit` if you used "
        "bare `except:`, and hides bugs in normal control flow either way. Prefer specific exceptions.",
    ],
    "javascript": [
        "**`forEach` with async callback** — `array.forEach(async x => await ...)` does NOT wait. "
        "Use `for...of` with `await`, or `Promise.all(array.map(...))`.",
        "**Falsy-zero bug** — `if (count) {...}` skips when `count === 0`. Use `count != null` "
        "or explicit `count > 0` for numeric values.",
        "**Missing `await` on a Promise** — calling an async function without `await` returns a "
        "Promise object, which is always truthy and never throws — silent bug.",
        "**`Object.keys(x)` order** is insertion order for string keys, but **integer-like keys "
        "are sorted numerically first**. Don't rely on declaration order with mixed keys.",
        "**`==` vs `===`** — `==` does type coercion (`'0' == false`, `null == undefined`). "
        "Always `===` unless you specifically want coercion.",
    ],
    "typescript": [
        "**`forEach` with async callback** — does NOT wait. Use `for...of` with `await` or "
        "`Promise.all(array.map(...))`.",
        "**`as` casts bypass type-checking** — `x as Foo` doesn't validate, it asserts. If `x` "
        "isn't actually a `Foo`, you'll get runtime errors with no warning.",
        "**Non-null assertion (`x!`)** silences the type checker but doesn't prevent `null`/`undefined` "
        "at runtime — flag whenever it's used on values that might genuinely be nullish.",
        "**`Promise.all` rejects on first failure** but doesn't cancel the others. If you need "
        "all-or-nothing behaviour with cleanup, use `Promise.allSettled` or explicit cancellation.",
        "**Floating promises** — an async call whose result is never awaited or `.catch`ed "
        "(event handlers, fire-and-forget cleanup like cancelling a job AND deleting its DB row) "
        "silently drops both errors and any follow-up steps after the first rejection.",
        "**`||` vs `??` for defaults** — `value || fallback` replaces `0`, `''`, and `false` with "
        "the fallback. Use `??` when only `null`/`undefined` should trigger the default.",
    ],
    "go": [
        "**Loop-variable capture** — `for _, v := range xs { go func() { use(v) }() }` captures "
        "the same `v` in every goroutine pre-Go-1.22. Pass `v` as a parameter.",
        "**`nil` interface vs `nil` concrete type** — `var p *T = nil; var i interface{} = p; i == nil` "
        "is FALSE. Returning a typed-nil pointer through an `error` return is a classic bug.",
        "**`defer` evaluates args at call site, runs at return** — `defer log.Print(time.Now())` "
        "captures the *current* time, not the time at defer execution.",
        "**Unchecked errors from deferred `Close()`** — `defer f.Close()` discards the error. "
        "If `Close()` flushes data, that error matters.",
        "**Slices share underlying arrays** — `b := a[2:5]; b[0] = 99` mutates `a[2]`. "
        "Use `append([]T{}, a[2:5]...)` to copy.",
        "**Method receivers: `func (p T)` vs `func (p *T)`** — value receivers can't mutate the "
        "receiver, and a value receiver on a struct with a sync.Mutex makes the mutex useless (copies it).",
        "**`err` shadowed by `:=`** — `if x, err := f(); ...` inside a block declares a NEW `err`; "
        "the outer `err` checked later never sees it.",
        "**Error path swallows the success case** — returning early on a non-fatal sentinel error "
        "(`if err != nil { return err }` where one error value should degrade gracefully, e.g. a "
        "limit-reached sentinel) fails the whole operation instead of handling that case.",
        "**Early return before `defer`** — a return between acquiring a resource and the "
        "`defer x.Close()` line leaks the resource.",
    ],
    "java": [
        "**Boxed-int equality** — `Integer a = 1000; Integer b = 1000; a == b` is FALSE (outside "
        "the cached -128..127 range). Use `.equals()` for boxed types.",
        "**`String.split()` returns empty array if regex matches** — empty string at position 0 "
        "is dropped silently. Pass `-1` as second arg to keep them.",
        "**`SimpleDateFormat` is not thread-safe** — use `DateTimeFormatter` (Java 8+).",
        "**`Iterator.remove()` is the only safe way to mutate during iteration** — modifying the "
        "collection directly throws `ConcurrentModificationException`.",
        "**`equals()` without `hashCode()`** — breaks every hash-based collection silently.",
        "**Unboxing NPE** — `int x = map.get(key)` throws `NullPointerException` when the key is "
        "absent; same for any `Integer`/`Boolean` auto-unboxed in a condition or arithmetic.",
        "**`Optional.get()` without `isPresent()`** — throws `NoSuchElementException`; also flag "
        "`Map.get()` results dereferenced without a null check.",
        "**Provider/factory lookup falling through to a default** — code that resolves a named "
        "provider (crypto, keystore, datasource) and silently falls back to the platform default "
        "when the lookup misses returns the WRONG provider rather than failing loudly.",
    ],
    "ruby": [
        "**`||=` doesn't short-circuit on `false`** — `x ||= compute_default()` calls `compute_default` "
        "every time `x` is `false` (not just `nil`). Use `x = compute_default() if x.nil?` if you need "
        "to distinguish.",
        "**`map` with side effects** — use `each`. `map`/`collect` build a new array; if you discard "
        "the result you're allocating for nothing.",
        "**Regex anchoring** — `^...$` matches **line** boundaries (multiline by default). For "
        "string boundaries use `\\A...\\z`. `'foo\\nbar'.match(/^bar$/)` succeeds.",
        "**`.send` vs `.public_send`** — `.send` ignores `private`/`protected` access. Use "
        "`public_send` unless you specifically intend to bypass.",
        "**Predicate methods (`foo?`) with side effects** — a `?` method that mutates state, "
        "writes to the DB, or records a match is a landmine: callers treat predicates as pure "
        "reads and call them freely (logging, guards, retries), multiplying the side effect.",
        "**Inline `rescue nil`** — `value = risky() rescue nil` swallows EVERY StandardError "
        "(typos, nil derefs included), not just the one you meant to ignore.",
        "**ActiveRecord callbacks on save paths** — `before_save`/`after_commit` hooks firing "
        "extra writes or external calls make innocent-looking `update`/`save!` calls have "
        "non-obvious side effects; flag new callbacks doing I/O.",
    ],
}

# Universal rules that aren't language-keyed.
_UNIVERSAL: list[str] = [
    "**Regex anchoring against full strings** — `pattern.match(s)` may match a substring. "
    "Verify the pattern uses anchors (`^...$` / `\\A...\\z`) when comparing whole values "
    "(domain validation, ID matching, origin checks).",
    "**Origin / hostname validation via `indexOf` or substring contains** — bypassable with "
    "lookalike domains (`indexOf('example.com')` matches `evil-example.com.attacker.tld`). "
    "Parse the URL and compare hostnames exactly.",
    "**TOCTOU (Time-of-Check-to-Time-of-Use)** — counting/checking a value then writing it "
    "is a race. Two concurrent requests can both pass the check before either writes. Use "
    "atomic upsert / unique constraint / advisory lock.",
    "**Unreleased resources** — a file, socket, DB connection/cursor, or lock acquired without "
    "a scoped-release construct (`with` / `defer` / try-finally) leaks when an exception or early "
    "return skips the close. Flag handles opened and never closed, and closes that only run on "
    "the happy path.",
]


def _primary_language(files: list[FileDiff]) -> str | None:
    """Pick the diff's primary language by total lines changed.

    Multi-language PRs are rare; when they happen, packing every language's
    footguns into the prompt dilutes attention. Picking the dominant
    language and skipping the rest keeps the section focused.
    """
    score: dict[str, int] = {}
    for f in files:
        ext = f.path.rsplit(".", 1)[-1].lower() if "." in f.path else ""
        lang = _EXT_TO_LANG.get(ext)
        if not lang:
            continue
        score[lang] = score.get(lang, 0) + f.total_changes
    if not score:
        return None
    # Sort: most changes wins; alphabetical break for determinism.
    return sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def get_footguns_for_files(files: list[FileDiff], primary_only: bool = True) -> str:
    """Return a markdown block of footgun rules for the languages in ``files``.

    By default, only the diff's primary language (most lines changed) is
    included — multi-language packing crowds the prompt and dilutes
    attention. Pass ``primary_only=False`` to include every detected
    language.

    Returns an empty string if no relevant languages are detected, so the
    review prompt's `{% if footguns %}` branch can skip the section cleanly.
    """
    if primary_only:
        primary = _primary_language(files)
        langs: list[str] = [primary] if primary else []
    else:
        seen: set[str] = set()
        for f in files:
            ext = f.path.rsplit(".", 1)[-1].lower() if "." in f.path else ""
            lang = _EXT_TO_LANG.get(ext)
            if lang:
                seen.add(lang)
        langs = sorted(seen)

    if not langs and not _UNIVERSAL:
        return ""

    parts: list[str] = []
    for lang in langs:
        rules = _FOOTGUNS.get(lang)
        if not rules:
            continue
        parts.append(f"### {lang.title()}")
        for rule in rules:
            parts.append(f"- {rule}")
        parts.append("")

    if _UNIVERSAL:
        parts.append("### Cross-language")
        for rule in _UNIVERSAL:
            parts.append(f"- {rule}")
        parts.append("")

    return "\n".join(parts).rstrip()
