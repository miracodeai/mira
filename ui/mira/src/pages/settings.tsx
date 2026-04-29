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

  useEffect(() => {
    if (!currentUser?.is_admin) return
    api.getModels().then((m) => {
      setIndexingModel(m.indexing_model)
      setReviewModel(m.review_model)
      setIndexingOptions(m.indexing_options)
      setReviewOptions(m.review_options)
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

    </div>
  )
}
