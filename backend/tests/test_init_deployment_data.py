import pytest

from scout.db.init_deployment_data import initialize_deployment_data


def test_initialize_deployment_data_seeds_and_updates_images_only_by_default():
    calls = []

    result = initialize_deployment_data(
        seed_func=lambda: calls.append("seed"),
        update_images_func=lambda: calls.append("images"),
        build_policy_embeddings_func=lambda: calls.append("policy_embeddings"),
        build_product_embeddings_func=lambda: calls.append("product_embeddings"),
    )

    assert calls == ["seed", "images"]
    assert result == {
        "seeded": True,
        "image_urls_updated": True,
        "embeddings_rebuilt": False,
        "embedding_error": None,
    }


def test_initialize_deployment_data_can_rebuild_embeddings():
    calls = []

    result = initialize_deployment_data(
        rebuild_embeddings=True,
        seed_func=lambda: calls.append("seed"),
        update_images_func=lambda: calls.append("images"),
        build_policy_embeddings_func=lambda: calls.append("policy_embeddings"),
        build_product_embeddings_func=lambda: calls.append("product_embeddings"),
    )

    assert calls == ["seed", "images", "policy_embeddings", "product_embeddings"]
    assert result["embeddings_rebuilt"] is True
    assert result["embedding_error"] is None


def test_initialize_deployment_data_allows_non_strict_embedding_failure():
    def fail_embeddings():
        raise RuntimeError("ollama unavailable")

    result = initialize_deployment_data(
        rebuild_embeddings=True,
        strict_embeddings=False,
        seed_func=lambda: None,
        update_images_func=lambda: None,
        build_policy_embeddings_func=fail_embeddings,
        build_product_embeddings_func=lambda: None,
    )

    assert result["seeded"] is True
    assert result["image_urls_updated"] is True
    assert result["embeddings_rebuilt"] is False
    assert result["embedding_error"] == "ollama unavailable"


def test_initialize_deployment_data_can_fail_strictly_on_embedding_failure():
    def fail_embeddings():
        raise RuntimeError("ollama unavailable")

    with pytest.raises(RuntimeError, match="ollama unavailable"):
        initialize_deployment_data(
            rebuild_embeddings=True,
            strict_embeddings=True,
            seed_func=lambda: None,
            update_images_func=lambda: None,
            build_policy_embeddings_func=fail_embeddings,
            build_product_embeddings_func=lambda: None,
        )
