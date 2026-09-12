"use client";

import Link from "next/link";
import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { FilterBar, type FilterValues } from "../../components/FilterBar";
import { PostCard } from "../../components/PostCard";
import { type PostPage, listPosts } from "../../lib/api";

const FILTER_NAMES = ["list_id", "topic", "author", "from", "to", "state"] as const;

function PostStream() {
  const searchParams = useSearchParams();
  const visibleQuery = searchParams.toString();
  const backendParams = new URLSearchParams(visibleQuery);
  const toDate = backendParams.get("to");
  if (toDate && /^\d{4}-\d{2}-\d{2}$/.test(toDate)) {
    backendParams.set("to", `${toDate}T23:59:59.999999Z`);
  }
  const backendQuery = backendParams.toString();
  const values: FilterValues = {};
  for (const name of FILTER_NAMES) values[name] = searchParams.get(name) || "";
  const [result, setResult] = useState<PostPage | null>(null);
  const [error, setError] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState(false);

  useEffect(() => {
    setResult(null);
    setError(false);
    setLoadMoreError(false);
    void listPosts(backendQuery).then(setResult).catch(() => setError(true));
  }, [backendQuery]);

  async function loadMore() {
    if (!result?.next_cursor) return;
    const nextPageParams = new URLSearchParams(backendQuery);
    nextPageParams.set("cursor", result.next_cursor);
    setLoadingMore(true);
    setLoadMoreError(false);
    try {
      const nextPage = await listPosts(nextPageParams.toString());
      setResult({
        items: [...result.items, ...nextPage.items],
        next_cursor: nextPage.next_cursor,
      });
    } catch {
      setLoadMoreError(true);
    } finally {
      setLoadingMore(false);
    }
  }

  return (
    <>
      <FilterBar values={values} />
      {result === null && !error ? <p className="state-panel">Loading posts…</p> : null}
      {error ? <p className="state-panel error-message" role="alert">Could not load posts. Try again shortly.</p> : null}
      {result?.items.length === 0 ? <p className="state-panel">No posts match these filters.</p> : null}
      <div className="post-stream">
        {result?.items.map((post) => <PostCard post={post} key={post.id} />)}
      </div>
      {result?.next_cursor ? (
        <div className="pagination-actions">
          <button type="button" disabled={loadingMore} onClick={() => void loadMore()}>
            {loadingMore ? "Loading more…" : "Load more posts"}
          </button>
        </div>
      ) : null}
      {loadMoreError ? (
        <p className="error-message" role="alert">Could not load more posts. Try again.</p>
      ) : null}
    </>
  );
}

export default function PostsPage() {
  return (
    <main className="page-shell">
      <header className="subpage-header">
        <div><p className="eyebrow">SOURCE STREAM</p><h1>Posts</h1></div>
        <Link href="/">Back to digests</Link>
      </header>
      <Suspense fallback={<p className="state-panel">Loading posts…</p>}>
        <PostStream />
      </Suspense>
    </main>
  );
}
