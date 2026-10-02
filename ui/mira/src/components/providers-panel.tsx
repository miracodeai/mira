import { useState } from "react"

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { useApiProviders } from "@/hooks/use-api-providers"

import {
  ConnectedProviderRow,
  CustomProvider,
  PresetRow,
} from "./providers/api-providers"
import { SubscriptionAccounts } from "./providers/subscription-accounts"

const TABS = [
  { value: "accounts", label: "Accounts" },
  { value: "free", label: "Free" },
  { value: "local", label: "Local" },
  { value: "paid", label: "Paid" },
] as const

export function ProvidersPanel({ onChanged }: { onChanged?: () => void }) {
  const [tab, setTab] = useState<string>("accounts")
  const [search, setSearch] = useState("")
  const providers = useApiProviders(onChanged)

  const q = search.trim().toLowerCase()
  const matches = (label: string) => label.toLowerCase().includes(q)
  const connectedIds = new Set(providers.connected.map((c) => c.id))

  return (
    <Card>
      <CardHeader>
        <CardTitle>Providers</CardTitle>
        <CardDescription>
          Sign in to ChatGPT/Codex or Claude, or connect API-key and local
          providers. Their models then appear in every model picker.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <Input
          placeholder="Search providers..."
          aria-label="Search providers"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        {providers.error && (
          <p className="flex items-center gap-2 text-xs text-destructive">
            Couldn't load providers: {providers.error}
            <Button size="sm" variant="outline" onClick={providers.refresh}>
              Retry
            </Button>
          </p>
        )}
        <Tabs value={tab} onValueChange={setTab}>
          <TabsList variant="line">
            {TABS.map((t) => (
              <TabsTrigger key={t.value} value={t.value} className="px-3">
                {t.label}
              </TabsTrigger>
            ))}
          </TabsList>
          {/* Stay mounted so a pending sign-in keeps polling while another tab is open. */}
          <TabsContent
            value="accounts"
            forceMount
            className="space-y-2 pt-2 data-[state=inactive]:hidden"
          >
            <SubscriptionAccounts query={q} onChanged={onChanged} />
            {providers.connected
              .filter((c) => matches(c.label))
              .map((c) => (
                <ConnectedProviderRow
                  key={c.id}
                  provider={c}
                  onChanged={providers.refresh}
                />
              ))}
          </TabsContent>
          {(["free", "local", "paid"] as const).map((tier) => (
            <TabsContent key={tier} value={tier} className="space-y-2 pt-2">
              {providers.presets
                .filter((p) => p.tier === tier && matches(p.label))
                .map((p) => (
                  <PresetRow
                    key={p.id}
                    preset={p}
                    connected={connectedIds.has(p.id)}
                    onChanged={providers.refresh}
                  />
                ))}
            </TabsContent>
          ))}
        </Tabs>
        <CustomProvider
          onDone={() => {
            setTab("accounts")
            providers.refresh()
          }}
        />
      </CardContent>
    </Card>
  )
}
