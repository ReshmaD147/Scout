import { createContext, useContext, useState } from "react";

const AuthContext = createContext(null);
const BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

export function AuthProvider({ children }) {
  const [customerId, setCustomerId] = useState(null);
  const [customerName, setCustomerName] = useState(null);
  const [sessionId, setSessionId] = useState(null);
  const [signingIn, setSigningIn] = useState(false);

  async function signInAsDemoCustomer(demoCustomerId = "C001") {
    setSigningIn(true);
    try {
      const response = await fetch(`${BASE_URL}/demo-auth/sign-in`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ customer_id: demoCustomerId }),
      });
      if (!response.ok) {
        throw new Error(`Demo sign-in failed: ${response.status}`);
      }
      const data = await response.json();
      setCustomerId(demoCustomerId);
      setCustomerName(data.customer_name || null);
      setSessionId(data.session_id);
      return data.session_id;
    } finally {
      setSigningIn(false);
    }
  }

  function signOut() {
    setCustomerId(null);
    setCustomerName(null);
    setSessionId(null);
  }

  const value = {
    customerId,
    customerName,
    sessionId,
    signingIn,
    isSignedIn: Boolean(customerId),
    signInAsDemoCustomer,
    signOut,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return ctx;
}
