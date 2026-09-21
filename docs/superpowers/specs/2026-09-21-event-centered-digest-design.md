# Event-Centered Digest Content Design

## 1. Purpose

The digest should let a reader systematically understand the meaningful developments in a time window without reading every source post. It should optimize for coverage by topic, while removing promotional noise and preserving a clear path from every claim to its sources.

The primary reading hierarchy is:

```text
topic -> event -> what happened / why it matters -> authors and sources
```

This replaces the current behavior in which one summary can contain unrelated developments from many authors and the digest header repeats the same key points shown in the item body.

## 2. Agreed Editorial Policy

### 2.1 Coverage

- Cover all substantive developments in the digest window.
- Treat an event, not a post or an entire feed window, as the smallest digest item.
- Merge posts only when they describe the same event or form one explicit author thread.
- Keep different events separate even when they share a broad topic such as AI.

### 2.2 Event content

Every event contains:

- a specific, informative title;
- `what_happened`: a concise factual account grounded in the sources;
- `why_it_matters`: a restrained explanation of relevance to products, research, or the industry;
- a topic;
- event type (`news`, `research`, `product`, `analysis`, or `opinion`);
- importance from 1 to 5;
- the exact source IDs used for the event.

The model must preserve uncertainty and attribution. `why_it_matters` may explain implications, but must not present unsupported predictions as facts.

### 2.3 Filtering

Exclude:

- event or conference promotion without substantive new information;
- podcast or content promotion without a meaningful claim;
- simple reposts or acknowledgements;
- repeated announcements that add no new information;
- low-information personal reactions.

Opinion posts are retained only when the author is influential in the relevant field or the post contains a clearly articulated, useful argument. Retained opinions are labeled as `opinion` and attributed to the author.

Filtering removes an item from the digest, not from the stored source-post collection.

## 3. Current Problems

The reference digest reports one item and one topic while its item references sixteen posts by several authors and discusses multiple unrelated events. This creates four failures:

1. The aggregation unit is misleading: one stored summary is not one coherent event.
2. The summary has no narrative hierarchy because unrelated claims are placed at the same level.
3. The digest summary repeats item key points instead of providing a window-level overview.
4. The UI shows the text of one post next to a summary and source list covering many posts.

Improving only the prompt would make the prose smoother but would not repair these structural failures.

## 4. Recommended Architecture

Use a two-stage summarization pipeline.

### 4.1 Stage one: source-unit summaries

The existing post-processing stage produces summaries for coherent source units:

- an explicit same-author X conversation/thread; or
- a single standalone post.

The source unit boundary must be established before the LLM call. Conversation IDs cannot merge different authors. Time proximity alone cannot create a thread.

Stage-one output remains evidence-oriented and contains a concise summary, key claims, topic candidates, importance, language, and exact source IDs.

### 4.2 Stage two: digest synthesis

For a completed digest window, a digest synthesizer receives the successful stage-one summaries plus source metadata. It performs four tasks in one structured operation:

1. filter items using the agreed editorial policy;
2. cluster source units that describe the same event;
3. assign each retained event to one primary topic;
4. write the digest overview and event fields.

The synthesizer may merge sources across authors only at this stage and only when they concern the same event. It must copy every retained event source ID from the supplied input and may not invent IDs.

If digest synthesis fails, digest creation should fall back to unmerged stage-one summaries rather than losing the window. The fallback must still preserve source-to-item correspondence.

## 5. Structured Digest Contract

The generated content should follow a dedicated digest-level contract rather than reusing the single-content summary contract.

```json
{
  "overview": "今天的主要动态集中在……",
  "topics": [
    {
      "name": "Agent 与模型",
      "events": [
        {
          "title": "长时间 Agent 任务存在语言质量漂移",
          "what_happened": "……",
          "why_it_matters": "……",
          "event_type": "analysis",
          "importance": 4,
          "source_ids": ["post-id-1", "post-id-2"]
        }
      ]
    }
  ],
  "excluded_source_ids": ["promotional-post-id"]
}
```

Contract invariants:

