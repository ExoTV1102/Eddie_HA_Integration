# EDDIE Home Assistant

Custom Home Assistant integration for connecting Home Assistant to the EDDIE Home Assistant outbound connector.

## Installation via HACS

1. Add `https://github.com/ExoTV1102/Eddie_HA` as a custom repository in HACS.
2. Select repository type `Integration`.
3. Install `EDDIE Home Assistant`.
4. Restart Home Assistant.
5. Add the integration from `Settings > Devices & services`.

## Pairing Flow

1. Open the EDDIE/Home Assistant UI, for example `https://wien.ddns.net:62002/`.
2. Select the data need that should be forwarded to Home Assistant.
3. Click `Connect AIIDA to EDDIE` and approve the AIIDA permission request.
4. Generate the Home Assistant pairing code in the EDDIE/Home Assistant UI.
5. In Home Assistant, add the `EDDIE Home Assistant` integration.
6. Enter the EDDIE/Home Assistant URL, for example `https://wien.ddns.net:62002`.
7. Enter the generated pairing code.
8. Return to the EDDIE/Home Assistant UI and approve the claimed Home Assistant request.

After pairing, Home Assistant connects to EDDIE via an authenticated WebSocket
and exposes diagnostic and OBIS-based energy sensors. The default single-home
pairing receives the complete outbound stream of that EDDIE instance.

## Manual Installation

Copy `custom_components/eddie_ha` into the `custom_components` directory of your
Home Assistant configuration and restart Home Assistant.

## Supported Sensors

The integration creates a diagnostic connection sensor immediately. OBIS sensors
are discovered dynamically when EDDIE sends matching values. The first mapped
OBIS values are:

- `1-0:1.8.0`: imported energy, default unit `kWh`
- `1-0:2.8.0`: exported energy, default unit `kWh`
- `1-0:16.7.0`: active power, normalized to `W`
