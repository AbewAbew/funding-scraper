from datetime import date

import pytest

from filters import assess_relevance, find_stated_deadlines, is_clearly_expired, normalize_deadline, trim_for_ai

TODAY = date(2026, 11, 22)


@pytest.mark.parametrize("eligible, excluded, verdict, expected", [
    (["Ethiopia"], [], None, True),
    (["Kenya", "Ethiopia", "Uganda"], [], None, True),
    (["Global"], [], None, True),
    (["Worldwide"], [], None, True),
    (["Sub-Saharan Africa"], [], None, True),
    (["East Africa"], [], None, True),
    (["African countries"], [], None, True),
    (["Low- and middle-income countries"], [], None, True),
    (["LMICs"], [], None, True),
    (["Global South"], [], None, True),
    (["Africa"], ["Ethiopia"], None, False),
    (["Global"], ["Sub-Saharan Africa"], None, False),
    (["Global"], ["International students"], None, True),    # not an exclusion of Ethiopia
    (["West Africa"], [], None, False),
    (["South Africa"], [], None, False),
    (["Southern Africa"], [], None, False),
    (["MENA"], [], None, False),
    (["Kenya", "Uganda"], [], None, False),
    (["United Kingdom"], [], "yes", False),                   # specific list beats a wrong AI verdict
    (["Africa", "Nigeria", "Ghana"], [], "yes", True),       # mixed: defer to AI
    (["Africa", "Nigeria", "Ghana"], [], "no", False),
    ([], [], "yes", True),                                    # no restriction stated
    ([], [], "unclear", False),
    ("Global", None, None, True),                             # tolerate a string instead of a list
])
def test_assess_relevance(eligible, excluded, verdict, expected):
    assert assess_relevance(eligible, excluded, verdict)[0] is expected


def test_find_stated_deadlines_formats():
    text = ("Deadline: January 15, 2027. Application deadline: 3rd March 2026. "
            "Closing date 2026-12-01. Applications close on Friday.")
    assert find_stated_deadlines(text) == [date(2027, 1, 15), date(2026, 3, 3), date(2026, 12, 1)]


def test_clearly_expired_only_when_all_deadlines_past():
    assert is_clearly_expired("Deadline: 1 July 2026. Payment $2,000.", TODAY) == date(2026, 7, 1)
    # A later round still open -> leave it for the AI
    assert is_clearly_expired("Round 1 deadline: 1 March 2026. Round 2 deadline: 1 March 2027.", TODAY) is None
    # Year-less or vague deadlines are never pre-filtered
    assert is_clearly_expired("Deadline: 24 April", TODAY) is None
    assert is_clearly_expired("Deadline: On Rolling Basis", TODAY) is None
    assert is_clearly_expired("", TODAY) is None


@pytest.mark.parametrize("raw, expected", [
    ("2027-01-15", "2027-01-15"),
    ("January 15, 2027", "2027-01-15"),
    ("Rolling", None),
    ("Not Specified", None),
    ("N/A", None),
    ("", None),
    (None, None),
    ("gibberish", None),
    ("1850-01-01", None),
])
def test_normalize_deadline(raw, expected):
    assert normalize_deadline(raw) == expected


def test_normalize_deadline_yearless_uses_publish_date():
    assert normalize_deadline("24 April", reference=date(2026, 3, 1)) == "2026-04-24"


def test_trim_keeps_head_and_key_passages():
    text = "intro " * 2000 + "Eligibility: open to NGOs in Ethiopia and Kenya. " + "filler " * 2000 + \
        "Deadline: March 3, 2027."
    out = trim_for_ai(text, 3000)
    assert len(out) <= 3100
    assert out.startswith("intro")
    assert "Ethiopia and Kenya" in out
    assert "March 3, 2027" in out


def test_trim_short_text_unchanged():
    assert trim_for_ai("  short   text ", 100) == "short text"
