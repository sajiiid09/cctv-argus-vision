"""argus.ingest — stream manager (PLAN.md M1).

One RTSP consumer per source, fanned out internally: the DVR's low
concurrent-client limit means a second connection can starve the first
(ARCHITECTURE.md §6), so there is exactly one connection and many subscribers.
"""
