import { Loader2, X } from "lucide-react"
import { useEffect, useState } from "react"

import { ModelCombobox, type ModelOption } from "@/components/model-combobox"
import { ProvidersPanel } from "@/components/providers-panel"
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
import { Link, useParams } from "react-router"

import { api } from "@/lib/api"
import { useAuth } from "@/lib/auth"
import { useDocumentTitle } from "@/lib/hooks"

// Ordered models tried when the one above fails (quota, outage); mixes providers freely.
function FallbackList({
  value,
  onChange,
  options,
  title = "Fallbacks, tried in order when the model above fails.",
  emptyHint,
}: {
  value: string[]
  onChange: (v: string[]) => void
  options: ModelOption[]
  title?: string
  emptyHint?: string
}) {
  const label = (id: string) => options.find((o) => o.value === id)?.label ?? id
  return (
    <div className="space-y-1.5 border-l pl-3">
      <p className="text-xs text-muted-foreground">
        {title}
        {value.length === 0 && emptyHint ? ` ${emptyHint}` : ""}
      </p>
      {value.map((id, i) => (
        <div key={id} className="flex items-center gap-2 text-sm">
          <span className="w-4 text-xs text-muted-foreground">{i + 1}.</span>
          <span className="flex-1 truncate">{label(id)}</span>
          <Button
            size="icon-sm"
            variant="ghost"
            aria-label={`Remove ${label(id)}`}
            onClick={() => onChange(value.filter((m) => m !== id))}
          >
            <X />
          </Button>
        </div>
      ))}
      <ModelCombobox
        value=""
        onChange={(id) => id && !value.includes(id) && onChange([...value, id])}
        options={options}
      />
    </div>
  )
}

