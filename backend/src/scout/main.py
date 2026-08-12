from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from scout.agents.supervisor import build_supervisor_app
from scout.api.chat import router as chat_router
from scout.api.affiliate import router as affiliate_router
from scout.api.products import router as products_router
from scout.api.checkout import router as checkout_router
from scout.api.cart import router as cart_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    supervisor_app, tool_manager = await build_supervisor_app()
    app.state.supervisor_app = supervisor_app
    app.state.tool_manager = tool_manager
    yield
    await tool_manager.stop()


app = FastAPI(title="Scout — Retail AI Shopping Assistant", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:5174", "http://127.0.0.1:5174",
        "https://gleaming-optimism-production-6f2a.up.railway.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_router) # the only agentic one
app.include_router(affiliate_router) # deterministic 
app.include_router(products_router) # deterministic 
app.include_router(checkout_router)# deterministic 
app.include_router(cart_router) # deterministic

STATIC_IMAGES_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "product_images"
STATIC_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static/products", StaticFiles(directory=str(STATIC_IMAGES_DIR)), name="product_images")


@app.get("/health")
def health_check():
    return {"status": "ok"}
