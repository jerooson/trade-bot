import { useEffect, useState } from "react";
import clsx from "clsx";
import { SectionHeader } from "./SectionHeader";

interface ReviewPayload {
  date: string;
  markdown: string;
  report: Record<string, unknown> | null;
}

async function fetchDates(): Promise<string[]> {
  const r = await fetch("/api/review");
  if (!r.ok) throw new Error(`review list: ${r.status}`);
  return (await r.json()).dates as string[];
}

async function fetchReview(day: string): Promise<ReviewPayload> {
  const r = await fetch(`/api/review/${day}`);
  if (!r.ok) throw new Error(`review ${day}: ${r.status}`);
  return r.json();
}

/** Minimal markdown renderer for the review pages (headers, tables, bullets, inline bold/italic). */
function inline(text: string): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  const re = /(\*\*[^*]+\*\*|_[^_]+_|`[^`]+`)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith("**")) out.push(<strong key={i++} className="text-bone-50">{tok.slice(2, -2)}</strong>);
    else if (tok.startsWith("`")) out.push(<code key={i++} className="rounded bg-ink-800 px-1 text-crt-amber">{tok.slice(1, -1)}</code>);
    else out.push(<em key={i++} className="text-bone-400">{tok.slice(1, -1)}</em>);
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function Markdown({ text }: { text: string }) {
  const lines = text.split("\n");
  const blocks: React.ReactNode[] = [];
  let i = 0;
  let key = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("# ")) {
      blocks.push(<h1 key={key++} className="font-editorial text-2xl text-bone-50">{line.slice(2)}</h1>);
      i++;
    } else if (line.startsWith("## ")) {
      blocks.push(<h2 key={key++} className="mt-6 border-b border-ink-500/60 pb-1 text-[11px] uppercase tracking-[0.28em] text-crt-amber">{line.slice(3)}</h2>);
      i++;
    } else if (line.startsWith("|")) {
      const rows: string[][] = [];
      while (i < lines.length && lines[i].startsWith("|")) {
        const cells = lines[i].split("|").slice(1, -1).map((c) => c.trim());
        if (!cells.every((c) => /^-+$/.test(c))) rows.push(cells);
        i++;
      }
      const [head, ...body] = rows;
      blocks.push(
        <div key={key++} className="my-2 overflow-x-auto">
          <table className="w-full text-[12px] tabular">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-[0.18em] text-bone-500">
                {head.map((c, j) => <th key={j} className="px-2 py-1 font-medium">{c}</th>)}
              </tr>
            </thead>
            <tbody>
              {body.map((r, ri) => (
                <tr key={ri} className="border-t border-ink-500/30 text-bone-200">
                  {r.map((c, j) => (
                    <td key={j} className={clsx("px-2 py-1", /^\$?[+-]\d/.test(c) && (c.includes("-") ? "text-crt-red" : "text-crt-green"))}>{inline(c)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
    } else if (line.startsWith("- ")) {
      const items: string[] = [];
      while (i < lines.length && lines[i].startsWith("- ")) { items.push(lines[i].slice(2)); i++; }
      blocks.push(
        <ul key={key++} className="my-1 space-y-1 text-[13px] text-bone-200">
          {items.map((it, j) => <li key={j} className="flex gap-2"><span className="text-bone-500">·</span><span className="min-w-0 break-words">{inline(it)}</span></li>)}
        </ul>,
      );
    } else if (line.trim() === "") {
      i++;
    } else {
      blocks.push(<p key={key++} className="my-1 whitespace-pre-wrap text-[13px] text-bone-200">{inline(line)}</p>);
      i++;
    }
  }
  return <div>{blocks}</div>;
}

export function ReviewView() {
  const [dates, setDates] = useState<string[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [review, setReview] = useState<ReviewPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchDates()
      .then((d) => { setDates(d); setSelected((s) => s ?? d[0] ?? null); })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!selected) return;
    setError(null);
    fetchReview(selected).then(setReview).catch((e) => setError(String(e)));
  }, [selected]);

  return (
    <section className="px-6 py-8 md:px-10">
      <SectionHeader
        index="04"
        label="Daily review"
        hint="What Heat said, what the bot did, and why they differ."
        right={dates.length ? `${dates.length} days` : undefined}
      />
      <div className="mt-6 flex flex-col gap-6 md:flex-row">
        <aside className="flex shrink-0 flex-row flex-wrap gap-1.5 md:w-[140px] md:flex-col">
          {dates.map((d) => (
            <button
              key={d}
              onClick={() => setSelected(d)}
              className={clsx(
                "border px-3 py-1.5 text-left text-[12px] tabular transition-colors",
                d === selected ? "border-crt-info/30 bg-crt-info/10 text-bone-50" : "border-ink-500/40 text-bone-400 hover:bg-ink-800/60",
              )}
            >
              {d}
            </button>
          ))}
          {!loading && dates.length === 0 && (
            <div className="text-[12px] text-bone-500">No reviews yet. The first one is written at 16:15 ET.</div>
          )}
        </aside>
        <div className="min-w-0 flex-1 border border-ink-500/30 bg-ink-900/40 p-5">
          {error && <div className="text-[12px] text-crt-red">{error}</div>}
          {review ? <Markdown text={review.markdown} /> : !error && <div className="text-[12px] text-bone-500">{loading ? "loading…" : "select a day"}</div>}
        </div>
      </div>
    </section>
  );
}
