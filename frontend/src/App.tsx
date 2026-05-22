import { Link, Route, Routes } from "react-router-dom";

import Landing from "@/pages/Landing";
import Ingest from "@/pages/Ingest";
import Answer from "@/pages/Answer";

export default function App() {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="border-b">
        <div className="container flex items-center justify-between py-4">
          <Link to="/" className="text-lg font-semibold tracking-tight">
            RFI Answer Builder
          </Link>
          <nav className="flex gap-4 text-sm text-muted-foreground">
            <Link to="/ingest" className="hover:text-foreground">
              Add RFI
            </Link>
            <Link to="/answer" className="hover:text-foreground">
              Answer RFI
            </Link>
          </nav>
        </div>
      </header>
      <main className="container py-10">
        <Routes>
          <Route path="/" element={<Landing />} />
          <Route path="/ingest" element={<Ingest />} />
          <Route path="/answer" element={<Answer />} />
        </Routes>
      </main>
    </div>
  );
}
