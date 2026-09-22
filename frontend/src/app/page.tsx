"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { type Digest, type SystemStatus, getStatus, listDigests } from "../lib/api";

function textField(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

const syncErrorLabels: Record<string, string> = {
  ingestion_failed: "Ingestion failed",
  ingestion_partial: "Ingestion completed with rejected items",
  link_failed: "Link processing failed",
  link_processing_failed: "Link processing failed",
  summary_processing_failed: "Summary processing failed",
  x_authorization_failed: "X authorization failed",
  x_rate_limited: "X rate limit reached",
  x_upstream_unavailable: "X is temporarily unavailable",
  x_response_invalid: "X returned an invalid response",
  x_request_invalid: "X rejected the request",
  oauth_not_configured: "X authorization is not configured",
  oauth_authorization_failed: "X authorization failed",
  oauth_upstream_unavailable: "X authorization service is unavailable",
  oauth_token_invalid: "X authorization has expired",
};

export default function HomePage() {
  const [digests, setDigests] = useState<Digest[] | null>(null);
  const [error, setError] = useState(false);
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [statusError, setStatusError] = useState(false);

  useEffect(() => {
    void listDigests().then(setDigests).catch(() => setError(true));
    void getStatus().then(setStatus).catch(() => setStatusError(true));
  }, []);

  const health = status?.sync_health;
  const syncWarning = health?.stale || health?.status === "partial" || health?.status === "failed";
  const lastSyncAt = health?.last_completed_at || health?.last_attempt_at;

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
      {statusError ? (
        <section className="sync-health sync-health-warning" role="alert">
          <strong>Sync status unavailable</strong>
          <p>The application could not verify whether the displayed data is current.</p>
        </section>
      ) : null}
      {health ? (
        <section
          className={`sync-health${syncWarning ? " sync-health-warning" : ""}`}
          role={syncWarning ? "alert" : "status"}
          aria-label="Data freshness"
        >
          <strong>{health.stale ? "Data is stale" : "Data is up to date"}</strong>
          <p>
            Last sync{" "}
            {lastSyncAt ? (
              <time dateTime={lastSyncAt}>{new Date(lastSyncAt).toLocaleString()}</time>
            ) : (
              "has not completed"
            )}
            {health.stale_list_count > 0
              ? ` · ${health.stale_list_count} of ${health.enabled_list_count} lists overdue`
              : ""}
          </p>
          {health.last_error ? (
            <p>{syncErrorLabels[health.last_error] || "The latest sync completed with an error"}.</p>
          ) : null}
        </section>
      ) : null}
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
