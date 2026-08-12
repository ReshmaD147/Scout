import { BrowserRouter, Routes, Route } from "react-router-dom";
import { CartProvider } from "./context/CartContext";
import { SavedItemsProvider } from "./context/SavedItemsContext";
import { ChatWidgetProvider } from "./context/ChatWidgetContext";
import Layout from "./components/Layout";
import HomePage from "./pages/HomePage";
import CategoryPage from "./pages/CategoryPage";
import CartPage from "./pages/CartPage";
import SearchResultsPage from "./pages/SearchResultsPage";
import SavedItemsPage from "./pages/SavedItemsPage";
import ProductDetailPage from "./pages/ProductDetailPage";
import "./styles/tokens.css";

function App() {
  return (
    <CartProvider>
      <SavedItemsProvider>
        <ChatWidgetProvider>
          <BrowserRouter>
            <Routes>
              <Route path="/" element={<Layout />}>
                <Route index element={<HomePage />} />
                <Route path="category/:category" element={<CategoryPage />} />
                <Route path="cart" element={<CartPage />} />
                <Route path="search" element={<SearchResultsPage />} />
                <Route path="saved" element={<SavedItemsPage />} />
                <Route path="product/:productId" element={<ProductDetailPage />} />
              </Route>
            </Routes>
          </BrowserRouter>
        </ChatWidgetProvider>
      </SavedItemsProvider>
    </CartProvider>
  );
}

export default App;
