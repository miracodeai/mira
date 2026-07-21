import { useRef, useState, type ReactNode } from "react"

import { Input } from "@/components/ui/input"

export type ModelOption = {
  value: string
  label: string
  recommended?: boolean
}

function ComboboxItem({
  onPick,
  highlighted,
  onHover,
  children,
}: {
  onPick: () => void
  highlighted: boolean
  onHover: () => void
  children: ReactNode
}) {
  return (
    <button
      type="button"
      role="option"
      aria-selected={highlighted}
      ref={(el) => {
        if (highlighted) el?.scrollIntoView({ block: "nearest" })
      }}
      className={`flex w-full items-center px-2 py-1.5 text-left text-sm ${
        highlighted ? "bg-accent text-accent-foreground" : ""
      }`}
      onMouseDown={(e) => {
        e.preventDefault()
        onPick()
      }}
      onMouseEnter={onHover}
    >
      {children}
    </button>
  )
}

// Searchable model picker. Typing filters the backend's catalog; arrows +
// Enter or click select; free-form ids commit via the "Use …" row. When
// `configModel` is set, an "Inherit from deployment config" row is pinned
// first and selecting it yields value "".
export function ModelCombobox({
  value,
  onChange,
  options,
  configModel,
}: {
  value: string
  onChange: (v: string) => void
  options: ModelOption[]
  configModel?: string
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState("")
  const [highlight, setHighlight] = useState(-1)

  const selected =
    value === "" && configModel !== undefined
      ? `Inherit from deployment config (${configModel})`
      : (options.find((o) => o.value === value)?.label ?? value)
  const q = query.trim().toLowerCase()
  const filtered = q
    ? options.filter((o) => `${o.label} ${o.value}`.toLowerCase().includes(q))
    : options
  const showInherit = configModel !== undefined
  const custom =
    query.trim() && !filtered.some((o) => o.value === query.trim())
      ? query.trim()
      : null
  const rowCount = (showInherit ? 1 : 0) + filtered.length + (custom ? 1 : 0)

  const pick = (v: string) => {
    onChange(v)
    inputRef.current?.blur()
  }

  const pickAt = (i: number) => {
    if (showInherit && i === 0) return pick("")
    const j = i - (showInherit ? 1 : 0)
    if (j < filtered.length) return pick(filtered[j].value)
    if (custom) return pick(custom)
  }

  // Row index of the first catalog option (after the inherit row, if any).
  const firstOption = showInherit ? 1 : 0

  return (
    <div className="relative">
      <Input
        ref={inputRef}
        role="combobox"
        aria-expanded={open}
        value={open ? query : selected}
        placeholder="Search models…"
        onFocus={() => {
          setOpen(true)
          setQuery("")
          setHighlight(-1)
        }}
        onBlur={() => setOpen(false)}
        onChange={(e) => {
          setQuery(e.target.value)
          setOpen(true)
          setHighlight(-1)
        }}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") {
            e.preventDefault()
            setHighlight((h) => Math.min(h + 1, rowCount - 1))
          } else if (e.key === "ArrowUp") {
            e.preventDefault()
            setHighlight((h) => Math.max(h - 1, 0))
          } else if (e.key === "Enter") {
            if (highlight >= 0) pickAt(highlight)
            else if (query.trim())
              // Prefer the top visible match; raw text only when nothing matches.
              pickAt(filtered.length > 0 ? firstOption : rowCount - 1)
          } else if (e.key === "Escape") {
            inputRef.current?.blur()
          }
        }}
      />
      {open && (
        <div
          role="listbox"
          className="absolute z-50 mt-1 max-h-64 w-full overflow-y-auto rounded-md border bg-popover text-popover-foreground shadow-md"
          onMouseDown={(e) => e.preventDefault()}
        >
          {showInherit && (
            <ComboboxItem
              onPick={() => pick("")}
              highlighted={highlight === 0}
              onHover={() => setHighlight(0)}
            >
              Inherit from deployment config
              <span className="ml-2 font-mono text-xs text-muted-foreground">
                {configModel}
              </span>
            </ComboboxItem>
          )}
          {filtered.map((opt, i) => (
            <ComboboxItem
              key={opt.value}
              onPick={() => pick(opt.value)}
              highlighted={highlight === firstOption + i}
              onHover={() => setHighlight(firstOption + i)}
            >
              <span className="truncate">{opt.label}</span>
              {opt.value !== opt.label && (
                <span className="ml-2 truncate font-mono text-xs text-muted-foreground">
                  {opt.value}
                </span>
              )}
              {opt.recommended && (
                <span className="ml-auto pl-2 text-xs text-muted-foreground">
                  Recommended
                </span>
              )}
            </ComboboxItem>
          ))}
          {custom && (
            <ComboboxItem
              onPick={() => pick(custom)}
              highlighted={highlight === rowCount - 1}
              onHover={() => setHighlight(rowCount - 1)}
            >
              Use “{custom}”
            </ComboboxItem>
          )}
        </div>
      )}
    </div>
  )
}
