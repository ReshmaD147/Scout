import { useParams } from "react-router-dom";
import ProductGrid from "../components/ProductGrid";

export default function CategoryPage() {
  const { category } = useParams();
  return (
    <div>
      <h2 style={{ fontSize: "16px", marginBottom: "16px", textTransform: "capitalize" }}>
        {category}
      </h2>
      <ProductGrid category={category} />
    </div>
  );
}
