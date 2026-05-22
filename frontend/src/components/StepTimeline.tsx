import { Check, AlertCircle, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * StepTimeline — renders SSE "step" / "error" events as a growing list.
 *
 * Accepts the raw events array from useSSE. Filters internally to
 * keep the prop surface small — the caller just hands over whatever
 * stream they're consuming. Renders an in-progress spinner at the
 * tail when `pending` is true (the stream is still open).
 *
 * Convention: each step event's `data` is the human-readable message
 * already formatted by the backend service. The frontend does not
 * re-phrase or summarise — what the backend says is what the user
 * sees. This keeps the source of truth on one side of the wire and
 * avoids two places re-deriving the same string.
 */

export interface StepTimelineEvent {
  type: string;
  data?: unknown;
}

export interface StepTimelineProps {
  events: StepTimelineEvent[];
  pending?: boolean;
  pendingLabel?: string;
  className?: string;
}

export function StepTimeline({
  events,
  pending = false,
  pendingLabel = "Working…",
  className,
}: StepTimelineProps) {
  return (
    <ol className={cn("space-y-2", className)}>
      {events.map((e, i) => {
        if (e.type === "step") {
          return (
            <li key={i} className="flex items-start gap-3 text-sm">
              <Check className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
              <span className="text-foreground">{String(e.data ?? "")}</span>
            </li>
          );
        }
        if (e.type === "error") {
          return (
            <li
              key={i}
              className="flex items-start gap-3 text-sm text-destructive"
            >
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>{String(e.data ?? "error")}</span>
            </li>
          );
        }
        return null;
      })}
      {pending && (
        <li className="flex items-start gap-3 text-sm text-muted-foreground">
          <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin" />
          <span>{pendingLabel}</span>
        </li>
      )}
    </ol>
  );
}
