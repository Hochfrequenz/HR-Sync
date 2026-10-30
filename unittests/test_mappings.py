import pytest

from hr_sync.mappings import CAREER_LEVELS, OFFICES, WORK_SCHEDULE_PROFILES, work_schedule_profile


class TestMappings:
    """
    Tests of the fixed mappings of phase 2. The target adapters map the values of their systems back onto the common
    model, so every mapping must be unique in both directions.
    """

    def test_career_levels(self) -> None:
        assert {level: (value.service_category, value.vacation_days) for level, value in CAREER_LEVELS.items()} == {
            "Consultant": ("Consultant", 25),
            "Senior Consultant": ("Senior Consultant", 26),
            "Principal Consultant": ("Manging Consultant", 30),
            "Senior Principal Consultant": ("Associate Partner", 30),
        }

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

    def test_service_categories_can_be_mapped_back(self) -> None:
        categories = [level.service_category for level in CAREER_LEVELS.values()]
        assert len(set(categories)) == len(categories)

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
