"""The pairing logic version.

Every derived row records this. A disputed line from two months ago has to be
reproducible, and that means knowing which arithmetic produced it -- so this is
bumped by hand, in the same commit as any change to how a case resolves, and it
shows up in the diff where a human reads it.
"""

from __future__ import annotations

LOGIC_VERSION = "pairing-1.0.0"
