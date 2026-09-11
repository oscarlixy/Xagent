import Link from "next/link";

const FILTERS = ["list_id", "topic", "author", "from", "to", "state"] as const;

export type FilterValues = Partial<Record<(typeof FILTERS)[number], string>>;

export function FilterBar({ values }: { values: FilterValues }) {
  return (
    <form className="filter-bar" action="/posts" method="get">
      <label>
        List ID
        <input name="list_id" defaultValue={values.list_id} placeholder="X List UUID" />
      </label>
      <label>
        Topic
        <input name="topic" defaultValue={values.topic} placeholder="ai" />
      </label>
      <label>
        Author
        <input name="author" defaultValue={values.author} placeholder="username" />
      </label>
      <label>
        From
        <input name="from" type="date" defaultValue={values.from} />
      </label>
      <label>
        To
        <input name="to" type="date" defaultValue={values.to} />
      </label>
      <label>
        State
        <select name="state" defaultValue={values.state || ""}>
          <option value="">Any</option>
          <option value="read">Read</option>
          <option value="saved">Saved</option>
          <option value="ignored">Ignored</option>
        </select>
      </label>
      <div className="filter-actions">
        <button type="submit">Apply filters</button>
        <Link href="/posts">Clear</Link>
      </div>
    </form>
  );
}
