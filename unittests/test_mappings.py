import pytest

from hr_sync.mappings import CAREER_LEVELS, OFFICES, WORK_SCHEDULE_PROFILES, work_schedule_profile


class TestMappings:
    """
    Tests of the fixed mappings of phase 2. The target adapters map offices and weekly hours back onto the common
    model, so these mappings must be unique in both directions. Career level and service category are read as they
    are, so several career levels may share a service category.
    """

    def test_career_levels(self) -> None:
        assert {
            level: (value.service_category, value.vacation_days, value.in_decidalo)
            for level, value in CAREER_LEVELS.items()
        } == {
            "Consultant": ("Consultant", 25, True),
            "Senior Consultant": ("Senior Consultant", 26, True),
            "Principal Consultant": ("Manging Consultant", 30, True),
            "Senior Principal Consultant": ("Associate Partner", 30, True),
            "Geschäftsführer": ("Associate Partner", None, True),
            "Finance Director": ("Associate Partner", None, True),
            "GF-Assistenz": (None, 30, True),
            "HR Consultant": ("Senior Consultant", 25, True),
            "Marketing": (None, 30, False),
            "Systemadministrator": (None, 26, False),
        }

    def test_service_categories_exist_in_decidalo(self) -> None:
        # for an unknown name, the import of Decidalo creates a new service category
        categories = {level.service_category for level in CAREER_LEVELS.values()} - {None}
        assert categories <= {"Consultant", "Senior Consultant", "Manging Consultant", "Associate Partner"}

    def test_career_levels_outside_decidalo_have_no_service_category(self) -> None:
        assert [name for name, level in CAREER_LEVELS.items() if not level.in_decidalo and level.service_category] == []

    def test_offices(self) -> None:
        assert {
            office_id: (office.name, office.holiday_list_code, office.weclapp_state_value_id)
            for office_id, office in OFFICES.items()
        } == {
            "906847": ("Bremen", "DE-HB", "9904"),
            "906855": ("Berlin", "DE-BE", "9905"),
            "906856": ("Grünwald", "DE-BY", "9903"),
        }

    @pytest.mark.parametrize("attribute", ["name", "holiday_list_code", "weclapp_state_value_id"])
    def test_offices_can_be_mapped_back(self, attribute: str) -> None:
        values = [getattr(office, attribute) for office in OFFICES.values()]
        assert len(set(values)) == len(values)

    def test_work_schedule_profiles_can_be_mapped_back(self) -> None:
        assert len(set(WORK_SCHEDULE_PROFILES.values())) == len(WORK_SCHEDULE_PROFILES)
        # the profiles for parental leave are never assigned
        assert not {"9835", "9836", "9837"} & set(WORK_SCHEDULE_PROFILES.values())


class TestWorkScheduleProfile:
    """
    Tests of the work schedule profile for the weekly hours from Personio.
    """

    @pytest.mark.parametrize(
        ("weekly_hours", "expected"),
        [
            pytest.param(40, "7177", id="full time"),
            pytest.param(40.0, "7177", id="float"),
            pytest.param(6, "7623", id="fewest hours"),
            pytest.param(37.5, None, id="fraction of an hour"),
            pytest.param(38, None, id="unknown hours"),
            pytest.param(0, None, id="no hours"),
        ],
    )
    def test_work_schedule_profile(self, weekly_hours: float, expected: str | None) -> None:
        assert work_schedule_profile(weekly_hours) == expected
