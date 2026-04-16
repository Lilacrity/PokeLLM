"""MemoryBackend adapter over the mgba-http REST API.

mgba-http (https://github.com/nikouu/mGBA-http) exposes mGBA's scripting
surface as plain HTTP. For the RAM reader we only need byte reads; the
rest of the API (button input, state save/load, screenshots, etc.) is
handled elsewhere once the agent needs to act on the game.

The only endpoint we use for ``read_bytes`` is ``GET /core/readrange``,
which returns comma-separated hex byte values. The API also exposes
``read8``/``read16``/``read32`` and per-domain variants, but a single
readrange covers every width and is always one round trip regardless of
length. We parse the response into a ``bytes`` object before handing it
back to the reader, so callers never see transport details.

Authoritative API reference: ``documentation/ApiDocumentation.md``.
"""

from __future__ import annotations

import requests


DEFAULT_BASE_URL = "http://localhost:5000"
DEFAULT_TIMEOUT_SECS = 5.0


class MgbaHttpError(RuntimeError):
    """Raised when mgba-http returns a non-success status or malformed body."""


class MgbaHttpBackend:
    """Read bytes from a running mGBA via mgba-http.

    Construction does not touch the network -- the first HTTP call is
    lazy, triggered by the first ``read_bytes``. Use :meth:`ping` if you
    want an up-front liveness check.

    Parameters
    ----------
    base_url:
        Root URL for the mgba-http server, no trailing slash. The mgba-http
        default is ``http://localhost:5000``.
    timeout:
        Per-request timeout in seconds. mgba-http responds fast for read
        calls (microseconds on localhost) so a small value is fine; we
        keep a generous default so transient stalls don't trip tests.
    session:
        Optional pre-built :class:`requests.Session`. Supplying one is
        worthwhile when the reader is hit thousands of times per frame --
        it keeps the TCP connection warm. If omitted, the backend creates
        and owns one internally.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECS,
        session: requests.Session | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._session = session or requests.Session()

    # --- MemoryBackend protocol --------------------------------------------

    def read_bytes(self, addr: int, length: int) -> bytes:
        """Fetch ``length`` bytes starting at GBA bus address ``addr``.

        Calls ``/core/readrange``; the response body is a comma-separated
        list of hex byte values (e.g. ``"d3,00,ea,66"``). We tolerate
        whitespace and an optional ``0x`` prefix on each token because the
        exact wire format is not contractually fixed.
        """
        if length <= 0:
            return b""
        params = {"address": f"0x{addr:08X}", "length": str(length)}
        resp = self._get("/core/readrange", params=params)
        data = _parse_hex_csv(resp.text)
        if len(data) != length:
            raise MgbaHttpError(
                f"readrange returned {len(data)} bytes for "
                f"addr={addr:#010x} length={length}: {resp.text!r}"
            )
        return data

    # --- Convenience --------------------------------------------------------

    def ping(self) -> str:
        """Verify the server is reachable and return the ROM's game title.

        Useful as an up-front sanity check. Raises :class:`MgbaHttpError`
        if the server is unreachable or no ROM is loaded.
        """
        resp = self._get("/core/getgametitle")
        return resp.text.strip().strip('"')

    def game_code(self) -> str:
        """Return the four-letter ROM product code (e.g. ``BPRE`` for FireRed)."""
        resp = self._get("/core/getgamecode")
        return resp.text.strip().strip('"')

    # --- Internals ----------------------------------------------------------

    def _get(self, path: str, params: dict[str, str] | None = None) -> requests.Response:
        url = f"{self._base_url}{path}"
        try:
            resp = self._session.get(url, params=params, timeout=self._timeout)
        except requests.RequestException as exc:
            raise MgbaHttpError(f"GET {url} failed: {exc}") from exc
        if resp.status_code != 200:
            raise MgbaHttpError(
                f"GET {url} -> HTTP {resp.status_code}: {resp.text!r}"
            )
        return resp


def _parse_hex_csv(text: str) -> bytes:
    """Parse ``"d3,00,ea,66"`` (with optional ``0x`` / whitespace) into bytes.

    mgba-http historically has returned values both with and without the
    ``0x`` prefix; accepting either keeps us resilient to version drift.
    Empty tokens are ignored so a trailing comma doesn't error.
    """
    stripped = text.strip().strip('"')
    if not stripped:
        return b""
    out = bytearray()
    for token in stripped.split(","):
        token = token.strip()
        if not token:
            continue
        if token.startswith(("0x", "0X")):
            token = token[2:]
        out.append(int(token, 16))
    return bytes(out)
