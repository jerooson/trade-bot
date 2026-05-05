import clsx from "clsx";

interface Props {
  index: string;
  label: string;
  hint?: string;
  right?: React.ReactNode;
  className?: string;
}

/** Editorial section header with index, label, and optional right-aligned content. */
export function SectionHeader({ index, label, hint, right, className }: Props) {
  return (
    <div className={clsx("flex items-end justify-between border-b border-ink-500/60 pb-3", className)}>
      <div>
        <div className="text-[10px] uppercase tracking-[0.32em] text-bone-500">
          <span className="mr-3 text-crt-amber">{index}</span>
          {label}
        </div>
        {hint && (
          <div className="mt-1 font-editorial text-base italic text-bone-300">{hint}</div>
        )}
      </div>
      {right && <div className="text-[11px] uppercase tracking-[0.18em] text-bone-400">{right}</div>}
    </div>
  );
}
