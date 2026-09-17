"""There is no export, and its absence is enforced.

ADR-0005 keeps the system in shadow mode; ADR-0006 says a human keys in a signed
export when one exists. Until then an export function in this package would be a
loaded gun -- and the moment it exists, turning it on is an AGENTS.md §2.1
sign-off, not an import away.
"""

from __future__ import annotations

import pkgutil
import re

import argus.payroll

FORBIDDEN = re.compile(r"export|csv|sign|payslip|deduct", re.IGNORECASE)


def test_package_exposes_no_export_symbol() -> None:
    offenders = [name for name in dir(argus.payroll) if FORBIDDEN.search(name)]
    assert not offenders, f"argus.payroll must expose no export path, found: {offenders}"


def test_no_module_is_named_for_export() -> None:
    modules = [m.name for m in pkgutil.iter_modules(argus.payroll.__path__)]
    offenders = [m for m in modules if FORBIDDEN.search(m)]
    assert not offenders, f"no export module may exist in argus.payroll, found: {offenders}"


def test_all_is_explicit() -> None:
    """__all__ is the list a reviewer reads to see what this package offers.
    An empty or missing one would make the previous two tests meaningless."""
    assert argus.payroll.__all__
    for name in argus.payroll.__all__:
        assert hasattr(argus.payroll, name)
