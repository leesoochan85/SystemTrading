import { useEffect, useState } from "react";

// Keep load stable (a module function or useCallback). Ignore results after cleanup.
export default function usePolling(load, intervalMs = 5000) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    let busy = false;
    const refresh = async () => {
      if (busy) return;
      busy = true;
      try {
        const result = await load();
        if (active) {
          setData(result);
          setError("");
        }
      } catch (e) {
        if (active) setError(e.message);
      } finally {
        busy = false;
      }
    };

    refresh();
    const timer = setInterval(refresh, intervalMs);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [load, intervalMs]);

  return { data, error, setData };
}
