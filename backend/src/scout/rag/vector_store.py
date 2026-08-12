from pathlib import Path

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_ollama import OllamaEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from scout.agents.diagnostics import timed_stage
from scout.config import settings

POLICY_DOCS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "policies"
PERSIST_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "chroma"
EMBEDDING_MODEL = "nomic-embed-text"
COLLECTION_NAME = "policy_docs"


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
    # base_url must be passed explicitly - OllamaEmbeddings defaults to
    # localhost:11434 regardless of the OLLAMA_BASE_URL environment
    # variable, which config.py DOES correctly read but this function
    # was never actually using. Confirmed via a real Docker deployment:
    # settings.OLLAMA_BASE_URL was correctly set to the ollama service's
    # container-network address, but requests still failed trying to
    # reach localhost - a genuine, real bug, not a config problem.
    return _TimedEmbeddings(OllamaEmbeddings(model=EMBEDDING_MODEL, base_url=settings.OLLAMA_BASE_URL))


def _load_policy_documents() -> list[Document]:
    """Reads each policy .md file directly into a Document — no document
    loader dependency needed for plain text files."""
    docs = []
    for file_path in sorted(POLICY_DOCS_DIR.glob("*.md")):
        text = file_path.read_text(encoding="utf-8")
        docs.append(Document(page_content=text, metadata={"source": file_path.name}))
    return docs


def build_vector_store() -> Chroma:
    """Loads all policy .md files, chunks them, and embeds them into Chroma.
    For a clean rebuild, delete data/chroma/ first."""
    docs = _load_policy_documents()

    splitter = RecursiveCharacterTextSplitter(chunk_size=400, chunk_overlap=50)
    chunks = splitter.split_documents(docs)

    vector_store = Chroma.from_documents(
        documents=chunks,
        embedding=_get_embeddings(),
        collection_name=COLLECTION_NAME,
        persist_directory=str(PERSIST_DIR),
    )
    return vector_store


def load_vector_store() -> Chroma:
    """Loads the existing persisted Chroma collection without re-embedding."""
    return Chroma(
        collection_name=COLLECTION_NAME,
        embedding_function=_get_embeddings(),
        persist_directory=str(PERSIST_DIR),
    )


def retrieve_policy_chunks(query: str, k: int = 3) -> list[dict]:
    """Retrieves the top-k most relevant policy chunks for a query."""
    with timed_stage("policy_chroma_load"):
        vector_store = load_vector_store()
    with timed_stage("policy_chroma_retrieval", k=k):
        results = vector_store.similarity_search(query, k=k)
    return [
        _policy_chunk_to_fact(doc)
        for doc in results
    ]


def _policy_chunk_to_fact(doc: Document) -> dict:
    source = doc.metadata.get("source", "unknown")
    content = doc.page_content.strip()
    policy_name = _policy_name_from_source(source)
    return {
        "statement": content,
        "policy_name": policy_name,
        "source_document": source,
        "source_section": policy_name.lower() if policy_name else None,
    }


def _policy_name_from_source(source: str) -> str:
    stem = Path(source).stem.replace("_", " ").replace("-", " ").strip()
    return stem.title() if stem else "Policy"


if __name__ == "__main__":
    build_vector_store()
    print(f"Vector store built at {PERSIST_DIR}")
