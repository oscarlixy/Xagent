"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { type Digest, listDigests } from "../lib/api";

function textField(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

export default function HomePage() {
  const [digests, setDigests] = useState<Digest[] | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    void listDigests().then(setDigests).catch(() => setError(true));
  }, []);

  return (
    <main className="page-shell">
      <header className="hero">
        <div>
          <p className="eyebrow">PRIVATE READING DESK</p>
          <h1>X List Digest</h1>
          <p>Traceable summaries beside the posts that informed them.</p>
        </div>
        <Link className="primary-link" href="/posts">Browse posts</Link>
      </header>
      <section aria-labelledby="latest-digests">
        <div className="section-heading">
          <h2 id="latest-digests">Latest digests</h2>
          <span>{digests?.length ?? 0} editions</span>
        </div>
        {error ? <p className="state-panel error-message" role="alert">Could not load digests.</p> : null}
        {!error && digests === null ? <p className="state-panel">Loading digests…</p> : null}
        {digests?.length === 0 ? <p className="state-panel">No digests have been generated yet.</p> : null}
        <div className="digest-grid">
          {digests?.map((digest) => {
            const title = textField(digest.rendered_content?.title, `Digest ${digest.window_key}`);
            const summary = textField(digest.rendered_content?.summary, "Open this digest to read its items.");
            return (
              <article className="digest-card" key={digest.id}>
                <div className="post-meta">
                  <span>Version {digest.version}</span>
                  <time dateTime={digest.created_at}>{new Date(digest.created_at).toLocaleDateString()}</time>
                </div>
                <h3><Link href={`/digests/${encodeURIComponent(digest.id)}`}>{title}</Link></h3>
                <p>{summary}</p>
                <span className="status-chip">{digest.status}</span>
              </article>
            );
          })}
        </div>
      </section>
    </main>
  );
}
