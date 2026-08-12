from sqlalchemy import func
from sqlalchemy.orm import Session

from scout.db.models import AffiliateClick, ExternalProduct


class AffiliateClickRepository:
    """Data access for AffiliateClick — logs each time a customer follows
    a link to an external vendor, for click-through tracking.
    """

    def __init__(self, session: Session):
        self.session = session

    def create(
        self,
        external_product_id: str,
        session_id: str | None = None,
    ) -> AffiliateClick:
        """Build a new AffiliateClick and stage it for saving. Does not
        commit — the caller commits once, after building the click.
        """
        click = AffiliateClick(
            external_product_id=external_product_id,
            session_id=session_id,
        )
        self.session.add(click)
        return click

    def get_click_counts_by_vendor(self) -> dict[str, int]:
        """Return total clicks grouped by vendor name, joined against
        ExternalProduct to resolve vendor names from product IDs.
        """
        rows = (
            self.session.query(
                ExternalProduct.vendor_name,
                func.count(AffiliateClick.click_id).label("clicks"),
            )
            .join(
                AffiliateClick,
                AffiliateClick.external_product_id == ExternalProduct.external_product_id,
            )
            .group_by(ExternalProduct.vendor_name)
            .all()
        )
        return {vendor: count for vendor, count in rows}