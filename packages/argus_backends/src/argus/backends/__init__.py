"""argus.backends — inference backends and their registry.

Application code never imports a backend or a runtime directly; it asks the
registry (ARCHITECTURE.md §5.1). The import-graph check in
tests/structural/test_structure.py enforces that onnxruntime appears nowhere
outside this package.
"""
