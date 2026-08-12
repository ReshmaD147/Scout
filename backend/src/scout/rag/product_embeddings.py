from pathlib import Path

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_ollama import OllamaEmbeddings

from scout.agents.diagnostics import timed_stage
from scout.config import settings
from scout.db.session import SessionLocal
from scout.db.models import Product

PERSIST_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "chroma_products"
EMBEDDING_MODEL = "nomic-embed-text"
COLLECTION_NAME = "product_catalog"


class _TimedEmbeddings:
    def __init__(self, inner: OllamaEmbeddings):
        self.inner = inner

    def embed_query(self, text: str) -> list[float]:
        with timed_stage("embedding_generation"):
            return self.inner.embed_query(text)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        with timed_stage("embedding_generation", document_count=len(texts)):
            return self.inner.embed_documents(texts)


def _get_embeddings() -> OllamaEmbeddings:
    # See vector_store.py's identical fix for the full explanation -
    # base_url must be passed explicitly, or this always defaults to
    # localhost regardless of OLLAMA_BASE_URL.
    return _TimedEmbeddings(OllamaEmbeddings(model=EMBEDDING_MODEL, base_url=settings.OLLAMA_BASE_URL))


def _product_to_document(product: Product) -> Document:
    text = f"{product.name}. {product.description} Tags: {product.tags}"
    return Document(
        page_content=text,
        metadata={"product_id": product.product_id, "category": product.category},
    )


def build_product_embeddings() -> Chroma:
    session = SessionLocal()
    try:
        products = session.query(Product).all()
        documents = [_product_to_document(p) for p in products]
    finally:
        session.close()

    vector_store = Chroma.from_documents(
        documents=documents,
        embedding=_get_embeddings(),
        collection_name=COLLECTION_NAME,
        persist_directory=str(PERSIST_DIR),
    )
    return vector_store


def load_product_embeddings() -> Chroma:
    return Chroma(
        collection_name=COLLECTION_NAME,
        embedding_function=_get_embeddings(),
        persist_directory=str(PERSIST_DIR),
    )


def semantic_search_products(query: str, k: int = 10) -> list[str]:
    with timed_stage("product_chroma_load"):
        vector_store = load_product_embeddings()
    with timed_stage("product_chroma_retrieval", k=k):
        results = vector_store.similarity_search(query, k=k)
    return [doc.metadata["product_id"] for doc in results]


if __name__ == "__main__":
    build_product_embeddings()
    print(f"Product embeddings built at {PERSIST_DIR}")
