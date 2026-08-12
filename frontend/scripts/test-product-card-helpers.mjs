import assert from "node:assert/strict";
import {
  formatCurrency,
  getAvailabilityPresentation,
  getImagePresentation,
  getPromotionPresentation,
  resolveImageUrl,
} from "../src/components/ProductCard.helpers.js";

const product = Object.freeze({
  product_id: "P003",
  name: "Wrap Dress",
  price: 68,
  image_url: "/static/products/wrap.jpg",
  promotion: Object.freeze({ discounted_price: 57.8 }),
});

assert.equal(formatCurrency(68), "$68.00");
assert.equal(resolveImageUrl("/static/products/wrap.jpg"), "http://127.0.0.1:8000/static/products/wrap.jpg");
assert.deepEqual(getPromotionPresentation(product), {
  hasPromotion: true,
  badgeText: "15% OFF",
  originalPrice: 68,
  salePrice: 57.8,
});
assert.equal(getPromotionPresentation({ price: 68, promotion: { discounted_price: 60 } }).badgeText, "12% OFF");
assert.equal(getPromotionPresentation({ price: 68, promotion: { label: "Sale" } }).badgeText, "Sale");
assert.deepEqual(getPromotionPresentation({ price: 68 }), {
  hasPromotion: false,
  badgeText: null,
  originalPrice: null,
  salePrice: null,
});
assert.deepEqual(getPromotionPresentation({ price: 68, promotion: { discounted_price: 68 } }), {
  hasPromotion: true,
  badgeText: "Sale",
  originalPrice: null,
  salePrice: null,
});
assert.equal(getPromotionPresentation({ price: 68, promotion: { discounted_price: "bad" } }).badgeText, "Sale");
assert.deepEqual(getImagePresentation(product, false), {
  imageUrl: "http://127.0.0.1:8000/static/products/wrap.jpg",
  altText: "Wrap Dress",
  showPlaceholder: false,
  placeholderAltText: "Image unavailable for Wrap Dress",
});
assert.deepEqual(getImagePresentation(product, true), {
  imageUrl: "",
  altText: "Wrap Dress",
  showPlaceholder: true,
  placeholderAltText: "Image unavailable for Wrap Dress",
});
assert.deepEqual(getAvailabilityPresentation(product), {
  text: "Check availability",
  status: "unchecked",
});
assert.deepEqual(getAvailabilityPresentation({ ...product, in_stock: true, store_name: "Maple Grove" }), {
  text: "In stock at Maple Grove",
  status: "available",
});
assert.deepEqual(getAvailabilityPresentation({ ...product, in_stock: false, color: "black", size: "M" }), {
  text: "Out of stock in black, M",
  status: "unavailable",
});
assert.deepEqual(getAvailabilityPresentation({ ...product, availability: "out_of_stock" }), {
  text: "Out of stock",
  status: "unavailable",
});
assert.deepEqual(product, {
  product_id: "P003",
  name: "Wrap Dress",
  price: 68,
  image_url: "/static/products/wrap.jpg",
  promotion: { discounted_price: 57.8 },
});

console.log("ProductCard helper regressions passed");
