from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class FightEvent:
    organization: str
    event_name: str
    slug: str

    main_event: Optional[str]

    location: Optional[str]

    early_prelims: Optional[datetime]
    prelims: Optional[datetime]
    main_card: Optional[datetime]

    source_url: Optional[str]
