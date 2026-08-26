import {
  createContext, useContext, useEffect, useState, type ReactNode,
} from "react";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api";

export interface ControllerRow {
  id: number;
  name: string;
  controller_type: string;
  controller_subtype: string | null;
  r1_tenant_id: string | null;
  r1_region: string | null;
}

interface AuthContextType {
  activeControllerId: number | null;
  activeControllerName: string | null;
  activeControllerType: string | null;
  activeControllerSubtype: string | null;
  controllers: ControllerRow[];
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export const useAuth = () => {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
};

/**
 * There is no authentication in standalone PISR.
 *
 * This shim exists so that PISR.tsx can be carried across from rtools2
 * unmodified. There, it read its tenant identity out of a login session via
 * useAuth(); here the same hook is backed by .env, fetched once from
 * /api/config. The five keys below are exactly what PISR.tsx destructures.
 *
 * Do not replace this with build-time VITE_* variables. Reading it at runtime
 * is what lets one built image serve any tenant, and what makes switching
 * R1_EC_TYPE a container restart instead of a rebuild.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [controller, setController] = useState<ControllerRow | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE_URL}/config`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((row: ControllerRow) => { if (!cancelled) setController(row); })
      .catch((e: Error) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, []);

  // Gate rendering on the fetch. PISR.tsx shows a "pick a RUCKUS ONE
  // controller" panel whenever activeControllerId is null, and without this
  // gate that panel flashes for one frame on every page load.
  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center p-8">
        <div className="max-w-lg rounded-lg border border-red-200 bg-red-50 p-6">
          <h1 className="font-semibold text-red-800">
            Could not read the PISR configuration
          </h1>
          <p className="mt-2 text-sm text-red-700">{error}</p>
          <p className="mt-3 text-sm text-red-700">
            The API did not answer <code>/api/config</code>. If the container
            just started, check its logs — a missing or invalid value in{" "}
            <code>.env</code> stops it from booting at all.
          </p>
        </div>
      </div>
    );
  }

  if (!controller) {
    return (
      <div className="min-h-screen flex items-center justify-center p-8 text-gray-500">
        Loading configuration…
      </div>
    );
  }

  return (
    <AuthContext.Provider
      value={{
        activeControllerId: controller.id,
        activeControllerName: controller.name,
        activeControllerType: controller.controller_type,
        activeControllerSubtype: controller.controller_subtype,
        controllers: [controller],
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}
