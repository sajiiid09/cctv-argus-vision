"""argus.payroll — pairing, dwell, overage, export (M3, PLAN.md).

Deliberately empty of code and of dependencies. This package is pure functions
over stored events: no vision imports, no numpy, no av, no onnxruntime —
enforced by tests/structural/test_structure.py. Keeping probabilistic code out
of wage arithmetic is what makes the wage arithmetic testable (AGENTS.md §4).
"""
