from scout.config import settings


def get_chat_model():
    if settings.MODEL_PROVIDER == "claude":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=settings.CLAUDE_MODEL, api_key=settings.ANTHROPIC_API_KEY)

    if settings.MODEL_PROVIDER == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(model=settings.GROQ_MODEL, api_key=settings.GROQ_API_KEY)

    if settings.MODEL_PROVIDER == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model=settings.GEMINI_MODEL,
            google_api_key=settings.GOOGLE_API_KEY,
        )

    from langchain_ollama import ChatOllama
    return ChatOllama(
        model=settings.OLLAMA_MODEL,
        base_url=settings.OLLAMA_BASE_URL,
        client_kwargs={"timeout": settings.MODEL_INVOCATION_TIMEOUT_SECONDS},
        async_client_kwargs={"timeout": settings.MODEL_INVOCATION_TIMEOUT_SECONDS},
        sync_client_kwargs={"timeout": settings.MODEL_INVOCATION_TIMEOUT_SECONDS},
    )


def with_no_think(prompt: str) -> str:
    if settings.MODEL_PROVIDER == "ollama":
        return prompt.rstrip() + "\n\n/no_think"
    return prompt
