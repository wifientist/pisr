import { AuthProvider } from "@/context/AuthContext";
import PISR from "@/pages/PISR";

/**
 * No router: PISR is the only page. rtools2 wrapped it in a Layout with a
 * sidebar, a toolbar and an AlphaRoute guard, none of which mean anything when
 * the app is one tool pointed at one tenant.
 */
export default function App() {
  return (
    <AuthProvider>
      <div className="min-h-screen bg-gray-50">
        <PISR />
      </div>
    </AuthProvider>
  );
}
