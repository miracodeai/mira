import { Loader2 } from "lucide-react"
import { useEffect, useState } from "react"

import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { api } from "@/lib/api"
import { useAuth } from "@/lib/auth"

export function SettingsPage() {
  const { user: currentUser } = useAuth()

  const [indexingModel, setIndexingModel] = useState("")
  const [reviewModel, setReviewModel] = useState("")
  const [indexingOptions, setIndexingOptions] = useState<
    { value: string; label: string; recommended?: boolean }[]
  >([])
  const [reviewOptions, setReviewOptions] = useState<
    { value: string; label: string; recommended?: boolean }[]
  >([])
  const [savingModels, setSavingModels] = useState(false)
  const [modelsSaved, setModelsSaved] = useState(false)

  const [effective, setEffective] = useState<{
    filter?: Record<string, number | boolean | string>
    review?: Record<string, number | boolean | string>
  } | null>(null)
  const [overrides, setOverrides] = useState<{
    filter: Record<string, number | boolean | string>
    review: Record<string, number | boolean | string>
  }>({ filter: {}, review: {} })
  const [savingOverrides, setSavingOverrides] = useState(false)
  const [overridesSaved, setOverridesSaved] = useState(false)
  // While the user is typing in a number field we hold their literal string
  // here. Without this, controlled inputs round-trip through `String(Number())`
  // on every keystroke and partial states like `"0."` get normalized to `"0"`,
  // making backspace/decimal entry feel broken.
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  // Field-keyed errors (e.g. "filter.confidence_threshold" → "must be ≤ 1")
  // render inline under the offending input. `_global` is the catch-all
  // bucket for non-field errors.
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})

  useEffect(() => {
    if (!currentUser?.is_admin) return
    api.getModels().then((m) => {
      setIndexingModel(m.indexing_model)
      setReviewModel(m.review_model)
      setIndexingOptions(m.indexing_options)
      setReviewOptions(m.review_options)
    })
    api.getGlobalSettings().then((s) => {
      setEffective(
        (s.effective as {
          filter?: Record<string, number | boolean | string>
          review?: Record<string, number | boolean | string>
        }) ?? null
      )
      setOverrides({
        filter: s.overrides.filter ?? {},
        review: s.overrides.review ?? {},
      })
    })
  }, [currentUser])

  if (!currentUser?.is_admin) {
    return (
      <div className="p-6 text-sm text-muted-foreground">
        Admin access required.
      </div>
    )
  }

  const saveModels = async () => {
    setSavingModels(true)
    await api.saveModels(indexingModel, reviewModel)
    setSavingModels(false)
    setModelsSaved(true)
    setTimeout(() => setModelsSaved(false), 2000)
  }

  const setOverride = (
    section: "filter" | "review",
    key: string,
    value: number | boolean | string | null
  ) => {
    setOverrides((prev) => {
      const next = { ...prev[section] }
      if (value === null || value === "") delete next[key]
      else next[key] = value
      return { ...prev, [section]: next }
    })
  }

  const saveOverrides = async () => {
    setSavingOverrides(true)
    setFieldErrors({})
    try {
      const body: Record<string, Record<string, number | boolean | string>> = {}
      if (Object.keys(overrides.filter).length > 0)
        body.filter = overrides.filter
      if (Object.keys(overrides.review).length > 0)
        body.review = overrides.review
      await api.saveGlobalSettings(body)
      setOverridesSaved(true)
      setTimeout(() => setOverridesSaved(false), 2000)
      const fresh = await api.getGlobalSettings()
      setEffective(
        (fresh.effective as {
          filter?: Record<string, number | boolean | string>
          review?: Record<string, number | boolean | string>
        }) ?? null
      )
    } catch (err) {
      // The API returns `{detail: {field?, message}}` for validation failures.
      // `fetchJson`/`putJson` wrap the response body in `API error NNN: <body>`,
      // so strip that prefix and JSON.parse the rest to recover the structured
      // detail. Regex-extracting the detail object choked on nested braces /
      // escaped quotes — full JSON.parse is the right tool.
      const raw = err instanceof Error ? err.message : String(err)
      // Try to parse the full error message as JSON to extract structured detail.
      let parsedError: { detail?: { field?: string; message: string } } | null =
        null
      try {
        parsedError = JSON.parse(raw.replace(/^API error \d+: /, ""))
      } catch {
        /* ignore */
      }
      const detail = parsedError?.detail
      if (detail && typeof detail === "object" && "message" in detail) {
        setFieldErrors({ [detail.field ?? "_global"]: detail.message })
      } else {
        setFieldErrors({ _global: raw })
      }
    } finally {
      setSavingOverrides(false)
    }
  }

  const numField = (
    section: "filter" | "review",
    key: string,
    label: string,
    description: string,
    step: string = "1",
    min?: number,
    max?: number
  ) => {
    const fieldKey = `${section}.${key}`
    const eff = (effective?.[section] as Record<string, unknown> | undefined)?.[
      key
    ]
    const override = overrides[section][key]
    const overridden = override !== undefined
    const committed = typeof override === "number" ? override : eff
    const draft = drafts[fieldKey]
    const display =
      draft !== undefined
        ? draft
        : committed !== undefined && committed !== null
          ? String(committed)
          : ""
    const error = fieldErrors[fieldKey]

    const commit = () => {
      const v = drafts[fieldKey]
      if (v === undefined) return
      // Drop the draft so the next render reads from `committed` again.
      setDrafts((d) => {
        const next = { ...d }
        delete next[fieldKey]
        return next
      })
      if (v === "") {
        setOverride(section, key, null)
        return
      }
      let n = Number(v)
      if (Number.isNaN(n)) return
      // Clamp to declared bounds so the user can't enter out-of-range
      // values that the server would just reject anyway.
      if (typeof min === "number" && n < min) n = min
      if (typeof max === "number" && n > max) n = max
      setOverride(section, key, n === eff ? null : n)
    }

    return (
      <div className="space-y-1">
        <div className="flex items-baseline gap-3">
          <label className="text-sm font-medium" htmlFor={fieldKey}>
            {label}
          </label>
          {overridden && (
            <span className="text-[11px] font-semibold text-primary">
              Overrides <code className="font-mono">mira.yaml</code>
            </span>
          )}
        </div>
        <Input
          id={fieldKey}
          type="number"
          step={step}
          min={min}
          max={max}
          aria-invalid={error ? true : undefined}
          className={error ? "border-destructive" : undefined}
          value={display}
          onChange={(e) =>
            setDrafts((d) => ({ ...d, [fieldKey]: e.target.value }))
          }
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.currentTarget.blur()
            }
          }}
        />
        {error ? (
          <p className="text-xs text-destructive">
            {label} {error}
          </p>
        ) : (
          <p className="text-xs text-muted-foreground">{description}</p>
        )}
      </div>
    )
  }

  const boolField = (
    section: "filter" | "review",
    key: string,
    label: string,
    description: string
  ) => {
    const eff = (effective?.[section] as Record<string, unknown> | undefined)?.[
      key
    ]
    const override = overrides[section][key]
    const overridden = override !== undefined
    const checked = typeof override === "boolean" ? override : Boolean(eff)
    const error = fieldErrors[`${section}.${key}`]
    return (
      <div className="space-y-1">
        <div className="flex items-center gap-3">
          <label
            className="flex items-center gap-2 text-sm font-medium"
            htmlFor={`${section}.${key}`}
          >
            <input
              id={`${section}.${key}`}
              type="checkbox"
              checked={checked}
              onChange={(e) =>
                setOverride(
                  section,
                  key,
                  e.target.checked === Boolean(eff) ? null : e.target.checked
                )
              }
              className="size-4 rounded border-input accent-primary"
            />
            {label}
          </label>
          {overridden && (
            <span className="text-[11px] font-semibold text-primary">
              Overrides <code className="font-mono">mira.yaml</code>
            </span>
          )}
        </div>
        {error ? (
          <p className="pl-6 text-xs text-destructive">
            {label} {error}
          </p>
        ) : (
          <p className="pl-6 text-xs text-muted-foreground">{description}</p>
        )}
      </div>
    )
  }

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground">
          Configure Mira models and behavior
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Models</CardTitle>
          <CardDescription>
            Choose models for indexing and PR reviews
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <label className="text-sm font-medium">Indexing Model</label>
            <Select value={indexingModel} onValueChange={setIndexingModel}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {indexingOptions.map((opt) => (
                  <SelectItem key={opt.value} value={opt.value}>
                    {opt.label}
                    {opt.recommended && (
                      <span className="ml-2 text-xs text-muted-foreground">
                        Recommended
                      </span>
                    )}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              Used to summarize files when building the code index. A cheaper
              model is recommended since it runs over every file.
            </p>
          </div>
          <div className="space-y-2">
            <label className="text-sm font-medium">Review Model</label>
            <Select value={reviewModel} onValueChange={setReviewModel}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {reviewOptions.map((opt) => (
                  <SelectItem key={opt.value} value={opt.value}>
                    {opt.label}
                    {opt.recommended && (
                      <span className="ml-2 text-xs text-muted-foreground">
                        Recommended
                      </span>
                    )}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              Used to analyze PRs and post review comments. A more powerful
              model gives better review quality.
            </p>
          </div>
          <div className="flex items-center gap-3">
            <Button size="sm" onClick={saveModels} disabled={savingModels}>
              {savingModels && (
                <Loader2 className="mr-2 h-3 w-3 animate-spin" />
              )}
              Save
            </Button>
            {modelsSaved && (
              <span className="text-xs text-muted-foreground">Saved</span>
            )}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Review behaviour overrides</CardTitle>
          <CardDescription>
            Tune the noise filter and review knobs without restarting the
            server. These overrides deep-merge over{" "}
            <code className="text-xs">mira.yaml</code> and apply to every repo
            this Mira instance reviews. Per-repo{" "}
            <code className="text-xs">.mira.yml</code> files still take
            precedence for individual repos.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <div>
            <h3 className="mb-3 text-sm font-semibold">Filter</h3>
            <div className="space-y-4">
              {numField(
                "filter",
                "confidence_threshold",
                "Confidence threshold",
                "Drop comments the LLM rated below this confidence (0.0–1.0). Lower = more comments survive.",
                "0.1",
                0,
                1
              )}
              {numField(
                "filter",
                "max_comments",
                "Max comments per PR",
                "Hard cap on inline comments per PR. Most severe + most confident N are kept.",
                "1",
                1
              )}
              {numField(
                "filter",
                "max_files",
                "Max files",
                "Cap on files reviewed in a single PR. PRs above this are partially reviewed.",
                "1",
                1
              )}
            </div>
          </div>

          <div>
            <h3 className="mb-3 text-sm font-semibold">Review</h3>
            <div className="space-y-4">
              {boolField(
                "review",
                "walkthrough",
                "Post walkthrough comment",
                "Top-level summary comment with file coverage and per-severity stats."
              )}
              {boolField(
                "review",
                "self_critique",
                "Self-critique pass",
                "Second-pass LLM critique on each draft comment. Drops confident-but-wrong findings at the cost of latency."
              )}
              {boolField(
                "review",
                "security_pass",
                "Security review pass",
                "Dedicated security pass (XSS, injection, auth, CSRF, SSRF, deserialization, crypto) merged with the main review."
              )}
              {boolField(
                "review",
                "blast_radius",
                "Blast radius",
                "Lists dependent repositories that import code touched by this PR in the walkthrough comment."
              )}
              {numField(
                "review",
                "max_concurrent_chunks",
                "Max concurrent chunks",
                "Parallelism for chunk reviews (1–20). Raise if your LLM provider can handle it.",
                "1",
                1,
                20
              )}
            </div>
          </div>

          <div className="space-y-2">
            <div className="flex items-center gap-3">
              <Button
                size="sm"
                onClick={saveOverrides}
                disabled={savingOverrides}
              >
                {savingOverrides && (
                  <Loader2 className="mr-2 h-3 w-3 animate-spin" />
                )}
                Save overrides
              </Button>
              {overridesSaved && (
                <span className="text-xs text-muted-foreground">Saved</span>
              )}
            </div>
            {fieldErrors._global && (
              <p className="text-xs break-words text-destructive">
                {fieldErrors._global}
              </p>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
