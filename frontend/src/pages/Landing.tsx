import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, FileSpreadsheet, Database } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { getCorpusStats, type CorpusStats } from "@/lib/api";

export default function Landing() {
  const [stats, setStats] = useState<CorpusStats | null>(null);
  const [statsError, setStatsError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
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
  }, []);

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

      <StatsFooter stats={stats} error={statsError} />
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

function StatsFooter({
  stats,
  error,
}: {
  stats: CorpusStats | null;
  error: string | null;
}) {
  // Empty-corpus and pre-load states both render quietly — the
  // Landing page is the entry point and shouldn't push errors at
  // the user if the corpus simply isn't ingested yet.
  if (error) {
    return (
      <div className="text-center text-sm text-muted-foreground border-t pt-6">
        Corpus stats unavailable — likely no RFIs ingested yet.{" "}
        <Link to="/ingest" className="underline hover:text-foreground">
          Add your first RFI
        </Link>
      </div>
    );
  }
  if (!stats) {
    return (
      <div className="text-center text-sm text-muted-foreground border-t pt-6">
        Loading corpus stats…
      </div>
    );
  }
  return (
    <div className="border-t pt-6 text-center">
      <p className="text-sm text-muted-foreground">
        Corpus: <strong className="text-foreground">{stats.total_pairs.toLocaleString()}</strong>{" "}
        Q&amp;A pairs across{" "}
        <strong className="text-foreground">{stats.source_files}</strong>{" "}
        source RFI{stats.source_files === 1 ? "" : "s"}
      </p>
      {stats.files.length > 0 && (
        <p
          className="text-xs text-muted-foreground/80 mt-2 max-w-2xl mx-auto truncate"
          title={stats.files.join("\n")}
        >
          {stats.files.join(" · ")}
        </p>
      )}
    </div>
  );
}
