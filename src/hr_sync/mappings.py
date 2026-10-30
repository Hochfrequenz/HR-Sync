"""
The fixed mappings of the fields of phase 2: career level, office and weekly hours, and the fixed values of new weclapp
users.

They are rules of Hochfrequenz and no settings (see section 4.4 of the plan). Only the values in these mappings are
allowed: any other value is a data problem, so that an employee is only created once the mappings know all of their
values. The ids in weclapp only exist in the tenant of Hochfrequenz.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class CareerLevel:
    """The values derived from a career level of Personio."""

    service_category: str
    """the ``serviceCategoryName`` in Decidalo"""
    vacation_days: int
    """the vacation entitlement in days per year, in weclapp"""


CAREER_LEVELS: Mapping[str, CareerLevel] = {
    "Consultant": CareerLevel(service_category="Consultant", vacation_days=25),
    "Senior Consultant": CareerLevel(service_category="Senior Consultant", vacation_days=26),
    # spelled like that in Decidalo
    "Principal Consultant": CareerLevel(service_category="Manging Consultant", vacation_days=30),
    "Senior Principal Consultant": CareerLevel(service_category="Associate Partner", vacation_days=30),
}
"""the allowed career levels, as written in Personio"""


@dataclass(frozen=True)
class Office:
    """An office of Hochfrequenz and the values derived from it."""

    name: str
    """the name in the common model"""
    holiday_list_code: str
    """the ``holidayListCode`` in Decidalo, i.e. the public holidays of the office"""
    weclapp_state_value_id: str
    """the selected value of the state in weclapp, which defines the public holidays there"""


OFFICES: Mapping[str, Office] = {
    "906847": Office(name="Bremen", holiday_list_code="DE-HB", weclapp_state_value_id="9904"),
    "906855": Office(name="Berlin", holiday_list_code="DE-BE", weclapp_state_value_id="9905"),
    "906856": Office(name="Grünwald", holiday_list_code="DE-BY", weclapp_state_value_id="9903"),
}
"""the allowed offices, by the id of the office in Personio"""

WORK_SCHEDULE_PROFILES: Mapping[int, str] = {
    6: "7623",
    20: "7629",
    24: "7630",
    25: "13996",
    28: "7632",
    30: "7633",
    32: "7634",
    34: "9833",
    35: "7635",
    36: "7636",
    37: "9834",
    40: "7177",
}
"""
the id of the work schedule profile in weclapp, by the weekly hours; HR-Sync never assigns the profiles for parental
leave (9835, 9836, 9837)
"""


def work_schedule_profile(weekly_hours: float) -> str | None:
    """
    Returns the id of the work schedule profile in weclapp for the weekly hours, or ``None`` if there is none.
    """
    # float() first: int.is_integer() only exists from Python 3.12 on
    if not float(weekly_hours).is_integer():
        return None
    return WORK_SCHEDULE_PROFILES.get(int(weekly_hours))


WECLAPP_NAME_MAX_LENGTH = 50
"""weclapp rejects longer first and last names"""
WECLAPP_LICENSES = ("WECLAPP_PROJECT", "ADDON_HUMAN_RESOURCES")
"""the licenses of new weclapp users"""
WECLAPP_USER_ROLE_IDS = ("7166", "4329")
"""the roles of new weclapp users: time recording and weclapp project (all permissions)"""
WECLAPP_COST_RATE = Decimal(1)
"""the cost rate of new weclapp users in EUR/h; a placeholder until someone maintains it by hand in weclapp (O20)"""
WECLAPP_COST_RATE_REASON = "Erstanlage (weclapp)"
"""the reason of the cost rate of new weclapp users"""


@dataclass(frozen=True)
class WeclappAttributeIds:
    """The ids of the definitions of the custom attributes at weclapp users that HR-Sync writes besides the number."""

    cost_rate: str
    """number, EUR/h"""
    cost_rate_reason: str
    """string"""
    vacation_days: str
    """number, days per year"""
    state: str
    """list: the federal state, which defines the public holidays"""
    career_level: str
    """string"""


WECLAPP_ATTRIBUTE_IDS = WeclappAttributeIds(
    cost_rate="8211", cost_rate_reason="8217", vacation_days="9795", state="9902", career_level="14175"
)
"""the custom attributes of new weclapp users"""
