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
  const query = searchParams.toString();
  const values: FilterValues = {};
  for (const name of FILTER_NAMES) values[name] = searchParams.get(name) || "";
  const [result, setResult] = useState<PostPage | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    setResult(null);
    setError(false);
    void listPosts(query).then(setResult).catch(() => setError(true));
  }, [query]);

  return (
    <>
      <FilterBar values={values} />
      {result === null && !error ? <p className="state-panel">Loading posts…</p> : null}
      {error ? <p className="state-panel error-message" role="alert">Could not load posts. Try again shortly.</p> : null}
      {result?.items.length === 0 ? <p className="state-panel">No posts match these filters.</p> : null}
      <div className="post-stream">
        {result?.items.map((post) => <PostCard post={post} key={post.id} />)}
      </div>
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
