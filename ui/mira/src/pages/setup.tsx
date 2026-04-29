import { Loader2 } from "lucide-react"
import { useEffect, useState } from "react"
import { useNavigate } from "react-router"

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

type ModelOption = {
  value: string
  label: string
  recommended?: boolean
}

export function SetupPage() {
  const navigate = useNavigate()
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [indexingModel, setIndexingModel] = useState("")
  const [reviewModel, setReviewModel] = useState("")
  const [indexingOptions, setIndexingOptions] = useState<ModelOption[]>([])
  const [reviewOptions, setReviewOptions] = useState<ModelOption[]>([])

  useEffect(() => {
    api.getModels().then((data) => {
      setIndexingModel(data.indexing_model)
      setReviewModel(data.review_model)
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
        <img src="/logo.png" alt="Mira" className="mx-auto mb-4 h-12 w-12" />
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
