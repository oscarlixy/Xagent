from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class DigestWindow:
    start: datetime
    end: datetime
    cadence: Literal["6h", "daily"]
    timezone: str

    @property
    def key(self) -> str:
        return f"{self.start.isoformat().replace('+00:00', 'Z')}/{self.timezone}/{self.cadence}"


def window_for(
    now: datetime, cadence: Literal["6h", "daily"], tz: ZoneInfo
) -> DigestWindow:
    local_now = (now.replace(tzinfo=tz) if now.tzinfo is None else now).astimezone(tz)
    if cadence == "daily":
        local_end = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        local_start = local_end - timedelta(days=1)
    elif cadence == "6h":
        local_end = local_now.replace(
            hour=local_now.hour - (local_now.hour % 6), minute=0, second=0, microsecond=0
        )
        local_start = local_end - timedelta(hours=6)
    else:
        raise ValueError(f"Unsupported digest cadence: {cadence}")
    return DigestWindow(
        start=local_start.astimezone(UTC),
        end=local_end.astimezone(UTC),
        cadence=cadence,
        timezone=str(tz),
    )
