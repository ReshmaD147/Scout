from pathlib import Path

from scout.db.session import SessionLocal
from scout.db.models import Product, ExternalProduct

IMAGES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "product_images"
VALID_EXTENSIONS = [".png", ".jpg", ".jpeg", ".webp"]


def find_image_file(product_id: str) -> str | None:
    for ext in VALID_EXTENSIONS:
        candidate = IMAGES_DIR / f"{product_id}{ext}"
        if candidate.exists():
            return candidate.name
    return None


def update_image_urls():
    session = SessionLocal()
    updated = 0
    try:
        for product in session.query(Product).all():
            filename = find_image_file(product.product_id)
            if filename:
                product.image_url = f"/static/products/{filename}"
                updated += 1

        for ep in session.query(ExternalProduct).all():
            filename = find_image_file(ep.external_product_id)
            if filename:
                ep.image_url = f"/static/products/{filename}"
                updated += 1

        session.commit()
        print(f"Updated {updated} product image URLs.")
    finally:
        session.close()


if __name__ == "__main__":
    update_image_urls()
