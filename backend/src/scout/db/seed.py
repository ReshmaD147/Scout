import itertools

from scout.db.session import SessionLocal, init_db
from scout.db.models import Product, Stock, Store, StoreStock, Order, OrderItem, ExternalProduct, Promotion

# ── PRODUCTS ──────────────────────────────────────────────────────────────
# (product_id, name, brand, department, category, description, price, rating, tags)
PRODUCTS = [
    ("P001", "Black Midi Dress", "Aria & Co", "Women", "dresses",
     "A versatile black midi dress, perfect for evening wear.", 79.99, 4.3, "black,formal,party,evening"),
    ("P002", "Floral Sundress", "Solstice", "Women", "dresses",
     "Lightweight floral sundress for warm weather.", 54.99, 4.1, "floral,summer,casual"),
    ("P003", "Wrap Dress", "Aria & Co", "Women", "dresses",
     "Flattering wrap dress suitable for the office or evenings out.", 68.00, 4.4, "wrap,workwear,office"),
    ("P004", "Slip Dress", "Meridian", "Women", "dresses",
     "Satin slip dress with a relaxed, elegant drape.", 62.50, 3.9, "satin,evening,party"),
    ("P005", "Running Shoes", "Cascade", "Unisex", "shoes",
     "Comfortable everyday running shoes.", 64.99, 4.5, "running,athletic,lightweight"),
    ("P006", "Leather Sneakers", "Birchwood", "Unisex", "shoes",
     "Casual leather sneakers, great for walking.", 89.99, 4.2, "casual,leather,walking"),
    ("P007", "Ankle Boots", "Fieldstone", "Women", "shoes",
     "Versatile ankle boots for fall and winter.", 94.99, 4.0, "boots,fall,casual"),
    ("P008", "Hiking Boots", "Nordic Trail", "Unisex", "shoes",
     "Waterproof hiking boots built for the trail.", 110.00, 4.6, "hiking,waterproof,outdoor"),
    ("P009", "Canvas Slip-Ons", "Lumen", "Unisex", "shoes",
     "Lightweight canvas slip-ons for summer.", 39.99, 3.8, "casual,summer,lightweight"),
    ("P010", "Denim Jacket", "Pierce Denim", "Unisex", "outerwear",
     "Classic denim jacket, unisex fit.", 69.99, 4.3, "denim,casual,layering"),
    ("P011", "Wool Peacoat", "Birchwood", "Women", "outerwear",
     "Warm wool peacoat with a tailored silhouette.", 149.99, 4.7, "winter,formal,warm"),
    ("P012", "Puffer Jacket", "Nordic Trail", "Unisex", "outerwear",
     "Insulated puffer jacket for cold weather.", 119.99, 4.4, "winter,warm,outdoor"),
    ("P013", "Trench Coat", "Meridian", "Women", "outerwear",
     "Classic trench coat, water resistant.", 134.99, 4.2, "rain,formal,classic"),
    ("P014", "Bomber Jacket", "Urban Thread", "Men", "outerwear",
     "Streetwear-inspired bomber jacket.", 84.99, 4.0, "casual,streetwear"),
    ("P015", "Cotton T-Shirt", "Urban Thread", "Unisex", "tops",
     "Soft cotton tee for everyday wear.", 19.99, 4.1, "casual,basic,everyday"),
    ("P016", "Silk Blouse", "Aria & Co", "Women", "tops",
     "Elegant silk blouse for the office.", 58.00, 4.3, "office,formal,silk"),
    ("P017", "Flannel Shirt", "Fieldstone", "Men", "tops",
     "Cozy plaid flannel shirt.", 44.99, 4.0, "casual,fall,plaid"),
    ("P018", "Cashmere Sweater", "Meridian", "Women", "tops",
     "Luxuriously soft cashmere sweater.", 98.00, 4.6, "winter,cozy,luxury"),
    ("P019", "Graphic Tee", "Urban Thread", "Unisex", "tops",
     "Bold graphic print tee.", 24.99, 3.7, "casual,streetwear,summer"),
    ("P020", "Polo Shirt", "Pierce Denim", "Men", "tops",
     "Classic fit polo shirt.", 39.99, 4.1, "casual,golf,summer"),
    ("P021", "Skinny Jeans", "Pierce Denim", "Unisex", "bottoms",
     "Stretch skinny jeans for everyday wear.", 58.99, 4.2, "denim,casual,everyday"),
    ("P022", "Wide-Leg Trousers", "Aria & Co", "Women", "bottoms",
     "Tailored wide-leg trousers.", 72.00, 4.0, "office,formal"),
    ("P023", "Chino Shorts", "Urban Thread", "Men", "bottoms",
     "Lightweight chino shorts for summer.", 34.99, 3.9, "summer,casual"),
    ("P024", "Pleated Skirt", "Solstice", "Women", "bottoms",
     "A-line pleated skirt.", 49.99, 4.1, "office,casual"),
    ("P025", "Joggers", "Cascade", "Unisex", "bottoms",
     "Comfortable athletic joggers.", 44.99, 4.3, "athletic,casual,comfort"),
    ("P026", "Leather Belt", "Birchwood", "Unisex", "accessories",
     "Classic full-grain leather belt.", 29.99, 4.2, "leather,classic"),
    ("P027", "Wool Scarf", "Meridian", "Unisex", "accessories",
     "Soft wool scarf for winter.", 24.99, 4.4, "winter,cozy"),
    ("P028", "Sunglasses", "Lumen", "Unisex", "accessories",
     "UV-protective sunglasses.", 54.99, 4.0, "summer,casual"),
    ("P029", "Crossbody Bag", "Aria & Co", "Women", "accessories",
     "Compact crossbody bag for everyday errands.", 89.99, 4.5, "formal,everyday"),
    ("P030", "Baseball Cap", "Urban Thread", "Unisex", "accessories",
     "Adjustable cotton baseball cap.", 22.99, 3.9, "casual,summer,streetwear"),
]