- every input source ID appears exactly once across retained events and `excluded_source_ids`;
- an event has at least one source ID;
- duplicate event titles and duplicate source assignments are rejected;
- topics and events preserve the model's intentional order;
- empty windows produce an empty topic list and a clear empty-state overview;
- excluded source IDs remain auditable but are not rendered as digest events.

## 6. Persistence and Versioning

- Keep the current immutable digest-version behavior: regeneration creates a new version.
- Persist the complete structured digest result in `Digest.rendered_content` so historical versions remain stable.
- Persist each retained event as a `DigestItem` snapshot with its title, explanatory fields, event type, importance, and source IDs.
- Use a new digest prompt version independent of the single-content prompt version.
- Existing digest versions remain readable through a compatibility renderer; they are not migrated or silently rewritten.

No new event table is required for the first iteration. A normalized event model can be introduced later if events must be queried independently of a digest.

## 7. Presentation

The digest detail page renders:

1. title and time window;
2. one short `overview` describing the topic landscape without repeating event copy;
3. topic sections in editorial order;
4. event cards under each topic.

Each event card displays:

- title;
- “发生了什么”;
- “为什么值得关注”;
- an opinion label when applicable;
- authors and the number of related sources;
- links only to the sources assigned to that event.

The page must not display one source-post body as though it represents a multi-source event. Source excerpts may be shown inside an expandable source list, but are secondary to the event summary.

## 8. Data Flow and Failure Handling

```text
posts
  -> explicit source-unit assembly
  -> source-unit summaries
  -> digest-window selection
  -> event filtering and clustering
  -> structured digest validation
  -> immutable digest snapshot
  -> topic/event rendering
```

Failure behavior:

- A failed source-unit summary does not remove the original post from storage.
- A digest synthesis provider error creates a deterministic fallback from available source-unit summaries.
- Invalid source coverage, unknown IDs, or duplicate source assignments reject the generated digest output and trigger fallback.
- Regeneration failure leaves the existing digest version unchanged.
- Filtered content is recorded through `excluded_source_ids` for auditability.

## 9. Reference-Digest Acceptance Criteria

For digest `7b66f0f6-2ca5-4388-8f5f-df9886b9e7f7`, a regenerated version should:

- stop reporting the entire window as one item and one topic;
- produce separate events for Decision Model, long-running agent language drift, DocJev, AI-focused social-science research, and the revised Waymo safety analysis;
- exclude promotional AI Engineer lineup and podcast-preview posts unless they contain a substantive independent claim;
- associate every displayed source link with exactly one displayed event;
- present a window-level overview that does not duplicate the event descriptions;
- preserve opinion attribution and uncertainty.

Exact wording and topic labels may vary, but the event boundaries and source traceability must satisfy these criteria.

## 10. Testing Strategy

### Backend

- Contract tests reject unknown, missing, or multiply assigned source IDs.
- Unit tests verify promotional filtering and retention of high-value attributed opinions.
- Unit tests verify same-event cross-author merging and different-event separation.
- Digest service tests verify persistence, versioning, fallback, and backward compatibility.
- Provider tests verify the new structured-output schema and prompt version.
- A regression fixture modeled on the reference digest asserts the five expected event boundaries.

### Frontend

- Component tests verify topic and event hierarchy.
- Tests verify that “发生了什么” and “为什么值得关注” render separately.
- Tests verify that only an event's assigned source links appear on its card.
- Tests verify the opinion label, empty state, and legacy digest compatibility.
- The existing reading-flow end-to-end test is extended with a multi-topic, multi-author digest.

## 11. Scope Boundaries

This change does not:

- delete or hide source posts from the general post feed;
- introduce user-configurable editorial rules in the first iteration;
- create a permanent cross-digest knowledge graph of events;
- add semantic-vector infrastructure solely for clustering;
- rewrite historical digest versions.

The first implementation should prefer a single structured digest-synthesis call and explicit validation. More complex multi-pass ranking or embedding-based clustering should be considered only if real digest windows exceed the model's safe input limit or quality evaluation shows a concrete need.
