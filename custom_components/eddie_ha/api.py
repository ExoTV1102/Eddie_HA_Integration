"""Client helpers for the EDDIE Home Assistant outbound connector."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from aiohttp import ClientError, ClientResponseError, ClientSession, WSMsgType

_LOGGER = logging.getLogger(__name__)


class EddieHaError(Exception):
    """Base EDDIE Home Assistant integration error."""


class EddieHaAuthError(EddieHaError):
    """Raised when EDDIE rejects authentication or pairing."""


class EddieHaCannotConnect(EddieHaError):
    """Raised when EDDIE cannot be reached."""


def normalize_base_url(base_url: str) -> str:
    """Normalize the user supplied connector URL."""
    normalized = base_url.strip().rstrip("/")
    if not normalized:
        raise EddieHaCannotConnect("Missing EDDIE URL")
    if not normalized.startswith(("http://", "https://")):
        normalized = f"https://{normalized}"
    return normalized


def websocket_url(base_url: str) -> str:
    """Build the WebSocket URL from the connector base URL."""
    parts = urlsplit(base_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    path = f"{parts.path.rstrip('/')}/ws/near-real-time-data"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


class EddieHaClient:
    """Small HTTP/WebSocket client for the EDDIE HA connector."""

    def __init__(self, session: ClientSession, base_url: str, token: str | None = None) -> None:
        self._session = session
        self.base_url = normalize_base_url(base_url)
        self.token = token

    async def claim_pairing(self, pairing_code: str, display_name: str) -> dict[str, Any]:
        """Claim a pairing code generated in the EDDIE HA UI."""
        try:
            response = await self._session.post(
                f"{self.base_url}/pairings/claim",
                json={
                    "pairingCode": pairing_code.strip(),
                    "displayName": display_name.strip() or "Home Assistant",
                },
                timeout=20,
            )
            response.raise_for_status()
            data: dict[str, Any] = await response.json()
        except ClientResponseError as err:
            if err.status in {401, 403, 404}:
                raise EddieHaAuthError("Pairing code was rejected by EDDIE") from err
            raise EddieHaCannotConnect(f"EDDIE returned HTTP {err.status}") from err
        except (ClientError, TimeoutError, asyncio.TimeoutError) as err:
            raise EddieHaCannotConnect("Could not connect to EDDIE") from err

        if not data.get("token"):
            raise EddieHaAuthError("EDDIE did not return a Home Assistant token")
        return data

    async def listen(
        self,
        message_handler: Callable[[dict[str, Any]], Awaitable[None]],
        stop_event: asyncio.Event,
    ) -> None:
        """Listen for EDDIE push messages until stopped."""
        if self.token is None:
            raise EddieHaAuthError("Missing EDDIE token")

        headers = {"Authorization": f"Bearer {self.token}"}
        while not stop_event.is_set():
            try:
                async with self._session.ws_connect(
                    websocket_url(self.base_url),
                    headers=headers,
                    heartbeat=30,
                    timeout=30,
                ) as websocket:
                    _LOGGER.info("Connected to EDDIE Home Assistant WebSocket")
                    async for message in websocket:
                        if stop_event.is_set():
                            return
                        if message.type == WSMsgType.TEXT:
                            await message_handler(json.loads(message.data))
                        elif message.type in {WSMsgType.CLOSED, WSMsgType.ERROR}:
                            break
            except asyncio.CancelledError:
                raise
            except (ClientResponseError, ClientError, TimeoutError, asyncio.TimeoutError, json.JSONDecodeError) as err:
                _LOGGER.warning("EDDIE WebSocket connection failed: %s", err)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=15)
            except asyncio.TimeoutError:
                continue

