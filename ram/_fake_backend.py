"""In-memory MemoryBackend fake for tests and local smoke-checking.

Not imported from the main package API -- keep it private so the real
backend path stays obvious.
"""

from __future__ import annotations


class DictBackend:
    """Sparse byte map keyed by GBA address.

    Writes are chunked byte-by-byte so overlapping extends/overwrites work
    intuitively. Reads require every byte in the requested range to have
    been set, otherwise KeyError.
    """

    def __init__(self, default: int = 0) -> None:
        self._mem: dict[int, int] = {}
        self._default = default & 0xFF

    def write(self, addr: int, data: bytes) -> None:
        for i, b in enumerate(data):
            self._mem[addr + i] = b

    def read_bytes(self, addr: int, length: int) -> bytes:
        out = bytearray(length)
        for i in range(length):
            out[i] = self._mem.get(addr + i, self._default)
        return bytes(out)
