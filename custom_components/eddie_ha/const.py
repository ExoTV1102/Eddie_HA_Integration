"""Constants for the EDDIE Home Assistant integration."""

from __future__ import annotations

DOMAIN = "eddie_ha"

CONF_PAIRING_ID = "pairing_id"
CONF_TOKEN = "token"
CONF_WS_URL = "ws_url"

DEFAULT_NAME = "EDDIE Home Assistant"
DEFAULT_DISPLAY_NAME = "Home Assistant"

PLATFORMS = ["sensor"]

SIGNAL_READING_DISCOVERED = f"{DOMAIN}_reading_discovered"

