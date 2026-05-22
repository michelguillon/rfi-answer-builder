import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import type { ProfileProposal } from "@/lib/api";

/**
 * ProposalCard — renders the profiler's column→role mapping with
 * editable client/date fields and Approve/Reject controls.
 *
 * Column roles are NOT editable here (see api/CLAUDE.md /
 * LEARNING_NOTES entry 18). If the LLM mis-classified a column,
 * the user rejects the proposal and re-profiles. Editing roles
 * inline would risk violating the validator's invariants
 * (exactly one question + exactly one answer column) without a
 * re-validation gate.
 *
 * Client and date ARE editable because the LLM frequently leaves
 * them null when it can't infer them from the filename/sheet —
 * the human commonly knows them. The two inputs are controlled
 * by the parent; this component is presentational.
 */

export interface ProposalCardProps {
  proposal: ProfileProposal;
  client: string;
  date: string;
  onClientChange: (value: string) => void;
  onDateChange: (value: string) => void;
  onApprove: () => void;
  onReject: () => void;
  busy?: boolean;
}

export function ProposalCard({
  proposal,
  client,
  date,
  onClientChange,
  onDateChange,
  onApprove,
  onReject,
  busy = false,
}: ProposalCardProps) {
  const headerLookup = Object.fromEntries(
    proposal.columns.map((c) => [c.letter, c.header]),
  );
  const sortedLetters = Object.keys(proposal.column_roles).sort((a, b) =>
    a.length !== b.length ? a.length - b.length : a.localeCompare(b),
  );
  // Collapse runs of consecutive `ignore` columns ("E", "F", "G", ...) into
  // a single "E-G ignored (3)" row. RFI files frequently have long tails of
  // empty / decorative columns that the heuristic marks ignored; listing
  // each one as its own row drowns out the load-bearing question/answer
  // assignments above. The condense keeps the user's attention on the
  // mapping decisions that matter.
  const mappingRows = collapseIgnoredRuns(
    sortedLetters,
    proposal.column_roles,
    headerLookup,
  );

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-xl">Proposed mapping</CardTitle>
        <CardDescription>
          Sheet <span className="font-mono">{proposal.sheet}</span>, header
          row {proposal.header_row}. Review the column assignments and the
          inferred client/date below before approving.
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-6">
        <div>
          <h3 className="text-sm font-medium mb-2">Column roles</h3>
          <ul className="space-y-1 text-sm">
            {mappingRows.map((row) => {
              if (row.kind === "ignored_range") {
                return (
                  <li
                    key={`ignored:${row.start}:${row.end}`}
                    className="flex items-center gap-3 font-mono text-xs text-muted-foreground"
                  >
                    <span className="w-16 shrink-0 font-semibold">
                      {row.start}–{row.end}
                    </span>
                    <span className="flex-1 truncate italic">
                      {row.count} columns
                    </span>
                    <Badge variant="secondary">ignored</Badge>
                  </li>
                );
              }
              const isReserved = [
                "question",
                "answer",
                "context",
                "ignore",
              ].includes(row.role);
              return (
                <li
                  key={row.letter}
                  className="flex items-center gap-3 font-mono text-xs"
                >
                  <span className="w-16 shrink-0 font-semibold text-muted-foreground">
                    {row.letter}
                  </span>
                  <span
                    className="flex-1 truncate text-foreground"
                    title={row.header}
                  >
                    {row.header || "—"}
                  </span>
                  <Badge variant={isReserved ? "default" : "secondary"}>
                    {row.role}
                  </Badge>
                </li>
              );
            })}
          </ul>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <label className="space-y-1.5">
            <span className="text-sm font-medium">
              Client{" "}
              <span className="text-xs text-muted-foreground font-normal">
                (inferred: {proposal.client ?? "—"})
              </span>
            </span>
            <Input
              type="text"
              value={client}
              onChange={(e) => onClientChange(e.target.value)}
              placeholder="e.g. Publicis"
              disabled={busy}
            />
          </label>
          <label className="space-y-1.5">
            <span className="text-sm font-medium">
              Date{" "}
              <span className="text-xs text-muted-foreground font-normal">
                (inferred: {proposal.date ?? "—"})
              </span>
            </span>
            <Input
              type="text"
              value={date}
              onChange={(e) => onDateChange(e.target.value)}
              placeholder="e.g. 2024"
              disabled={busy}
            />
          </label>
        </div>

        {proposal.reasoning && (
          <div className="text-xs text-muted-foreground border-l-2 pl-3">
            <span className="font-medium">Model reasoning: </span>
            {proposal.reasoning}
          </div>
        )}
      </CardContent>

      <CardFooter className="gap-3">
        <Button onClick={onApprove} disabled={busy}>
          Approve &amp; ingest
        </Button>
        <Button variant="outline" onClick={onReject} disabled={busy}>
          Reject &amp; re-profile
        </Button>
      </CardFooter>
    </Card>
  );
}

// ── helpers ───────────────────────────────────────────────────────────

type MappingRow =
  | { kind: "single"; letter: string; header: string; role: string }
  | { kind: "ignored_range"; start: string; end: string; count: number };

function letterToColIndex(letter: string): number {
  let n = 0;
  for (const ch of letter.toUpperCase()) {
    n = n * 26 + (ch.charCodeAt(0) - "A".charCodeAt(0) + 1);
  }
  return n;
}

function collapseIgnoredRuns(
  sortedLetters: string[],
  roles: Record<string, string>,
  headerLookup: Record<string, string>,
): MappingRow[] {
  const out: MappingRow[] = [];
  let i = 0;
  while (i < sortedLetters.length) {
    const letter = sortedLetters[i];
    const role = roles[letter];
    if (role === "ignore") {
      // Look ahead for a run of consecutive ignored columns. "Consecutive"
      // means their Excel column indexes differ by exactly 1 (so "E F G"
      // collapses but "E F H" splits into "E-F" + "H").
      let j = i;
      while (j + 1 < sortedLetters.length) {
        const next = sortedLetters[j + 1];
        if (roles[next] !== "ignore") break;
        if (letterToColIndex(next) !== letterToColIndex(sortedLetters[j]) + 1)
          break;
        j++;
      }
      if (j === i) {
        out.push({
          kind: "single",
          letter,
          header: headerLookup[letter] ?? "",
          role: "ignore",
        });
      } else {
        out.push({
          kind: "ignored_range",
          start: sortedLetters[i],
          end: sortedLetters[j],
          count: j - i + 1,
        });
      }
      i = j + 1;
    } else {
      out.push({
        kind: "single",
        letter,
        header: headerLookup[letter] ?? "",
        role,
      });
      i++;
    }
  }
  return out;
}
