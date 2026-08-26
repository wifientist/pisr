import { useState, useEffect } from "react";
import { useAuth } from "@/context/AuthContext";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api";

export function useSingleEc(controllerId: number | null) {
  const [ecData, setEcData] = useState<any[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!controllerId) {
      setLoading(false);
      return;
    }

    const controller = new AbortController();
    setLoading(true);
    setError(null);

    fetch(`${API_BASE_URL}/r1/${controllerId}/msp/mspEcs`, {
      method: "GET",
      credentials: "include",
      signal: controller.signal,
    })
      .then(async (res) => {
        if (!res.ok) {
          // The API reports upstream RUCKUS ONE failures in `error`; show that
          // rather than a bare status code.
          const detail = await res
            .json()
            .then((body) => body?.error)
            .catch(() => null);
          throw new Error(detail || `HTTP error ${res.status}`);
        }
        return res.json();
      })
      .then((json) => {
        // Handle both array and object responses
        if (Array.isArray(json)) {
          setEcData(json);
        } else if (json.data && Array.isArray(json.data)) {
          setEcData(json.data);
        } else {
          setEcData([]);
        }
        setLoading(false);
      })
      .catch((err) => {
        if (err.name !== "AbortError") {
          setError(err.message);
          setLoading(false);
        }
      });

    return () => controller.abort();
  }, [controllerId]);

  return { ecData, loading, error };
}
