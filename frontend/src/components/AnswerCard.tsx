import { useState } from "react";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  Pencil,
  Save,
  SkipForward,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import type { AnswerPayload, AnswerSource } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * AnswerCard — one generated answer with full retrieval provenance,
 * a cross-tenant client mention warning when applicable, and
 * Accept / Edit / Skip controls.
 *
 * The card encodes the project's verbose-provenance contract
 * (active memory feedback-show-provenance, LEARNING_NOTES entry
 * 12): every retrieved chunk that fed generation is rendered with
 * rank + source_file + row + score AT MINIMUM. The chunk's
 * question + answer text is one click away, never collapsed
 * behind a generic "show details" link that hides the trace.
 *
 * The cross-tenant warning (active memory
 * feedback-cross-tenant-leakage, entry 14 + 19) renders a yellow
 * warning panel BEFORE the action buttons whenever
 * `mentioned_clients` is non-empty. The user cannot miss it.
 */

export type CardStatus = "pending" | "accepted" | "edited" | "skipped";

export interface AnswerCardProps {
  payload: AnswerPayload;
  status: CardStatus;
  editedText?: string;
  isEditing: boolean;
  onAccept: () => void;
  onStartEdit: () => void;
  onCancelEdit: () => void;
  onSaveEdit: (newText: string) => void;
  onSkip: () => void;
  onUnskip: () => void;
}

export function AnswerCard({
  payload,
  status,
  editedText,
  isEditing,
  onAccept,
  onStartEdit,
  onCancelEdit,
  onSaveEdit,
  onSkip,
  onUnskip,
}: AnswerCardProps) {
  const [draft, setDraft] = useState(editedText ?? payload.answer);

  // Re-seed local draft when entering edit mode fresh.
  if (isEditing && draft !== (editedText ?? payload.answer) && draft === "") {
    setDraft(editedText ?? payload.answer);
  }

  const displayText =
    status === "edited" && editedText !== undefined ? editedText : payload.answer;

  return (
    <Card
      className={cn(
        status === "accepted" && "border-emerald-500/50 bg-emerald-50/30 dark:bg-emerald-950/10",
        status === "edited" && "border-amber-500/50 bg-amber-50/30 dark:bg-amber-950/10",
        status === "skipped" && "opacity-60",
      )}
    >
      <CardContent className="pt-6 space-y-4">
        <header className="flex items-start justify-between gap-4">
          <div className="space-y-1 flex-1 min-w-0">
            <p className="text-xs font-mono text-muted-foreground">
              Q{payload.index}
            </p>
            <h3 className="font-medium leading-snug">{payload.question}</h3>
          </div>
          <StatusBadge status={status} refused={payload.refused} />
        </header>

        {payload.mentioned_clients.length > 0 && (
          <CrossTenantWarning clients={payload.mentioned_clients} />
        )}

        <section className="space-y-1.5">
          <label className="text-xs font-medium text-muted-foreground">
            Suggested answer
          </label>
          {isEditing ? (
            <Textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              rows={Math.min(12, Math.max(4, draft.split("\n").length + 1))}
              className="font-normal"
            />
          ) : (
            <div className="whitespace-pre-wrap rounded-md border bg-muted/40 px-3 py-2 text-sm">
              {displayText || (
                <span className="text-muted-foreground italic">
                  (blank — will be empty in export)
                </span>
              )}
            </div>
          )}
        </section>

        <SourceList sources={payload.sources} confidence={payload.confidence} />

        <footer className="flex flex-wrap items-center justify-between gap-2 pt-2 border-t">
          <p className="text-xs text-muted-foreground">
            confidence{" "}
            <span className="font-mono">{payload.confidence.toFixed(2)}</span>{" "}
            ({payload.sources[0]?.score_type ?? "—"})
          </p>
          <ActionButtons
            status={status}
            isEditing={isEditing}
            onAccept={onAccept}
            onStartEdit={onStartEdit}
            onSaveEdit={() => onSaveEdit(draft)}
            onCancelEdit={() => {
              setDraft(editedText ?? payload.answer);
              onCancelEdit();
            }}
            onSkip={onSkip}
            onUnskip={onUnskip}
          />
        </footer>
      </CardContent>
    </Card>
  );
}

// ── Sub-components ────────────────────────────────────────────────────

function StatusBadge({ status, refused }: { status: CardStatus; refused: boolean }) {
  if (refused && status === "pending") {
    return <Badge variant="secondary">no corpus match</Badge>;
  }
  if (status === "accepted") {
    return (
      <Badge className="bg-emerald-600 hover:bg-emerald-600 gap-1">
        <Check className="h-3 w-3" />
        Accepted
      </Badge>
    );
  }
  if (status === "edited") {
    return (
      <Badge className="bg-amber-600 hover:bg-amber-600 gap-1">
        <Pencil className="h-3 w-3" />
        Edited
      </Badge>
    );
  }
  if (status === "skipped") {
    return <Badge variant="secondary">Skipped</Badge>;
  }
  return null;
}

