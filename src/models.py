from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional


@dataclass
class FightEvent:
    organization: str
    event_name: str
    slug: str

    main_event: Optional[str] = None
    fight_list: Optional[str] = None

    location: Optional[str] = None

    event_date: Optional[date] = None
    early_prelims: Optional[datetime] = None
    prelims: Optional[datetime] = None
    main_card: Optional[datetime] = None

    source_url: Optional[str] = None
