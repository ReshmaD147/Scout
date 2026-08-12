import { strict as assert } from "node:assert";

import {
  formatVariantLabel,
  findVariantStock,
  getUniqueVariantValues,
  getVariantAvailability,
  isVariantOptionUnavailable,
  isVariantOptionVisuallyUnavailable,
} from "../src/pages/ProductDetailPage.helpers.js";

const blackDress = {
  product_id: "P001",
  variants: [
    { size: "S", color: "black", quantity: 3, in_stock: true },
    { size: "M", color: "black", quantity: 0, in_stock: false },
    { size: "L", color: "black", quantity: 3, in_stock: true },
    { size: "M", color: "floral", quantity: 2, in_stock: true },
  ],
};

assert.equal(formatVariantLabel("black"), "Black");
assert.equal(formatVariantLabel("floral"), "Floral");
assert.equal(formatVariantLabel("M"), "M");

assert.deepEqual(getUniqueVariantValues(blackDress.variants, "size"), ["S", "M", "L"]);
assert.deepEqual(getUniqueVariantValues(blackDress.variants, "color"), ["black", "floral"]);

assert.equal(findVariantStock(blackDress.variants, "M", "black").quantity, 0);
assert.equal(findVariantStock(blackDress.variants, "L", "black").quantity, 3);

assert.deepEqual(getVariantAvailability(blackDress, "S", "black", 1), {
  requiresVariant: true,
  hasValidSelection: true,
  quantity: 3,
  inStock: true,
  canAddToCart: true,
  message: "Black / Small · 3 available",
});

assert.deepEqual(getVariantAvailability(blackDress, "M", "black", 1), {
  requiresVariant: true,
  hasValidSelection: true,
  quantity: 0,
  inStock: false,
  canAddToCart: false,
  message: "Black / Medium is out of stock",
});

assert.equal(getVariantAvailability(blackDress, "L", "black", 4).canAddToCart, false);
const blackSelectedSizeStates = ["S", "M", "L"].map((size) => ({
  size,
  unavailable: isVariantOptionUnavailable(blackDress, "size", size, "M", "black"),
}));
assert.deepEqual(blackSelectedSizeStates, [
  { size: "S", unavailable: false },
  { size: "M", unavailable: true },
  { size: "L", unavailable: false },
]);
assert.equal(isVariantOptionUnavailable(blackDress, "size", "L", "M", "black"), false);
assert.equal(isVariantOptionUnavailable(blackDress, "color", "black", "M", "floral"), true);
assert.equal(isVariantOptionUnavailable(blackDress, "color", "floral", "M", "black"), false);

const mediumSelectedColorStates = ["black", "floral"].map((color) => ({
  color,
  unavailable: isVariantOptionUnavailable(blackDress, "color", color, "M", "black"),
}));
assert.deepEqual(mediumSelectedColorStates, [
  { color: "black", unavailable: true },
  { color: "floral", unavailable: false },
]);

assert.equal(
  isVariantOptionVisuallyUnavailable(blackDress, "size", "M", "M", "black", "size"),
  true
);
assert.equal(
  isVariantOptionVisuallyUnavailable(blackDress, "color", "black", "M", "black", "size"),
  false
);
assert.equal(
  isVariantOptionVisuallyUnavailable(blackDress, "color", "black", "M", "black", "color"),
  true
);
assert.equal(
  isVariantOptionVisuallyUnavailable(blackDress, "size", "M", "M", "black", "color"),
  false
);

assert.deepEqual(getVariantAvailability({ product_id: "P026", variants: [] }, "", "", 1), {
  requiresVariant: false,
  hasValidSelection: true,
  quantity: null,
  inStock: true,
  canAddToCart: true,
  message: "In stock",
});

console.log("ProductDetailPage helper regressions passed");
