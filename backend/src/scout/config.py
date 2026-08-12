import os

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    APP_ENV: str = os.getenv("APP_ENV", "development")

    OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen3:8b")
    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

    STRIPE_MCP_KEY: str = os.getenv("STRIPE_MCP_KEY", "")
    STRIPE_MCP_URL: str = os.getenv("STRIPE_MCP_URL", "https://mcp.stripe.com")
    ENABLE_STRIPE_MCP: bool = _env_bool("ENABLE_STRIPE_MCP", False)

    MODEL_INVOCATION_TIMEOUT_SECONDS: float = float(os.getenv("MODEL_INVOCATION_TIMEOUT_SECONDS", "45"))
    SUB_INTENT_TIMEOUT_SECONDS: float = float(os.getenv("SUB_INTENT_TIMEOUT_SECONDS", "90"))
    CHAT_REQUEST_TIMEOUT_SECONDS: float = float(os.getenv("CHAT_REQUEST_TIMEOUT_SECONDS", "110"))

    # "ollama" (documented local default) or "gemini" (fast dev testing)
    MODEL_PROVIDER: str = os.getenv("MODEL_PROVIDER", "ollama")
    GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "qwen/qwen3.6-27b")

    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

    ENABLE_DEMO_AUTH: bool = _env_bool("ENABLE_DEMO_AUTH", False)
    DEMO_CUSTOMER_IDS: str = os.getenv("DEMO_CUSTOMER_IDS", "C001,C002")

    @property
    def demo_auth_enabled(self) -> bool:
        return self.ENABLE_DEMO_AUTH and self.APP_ENV.strip().lower() not in {"prod", "production"}

    @property
    def demo_customer_ids(self) -> set[str]:
        return {item.strip() for item in self.DEMO_CUSTOMER_IDS.split(",") if item.strip()}


settings = Settings()
