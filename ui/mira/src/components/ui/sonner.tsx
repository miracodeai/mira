import { Toaster as Sonner, type ToasterProps } from "sonner"

import { useTheme } from "@/components/theme-provider"

// Toast host. Themed via the app's own ThemeProvider (not next-themes).
function Toaster(props: ToasterProps) {
  const { theme } = useTheme()
  return (
    <Sonner
      theme={theme}
      className="toaster group"
      richColors
      closeButton
      {...props}
    />
  )
}

export { Toaster }
export { toast } from "sonner"
