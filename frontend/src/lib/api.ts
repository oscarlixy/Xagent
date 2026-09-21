export type PostState = {
  read: boolean;
  saved: boolean;
  ignored: boolean;
};

export type Author = {
  id: string;
  username: string;
  display_name: string | null;
  profile_image_url: string | null;
};

export type Post = {
  id: string;
  platform: string;
  platform_post_id: string;
  text: string;
  created_at: string;
  source_url: string;
  author: Author;
  state: PostState;
  topics: string[];
};

export type PostPage = { items: Post[]; next_cursor: string | null };

export type DigestItem = {
  id: string;
  source_type: string;
  source_id: string;
  topic: string | null;
  position: number;
  snapshot: Record<string, unknown> | null;
};

export type DigestEventType = "news" | "research" | "product" | "analysis" | "opinion";

export type DigestEventSourceSnapshot = {
  id: string;
  username: string;
  text: string;
  url: string;
};

export type DigestEventSnapshot = {
  title: string;
  what_happened: string;
  why_it_matters: string;
  event_type: DigestEventType;
  importance: number;
  source_ids: string[];
  sources: DigestEventSourceSnapshot[];
};

export type DigestTopicIndex = {
  name: string;
  events: Array<DigestEventSnapshot & { item_id: string }>;
};

export type EventDigestContent = {
  schema_version: "event-digest-v1";
  title: string;
  overview: string;
  topics: DigestTopicIndex[];
  excluded_source_ids: string[];
};

export type Digest = {
  id: string;
  window_key: string;
  version: number;
  timezone: string;
  rendered_content: Record<string, unknown> | null;
  status: string;
  created_at: string;
  items: DigestItem[];
};

export type SystemStatus = {
  sync_health: {
    enabled_list_count: number;
    stale_list_count: number;
    stale: boolean;
    status: "running" | "succeeded" | "partial" | "failed" | null;
    last_attempt_at: string | null;
    last_completed_at: string | null;
    last_error: string | null;
  };
};

export type Summary = {
  id: string;
  generation: number;
  summary: string;
  key_points: string[];
  topics: string[];
  importance: number;
  language: string;
  source_ids: string[];
  status: string;
};

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
  }
}

async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, headers: { Accept: "application/json", ...init?.headers } });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const payload = (await response.json()) as { message?: unknown; detail?: unknown };
      if (typeof payload.message === "string") message = payload.message;
      else if (typeof payload.detail === "string") message = payload.detail;
    } catch {
      // Preserve the status-based message for non-JSON failures.
    }
    throw new ApiError(message, response.status);
  }
  return response.json() as Promise<T>;
}

export const listDigests = () => apiRequest<Digest[]>("/api/digests");
export const getStatus = () => apiRequest<SystemStatus>("/api/status");
export const getDigest = (id: string) => apiRequest<Digest>(`/api/digests/${encodeURIComponent(id)}`);
export const listPosts = (query: string) => apiRequest<PostPage>(`/api/posts${query ? `?${query}` : ""}`);

export async function getPostsByIds(ids: string[]): Promise<Record<string, Post>> {
  const wanted = new Set(ids);
  const matches: Record<string, Post> = {};
  const seenCursors = new Set<string>();
  let cursor: string | null = null;
  while (wanted.size > 0) {
    const query = new URLSearchParams({ limit: "100" });
    if (cursor) query.set("cursor", cursor);
    const result = await listPosts(query.toString());
    for (const post of result.items) {
      if (wanted.delete(post.id)) matches[post.id] = post;
    }
    cursor = result.next_cursor;
    if (!cursor || seenCursors.has(cursor)) break;
    seenCursors.add(cursor);
  }
  return matches;
}

export const updatePostState = (id: string, state: PostState) =>
  apiRequest<PostState>(`/api/posts/${encodeURIComponent(id)}/state`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(state),
  });

export const regenerateSummary = (id: string) =>
  apiRequest<Summary>(`/api/posts/${encodeURIComponent(id)}/summaries/regenerate`, {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
  });

export const regenerateDigest = (id: string) =>
  apiRequest<Digest>(`/api/digests/${encodeURIComponent(id)}/regenerate`, { method: "POST" });
