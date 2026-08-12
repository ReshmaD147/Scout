import { createContext, useContext, useState } from "react";

const ChatWidgetContext = createContext(null);

export function ChatWidgetProvider({ children }) {
  const [isOpen, setIsOpen] = useState(false);
  const [prefillMessage, setPrefillMessage] = useState("");
  const [prefillNonce, setPrefillNonce] = useState(0);

  function openChatWithMessage(message) {
    setPrefillMessage(message);
    setPrefillNonce((n) => n + 1); // ensures ChatWidget reacts even if the same message is sent twice
    setIsOpen(true);
  }

  const value = {
    isOpen,
    setIsOpen,
    prefillMessage,
    prefillNonce,
    openChatWithMessage,
  };

  return (
    <ChatWidgetContext.Provider value={value}>{children}</ChatWidgetContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function useChatWidget() {
  const ctx = useContext(ChatWidgetContext);
  if (!ctx) {
    throw new Error("useChatWidget must be used within a ChatWidgetProvider");
  }
  return ctx;
}
