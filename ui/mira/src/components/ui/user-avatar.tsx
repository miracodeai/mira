import { glass } from "@dicebear/collection"
import { createAvatar } from "@dicebear/core"
import { useMemo } from "react"

import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { cn } from "@/lib/utils"

/**
 * Deterministic user avatar generated locally with DiceBear (no network calls).
 * Same `seed` (e.g. a username) always yields the same image; initials are the
 * fallback while/if the SVG can't render.
 */
export function UserAvatar({
  seed,
  className,
}: {
  seed: string
  className?: string
}) {
  const src = useMemo(
    () => createAvatar(glass, { seed, radius: 50 }).toDataUri(),
    [seed]
  )
  const initials = seed.slice(0, 2).toUpperCase()

  return (
    <Avatar className={cn(className)}>
      <AvatarImage src={src} alt={seed} />
      <AvatarFallback>{initials}</AvatarFallback>
    </Avatar>
  )
}
