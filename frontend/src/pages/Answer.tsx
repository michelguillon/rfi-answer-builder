import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export default function Answer() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Answer — coming soon</CardTitle>
        <CardDescription>
          Step 9 will build the 3-step flow here (Upload → Process SSE
          with per-question AnswerCards → Review &amp; Export).
        </CardDescription>
      </CardHeader>
      <CardContent>
        <p className="text-sm text-muted-foreground">
          AnswerCards will render the full retrieval trace per answer
          (sources, scores, pair_ids) plus a warning badge when the
          generated text mentions a non-target client name.
        </p>
      </CardContent>
    </Card>
  );
}
