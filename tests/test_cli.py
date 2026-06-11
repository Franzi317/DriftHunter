import pytest

from drifthunter.cli import _looks_like_derivative_listing


@pytest.mark.parametrize(
    "ticker, expected",
    [
        ("AAPLW", True),
        ("ABLVW", True),
        ("ACHR-WT", True),
        ("BTSGU", True),
        ("GENVR", True),
        ("AAPL", False),
        ("GLW", False),
        ("SPC", False),
        ("BRK.B", False),
        ("UHAL", False),
    ],
)
def test_looks_like_derivative_listing(ticker, expected):
    assert _looks_like_derivative_listing(ticker) is expected
