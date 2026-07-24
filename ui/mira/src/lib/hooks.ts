import { useEffect, useState } from "react"

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    fn()
      .then((result) => {
        if (!cancelled) setData(result)
      })
      .catch((err) => {
        if (!cancelled) setError(err.message)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return { data, loading, error }
}

const APP_NAME = "Mira"

/**
 * Sets the browser tab title to `${title} · Mira`. Pass `null` (e.g. while data
 * is still loading) to show the bare app name rather than flashing a
 * placeholder.
 *
 * No restore-on-unmount: each page sets its own title on mount, so the incoming
 * page always overwrites the outgoing one. Snapshotting the previous title and
 * restoring it on cleanup would be wrong here — during an overlapping route
 * transition (or React Strict Mode's double-invoke) the outgoing page's cleanup
 * runs after the incoming page's effect, wiping the title the new page just set.
 */
export function useDocumentTitle(title: string | null) {
  useEffect(() => {
    document.title = title ? `${title} · ${APP_NAME}` : APP_NAME
  }, [title])
}
