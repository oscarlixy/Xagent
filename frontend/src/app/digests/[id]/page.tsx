"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { DigestEventCard } from "../../../components/DigestEventCard";
import { SummaryPanel } from "../../../components/SummaryPanel";
import {
  type Digest,
  type DigestEventSnapshot,
  type DigestEventSourceSnapshot,
  type DigestEventType,
  type EventDigestContent,
  type Post,
  getDigest,
  getPostsByIds,
  regenerateDigest,
} from "../../../lib/api";
import { trustedSourceUrl } from "../../../lib/source-url";

const stringValue = (value: unknown, fallback = "") => typeof value === "string" ? value : fallback;
const stringList = (value: unknown) => Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];

const eventTypes = new Set<DigestEventType>(["news", "research", "product", "analysis", "opinion"]);

function isEventType(value: unknown): value is DigestEventType {
  return typeof value === "string" && eventTypes.has(value as DigestEventType);
}

function eventSourceSnapshot(value: unknown): DigestEventSourceSnapshot | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  if (
    typeof record.id !== "string"
    || typeof record.username !== "string"
    || typeof record.text !== "string"
    || typeof record.url !== "string"
  ) return null;
  return record as DigestEventSourceSnapshot;
}

function eventSnapshot(value: unknown): DigestEventSnapshot | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  if (
    typeof record.title !== "string"
    || typeof record.what_happened !== "string"
    || typeof record.why_it_matters !== "string"
    || typeof record.importance !== "number"
    || !Array.isArray(record.source_ids)
    || !record.source_ids.every((sourceId) => typeof sourceId === "string")
    || !Array.isArray(record.sources)
    || !isEventType(record.event_type)
  ) return null;
  const sources = record.sources.map(eventSourceSnapshot);
  if (sources.some((source) => source === null)) return null;
  return { ...record, sources } as DigestEventSnapshot;
}

function eventDigestContent(value: unknown): EventDigestContent | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  if (
    record.schema_version !== "event-digest-v1"
    || typeof record.title !== "string"
    || typeof record.overview !== "string"
    || !Array.isArray(record.topics)
    || !Array.isArray(record.excluded_source_ids)
    || !record.excluded_source_ids.every((sourceId) => typeof sourceId === "string")
  ) return null;
  const topics = record.topics.map((value) => {
    if (!value || typeof value !== "object") return null;
    const topic = value as Record<string, unknown>;
    if (typeof topic.name !== "string" || !Array.isArray(topic.events)) return null;
    const events = topic.events.map((value) => {
      if (!value || typeof value !== "object") return null;
      const event = value as Record<string, unknown>;
      const snapshot = eventSnapshot(event);
      return typeof event.item_id === "string" && snapshot
        ? { ...snapshot, item_id: event.item_id }
        : null;
    });
    if (events.some((event) => event === null)) return null;
    return { name: topic.name, events: events.filter((event) => event !== null) };
  });
  if (topics.some((topic) => topic === null)) return null;
  return {
    schema_version: "event-digest-v1",
    title: record.title,
    overview: record.overview,
    topics: topics.filter((topic) => topic !== null),
    excluded_source_ids: record.excluded_source_ids as string[],
  };
}

export default function DigestPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [digest, setDigest] = useState<Digest | null>(null);
  const [sourcePosts, setSourcePosts] = useState<Record<string, Post>>({});
  const [loadError, setLoadError] = useState<string | null>(null);
  const [sourceWarning, setSourceWarning] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [regenerating, setRegenerating] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setDigest(null);
    setSourcePosts({});
    setLoadError(null);
    setSourceWarning(null);
    setActionError(null);

    async function load() {
      let loaded: Digest;
      try {
        loaded = await getDigest(id);
      } catch {
        if (!cancelled) setLoadError("Could not load this digest.");
        return;
      }

      if (cancelled) return;
      setDigest(loaded);
      const sourceIds = loaded.items.flatMap((item) => stringList(item.snapshot?.source_ids));
      if (sourceIds.length === 0) return;

      try {
        const posts = await getPostsByIds(sourceIds);
        if (!cancelled) setSourcePosts(posts);
      } catch {
        if (!cancelled) {
          setSourceWarning(
            "Digest loaded successfully. Some original-source details could not be refreshed; the saved digest snapshot below is unchanged.",
          );
        }
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
  }, [id]);

  async function handleRegenerate() {
    setRegenerating(true);
    setActionError(null);
    try {
      const regenerated = await regenerateDigest(id);
      setSourcePosts({});
      setDigest(regenerated);
      router.push(`/digests/${encodeURIComponent(regenerated.id)}`);
    } catch {
      setActionError("Could not regenerate this digest. The existing version is unchanged.");
    } finally {
      setRegenerating(false);
    }
  }

  if (!digest && !loadError) return <main className="page-shell"><p className="state-panel">Loading digest…</p></main>;
  if (!digest) return <main className="page-shell"><p className="state-panel error-message" role="alert">{loadError}</p></main>;

  const eventContent = eventDigestContent(digest.rendered_content);
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
      {actionError ? <p className="error-message" role="alert">{actionError}</p> : null}
      {sourceWarning ? <p className="digest-source-warning" role="status">{sourceWarning}</p> : null}
      {eventContent ? (
        <>
          <section className="digest-overview" aria-label="摘要概览">
            <p>{eventContent.overview}</p>
          </section>
          <div className="digest-topics">
            {eventContent.topics.map((topic) => (
              <section className="digest-topic" key={topic.name}>
                <h2>{topic.name}</h2>
                {topic.events.map((event) => {
                  const eventSources = event.source_ids
                    .map((sourceId) => sourcePosts[sourceId])
                    .filter((post): post is Post => post !== undefined);
                  return (
                    <DigestEventCard
                      key={event.item_id}
                      eventItemId={event.item_id}
                      event={event}
                      sources={eventSources}
                    />
                  );
                })}
              </section>
            ))}
          </div>
        </>
      ) : (
        <>
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
        </>
      )}
    </main>
  );
}
