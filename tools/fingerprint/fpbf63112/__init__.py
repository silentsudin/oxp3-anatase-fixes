"""fpbf63112 - Betterlife BF63112 SPI fingerprint protocol (userspace library).

A dependency-free (stdlib only) implementation of the wire protocol recovered
from the Windows UMDF driver for the ONEXPLAYER 3 fingerprint module
(``ACPI\\BF63112``).  See :mod:`fpbf63112.protocol` for the field layout, the
opcode table, the GET_IMG handshake and the retry state machine, and
``tools/fingerprint/README.md`` for what is proven and what is not.

The library never talks to hardware: it builds and validates frames, so both
the privileged probe and the offline test suite share one source of truth.
"""

from .protocol import *  # noqa: F401,F403 - the public surface lives in protocol
from . import protocol  # noqa: F401 - keep ``fpbf63112.protocol`` importable

__version__ = "0.1.0"
