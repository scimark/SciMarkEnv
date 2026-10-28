import pytest

from rudesktop_mcp.dialogs import normalize_id


@pytest.mark.parametrize("raw, expected", [
    ("234 968 320", "234968320"),
    ("  234968320 ", "234968320"),
    ("234\t968\n320", "234968320"),
    ("   ", ""),
])
def test_normalize_id(raw, expected):
    assert normalize_id(raw) == expected
