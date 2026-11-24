import { Badge } from "@/components/ui/badge"
import type { ReviewOutcome } from "@/lib/api/types"

const DELIVERY_LABEL = {
  complete: "Finding delivery complete",
  partial: "Finding delivery partial",
  failed: "Finding delivery failed",
  unknown: "Finding delivery unconfirmed",
  not_attempted: "Findings not sent",
}

export function ReviewDeliveryBadge({
  outcome,
}: {
  outcome?: ReviewOutcome | null
}) {
  return (
    <Badge
      variant={
        outcome?.status === "failed" || outcome?.delivery_error
          ? "destructive"
          : "secondary"
      }
    >
      {outcome ? DELIVERY_LABEL[outcome.status] : "Delivery unverified"}
    </Badge>
  )
}

export function ReviewOutcomeSummary({
  outcome,
}: {
  outcome?: ReviewOutcome | null
}) {
  if (!outcome) {
    return (
      <p className="mt-2 text-xs text-muted-foreground">
        Delivery details were not recorded for this review.
      </p>
    )
  }
  const counts = [
    ["Drafted", outcome.drafted ?? "Not recorded"],
    ["Retained", outcome.retained],
    ["Confirmed delivered", outcome.delivered],
    ["Not delivered", outcome.failed],
    ["Unconfirmed", outcome.unknown],
  ]
  return (
    <div className="mt-3 space-y-2 rounded-md border p-3">
      <div className="flex flex-wrap gap-2">
        <ReviewDeliveryBadge outcome={outcome} />
        <Badge
          variant={outcome.coverage === "failed" ? "destructive" : "outline"}
        >
          Review {outcome.coverage}
        </Badge>
      </div>
      <dl className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-3">
        {counts.map(([label, value]) => (
          <div key={label}>
            <dt className="text-muted-foreground">{label}</dt>
            <dd className="font-medium">{value}</dd>
          </div>
        ))}
      </dl>
      {outcome.coverage === "complete" && outcome.retained === 0 && (
        <p className="text-xs text-muted-foreground">
          {outcome.drafted === 0
            ? "No findings were drafted."
            : "No drafted findings were retained."}
        </p>
      )}
      {outcome.status === "not_attempted" && (
        <p className="text-xs text-muted-foreground">
          Finding delivery was not attempted.
        </p>
      )}
      {outcome.delivery_error && (
        <p className="text-xs text-muted-foreground">
          Posting ended with an error. Confirmed deliveries remain recorded.
        </p>
      )}
    </div>
  )
}
