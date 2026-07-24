import { Loader2 } from "lucide-react"
import { useEffect, useState } from "react"
import { useNavigate } from "react-router"

import { ModelCombobox, type ModelOption } from "@/components/model-combobox"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { api } from "@/lib/api"
import { useDocumentTitle } from "@/lib/hooks"

export function SetupPage() {
  useDocumentTitle("Setup")
  const navigate = useNavigate()
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  // "" = inherit from deployment config — an untouched save must not
  // convert mira.yaml-sourced models into dashboard overrides.
  const [indexingModel, setIndexingModel] = useState("")
  const [reviewModel, setReviewModel] = useState("")
  const [configIndexingModel, setConfigIndexingModel] = useState("")
  const [configReviewModel, setConfigReviewModel] = useState("")
  const [indexingOptions, setIndexingOptions] = useState<ModelOption[]>([])
  const [reviewOptions, setReviewOptions] = useState<ModelOption[]>([])

  useEffect(() => {
    api.getModels().then((data) => {
      setIndexingModel(data.indexing_source === "config" ? "" : data.indexing_model)
      setReviewModel(data.review_source === "config" ? "" : data.review_model)
      setConfigIndexingModel(data.config_indexing_model)
      setConfigReviewModel(data.config_review_model)
      setIndexingOptions(data.indexing_options)
      setReviewOptions(data.review_options)
      setLoading(false)
    })
  }, [])

  const handleSave = async () => {
    setSaving(true)
    await api.saveModels(indexingModel, reviewModel)
    navigate("/")
  }

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-lg space-y-6 px-4 py-16">
      <div className="text-center">
        <img src="/logo.png" alt="Mira" className="mx-auto mb-4 hidden h-12 w-12 dark:block" />
        <img src="/logo-light.png" alt="Mira" className="mx-auto mb-4 h-12 w-12 dark:hidden" />
        <h1 className="text-2xl font-semibold tracking-tight">
          Welcome to Mira
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Choose which models to use for indexing and reviews
        </p>
      </div>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Indexing Model</CardTitle>
          <CardDescription>
            Used to summarize files when building the code index. We recommend
            a cheaper model here since it runs over every file.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ModelCombobox
            value={indexingModel}
            onChange={setIndexingModel}
            options={indexingOptions}
            configModel={configIndexingModel}
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Review Model</CardTitle>
          <CardDescription>
            Used to analyze PRs and post comments. A more powerful model here
            gives better review quality.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ModelCombobox
            value={reviewModel}
            onChange={setReviewModel}
            options={reviewOptions}
            configModel={configReviewModel}
          />
        </CardContent>
      </Card>

      <p className="text-center text-xs text-muted-foreground">
        You can change these later in Settings
      </p>

      <Button className="w-full" size="lg" onClick={handleSave} disabled={saving}>
        {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
        Save and Continue
      </Button>
    </div>
  )
}