export function SettingsPage() {
  useDocumentTitle("Settings")
  const { user: currentUser } = useAuth()
  const { section = "models" } = useParams()

  // "" = inherit from deployment config; anything else is a model id.
  const [indexingModel, setIndexingModel] = useState("")
  const [reviewModel, setReviewModel] = useState("")
  const [securityModel, setSecurityModel] = useState("")
  const [configIndexingModel, setConfigIndexingModel] = useState("")
  const [configReviewModel, setConfigReviewModel] = useState("")
  const [configSecurityModel, setConfigSecurityModel] = useState("")
  const [backend, setBackend] = useState("")
  const [missingApiKey, setMissingApiKey] = useState("")
  const [indexingOptions, setIndexingOptions] = useState<ModelOption[]>([])
  const [reviewOptions, setReviewOptions] = useState<ModelOption[]>([])
  const [securityOptions, setSecurityOptions] = useState<ModelOption[]>([])
  const [fallbacks, setFallbacks] = useState({
    indexing_fallbacks: [] as string[],
    review_fallbacks: [] as string[],
    security_fallbacks: [] as string[],
  })
  const [critiqueModel, setCritiqueModel] = useState("")
  const [ensembleModels, setEnsembleModels] = useState<string[]>([])
  const [thinkingMode, setThinkingMode] = useState("off")
  const [thinkingOptions, setThinkingOptions] = useState<ModelOption[]>([])
  const [apiStyle, setApiStyle] = useState("chat")
  const [apiStyleOptions, setApiStyleOptions] = useState<ModelOption[]>([])
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

  // Provider changes refresh only the catalog so unsaved picks survive.
  const loadModels = (withSelections = true) =>
    api.getModels().then((m) => {
      setBackend(m.backend)
      setMissingApiKey(m.missing_api_key ?? "")
      setIndexingOptions(m.indexing_options)
      setReviewOptions(m.review_options)
      setSecurityOptions(m.security_options)
      if (!withSelections) return
      setIndexingModel(m.indexing_source === "config" ? "" : m.indexing_model)
      setReviewModel(m.review_source === "config" ? "" : m.review_model)
      setSecurityModel(m.security_source === "config" ? "" : m.security_model)
      setConfigIndexingModel(m.config_indexing_model)
      setConfigReviewModel(m.config_review_model)
      setConfigSecurityModel(m.config_security_model)
      setThinkingMode(m.review_thinking_mode)
      setThinkingOptions(m.thinking_options)
      setApiStyle(m.api_style ?? "chat")
      setApiStyleOptions(m.api_style_options ?? [])
      setFallbacks({
        indexing_fallbacks: m.indexing_fallbacks ?? [],
        review_fallbacks: m.review_fallbacks ?? [],
        security_fallbacks: m.security_fallbacks ?? [],
      })
      setCritiqueModel(m.critique_model ?? "")
      setEnsembleModels(m.ensemble_models ?? [])
    })

  useEffect(() => {
    if (!currentUser?.is_admin) return
    loadModels()
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
    await api.saveModels(
      indexingModel,
      reviewModel,
      securityModel,
      thinkingMode,
      apiStyle,
      {
        ...fallbacks,
        critique_model: critiqueModel,
        ensemble_models: ensembleModels,
      }
    )
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

      {section === "providers" && (
        <ProvidersPanel onChanged={() => loadModels(false)} />
      )}

      {section === "models" && (
        <Card>
          <CardHeader>
            <CardTitle>Models</CardTitle>
            <CardDescription>
              Choose models for indexing and PR reviews
              {backend &&
                !missingApiKey &&
                ` — listed from ${
                  {
                    openrouter: "OpenRouter",
                    bedrock: "AWS Bedrock",
                  }[backend] ?? "your configured endpoint"
                } plus any subscriptions signed in under Providers`}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {missingApiKey && (
              <p className="rounded-md border px-3 py-2 text-xs text-muted-foreground">
                Connect API-key or local providers under{" "}
                <Link to="/settings/providers" className="underline">
                  Providers
                </Link>{" "}
                to add their models. The server&apos;s default endpoint also
                needs <code className="font-mono">{missingApiKey}</code>.
              </p>
            )}
            <div className="space-y-2">
              <label className="text-sm font-medium">Indexing Model</label>
              <ModelCombobox
                value={indexingModel}
                onChange={setIndexingModel}
                options={indexingOptions}
                configModel={configIndexingModel}
              />
              <FallbackList
                value={fallbacks.indexing_fallbacks}
                onChange={(v) =>
                  setFallbacks((f) => ({ ...f, indexing_fallbacks: v }))
                }
                options={indexingOptions}
              />
              <p className="text-xs text-muted-foreground">
                Used to summarize files when building the code index. A cheaper
                model is recommended since it runs over every file.
              </p>
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">Review Model</label>
              <ModelCombobox
                value={reviewModel}
                onChange={setReviewModel}
                options={reviewOptions}
                configModel={configReviewModel}
              />
              <FallbackList
                value={fallbacks.review_fallbacks}
                onChange={(v) =>
                  setFallbacks((f) => ({ ...f, review_fallbacks: v }))
                }
                options={reviewOptions}
              />
              <p className="text-xs text-muted-foreground">
                Used to analyze PRs and post review comments. A more powerful
                model gives better review quality.
              </p>
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">Security Model</label>
              <ModelCombobox
                value={securityModel}
                onChange={setSecurityModel}
                options={securityOptions}
                configModel={configSecurityModel}
              />
              <FallbackList
                value={fallbacks.security_fallbacks}
                onChange={(v) =>
                  setFallbacks((f) => ({ ...f, security_fallbacks: v }))
                }
                options={securityOptions}
                emptyHint="Uses the review fallbacks when empty."
              />
              <p className="text-xs text-muted-foreground">
                Used for the dedicated security pass. Defaults to the review
                model — set a cheaper one only if you accept lower security
                recall.
              </p>
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">Critic Model</label>
              <ModelCombobox
                value={critiqueModel}
                onChange={setCritiqueModel}
                options={reviewOptions}
                configModel={indexingModel || configIndexingModel}
                inheritLabel="Use the indexing model"
              />
              <p className="text-xs text-muted-foreground">
                Checks each drafted comment against the code and drops the
                unsupported ones. A model from a different family than the
                review model catches mistakes the reviewer keeps repeating.
              </p>
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">
                Second-opinion Models
              </label>
              <FallbackList
                value={ensembleModels}
                onChange={setEnsembleModels}
                options={reviewOptions}
                title="Also review each PR with these; a finding is kept only when most models report it."
              />
              <p className="text-xs text-muted-foreground">
                Posts fewer comments and adds one review per model. With one
                extra model both must agree, which drops many real findings; use
                two or more.
              </p>
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">
                Review Thinking Mode
              </label>
              <Select value={thinkingMode} onValueChange={setThinkingMode}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {thinkingOptions.map((opt) => (
                    <SelectItem key={opt.value} value={opt.value}>
                      {opt.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                Extended reasoning budget for reviews — improves depth on
                capable models at the cost of latency and tokens. Works on
                OpenRouter, Bedrock (Claude) and ChatGPT/Claude subscriptions;
                on other endpoints it's skipped automatically when unsupported.
              </p>
            </div>
            {backend !== "bedrock" && (
              <div className="space-y-2">
                <label className="text-sm font-medium">API Protocol</label>
                <Select value={apiStyle} onValueChange={setApiStyle}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {apiStyleOptions.map((opt) => (
                      <SelectItem key={opt.value} value={opt.value}>
                        {opt.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  Protocol used to talk to this endpoint. Responses API requires
                  a server exposing /responses (OpenAI and compatible proxies);
                  Chat Completions works everywhere.
                </p>
              </div>
            )}
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
      )}

      {section === "review" && (
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
                {boolField(
                  "review",
                  "dependency_overlap",
                  "Duplicate dependency check",
                  "Warns when a PR adds a dependency that duplicates an existing one (e.g. a second table or HTTP-client library)."
                )}
                {boolField(
                  "review",
                  "auto_resolve_conversations",
                  "Auto-resolve conversations",
                  "Automatically resolve bot review threads the LLM verifies as fixed on each review. Turn off to leave comments open until a human resolves them."
                )}
                {boolField(
                  "review",
                  "review_on_synchronize",
                  "Review on every push",
                  "When enabled, Mira reviews every new commit pushed to a PR. Disable to only review on PR open or manual @bot_name review."
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
      )}
    </div>
  )
}
