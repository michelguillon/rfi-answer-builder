import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  ArrowRight,
  Database,
  FileSpreadsheet,
  Loader2,
  Trash2,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { deleteRfi, getCorpusStats, type CorpusStats } from "@/lib/api";

export default function Landing() {
  const [stats, setStats] = useState<CorpusStats | null>(null);
  const [statsError, setStatsError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setStats(null);
    setStatsError(null);
    getCorpusStats()
      .then((s) => {
        if (!cancelled) setStats(s);
      })
      .catch((err: Error) => {
        if (!cancelled) setStatsError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  return (
    <div className="space-y-10">
      <div className="text-center space-y-3">
        <h1 className="text-3xl font-semibold tracking-tight">
          RFI Answer Builder
        </h1>
        <p className="text-muted-foreground max-w-xl mx-auto">
          Draft answers to new RFI questions from your corpus of past
          responses. Two workflows: add new RFIs to the corpus, or use
          the corpus to answer a fresh client RFI.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <WorkflowCard
          icon={<Database className="h-6 w-6" />}
          title="Add RFI to corpus"
          description="Upload a past RFI Excel. We profile it for column structure, you approve the mapping, and we ingest it into the searchable corpus."
          href="/ingest"
          ctaLabel="Get started"
        />
        <WorkflowCard
          icon={<FileSpreadsheet className="h-6 w-6" />}
          title="Answer a new RFI"
          description="Upload a new client RFI. We retrieve relevant past Q&A for each question, draft an answer, and let you review with full source visibility before exporting."
          href="/answer"
          ctaLabel="Get started"
        />
      </div>

      <CorpusPanel
        stats={stats}
        error={statsError}
        onReload={() => setReloadKey((k) => k + 1)}
      />
    </div>
  );
}

function WorkflowCard({
  icon,
  title,
  description,
  href,
  ctaLabel,
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
  href: string;
  ctaLabel: string;
}) {
  return (
    <Card className="flex flex-col">
      <CardHeader>
        <div className="flex items-center gap-3">
          <div className="rounded-md bg-secondary p-2 text-secondary-foreground">
            {icon}
          </div>
          <CardTitle className="text-xl">{title}</CardTitle>
        </div>
        <CardDescription className="pt-2">{description}</CardDescription>
      </CardHeader>
      <CardContent className="mt-auto pt-4">
        <Button asChild className="w-full sm:w-auto">
          <Link to={href}>
            {ctaLabel}
            <ArrowRight className="ml-1 h-4 w-4" />
          </Link>
        </Button>
      </CardContent>
    </Card>
  );
}

function CorpusPanel({
  stats,
  error,
  onReload,
}: {
  stats: CorpusStats | null;
  error: string | null;
  onReload: () => void;
}) {
  if (error) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Corpus</CardTitle>
          <CardDescription>
            Corpus stats unavailable — likely no RFIs ingested yet.{" "}
            <Link to="/ingest" className="underline hover:text-foreground">
              Add your first RFI
            </Link>
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }
  if (!stats) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Corpus</CardTitle>
          <CardDescription>Loading corpus stats…</CardDescription>
        </CardHeader>
      </Card>
    );
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Corpus</CardTitle>
        <CardDescription>
          <strong className="text-foreground">
            {stats.total_pairs.toLocaleString()}
          </strong>{" "}
          Q&amp;A pairs across{" "}
          <strong className="text-foreground">{stats.source_files}</strong>{" "}
          source RFI{stats.source_files === 1 ? "" : "s"}.
        </CardDescription>
      </CardHeader>
      {stats.files.length > 0 && (
        <CardContent>
          <CorpusTable files={stats.files} onChanged={onReload} />
        </CardContent>
      )}
    </Card>
  );
}

function CorpusTable({
  files,
  onChanged,
}: {
  files: CorpusStats["files"];
  onChanged: () => void;
}) {
  const [confirming, setConfirming] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const onConfirmDelete = async () => {
    if (!confirming) return;
    setDeleting(confirming);
    setDeleteError(null);
    try {
      await deleteRfi(confirming);
      setConfirming(null);
      onChanged();
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : String(err));
    } finally {
      setDeleting(null);
    }
  };

  return (
    <>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Source RFI</TableHead>
            <TableHead className="w-24 text-right">Q&amp;A pairs</TableHead>
            <TableHead className="w-12" />
          </TableRow>
        </TableHeader>
        <TableBody>
          {files.map((f) => (
            <TableRow key={f.source_file}>
              <TableCell
                className="font-mono text-xs truncate max-w-md"
                title={f.source_file}
              >
                {f.source_file}
              </TableCell>
              <TableCell className="text-right font-mono text-xs">
                {f.chunks.toLocaleString()}
              </TableCell>
              <TableCell className="text-right">
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setDeleteError(null);
                    setConfirming(f.source_file);
                  }}
                  aria-label={`Delete ${f.source_file}`}
                >
                  <Trash2 className="h-4 w-4 text-destructive" />
                </Button>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>

      <Dialog
        open={confirming !== null}
        onOpenChange={(open) => !open && setConfirming(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Remove from corpus?</DialogTitle>
            <DialogDescription>
              <span className="font-mono text-xs block mt-1 mb-3 truncate">
                {confirming}
              </span>
              This removes the RFI's chunks from all four ChromaDB
              collections and deletes its{" "}
              <span className="font-mono">config_rfi_*.json</span>. The
              uploaded <span className="font-mono">.xlsx</span> stays in{" "}
              <span className="font-mono">data/</span> in case you want to
              re-upload later. Cannot be undone.
            </DialogDescription>
          </DialogHeader>
          {deleteError && (
            <p className="text-sm text-destructive">{deleteError}</p>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setConfirming(null)}
              disabled={deleting !== null}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={onConfirmDelete}
              disabled={deleting !== null}
            >
              {deleting ? (
                <>
                  <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                  Deleting…
                </>
              ) : (
                <>
                  <Trash2 className="mr-1 h-4 w-4" />
                  Delete
                </>
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
