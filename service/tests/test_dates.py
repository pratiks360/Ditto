"""Date re-encoding. A widget rejects anything but its own format, which is why
dates were the field type that kept coming back empty."""

import pytest

from jobkb.dates import detect_mask, format_for_field, is_date_field, parse_date, sort_key


@pytest.mark.parametrize("text,expected", [
    ("Sept 2025", {"year": 2025, "month": 9, "day": None}),
    ("Sep 2018", {"year": 2018, "month": 9, "day": None}),
    ("2022-08-01", {"year": 2022, "month": 8, "day": 1}),
    ("01/06/2019", {"year": 2019, "month": 6, "day": 1}),
    ("06/2019", {"year": 2019, "month": 6, "day": None}),
    ("2017", {"year": 2017, "month": None, "day": None}),
])
def test_parse(text, expected):
    assert parse_date(text) == expected


@pytest.mark.parametrize("text", ["Present", "Current", "till date", "ongoing"])
def test_ongoing(text):
    assert parse_date(text) == {"ongoing": True}


@pytest.mark.parametrize("hint,order,sep", [
    ("MM/YYYY", "MY", "/"),
    ("DD-MM-YYYY", "DMY", "-"),
    ("YYYY/MM/DD", "YMD", "/"),
    ("Format: yyyy-mm", "YM", "-"),
    ("MM/DD/YYYY", "MDY", "/"),
])
def test_mask_detection_including_separator(hint, order, sep):
    mask = detect_mask({"placeholder": hint})
    assert mask == {"order": order, "sep": sep}


def test_mask_is_read_from_any_advertised_source():
    """The format can live in the placeholder, the pattern, a title, an alt
    attribute or a data-* attribute; the extension folds them into `hint`."""
    for hints in [{"placeholder": "MM/YYYY"}, {"pattern": "MM/YYYY"},
                  {"hint": "Enter as MM/YYYY"}, {"label": "From (MM/YYYY)"}]:
        assert is_date_field(hints)


@pytest.mark.parametrize("hints,expected", [
    ({"inputType": "month"}, "2025-09"),
    ({"inputType": "date"}, "2025-09-01"),
    ({"placeholder": "MM/YYYY"}, "09/2025"),
    ({"placeholder": "YYYY-MM"}, "2025-09"),
    ({"placeholder": "DD-MM-YYYY"}, "01-09-2025"),
    ({"placeholder": "YYYY"}, "2025"),
])
def test_reencoding_for_each_widget(hints, expected):
    assert format_for_field("Sept 2025", hints) == expected


def test_day_is_always_the_first():
    """Resume dates are month + year, so the day is ours to choose."""
    assert format_for_field("Sept 2025", {"placeholder": "DD/MM/YYYY"}) == "01/09/2025"


def test_ongoing_role_produces_no_end_date():
    assert format_for_field("Present", {"inputType": "month"}) == ""


def test_year_only_value_cannot_fill_a_month_field():
    """Better empty and flagged than a fabricated month."""
    assert format_for_field("2017", {"inputType": "month"}) == ""


def test_non_date_fields_pass_through():
    assert format_for_field("Kafka, Python", {"inputType": "text"}) == "Kafka, Python"


def test_ongoing_sorts_above_dated():
    assert sort_key("Present") > sort_key("Sept 2025") > sort_key("Sep 2018")
