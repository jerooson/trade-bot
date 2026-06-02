import { useCallback, useEffect, useState } from "react";

const STORAGE_KEY = "wtr.pinned-plans";

/**
 * Track which plan message IDs the user has pinned, persisted in localStorage.
 *
 * We store IDs as strings because JSON.parse loses Number precision for
 * 64-bit Discord IDs. The set of pinned ids is the source of truth; the
 * page composes the actual pinned plan objects by filtering the live list.
 */
export function usePinnedPlans() {
  const [pinned, setPinned] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return new Set();
      const arr = JSON.parse(raw);
      return new Set(Array.isArray(arr) ? arr.map(String) : []);
    } catch {
      return new Set();
    }
  });

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify([...pinned]));
    } catch {
      // localStorage quota or disabled -- silently degrade.
    }
  }, [pinned]);

  const isPinned = useCallback(
    (id: number | string | undefined) => (id == null ? false : pinned.has(String(id))),
    [pinned],
  );

  const toggle = useCallback((id: number | string) => {
    setPinned((prev) => {
      const next = new Set(prev);
      const key = String(id);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const clear = useCallback(() => setPinned(new Set()), []);

  return { pinned, isPinned, toggle, clear, count: pinned.size };
}
