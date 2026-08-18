import argparse

from scout.db.seed import seed
from scout.db.update_image_urls import update_image_urls
from scout.rag.product_embeddings import build_product_embeddings
from scout.rag.vector_store import build_vector_store


def initialize_deployment_data(
    *,
    rebuild_embeddings: bool = False,
    strict_embeddings: bool = False,
    seed_func=seed,
    update_images_func=update_image_urls,
    build_policy_embeddings_func=build_vector_store,
    build_product_embeddings_func=build_product_embeddings,
) -> dict:
    """Initialize mutable app data for a fresh deployed database.

    The relational database seed and image URL update are deterministic and
    idempotent. Embeddings are optional because Railway deployments may bring up
    Postgres before the private Ollama embedding service is ready.
    """
    result = {
        "seeded": False,
        "image_urls_updated": False,
        "embeddings_rebuilt": False,
        "embedding_error": None,
    }

    seed_func()
    result["seeded"] = True

    update_images_func()
    result["image_urls_updated"] = True

    if not rebuild_embeddings:
        return result

    try:
        build_policy_embeddings_func()
        build_product_embeddings_func()
        result["embeddings_rebuilt"] = True
    except Exception as exc:
        result["embedding_error"] = str(exc)
        if strict_embeddings:
            raise

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize Scout deployment data.")
    parser.add_argument(
        "--rebuild-embeddings",
        action="store_true",
        help="Also rebuild Chroma policy/product embeddings using the configured Ollama service.",
    )
    parser.add_argument(
        "--strict-embeddings",
        action="store_true",
        help="Fail the command if embedding rebuild fails.",
    )
    args = parser.parse_args()

    result = initialize_deployment_data(
        rebuild_embeddings=args.rebuild_embeddings,
        strict_embeddings=args.strict_embeddings,
    )
    print("Deployment data initialized.")
    if result["embedding_error"]:
        print(f"Embedding rebuild skipped/failed: {result['embedding_error']}")


if __name__ == "__main__":
    main()
