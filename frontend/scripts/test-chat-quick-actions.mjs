import { strict as assert } from "node:assert";

import {
  buildInventoryQuickActions,
  findRecentShoppingConstraints,
} from "../src/components/ChatWidget.helpers.js";

const conversation = [
  { role: "user", content: "Recommend a dress under $80" },
  {
    role: "assistant",
    content: "I found 3 Scout dresses.",
    products: [{ name: "Wrap Dress" }, { name: "Black Midi Dress" }],
  },
  { role: "user", content: "Is the black midi dress in a medium?" },
];

assert.equal(findRecentShoppingConstraints(conversation), "black dresses under $80");

assert.deepEqual(
  buildInventoryQuickActions(
    "The Black Midi Dress is out of stock in black, size M. I can check nearby stores, check online or delivery availability, or find similar products.",
    conversation
  ),
  [
    "Check nearby stores for Black Midi Dress in black, size M",
    "Check online or delivery availability for Black Midi Dress in black, size M",
    "Find similar black dresses under $80",
  ]
);

assert.deepEqual(
  buildInventoryQuickActions("The Black Midi Dress is available in black, size S.", conversation),
  []
);

assert.deepEqual(
  buildInventoryQuickActions(
    "The Black Midi Dress is out of stock in black, size M.",
    [{ role: "user", content: "Is the black midi dress in a medium?" }]
  ),
  [
    "Check nearby stores for Black Midi Dress in black, size M",
    "Check online or delivery availability for Black Midi Dress in black, size M",
    "Find similar black dresses",
  ]
);

console.log("Chat quick action helper regressions passed");
