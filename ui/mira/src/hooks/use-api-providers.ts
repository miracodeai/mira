import { useCallback, useEffect, useRef, useState } from "react"

import { api } from "@/lib/api"
import { errorText } from "@/lib/api/http"
import type { ConnectedProvider, ProviderPreset } from "@/lib/api/settings"

// Provider presets plus connected API-key/local providers; refresh also notifies the page.
export function useApiProviders(onChanged?: () => void) {
  const [data, setData] = useState<{
    presets: ProviderPreset[]
    connected: ConnectedProvider[]
  }>({ presets: [], connected: [] })
  const [error, setError] = useState("")
  // Only the newest request may update state, so a slow earlier load can't win.
  const latest = useRef(0)
  const load = useCallback(() => {
    const id = ++latest.current
    api.getApiProviders().then(
      (d) => {
        if (id !== latest.current) return
        setData(d)
        setError("")
      },
      (e) => {
        if (id === latest.current) setError(errorText(e))
      }
    )
  }, [])
  useEffect(load, [load])
  const refresh = useCallback(() => {
    load()
    onChanged?.()
  }, [load, onChanged])
  return { ...data, error, refresh }
}
