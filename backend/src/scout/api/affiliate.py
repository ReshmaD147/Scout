from fastapi import APIRouter, Query
from fastapi.responses import RedirectResponse

from scout.db.session import SessionLocal
from scout.services.affiliate_service import log_affiliate_click, get_click_stats

router = APIRouter()


@router.get("/affiliate/click/{external_product_id}")
async def affiliate_click(external_product_id: str, session_id: str | None = Query(default=None)):
    db = SessionLocal()
    try:
        result = log_affiliate_click(db, external_product_id, session_id=session_id)
    finally:
        db.close()

    if not result.get("success"):
        return {"error": result.get("error", "Unknown error")}

    return RedirectResponse(url=result["redirect_url"], status_code=302)


@router.get("/affiliate/stats")
async def affiliate_stats():
    db = SessionLocal()
    try:
        return get_click_stats(db)
    finally:
        db.close()
