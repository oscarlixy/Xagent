import { type DigestEventSnapshot, type Post } from "../lib/api";
import { trustedSourceUrl } from "../lib/source-url";

type DigestEventCardProps = {
  eventItemId: string;
  event: DigestEventSnapshot;
  sources: Post[];
};

const eventTypeLabel = {
  news: "新闻",
  research: "研究",
  product: "产品",
  analysis: "分析",
  opinion: "观点",
} as const;

export function DigestEventCard({ eventItemId, event, sources }: DigestEventCardProps) {
  const liveSources = new Map(sources.map((source) => [source.id, source]));
  const savedSources = new Map(event.sources.map((source) => [source.id, source]));
  const sourceNames = event.source_ids
    .map((sourceId) => {
      const live = liveSources.get(sourceId);
      return live?.author.display_name || live?.author.username || savedSources.get(sourceId)?.username;
    })
    .filter((name): name is string => Boolean(name));

  return (
    <article className="digest-event" data-event-id={eventItemId}>
      <div className="event-heading">
        <h3>{event.title}</h3>
        <span className="event-type">{eventTypeLabel[event.event_type]}</span>
      </div>
      <div className="event-explanation">
        <section>
          <h4>发生了什么</h4>
          <p>{event.what_happened}</p>
        </section>
        <section>
          <h4>为什么值得关注</h4>
          <p>{event.why_it_matters}</p>
        </section>
      </div>
      <p className="event-source-summary">
        {sourceNames.join("、")} · {event.source_ids.length} 条来源
      </p>
      <div className="event-sources" aria-label="事件来源">
        {event.source_ids.map((sourceId) => {
          const live = liveSources.get(sourceId);
          const saved = savedSources.get(sourceId);
          const username = live?.author.username || saved?.username;
          const sourceUrl = trustedSourceUrl(live?.source_url) || trustedSourceUrl(saved?.url);
          const label = username ? `查看 @${username} 的原文` : "查看原文";
          return sourceUrl ? (
            <a
              className="source-link"
              href={sourceUrl}
              key={sourceId}
              target="_blank"
              rel="noopener noreferrer"
            >
              {label} <span aria-hidden="true">↗</span>
            </a>
          ) : username ? (
            <span className="source-label" key={sourceId}>@{username}</span>
          ) : null;
        })}
      </div>
    </article>
  );
}
