import { useEffect, useMemo, useState } from "react";
import { useDropzone } from "react-dropzone";
import { ArrowRight, Loader2, Upload } from "lucide-react";

import { AnswerCard, type CardStatus } from "@/components/AnswerCard";
import { ExportButton } from "@/components/ExportButton";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  createSession,
  uploadAnswer,
  type AnswerEvent,
  type AnswerPayload,
  type AnswerUploadResponse,
} from "@/lib/api";
import { useSSE } from "@/lib/sse";
import { cn } from "@/lib/utils";

const SESSION_KEY = "rfi.answer.session_id";

type WizardStep = "upload" | "processing";

// Per-card UI state. The backend cares only about overrides + skipped;
// 'pending' and 'accepted' both fall through to "write the generated
// answer" on the server side. Tracking 'pending' explicitly lets the
// summary table show which cards the user hasn't reviewed yet.
type CardState = {
  status: CardStatus;
  editedText?: string;
};

export default function Answer() {
  const [step, setStep] = useState<WizardStep>("upload");

  // Step 1 state
  const [pickedFile, setPickedFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [session, setSession] = useState<AnswerUploadResponse | null>(null);

  // Step 2 SSE
  const answerSSE = useSSE<AnswerEvent>();

  // Per-card status keyed by question index
  const [cards, setCards] = useState<Record<number, CardState>>({});
  const [editingIndex, setEditingIndex] = useState<number | null>(null);

  // ── Mount: ensure clean slate ────────────────────────────────────────
  useEffect(() => {
    localStorage.removeItem(SESSION_KEY);
  }, []);

  // ── Derived state from the SSE stream ────────────────────────────────
  const answers = useMemo<AnswerPayload[]>(
    () =>
      answerSSE.events
        .filter((e): e is Extract<AnswerEvent, { type: "answer" }> =>
          e.type === "answer",
        )
        .map((e) => e.data),
    [answerSSE.events],
  );

  const latestProgress = useMemo(() => {
    // Walk backwards for the most recent progress event.
    for (let i = answerSSE.events.length - 1; i >= 0; i--) {
      const e = answerSSE.events[i];
      if (e.type === "progress") return e.data;
    }
    return null;
  }, [answerSSE.events]);

  const doneEvent = useMemo(
    () => answerSSE.events.find((e) => e.type === "done"),
    [answerSSE.events],
  );

  // Seed a 'pending' state for each new answer that arrives.
  useEffect(() => {
    setCards((prev) => {
      const next = { ...prev };
      for (const a of answers) {
        if (next[a.index] === undefined) {
          next[a.index] = { status: "pending" };
        }
      }
      return next;
    });
  }, [answers]);

  // ── Step 1 actions ───────────────────────────────────────────────────
  const onDrop = (files: File[]) => {
    setUploadError(null);
    if (files[0]) setPickedFile(files[0]);
  };

  const dropzone = useDropzone({
    onDrop,
    multiple: false,
    accept: {
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": [".xlsx"],
      "application/vnd.ms-excel.sheet.macroEnabled.12": [".xlsm"],
    },
  });

  const onStartAnswering = async () => {
    if (!pickedFile) return;
    setUploading(true);
    setUploadError(null);
    try {
      const { session_id } = await createSession();
      const uploadResp = await uploadAnswer(session_id, pickedFile);
      localStorage.setItem(SESSION_KEY, session_id);
      setSession(uploadResp);
      setStep("processing");
      answerSSE.start({
        url: `/api/answer/process?session_id=${encodeURIComponent(session_id)}`,
      });
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : String(err));
    } finally {
      setUploading(false);
    }
  };

  const onStartOver = () => {
    answerSSE.reset();
    setSession(null);
    setPickedFile(null);
    setCards({});
    setEditingIndex(null);
    localStorage.removeItem(SESSION_KEY);
    setStep("upload");
  };

  // ── Per-card transitions ─────────────────────────────────────────────
  const setCard = (index: number, patch: Partial<CardState>) =>
    setCards((prev) => ({
      ...prev,
      [index]: { ...(prev[index] ?? { status: "pending" }), ...patch },
    }));

  const onAccept = (index: number) =>
    setCard(index, { status: "accepted", editedText: undefined });
  const onSkip = (index: number) =>
    setCard(index, { status: "skipped", editedText: undefined });
  const onUnskip = (index: number) =>
    setCard(index, { status: "pending", editedText: undefined });
  const onStartEdit = (index: number) => setEditingIndex(index);
  const onCancelEdit = () => setEditingIndex(null);
  const onSaveEdit = (index: number, text: string) => {
    setCard(index, { status: "edited", editedText: text });
    setEditingIndex(null);
  };

  // ── Export bookkeeping ───────────────────────────────────────────────
  const overrides = useMemo(() => {
    const out: Record<number, string> = {};
    for (const [idx, st] of Object.entries(cards)) {
      if (st.status === "edited" && st.editedText !== undefined) {
        out[Number(idx)] = st.editedText;
      }
    }
    return out;
  }, [cards]);

  const skipped = useMemo(
    () =>
      Object.entries(cards)
        .filter(([, st]) => st.status === "skipped")
        .map(([idx]) => Number(idx)),
    [cards],
  );

  const summary = useMemo(() => {
    const total = answers.length;
    let accepted = 0,
      edited = 0,
      skippedCount = 0,
      pending = 0;
    for (const a of answers) {
      const st = cards[a.index]?.status ?? "pending";
      if (st === "accepted") accepted++;
      else if (st === "edited") edited++;
      else if (st === "skipped") skippedCount++;
      else pending++;
    }
    return { total, accepted, edited, skipped: skippedCount, pending };
  }, [answers, cards]);

  // ── Render ───────────────────────────────────────────────────────────
  if (step === "upload") {
    return (
      <div className="max-w-3xl mx-auto">
        <UploadStep
          dropzone={dropzone}
          pickedFile={pickedFile}
          uploadError={uploadError}
          uploading={uploading}
          onStartAnswering={onStartAnswering}
        />
      </div>
    );
  }

  // step === "processing"
  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <ProcessingHeader
        session={session}
        latestProgress={latestProgress}
        total={session?.question_count ?? 0}
        sseError={answerSSE.status === "error" ? answerSSE.error : null}
        isSlowLoad={answerSSE.isSlowLoad}
        onStartOver={onStartOver}
      />

      {answers.map((a) => {
        const state = cards[a.index] ?? { status: "pending" as CardStatus };
        return (
          <AnswerCard
            key={a.index}
            payload={a}
            status={state.status}
            editedText={state.editedText}
            isEditing={editingIndex === a.index}
            onAccept={() => onAccept(a.index)}
            onStartEdit={() => onStartEdit(a.index)}
            onCancelEdit={onCancelEdit}
            onSaveEdit={(text) => onSaveEdit(a.index, text)}
            onSkip={() => onSkip(a.index)}
            onUnskip={() => onUnskip(a.index)}
          />
        );
      })}

      {doneEvent && session && (
        <ReviewSection
          answers={answers}
          cards={cards}
          summary={summary}
          sessionId={session.session_id}
          overrides={overrides}
          skipped={skipped}
          onStartOver={onStartOver}
        />
      )}
    </div>
  );
}

