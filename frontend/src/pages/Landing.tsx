import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export default function Landing() {
  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>Landing — coming soon</CardTitle>
          <CardDescription>
            Two workflow cards (Add RFI to corpus / Answer a new RFI) land
            here in Step 7. The scaffold confirms routing + Tailwind +
            shadcn are wired correctly.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            Routes available: <code className="font-mono">/</code>{" "}
            <code className="font-mono">/ingest</code>{" "}
            <code className="font-mono">/answer</code>
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
