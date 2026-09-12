"use client";

import { useState } from "react";
import {
  type Post,
  type PostState,
  type Summary,
  regenerateSummary,
  updatePostState,
} from "../lib/api";
import { SummaryPanel } from "./SummaryPanel";
import { trustedSourceUrl } from "../lib/source-url";

const actions: Array<{ key: keyof PostState; label: string }> = [
  { key: "read", label: "Mark as read" },
  { key: "saved", label: "Save post" },
  { key: "ignored", label: "Ignore post" },
];

export function PostCard({ post }: { post: Post }) {
  const [state, setState] = useState(post.state);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [pending, setPending] = useState<keyof PostState | "summary" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const sourceUrl = trustedSourceUrl(post.source_url);

  async function toggle(key: keyof PostState) {
    const previous = state;
    const next = { ...state, [key]: !state[key] };
    setState(next);
    setPending(key);
    setError(null);
    try {
      setState(await updatePostState(post.id, next));
    } catch {
      setState(previous);
      setError("Could not update this post. Your previous state was restored.");
    } finally {
      setPending(null);
    }
  }

  async function handleRegenerate() {
    setPending("summary");
    setError(null);
    try {
      setSummary(await regenerateSummary(post.id));
    } catch {
      setError("Could not regenerate the summary. Please try again.");
    } finally {
      setPending(null);
    }
  }

  return (
    <article className="post-card">
      <div className="post-content">
        <div className="post-meta">
          <strong>@{post.author.username}</strong>
          <time dateTime={post.created_at}>{new Date(post.created_at).toLocaleString()}</time>
        </div>
        <p className="post-text">{post.text}</p>
        <div className="topic-row">
          {post.topics.map((topic) => <span key={topic}>{topic}</span>)}
        </div>
        {sourceUrl ? (
          <a className="source-link" href={sourceUrl} target="_blank" rel="noopener noreferrer">
            View original on X <span aria-hidden="true">↗</span>
          </a>
        ) : (
          <span className="source-unavailable">Original source link unavailable</span>
        )}
        <div className="action-row" aria-label="Post actions">
          {actions.map(({ key, label }) => (
            <button
              className="toggle-button"
              key={key}
              type="button"
              aria-label={label}
              aria-pressed={state[key]}
              disabled={pending !== null}
              onClick={() => void toggle(key)}
            >
              {label.replace("Mark as ", "")}
            </button>
          ))}
          <button type="button" disabled={pending !== null} onClick={() => void handleRegenerate()}>
            {pending === "summary" ? "Regenerating…" : "Regenerate summary"}
          </button>
        </div>
        {error ? <p className="error-message" role="alert">{error}</p> : null}
      </div>
      <SummaryPanel
        summary={summary?.summary}
        keyPoints={summary?.key_points}
        title={summary ? `Summary · generation ${summary.generation}` : "Summary"}
      />
    </article>
  );
}
