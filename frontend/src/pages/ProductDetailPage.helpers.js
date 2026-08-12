function normalize(value) {
  return String(value || "").trim().toLowerCase();
}

export function formatVariantLabel(value) {
  const text = String(value || "").trim();
  if (!text) return "";
  if (/^[A-Z0-9]+$/.test(text)) return text;
  return text
    .split(/\s+/)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(" ");
}

export function formatVariantSize(value) {
  const text = String(value || "").trim();
  const sizeNames = {
    S: "Small",
    M: "Medium",
    L: "Large",
    XS: "Extra Small",
    XL: "Extra Large",
  };
  return sizeNames[text.toUpperCase()] || formatVariantLabel(text);
}

export function formatVariantCombination(selectedSize, selectedColor) {
  return [formatVariantLabel(selectedColor), formatVariantSize(selectedSize)]
    .filter(Boolean)
    .join(" / ");
}

export function getUniqueVariantValues(variants, field) {
  const seen = new Set();
  const values = [];
  for (const variant of variants || []) {
    const value = variant?.[field];
    const key = normalize(value);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    values.push(value);
  }
  return values;
}

export function findVariantStock(variants, selectedSize, selectedColor) {
  const requestedSize = normalize(selectedSize);
  const requestedColor = normalize(selectedColor);
  return (variants || []).find(
    (variant) =>
      normalize(variant.size) === requestedSize &&
      normalize(variant.color) === requestedColor
  ) || null;
}

export function getVariantAvailability(product, selectedSize, selectedColor, quantity = 1) {
  const variants = product?.variants || [];
  if (variants.length === 0) {
    return {
      requiresVariant: false,
      hasValidSelection: true,
      quantity: null,
      inStock: true,
      canAddToCart: quantity > 0,
      message: "In stock",
    };
  }

  const hasValidSelection = Boolean(selectedSize && selectedColor);
  if (!hasValidSelection) {
    return {
      requiresVariant: true,
      hasValidSelection: false,
      quantity: 0,
      inStock: false,
      canAddToCart: false,
      message: "Select size and color",
    };
  }

  const variant = findVariantStock(variants, selectedSize, selectedColor);
  const availableQuantity = variant?.quantity || 0;
  const inStock = availableQuantity > 0;
  const combination = formatVariantCombination(selectedSize, selectedColor);
  return {
    requiresVariant: true,
    hasValidSelection,
    quantity: availableQuantity,
    inStock,
    canAddToCart: inStock && quantity > 0 && quantity <= availableQuantity,
    message: inStock
      ? `${combination} · ${availableQuantity} available`
      : `${combination} is out of stock`,
  };
}

export function isVariantOptionUnavailable(product, field, value, selectedSize, selectedColor) {
  const variants = product?.variants || [];
  if (variants.length === 0) return false;

  const nextSize = field === "size" ? value : selectedSize;
  const nextColor = field === "color" ? value : selectedColor;
  if (!nextSize || !nextColor) return false;

  const variant = findVariantStock(variants, nextSize, nextColor);
  return !variant || variant.quantity <= 0;
}

export function isVariantOptionVisuallyUnavailable(
  product,
  field,
  value,
  selectedSize,
  selectedColor,
  contextField
) {
  return field === contextField
    && isVariantOptionUnavailable(product, field, value, selectedSize, selectedColor);
}
