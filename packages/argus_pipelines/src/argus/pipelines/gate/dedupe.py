"""One crossing of a reader is one tap.

Two reasons a tap arrives twice: somebody taps again because the beep was
ambiguous, and a replay hands back a tap we already saw live. The first is a
window; the second is an exact identity, and the unique index on
`(reader_id, reader_seq)` makes it a database fact as well as a decision here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from argus.pipelines.gate.taps import Tap, TapChannel


@dataclass
class TapDeduper:
    window_s: float = 5.0
    _last: dict[tuple[str, str], Tap] = field(default_factory=dict)
    _seen_seq: set[tuple[str, int]] = field(default_factory=set)

    def accept(self, tap: Tap) -> bool:
        """True if this tap is new. Live always wins over a replay of itself."""
        if tap.reader_seq is not None:
            key = (tap.reader_id, tap.reader_seq)
            if key in self._seen_seq:
                return False
            self._seen_seq.add(key)
        badge_key = (tap.reader_id, tap.badge_id)
        previous = self._last.get(badge_key)
        if previous is not None:
            delta = abs((tap.ts_utc - previous.ts_utc).total_seconds())
            if delta <= self.window_s and tap.channel is not TapChannel.LIVE:
                return False
            if delta <= self.window_s and previous.channel is TapChannel.LIVE:
                return False
        self._last[badge_key] = tap
        return True
