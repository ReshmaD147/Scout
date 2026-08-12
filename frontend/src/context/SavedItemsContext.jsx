import { createContext, useContext, useReducer, useEffect } from "react";

const SavedItemsContext = createContext(null);
const STORAGE_KEY = "scout_saved_items";

function loadInitialState() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) {
      return JSON.parse(saved);
    }
  } catch (e) {
    console.warn("Failed to load saved items from localStorage:", e);
  }
  return { items: [] };
}

function savedItemsReducer(state, action) {
  switch (action.type) {
    case "TOGGLE_SAVE": {
      const key = action.product.product_id || action.product.external_product_id;
      const exists = state.items.some(
        (i) => (i.product_id || i.external_product_id) === key
      );
      if (exists) {
        return {
          items: state.items.filter(
            (i) => (i.product_id || i.external_product_id) !== key
          ),
        };
      }
      return { items: [...state.items, action.product] };
    }
    case "REMOVE": {
      return {
        items: state.items.filter(
          (i) => (i.product_id || i.external_product_id) !== action.key
        ),
      };
    }
    default:
      return state;
  }
}

export function SavedItemsProvider({ children }) {
  const [state, dispatch] = useReducer(savedItemsReducer, undefined, loadInitialState);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch (e) {
      console.warn("Failed to save items to localStorage:", e);
    }
  }, [state]);

  const toggleSave = (product) => dispatch({ type: "TOGGLE_SAVE", product });
  const removeSaved = (key) => dispatch({ type: "REMOVE", key });

  const isSaved = (product) => {
    const key = product.product_id || product.external_product_id;
    return state.items.some((i) => (i.product_id || i.external_product_id) === key);
  };

  const value = {
    items: state.items,
    toggleSave,
    removeSaved,
    isSaved,
    count: state.items.length,
  };

  return (
    <SavedItemsContext.Provider value={value}>{children}</SavedItemsContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function useSavedItems() {
  const ctx = useContext(SavedItemsContext);
  if (!ctx) {
    throw new Error("useSavedItems must be used within a SavedItemsProvider");
  }
  return ctx;
}
