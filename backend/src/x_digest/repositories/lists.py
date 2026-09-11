from sqlalchemy import select
from sqlalchemy.orm import Session

from x_digest.models import XList


def upsert_list(
    session: Session,
    *,
    platform_list_id: str,
    name: str,
    sync_interval_minutes: int = 1440,
    enabled: bool = True,
) -> XList:
    x_list = session.scalar(select(XList).where(XList.platform_list_id == platform_list_id))
    if x_list is None:
        x_list = XList(
            platform_list_id=platform_list_id,
            name=name,
            sync_interval_minutes=sync_interval_minutes,
            enabled=enabled,
        )
        session.add(x_list)
        session.flush()
        return x_list

    x_list.name = name
    x_list.sync_interval_minutes = sync_interval_minutes
    x_list.enabled = enabled
    session.flush()
    return x_list
