"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { SummaryPanel } from "../../../components/SummaryPanel";
import { type Digest, type Post, getDigest, getPostsByIds, regenerateDigest } from "../../../lib/api";
import { trustedSourceUrl } from "../../../lib/source-url";

const stringValue = (value: unknown, fallback = "") => typeof value === "string" ? value : fallback;
const stringList = (value: unknown) => Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];

export default function DigestPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [digest, setDigest] = useState<Digest | null>(null);
  const [sourcePosts, setSourcePosts] = useState<Record<string, Post>>({});
  const [error, setError] = useState<string | null>(null);
  const [regenerating, setRegenerating] = useState(false);

  useEffect(() => {
    void getDigest(id)
      .then(async (loaded) => {
        setDigest(loaded);
        const sourceIds = loaded.items.flatMap((item) => stringList(item.snapshot?.source_ids));
        if (sourceIds.length > 0) setSourcePosts(await getPostsByIds(sourceIds));
      })
      .catch(() => setError("Could not load this digest."));
  }, [id]);

  async function handleRegenerate() {
    setRegenerating(true);
    setError(null);
    try {
      const regenerated = await regenerateDigest(id);
      setSourcePosts({});
      setDigest(regenerated);
      router.push(`/digests/${encodeURIComponent(regenerated.id)}`);
    } catch {
      setError("Could not regenerate this digest. The existing version is unchanged.");
    } finally {
      setRegenerating(false);
    }
  }

  if (!digest && !error) return <main className="page-shell"><p className="state-panel">Loading digest…</p></main>;
  if (!digest) return <main className="page-shell"><p className="state-panel error-message" role="alert">{error}</p></main>;

  const title = stringValue(digest.rendered_content?.title, `Digest ${digest.window_key}`);
  return (
    <main className="page-shell">
      <header className="subpage-header digest-header">
        <div>
          <p className="eyebrow">{digest.window_key}</p>
          <h1>{title}</h1>
          <p>Version {digest.version} · {digest.timezone}</p>
        </div>
        <div className="header-actions">
          <Link href="/">All digests</Link>
          <button type="button" disabled={regenerating} onClick={() => void handleRegenerate()}>
            {regenerating ? "Regenerating…" : "Regenerate digest"}
          </button>
        </div>
      </header>
      {error ? <p className="error-message" role="alert">{error}</p> : null}
      <SummaryPanel
        title="Digest summary"
        summary={stringValue(digest.rendered_content?.summary)}
        keyPoints={stringList(digest.rendered_content?.key_points)}
      />
      <section className="digest-items" aria-label="Digest items">
        {digest.items.map((item) => {
          const snapshot = item.snapshot || {};
          const sources = stringList(snapshot.source_ids)
            .map((sourceId) => sourcePosts[sourceId])
            .filter((post): post is Post => post !== undefined);
          const snapshotSourceUrl = trustedSourceUrl(snapshot.source_url);
          const originalText = sources[0]?.text || stringValue(snapshot.text, "Original text unavailable in this snapshot.");
          return (
            <article className="digest-item" key={item.id}>
              <div className="post-meta"><span>{item.topic || "General"}</span><span>#{item.position + 1}</span></div>
              <p className="post-text">{originalText}</p>
              <SummaryPanel summary={stringValue(snapshot.summary)} keyPoints={stringList(snapshot.key_points)} />
              <div className="source-list">
                {sources.map((source) => {
                  const sourceUrl = trustedSourceUrl(source.source_url);
                  return sourceUrl ? (
                    <a className="source-link" href={sourceUrl} key={source.id} target="_blank" rel="noopener noreferrer">
                      View original on X (@{source.author.username}) <span aria-hidden="true">↗</span>
                    </a>
                  ) : null;
                })}
                {sources.length === 0 && snapshotSourceUrl ? (
                  <a className="source-link" href={snapshotSourceUrl} target="_blank" rel="noopener noreferrer">View original on X <span aria-hidden="true">↗</span></a>
                ) : null}
              </div>
            </article>
          );
        })}
      </section>
    </main>
  );
}
