import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from openai import OpenAI

from scout.db.session import SessionLocal
from scout.db.models import Product, ExternalProduct

load_dotenv()
client = OpenAI()

IMAGES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "product_images"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)


def _generate_and_save(product_id: str, name: str, description: str, category: str) -> str:
    prompt = (
        f"Professional e-commerce product photography of {name}, {category}. "
        f"{description} Clean white studio background, soft even lighting, "
        f"centered composition, no text, no watermark, realistic fabric texture."
    )

    response = client.images.generate(
        model="gpt-image-1-mini",
        prompt=prompt,
        size="1024x1024",
        n=1,
    )
    # gpt-image models return base64-encoded image data directly,
    # not a URL like the old DALL-E API did.
    import base64
    img_data = base64.b64decode(response.data[0].b64_json)
    file_path = IMAGES_DIR / f"{product_id}.png"
    file_path.write_bytes(img_data)

    return f"/static/products/{product_id}.png"


def generate_all_images(overwrite: bool = False) -> None:
    session = SessionLocal()
    try:
        products = session.query(Product).all()
        externals = session.query(ExternalProduct).all()

        total = len(products) + len(externals)
        done = 0

        for p in products:
            file_path = IMAGES_DIR / f"{p.product_id}.png"
            if file_path.exists() and not overwrite:
                print(f"[skip] {p.product_id} already has an image")
                done += 1
                continue
            try:
                url = _generate_and_save(p.product_id, p.name, p.description, p.category)
                p.image_url = url
                session.commit()
                done += 1
                print(f"[{done}/{total}] Generated: {p.name}")
            except Exception as e:
                print(f"[error] {p.product_id}: {e}")
            time.sleep(1)

        for ep in externals:
            file_path = IMAGES_DIR / f"{ep.external_product_id}.png"
            if file_path.exists() and not overwrite:
                print(f"[skip] {ep.external_product_id} already has an image")
                done += 1
                continue
            try:
                url = _generate_and_save(
                    ep.external_product_id, ep.name, ep.name, ep.category
                )
                ep.image_url = url
                session.commit()
                done += 1
                print(f"[{done}/{total}] Generated: {ep.name}")
            except Exception as e:
                print(f"[error] {ep.external_product_id}: {e}")
            time.sleep(1)

        print(f"\nDone: {done}/{total} images generated.")
    finally:
        session.close()


if __name__ == "__main__":
    generate_all_images(overwrite=False)
