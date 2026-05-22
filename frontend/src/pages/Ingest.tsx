import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export default function Ingest() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Ingest — coming soon</CardTitle>
        <CardDescription>
          Step 8 will build the 3-step wizard here (Upload → Profile SSE
          + ProposalCard → Ingest SSE + ProgressBars).
        </CardDescription>
      </CardHeader>
      <CardContent>
        <p className="text-sm text-muted-foreground">
          Wires the backend's POST /api/sessions, POST /api/ingest/upload,
          GET /api/ingest/profile (SSE), POST /api/ingest/approve (SSE).
        </p>
      </CardContent>
    </Card>
  );
}
