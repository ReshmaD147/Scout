import { useSearchParams } from "react-router-dom";
import ProductGrid from "../components/ProductGrid";

export default function SearchResultsPage() {
  const [searchParams] = useSearchParams();
  const q = searchParams.get("q") || "";

  return (
    <div>
      <h2 style={{ fontSize: "16px", marginBottom: "16px" }}>
        Search results for "{q}"
      </h2>
      <ProductGrid query={q} />
    </div>
  );
}
