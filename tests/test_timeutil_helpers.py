"""Characterization tests for pure helpers in core.timeutil."""
from core import timeutil as tu


def test_has_monthly_boundary():
    assert tu._has_monthly_boundary(1) is True
    assert tu._has_monthly_boundary(15) is True
    assert tu._has_monthly_boundary(31) is True
    assert tu._has_monthly_boundary(0) is False
    assert tu._has_monthly_boundary(-1) is False
