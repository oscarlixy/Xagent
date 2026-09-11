type SummaryPanelProps = {
  summary?: string;
  keyPoints?: string[];
  title?: string;
};

export function SummaryPanel({ summary, keyPoints = [], title = "Summary" }: SummaryPanelProps) {
  return (
    <section className="summary-panel" aria-label={title}>
      <p className="eyebrow">{title}</p>
      <p className="summary-copy">{summary || "Summary not available yet."}</p>
      {keyPoints.length > 0 ? (
        <ul>
          {keyPoints.map((point) => <li key={point}>{point}</li>)}
        </ul>
      ) : null}
    </section>
  );
}
