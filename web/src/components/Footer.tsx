export function Footer() {
  return (
    <footer className="mt-12 border-t border-ink-500/40 pb-10 pt-6">
      <div className="mx-auto flex max-w-[1600px] items-end justify-between px-6">
        <div>
          <div className="text-[10px] uppercase tracking-[0.32em] text-bone-500">
            instrument
          </div>
          <div className="mt-1 font-editorial text-xl italic text-bone-300">
            Will-the-Rocket signal terminal
          </div>
          <div className="mt-2 max-w-md text-[11px] uppercase tracking-[0.18em] text-bone-500">
            built for personal review · not a trade execution surface · live data
            forwarded from local discord listener
          </div>
        </div>
        <div className="text-right">
          <div className="text-[10px] uppercase tracking-[0.32em] text-bone-500">
            local
          </div>
          <div className="tabular mt-1 text-base text-bone-300">
            127.0.0.1 :: 8787 / 5173
          </div>
        </div>
      </div>
    </footer>
  );
}