// ── Sub-components ────────────────────────────────────────────────────

function UploadStep({
  dropzone,
  pickedFile,
  uploadError,
  uploading,
  onStartAnswering,
}: {
  dropzone: ReturnType<typeof useDropzone>;
  pickedFile: File | null;
  uploadError: string | null;
  uploading: boolean;
  onStartAnswering: () => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Answer a new RFI</CardTitle>
        <CardDescription>
          Drop a new client RFI (.xlsx / .xlsm). The backend extracts the
          questions, then generates an answer for each from the corpus —
          you review with full source visibility before exporting.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div
          {...dropzone.getRootProps()}
          className={cn(
            "border-2 border-dashed rounded-lg p-10 text-center cursor-pointer transition-colors",
            dropzone.isDragActive
              ? "border-primary bg-primary/5"
              : "border-input hover:border-foreground/40",
          )}
        >
          <input {...dropzone.getInputProps()} />
          <Upload className="h-8 w-8 mx-auto mb-3 text-muted-foreground" />
          {pickedFile ? (
            <div className="space-y-1">
              <p className="font-medium">{pickedFile.name}</p>
              <p className="text-xs text-muted-foreground">
                {(pickedFile.size / 1024).toFixed(1)} KB · click or drop to
                replace
              </p>
            </div>
          ) : dropzone.isDragActive ? (
            <p className="text-sm">Drop the file here…</p>
          ) : (
            <p className="text-sm text-muted-foreground">
              Drag &amp; drop, or click to browse. .xlsx / .xlsm only.
            </p>
          )}
        </div>

        {uploadError && (
          <p className="text-sm text-destructive">{uploadError}</p>
        )}

        <div className="flex justify-end">
          <Button onClick={onStartAnswering} disabled={!pickedFile || uploading}>
            {uploading ? (
              <>
                <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                Extracting questions…
              </>
            ) : (
              <>
                Start answering
                <ArrowRight className="ml-1 h-4 w-4" />
              </>
            )}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function ProcessingHeader({
  session,
  latestProgress,
  total,
  sseError,
  isSlowLoad,
  onStartOver,
}: {
  session: AnswerUploadResponse | null;
  latestProgress: { index: number; total: number; question_text: string } | null;
  total: number;
  sseError: string | null;
  isSlowLoad: boolean;
  onStartOver: () => void;
}) {
  const current = latestProgress?.index ?? 0;
  const pct = total > 0 ? Math.round((current / total) * 100) : 0;
  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1 min-w-0">
            <CardTitle>Generating answers</CardTitle>
            <CardDescription>
              {session ? (
                <>
                  <span className="font-mono">{session.filename}</span> ·{" "}
                  {session.question_count} questions from column{" "}
                  <span className="font-mono">{session.question_column}</span> (
                  {session.question_column_header || "—"})
                </>
              ) : (
                "—"
              )}
            </CardDescription>
          </div>
          <Button variant="ghost" size="sm" onClick={onStartOver}>
            Start over
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        <div className="flex items-center justify-between text-sm">
          <span className="text-muted-foreground">
            {current === total && total > 0 ? (
              <>Done. {total} questions processed.</>
            ) : (
              <>
                Question {current}/{total}
                {latestProgress?.question_text && (
                  <span className="ml-2 text-muted-foreground/80 truncate">
                    — {latestProgress.question_text}
                  </span>
                )}
              </>
            )}
          </span>
          <span className="font-mono text-xs text-muted-foreground">
            {pct}%
          </span>
        </div>
        <Progress value={pct} />
        {isSlowLoad && (
          <p className="text-sm text-muted-foreground pt-2">
            Searching knowledge base… First query may take a few seconds
            while the system initialises.
          </p>
        )}
        {sseError && (
          <p className="text-sm text-destructive pt-2">
            Stream error: {sseError}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function ReviewSection({
  answers,
  cards,
  summary,
  sessionId,
  overrides,
  skipped,
  onStartOver,
}: {
  answers: AnswerPayload[];
  cards: Record<number, CardState>;
  summary: {
    total: number;
    accepted: number;
    edited: number;
    skipped: number;
    pending: number;
  };
  sessionId: string;
  overrides: Record<number, string>;
  skipped: number[];
  onStartOver: () => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Review &amp; export</CardTitle>
        <CardDescription>
          {summary.total} questions ·{" "}
          <span className="text-foreground">
            {summary.accepted} accepted
          </span>
          {" · "}
          <span className="text-foreground">{summary.edited} edited</span>
          {" · "}
          <span className="text-foreground">{summary.skipped} skipped</span>
          {summary.pending > 0 && (
            <>
              {" · "}
              <span className="text-amber-700 dark:text-amber-400">
                {summary.pending} still pending review
              </span>
            </>
          )}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-12">Q#</TableHead>
              <TableHead>Question</TableHead>
              <TableHead className="w-32">Status</TableHead>
              <TableHead className="w-24">Confidence</TableHead>
              <TableHead className="w-32">Flags</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {answers.map((a) => {
              const st = cards[a.index]?.status ?? "pending";
              return (
                <TableRow key={a.index}>
                  <TableCell className="font-mono text-xs">
                    Q{a.index}
                  </TableCell>
                  <TableCell className="truncate max-w-md" title={a.question}>
                    {a.question}
                  </TableCell>
                  <TableCell>
                    <ReviewStatusBadge status={st} refused={a.refused} />
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {a.confidence.toFixed(2)}
                  </TableCell>
                  <TableCell>
                    {a.mentioned_clients.length > 0 && (
                      <Badge
                        variant="outline"
                        className="border-yellow-500 text-yellow-700 dark:text-yellow-400"
                      >
                        cross-tenant
                      </Badge>
                    )}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>

        <div className="flex items-center justify-between gap-3 pt-2 border-t">
          <Button variant="outline" onClick={onStartOver}>
            Answer another RFI
          </Button>
          <ExportButton
            sessionId={sessionId}
            overrides={overrides}
            skipped={skipped}
          />
        </div>
      </CardContent>
    </Card>
  );
}

function ReviewStatusBadge({
  status,
  refused,
}: {
  status: CardStatus;
  refused: boolean;
}) {
  if (status === "accepted") {
    return (
      <Badge className="bg-emerald-600 hover:bg-emerald-600">Accepted</Badge>
    );
  }
  if (status === "edited") {
    return <Badge className="bg-amber-600 hover:bg-amber-600">Edited</Badge>;
  }
  if (status === "skipped") {
    return <Badge variant="secondary">Skipped</Badge>;
  }
  if (refused) {
    return <Badge variant="outline">Refused</Badge>;
  }
  return <Badge variant="outline">Pending</Badge>;
}