SIZE_MAP = {
    "dresses": ["S", "M", "L"],
    "tops": ["S", "M", "L"],
    "bottoms": ["S", "M", "L"],
    "shoes": ["8", "9", "10"],
    "outerwear": ["S", "M", "L"],
    "accessories": ["One Size"],
}
COLOR_MAP = {
    "dresses": ["black", "floral", "navy"],
    "tops": ["white", "black", "gray"],
    "bottoms": ["blue", "black", "khaki"],
    "shoes": ["white", "black", "brown"],
    "outerwear": ["blue", "black", "olive"],
    "accessories": ["brown", "black"],
}


def _ensure_stock(session, product_id: str, size: str, color: str, quantity: int) -> None:
    rows = session.query(Stock).filter_by(
        product_id=product_id,
        size=size,
        color=color,
    ).all()
    if not rows:
        session.add(Stock(product_id=product_id, size=size, color=color, quantity=quantity))
        return
    for row in rows:
        row.quantity = quantity


def _ensure_demo_variant_inventory(session) -> None:
    # Preserve the canonical demo case while keeping variant inventory data
    # internally consistent for product-detail/cart validation.
    _ensure_stock(session, product_id="P001", size="S", color="black", quantity=3)
    _ensure_stock(session, product_id="P001", size="M", color="black", quantity=0)
    _ensure_stock(session, product_id="P001", size="L", color="black", quantity=3)


def seed() -> None:
    init_db()
    session = SessionLocal()

    if session.query(Product).first():
        _ensure_demo_variant_inventory(session)
        session.commit()
        print("Database already seeded — skipping.")
        session.close()
        return

    # ── products ──
    for pid, name, brand, dept, category, desc, price, rating, tags in PRODUCTS:
        session.add(Product(
            product_id=pid, name=name, brand=brand, department=dept,
            category=category, description=desc, price=price, rating=rating,
            image_url=f"https://picsum.photos/seed/{pid}/400/500", tags=tags,
        ))

    # ── stock (2 size/color variants per product, deterministic) ──
    qty_cycle = itertools.cycle([0, 3, 6, 10, 4, 8])
    for pid, _, _, _, category, *_ in PRODUCTS:
        sizes = SIZE_MAP[category]
        colors = COLOR_MAP[category]
        for size, color in zip(sizes[:2], colors[:2]):
            session.add(Stock(product_id=pid, size=size, color=color, quantity=next(qty_cycle)))

    # preserve known test case: Black Midi Dress, size M, black = OUT OF STOCK
    _ensure_demo_variant_inventory(session)

    # ── stores ──
    stores = [
        Store(store_id="S01", name="Maple Grove", address="123 Main St, Maple Grove, MN"),
        Store(store_id="S02", name="Downtown Minneapolis", address="500 Nicollet Mall, Minneapolis, MN"),
        Store(store_id="S03", name="Ridgedale", address="12401 Wayzata Blvd, Minnetonka, MN"),
    ]
    session.add_all(stores)

    # ── store stock (first 10 products stocked across 3 stores) ──
    store_qty_cycle = itertools.cycle([2, 0, 5, 3, 7])
    for store_id in ["S01", "S02", "S03"]:
        for pid, *_ in PRODUCTS[:10]:
            session.add(StoreStock(store_id=store_id, product_id=pid, quantity=next(store_qty_cycle)))

    # preserve known test case: Black Midi Dress at Maple Grove = qty 2
    session.query(StoreStock).filter_by(store_id="S01", product_id="P001").delete()
    session.add(StoreStock(store_id="S01", product_id="P001", quantity=2))

    # ── orders ──
    orders = [
        Order(order_id="O1001", customer_id="C001", status="shipped"),
        Order(order_id="O1002", customer_id="C002", status="processing"),
        Order(order_id="O1003", customer_id="C001", status="delivered"),
    ]
    session.add_all(orders)

    order_items = [
        OrderItem(order_id="O1001", product_id="P005", quantity=1, price_at_purchase=64.99),
        OrderItem(order_id="O1002", product_id="P011", quantity=1, price_at_purchase=149.99),
        OrderItem(order_id="O1003", product_id="P015", quantity=2, price_at_purchase=19.99),
        OrderItem(order_id="O1003", product_id="P021", quantity=1, price_at_purchase=58.99),
    ]
    session.add_all(order_items)

    seed_external_products(session)
    seed_promotions(session)

    session.commit()
    session.close()
    print(f"Database seeded: {len(PRODUCTS)} products, {len(stores)} stores, {len(orders)} orders.")




