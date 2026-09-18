"""argus.pipelines -- gate, canteen, occupancy, violence.

Pure-ish analysis logic: it reads frames from a source it does not own, asks
``argus.backends`` for models it does not import directly, and writes evidence
through a sink Protocol. It imports **no** ``argus.payroll`` -- evidence flows
one way, and a structural test says so.
"""