function CrossTenantWarning({ clients }: { clients: string[] }) {
  return (
    <div className="flex items-start gap-3 rounded-md border-l-4 border-yellow-500 bg-yellow-50 px-3 py-2 dark:bg-yellow-950/40">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-yellow-700 dark:text-yellow-400" />
      <div className="text-xs text-yellow-900 dark:text-yellow-100">
        <p className="font-medium">
          Names past clients: {clients.join(", ")}
        </p>
        <p className="opacity-80 mt-0.5">
          Cross-tenant content can leak when the corpus mentions a different
          client by name. Review and edit before accepting.
        </p>
      </div>
    </div>
  );
}

function SourceList({
  sources,
  confidence: _confidence,
}: {
  sources: AnswerSource[];
  confidence: number;
}) {
  if (sources.length === 0) {
    return (
      <section className="text-xs text-muted-foreground italic">
        No sources retrieved.
      </section>
    );
  }
  return (
    <section className="space-y-1.5">
      <p className="text-xs font-medium text-muted-foreground">
        Sources ({sources.length})
      </p>
      <ul className="space-y-1.5">
        {sources.map((s) => (
          <SourceRow key={`${s.rank}-${s.pair_id}`} source={s} />
        ))}
      </ul>
    </section>
  );
}

function SourceRow({ source }: { source: AnswerSource }) {
  const [open, setOpen] = useState(false);
  const row = extractRowFromPairId(source.pair_id);
  return (
    <li className="rounded-md border bg-card">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-start gap-2 px-3 py-2 text-left text-xs hover:bg-muted/40"
      >
        {open ? (
          <ChevronDown className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        )}
        <div className="flex-1 min-w-0 space-y-0.5">
          <p className="font-mono truncate">
            <span className="font-semibold">[{source.rank}]</span>{" "}
            {source.source_file}{" "}
            <span className="text-muted-foreground">row {row}</span>
          </p>
          <p className="text-muted-foreground/80">
            {source.client && <span>client {source.client} · </span>}
            score{" "}
            <span className="font-mono">{source.score.toFixed(2)}</span>{" "}
            ({source.score_type})
          </p>
        </div>
      </button>
      {open && (
        <div className="border-t px-3 py-2 space-y-2 text-xs">
          <div>
            <p className="font-medium text-muted-foreground mb-0.5">
              Past Q
            </p>
            <p className="whitespace-pre-wrap">{source.question_text}</p>
          </div>
          <div>
            <p className="font-medium text-muted-foreground mb-0.5">
              Past A
            </p>
            <p className="whitespace-pre-wrap">{source.answer_text}</p>
          </div>
        </div>
      )}
    </li>
  );
}

function ActionButtons({
  status,
  isEditing,
  onAccept,
  onStartEdit,
  onSaveEdit,
  onCancelEdit,
  onSkip,
  onUnskip,
}: {
  status: CardStatus;
  isEditing: boolean;
  onAccept: () => void;
  onStartEdit: () => void;
  onSaveEdit: () => void;
  onCancelEdit: () => void;
  onSkip: () => void;
  onUnskip: () => void;
}) {
  if (isEditing) {
    return (
      <div className="flex gap-2">
        <Button size="sm" onClick={onSaveEdit}>
          <Save className="mr-1 h-3.5 w-3.5" />
          Save edit
        </Button>
        <Button size="sm" variant="outline" onClick={onCancelEdit}>
          <X className="mr-1 h-3.5 w-3.5" />
          Cancel
        </Button>
      </div>
    );
  }
  if (status === "skipped") {
    return (
      <Button size="sm" variant="outline" onClick={onUnskip}>
        Restore
      </Button>
    );
  }
  return (
    <div className="flex gap-2">
      <Button
        size="sm"
        onClick={onAccept}
        className={cn(
          status === "accepted" &&
            "bg-emerald-600 hover:bg-emerald-700",
        )}
      >
        <Check className="mr-1 h-3.5 w-3.5" />
        {status === "accepted" ? "Accepted" : "Accept"}
      </Button>
      <Button size="sm" variant="outline" onClick={onStartEdit}>
        <Pencil className="mr-1 h-3.5 w-3.5" />
        Edit
      </Button>
      <Button size="sm" variant="ghost" onClick={onSkip}>
        <SkipForward className="mr-1 h-3.5 w-3.5" />
        Skip
      </Button>
    </div>
  );
}

// ── helpers ───────────────────────────────────────────────────────────

function extractRowFromPairId(pairId: string): string {
  const m = /_row_(\d+)$/.exec(pairId);
  return m ? m[1] : pairId;
}
