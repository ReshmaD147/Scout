import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getScoutImpact } from "../api/client";
import "./ImpactDashboardPage.css";

export default function ImpactDashboardPage() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [asOf, setAsOf] = useState(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const result = await getScoutImpact();
        if (!cancelled) {
          setData(result);
          setAsOf(new Date());
          setError(null);
        }
      } catch {
        if (!cancelled) {
          setError("Could not load Scout Impact data. Check that the backend is running.");
        }
      }
    }

    load();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="impact-page">
      <div className="impact-header">
        <span className="impact-eyebrow">Internal — Scout Impact</span>
        <h1>How much has Scout actually sold?</h1>
        <p className="impact-subtitle">
          Deterministic, backend-calculated revenue attribution. No AI is involved in
          producing these numbers — this is a direct SQL aggregation over completed orders
          where the purchase was validated back to a real Scout recommendation.
        </p>
      </div>

      {error && <div className="impact-error">{error}</div>}

      {!error && !data && <div className="impact-loading">Loading real data…</div>}

      {data && (
        <>
          <div className="impact-hero">
            <span className="impact-hero-label">Scout-assisted revenue</span>
            <span className="impact-hero-value">
              ${data.scout_assisted_revenue.toFixed(2)}
            </span>
            <span className="impact-hero-note">
              Sum of completed, paid order-item value where attribution was independently
              validated at cart-add time — not the originally recommended price.
            </span>
          </div>

          <div className="impact-secondary">
            <div className="impact-metric">
              <span className="impact-metric-value">{data.scout_assisted_orders}</span>
              <span className="impact-metric-label">Assisted orders</span>
            </div>
            <div className="impact-metric">
              <span className="impact-metric-value">{data.scout_attributed_items}</span>
              <span className="impact-metric-label">Attributed items</span>
            </div>
          </div>

          {asOf && (
            <p className="impact-freshness">
              Live as of {asOf.toLocaleTimeString()} — refresh to update
            </p>
          )}
        </>
      )}

      <Link to="/" className="impact-back-link">
        ← Back to storefront
      </Link>
    </div>
  );
}