# ── MOCK EXTERNAL VENDOR CATALOG ──────────────────────────────────────────
# In production this would come from a real affiliate network API, not be
# stored locally. affiliate_link_template points at our own click-tracking
# redirect route — see api/affiliate.py.
import urllib.parse

def _real_vendor_link(domain: str, product_name: str) -> str:
    """Points at a real, working page: a Google search restricted to the
    vendor's actual site (site:domain). This avoids depending on each
    retailer's internal search-URL syntax, which changes without notice
    and can't be reliably verified as current. In production, this would
    be replaced by a real affiliate network's tracking link."""
    query = urllib.parse.quote_plus(f"site:{domain} {product_name}")
    return f"https://www.google.com/search?q={query}"


EXTERNAL_PRODUCTS = [
    # Verified real, live category pages (real products, not pinned to one SKU
    # — matches how a live affiliate feed would surface current in-stock items):
    ("EX003", "Running Shoe", "Zappos", "shoes", 69.00, 4.4,
     "use:running,attr:athletic,waterproof:false,availability:in_stock,subcategory:running shoes",
     "https://www.zappos.com/mens-running-shoes"),

    # Synthetic/demo structured external offer for deterministic validation.
    ("EX010", "TrailGuard Waterproof Hiking Shoe", "Outdoor Demo Retailer", "shoes", 64.99, 4.4,
     "use:hiking,attr:waterproof,attr:trail,waterproof:true,color:black,availability:in_stock,subcategory:hiking shoes",
     "https://example.com/outdoor-demo/trailguard-waterproof-hiking-shoe"),

    # Verified real, specific, currently-live, IN-STOCK product page:
    ("EX002", "Florine Dress", "Shopbop", "dresses", 218.50, 4.0,
     "floral,summer,casual",
     "https://www.shopbop.com/florine-sundress/vp/v=1/1559901421.htm"),
    ("EX004", "Leather Casual Sneaker", "DSW", "shoes", 84.00, 4.1,
     "casual,leather", _real_vendor_link("dsw.com", "leather sneaker")),
    ("EX005", "Classic Denim Jacket", "Macy's", "outerwear", 64.00, 4.3,
     "denim,casual", _real_vendor_link("macys.com", "denim jacket")),
    ("EX006", "Wool Blend Peacoat", "Nordstrom", "outerwear", 139.00, 4.5,
     "winter,formal", _real_vendor_link("nordstrom.com", "wool peacoat")),
    ("EX007", "Soft Cotton Tee", "Gap", "tops", 18.00, 4.0,
     "casual,basic", _real_vendor_link("gap.com", "cotton tee")),
    ("EX008", "Stretch Skinny Jean", "Old Navy", "bottoms", 44.00, 4.1,
     "denim,casual", _real_vendor_link("oldnavy.gap.com", "skinny jean")),

    # Verified real, specific, currently-live product page:
    ("EX011", "Enid Satin Body-Con Evening Dress", "Nordstrom Rack", "dresses", 35.98, 4.1,
     "red,cocktail,party,evening",
     "https://www.nordstromrack.com/s/petal-and-pup-enid-satin-body-con-evening-dress/8336558",
     "https://n.nordstrommedia.com/it/e6ad6561-d8a8-4c3f-82c5-d10f5bf0c02d.jpeg?h=368&w=240&dpr=2"),

    # Verified real, specific, currently in-stock product page:
    ("EX009", "Twist-Front Midi Dress", "Target", "dresses", 45.00, 3.4,
     "casual,everyday",
     "https://www.target.com/p/women-39-s-twist-front-midi-dress-a-new-day-8482-blue-gingham-check-xs/-/A-95300205",
     "https://target.scene7.com/is/image/Target/GUEST_ed571594-9abb-45e3-b052-0aee6e49a634?wid=600&hei=600&qlt=80&fmt=pjpeg"),
]


def seed_external_products(session) -> None:
    if session.query(ExternalProduct).first():
        return
    for external_product in EXTERNAL_PRODUCTS:
        ex_id, name, vendor, category, price, rating, tags, link, *image_values = external_product
        image_url = image_values[0] if image_values else ""
        session.add(ExternalProduct(
            external_product_id=ex_id, name=name, vendor_name=vendor,
            category=category, price=price, rating=rating, tags=tags,
            image_url=image_url,
            affiliate_link_template=link,
        ))
    session.commit()




# ── PROMOTIONS ─────────────────────────────────────────────────────────────
def seed_promotions(session) -> None:
    if session.query(Promotion).first():
        return
    session.add_all([
        Promotion(category="dresses", discount_percent=15.0, label="Dress Sale", active=True),
        Promotion(product_id="P008", discount_percent=20.0, label="Hiking Boots Clearance", active=True),
    ])
    session.commit()


if __name__ == "__main__":
    seed()
