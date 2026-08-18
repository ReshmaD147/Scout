const BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

export async function sendChatMessage(message, sessionId = null) {
  const response = await fetch(`${BASE_URL}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, session_id: sessionId }),
  });

  if (!response.ok) {
    throw new Error(`Chat request failed: ${response.status}`);
  }

  return response.json();
}

export async function streamChatMessage(message, sessionId, callbacks = {}) {
  const { onSession, onToken, onProgress, onDone, onError } = callbacks;

  const response = await fetch(`${BASE_URL}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, session_id: sessionId }),
  });

  if (!response.ok || !response.body) {
    onError?.(new Error(`Stream request failed: ${response.status}`));
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    const events = buffer.split(/\r\n\r\n|\n\n/);
    buffer = events.pop();

    for (const rawEvent of events) {
      const eventMatch = rawEvent.match(/^event:\s*(.+?)\r?$/m);
      const dataMatch = rawEvent.match(/^data:\s*(.*?)\r?$/m);
      const eventType = eventMatch?.[1]?.trim();
      const data = dataMatch?.[1] ?? "";

      if (eventType === "session") {
        onSession?.(data);
      } else if (eventType === "progress") {
        onProgress?.(data);
      } else if (eventType === "token") {
        onToken?.(data);
      } else if (eventType === "error") {
        onError?.(new Error(data));
      } else if (eventType === "done") {
        try {
          const parsed = JSON.parse(data);
          onDone?.(parsed.reply, parsed.products || []);
        } catch {
          onDone?.(data, []);
        }
      }
    }
  }
}

export async function sendChatFeedback(feedback) {
  const response = await fetch(`${BASE_URL}/chat/feedback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(feedback),
  });

  if (!response.ok) {
    throw new Error(`Feedback request failed: ${response.status}`);
  }

  return response.json();
}

export async function addToCartRequest(productId, quantity = 1, options = {}) {
  const payload = {
    product_id: productId,
    quantity,
  };
  if (options.size) payload.size = options.size;
  if (options.color) payload.color = options.color;
  // Real bug fix: recommendation_id was being silently dropped here,
  // breaking attribution for every cart-add that carried one (e.g. the
  // "Add to Cart" button on chat product cards).
  if (options.recommendation_id) payload.recommendation_id = options.recommendation_id;

  const response = await fetch(`${BASE_URL}/cart/add`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    throw new Error(`Add to cart request failed: ${response.status}`);
  }

  return response.json();
}

export async function getScoutImpact() {
  const response = await fetch(`${BASE_URL}/analytics/scout-attributed-revenue`);

  if (!response.ok) {
    throw new Error(`Scout impact request failed: ${response.status}`);
  }

  return response.json();
}
