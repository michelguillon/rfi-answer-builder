import { useState } from "react";
import { Download, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { downloadExportUrl, postAnswerEdits } from "@/lib/api";

/**
 * ExportButton — POSTs the user's edits/skips, then triggers a
 * browser download of /api/answer/export?session_id=...
 *
 * ARCHITECTURAL DECISION: window.location.href for the actual
 * download, not fetch + Blob + anchor click.
 *
 * The backend's FileResponse sets Content-Disposition: attachment.
 * In every modern browser, navigating to a URL with that header
 * triggers a download WITHOUT unloading the current page, so the
 * React app keeps running. Going through fetch + new Blob() +
 * URL.createObjectURL would re-stream the bytes through JavaScript
 * memory unnecessarily — for a ~20KB xlsx it's fine, for a future
 * 5MB filled RFI it's wasteful. window.location.href is the
 * lighter path.
 */

export interface ExportButtonProps {
  sessionId: string;
  overrides: Record<number, string>;
  skipped: number[];
  disabled?: boolean;
}

export function ExportButton({
  sessionId,
  overrides,
  skipped,
  disabled,
}: ExportButtonProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onClick = async () => {
    setBusy(true);
    setError(null);
    try {
      await postAnswerEdits(sessionId, overrides, skipped);
      window.location.href = downloadExportUrl(sessionId);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2">
      <Button onClick={onClick} disabled={busy || disabled}>
        {busy ? (
          <>
            <Loader2 className="mr-1 h-4 w-4 animate-spin" />
            Preparing download…
          </>
        ) : (
          <>
            <Download className="mr-1 h-4 w-4" />
            Download filled RFI
          </>
        )}
      </Button>
      {error && <p className="text-sm text-destructive">{error}</p>}
    </div>
  );
}
