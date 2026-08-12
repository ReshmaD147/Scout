const EXACT_VARIANT_OUT_OF_STOCK_PATTERN =
  /^The (?<productName>.+?) is out of stock in (?<color>[^,]+), size (?<size>[A-Za-z0-9]+)\./i;

const BUDGET_PATTERN = /\b(?:under|below|less than|up to)\s*\$?(?<amount>\d+(?:\.\d{1,2})?)\b/i;

export function buildInventoryQuickActions(reply, messages = []) {
  const match = String(reply || "").match(EXACT_VARIANT_OUT_OF_STOCK_PATTERN);
  if (!match?.groups) return [];

  const productName = match.groups.productName.trim();
  const color = match.groups.color.trim();
  const size = match.groups.size.trim().toUpperCase();
  const variantText = `${productName} in ${color}, size ${size}`;
  const constraints = findRecentShoppingConstraints(messages);

  return [
    `Check nearby stores for ${variantText}`,
    `Check online or delivery availability for ${variantText}`,
    `Find similar ${constraints || `products like ${productName}`}`,
  ];
}

export function findRecentShoppingConstraints(messages = []) {
  let recentCategory = "";
  let recentBudget = "";
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message?.role !== "user") continue;
    const content = String(message.content || "");
    recentCategory ||= categoryFromMessage(content);
    recentBudget ||= budgetFromMessage(content);
    if (recentCategory && recentBudget) return `${recentCategory} ${recentBudget}`;
  }
  return recentCategory || "";
}

function categoryFromMessage(message) {
  const normalized = message.toLowerCase();
  if (normalized.includes("hiking shoe")) return "hiking shoes";
  if (normalized.includes("shoe")) return "shoes";
  if (normalized.includes("cocktail dress")) return "cocktail dresses";
  if (normalized.includes("black") && normalized.includes("dress")) return "black dresses";
  if (normalized.includes("dress")) return "dresses";
  if (normalized.includes("boot")) return "boots";
  return "";
}

function budgetFromMessage(message) {
  const match = message.match(BUDGET_PATTERN);
  return match?.groups?.amount ? `under $${Number(match.groups.amount).toFixed(0)}` : "";
}
