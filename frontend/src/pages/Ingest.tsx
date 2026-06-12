import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useDropzone } from "react-dropzone";
import { ArrowRight, Check, Loader2, Upload } from "lucide-react";

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
import { ProposalCard } from "@/components/ProposalCard";
import { StepTimeline } from "@/components/StepTimeline";
import {
  createSession,
  uploadIngest,
  type IngestEvent,
  type IngestUploadResponse,
  type ProfileEvent,
  type ProfileProposal,
} from "@/lib/api";
import { useSSE } from "@/lib/sse";
import { cn } from "@/lib/utils";

const SESSION_KEY = "rfi.ingest.session_id";

// ARCHITECTURAL DECISION: page-local wizard state via useState, not
// a state-machine library. Three states (1=upload, 2=profile,
// 3=ingest), four transitions (upload→profile, approve→ingest,
// reject→upload, finish→upload). A reducer would be overkill;
// useState reads top-to-bottom and the transitions are colocated
// with the buttons that trigger them.
type WizardStep = "upload" | "profile" | "ingest";

export default function Ingest() {
  const [step, setStep] = useState<WizardStep>("upload");

  // Step 1 state
  const [pickedFile, setPickedFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  // Carried across steps
  const [session, setSession] = useState<IngestUploadResponse | null>(null);

  // Step 2 state — edits to inferred client/date
  const [clientEdit, setClientEdit] = useState("");
  const [dateEdit, setDateEdit] = useState("");

  // Step 2 SSE
  const profileSSE = useSSE<ProfileEvent>();
  // Step 3 SSE
  const ingestSSE = useSSE<IngestEvent>();

  const navigate = useNavigate();

  // Clear any stale session on mount — fresh visits start at Step 1.
  // (Resuming a partial wizard from localStorage is out of scope; the
  // session_id is stored only while the wizard is active so the
  // approve POST and the export-side observers see a stable id.)
  useEffect(() => {
    localStorage.removeItem(SESSION_KEY);
  }, []);

  // Extract the proposal from the profile event stream.
  const proposal = useMemo<ProfileProposal | null>(() => {
    const ev = profileSSE.events.find((e) => e.type === "proposal");
    return ev?.type === "proposal" ? ev.data : null;
  }, [profileSSE.events]);

  // Seed the edit fields the first time a proposal lands.
  useEffect(() => {
    if (proposal) {
      setClientEdit(proposal.client ?? "");
      setDateEdit(proposal.date ?? "");
    }
  }, [proposal]);

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

  const onAnalyse = async () => {
    if (!pickedFile) return;
    setUploading(true);
    setUploadError(null);
    try {
      const { session_id } = await createSession();
      const uploadResp = await uploadIngest(session_id, pickedFile);
      localStorage.setItem(SESSION_KEY, session_id);
      setSession(uploadResp);
      setStep("profile");
      profileSSE.start({
        url: `/api/ingest/profile?session_id=${encodeURIComponent(session_id)}`,
      });
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : String(err));
    } finally {
      setUploading(false);
    }
  };

  // ── Step 2 actions ───────────────────────────────────────────────────
  const onApprove = () => {
    if (!session) return;
    setStep("ingest");
    ingestSSE.start({
      url: "/api/ingest/approve",
      method: "POST",
      body: {
        session_id: session.session_id,
        client: clientEdit.trim() || null,
        date: dateEdit.trim() || null,
      },
    });
  };

  const onReject = () => {
    profileSSE.reset();
    setSession(null);
    setPickedFile(null);
    setClientEdit("");
    setDateEdit("");
    localStorage.removeItem(SESSION_KEY);
    setStep("upload");
  };

  // ── Step 3 derived state ─────────────────────────────────────────────
  type PerCollection = {
    batch: number;
    total: number;
    complete: boolean;
    chunks: number;
    note?: string;
  };

  const perCollection = useMemo(() => {
    const map = new Map<string, PerCollection>();
    for (const e of ingestSSE.events) {
      if (e.type === "collection") {
        if (!map.has(e.data)) {
          map.set(e.data, { batch: 0, total: 0, complete: false, chunks: 0 });
        }
      } else if (e.type === "progress") {
        const cur = map.get(e.data.collection) ?? {
          batch: 0,
          total: 0,
          complete: false,
          chunks: 0,
        };
        map.set(e.data.collection, {
          ...cur,
          batch: e.data.batch,
          total: e.data.total,
        });
      } else if (e.type === "complete") {
        const cur = map.get(e.data.collection) ?? {
          batch: 0,
          total: 0,
          complete: true,
          chunks: 0,
        };
        map.set(e.data.collection, {
          ...cur,
          complete: true,
          chunks: e.data.chunks,
          note: e.data.note,
        });
      }
    }
    return map;
  }, [ingestSSE.events]);

  const doneEvent = useMemo(
    () => ingestSSE.events.find((e) => e.type === "done"),
    [ingestSSE.events],
  );

  const onAddAnother = () => {
    profileSSE.reset();
    ingestSSE.reset();
    setSession(null);
    setPickedFile(null);
    setClientEdit("");
    setDateEdit("");
    localStorage.removeItem(SESSION_KEY);
    setStep("upload");
  };

  const onGoToAnswer = () => {
    profileSSE.reset();
    ingestSSE.reset();
    localStorage.removeItem(SESSION_KEY);
    navigate("/answer");
  };

  // ── Render ───────────────────────────────────────────────────────────
  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <StepIndicator current={step} />

      {step === "upload" && (
        <Card>
          <CardHeader>
            <CardTitle>Step 1 — Upload</CardTitle>
            <CardDescription>
              Drop a past RFI Excel (.xlsx / .xlsm) to add it to the corpus.
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
              <Button onClick={onAnalyse} disabled={!pickedFile || uploading}>
                {uploading ? (
                  <>
                    <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                    Uploading…
                  </>
                ) : (
                  <>
                    Analyse
                    <ArrowRight className="ml-1 h-4 w-4" />
                  </>
                )}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {step === "profile" && (
        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>Step 2 — Profile</CardTitle>
              <CardDescription>
                Profiling{" "}
                <span className="font-mono">{session?.filename}</span> (
                {session?.detected_rows.toLocaleString()} rows). The model
                proposes a column→role mapping; review the inferred
                client/date below before approving.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <StepTimeline
                events={profileSSE.events as { type: string; data?: unknown }[]}
                pending={profileSSE.status === "open" && !proposal}
                pendingLabel="Awaiting LLM mapping…"
              />
            </CardContent>
          </Card>

          {proposal && (
            <ProposalCard
              proposal={proposal}
              client={clientEdit}
              date={dateEdit}
              onClientChange={setClientEdit}
              onDateChange={setDateEdit}
              onApprove={onApprove}
              onReject={onReject}
              busy={profileSSE.status === "open"}
            />
          )}

          {profileSSE.status === "error" && profileSSE.error && (
            <Card>
              <CardContent className="pt-6 text-sm text-destructive">
                Profile stream failed: {profileSSE.error}
                <div className="mt-3">
                  <Button variant="outline" onClick={onReject}>
                    Start over
                  </Button>
                </div>
              </CardContent>
            </Card>
          )}
        </div>
      )}

      {step === "ingest" && (
        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>Step 3 — Ingest</CardTitle>
              <CardDescription>
                Embedding chunks into all four ChromaDB collections.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              <IngestProgressList perCollection={perCollection} />
              {ingestSSE.isSlowLoad && (
                <p className="text-sm text-muted-foreground">
                  Searching knowledge base… First query may take a few
                  seconds while the system initialises.
                </p>
              )}
            </CardContent>
          </Card>

          {doneEvent?.type === "done" && (
            <Card>
              <CardContent className="pt-6 space-y-4">
                <div className="flex items-start gap-3">
                  <Check className="mt-0.5 h-5 w-5 text-emerald-600 shrink-0" />
                  <div className="space-y-1">
                    <p className="font-medium">Ingest complete.</p>
                    <p className="text-sm text-muted-foreground">
                      {doneEvent.data.total_chunks.toLocaleString()} new
                      chunk{doneEvent.data.total_chunks === 1 ? "" : "s"}{" "}
                      embedded · corpus now holds{" "}
                      {doneEvent.data.corpus_size.toLocaleString()} chunks
                      across all four collections.
                    </p>
                  </div>
                </div>
                <div className="flex gap-3">
                  <Button variant="outline" onClick={onAddAnother}>
                    Add another RFI
                  </Button>
                  <Button onClick={onGoToAnswer}>
                    Go to answer
                    <ArrowRight className="ml-1 h-4 w-4" />
                  </Button>
                </div>
              </CardContent>
            </Card>
          )}

          {ingestSSE.status === "error" && ingestSSE.error && (
            <Card>
              <CardContent className="pt-6 text-sm text-destructive">
                Ingest stream failed: {ingestSSE.error}
                <div className="mt-3">
                  <Button variant="outline" onClick={onAddAnother}>
                    Start over
                  </Button>
                </div>
              </CardContent>
            </Card>
          )}
        </div>
      )}
    </div>
  );
}

// ── Sub-components ────────────────────────────────────────────────────

function StepIndicator({ current }: { current: WizardStep }) {
  const items: { id: WizardStep; label: string }[] = [
    { id: "upload", label: "Upload" },
    { id: "profile", label: "Profile" },
    { id: "ingest", label: "Ingest" },
  ];
  const currentIndex = items.findIndex((i) => i.id === current);
  return (
    <ol className="flex items-center gap-2 text-sm text-muted-foreground">
      {items.map((item, i) => {
        const reached = i <= currentIndex;
        const isCurrent = i === currentIndex;
        return (
          <li key={item.id} className="flex items-center gap-2">
            <span
              className={cn(
                "flex h-6 w-6 items-center justify-center rounded-full border text-xs font-medium",
                reached
                  ? "border-primary bg-primary text-primary-foreground"
                  : "border-input",
              )}
            >
              {i + 1}
            </span>
            <span
              className={cn(
                isCurrent ? "text-foreground font-medium" : undefined,
                reached ? undefined : "text-muted-foreground/60",
              )}
            >
              {item.label}
            </span>
            {i < items.length - 1 && (
              <span className="text-muted-foreground/40">·</span>
            )}
          </li>
        );
      })}
    </ol>
  );
}

interface PerCollection {
  batch: number;
  total: number;
  complete: boolean;
  chunks: number;
  note?: string;
}

function IngestProgressList({
  perCollection,
}: {
  perCollection: Map<string, PerCollection>;
}) {
  // Fixed order matches the four collections in pipeline.ingest.COLLECTIONS.
  // Listed unconditionally so the layout doesn't reflow as events arrive;
  // collections not yet touched show as "waiting".
  const collections = [
    "rfi_combined_cosine",
    "rfi_combined_l2",
    "rfi_separated_cosine",
    "rfi_separated_l2",
  ];

  return (
    <div className="space-y-4">
      {collections.map((name) => {
        const state = perCollection.get(name);
        const waiting = !state;
        const pct =
          state && state.total > 0
            ? Math.round((state.batch / state.total) * 100)
            : state?.complete
              ? 100
              : 0;
        return (
          <div key={name} className="space-y-1.5">
            <div className="flex items-center justify-between text-sm">
              <span className="font-mono">{name}</span>
              <CollectionStatus state={state} waiting={waiting} />
            </div>
            <Progress value={pct} />
          </div>
        );
      })}
    </div>
  );
}

function CollectionStatus({
  state,
  waiting,
}: {
  state?: PerCollection;
  waiting: boolean;
}) {
  if (waiting) {
    return (
      <span className="text-xs text-muted-foreground">waiting…</span>
    );
  }
  if (state!.complete) {
    if (state!.note) {
      return <Badge variant="secondary">{state!.note}</Badge>;
    }
    return (
      <Badge variant="default" className="gap-1">
        <Check className="h-3 w-3" />
        {state!.chunks} chunk{state!.chunks === 1 ? "" : "s"}
      </Badge>
    );
  }
  if (state!.total > 0) {
    return (
      <span className="text-xs text-muted-foreground">
        embedding batch {state!.batch}/{state!.total}
      </span>
    );
  }
  return (
    <span className="text-xs text-muted-foreground inline-flex items-center gap-1">
      <Loader2 className="h-3 w-3 animate-spin" />
      starting…
    </span>
  );
}

