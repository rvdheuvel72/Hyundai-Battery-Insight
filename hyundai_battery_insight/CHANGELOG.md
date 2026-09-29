# Changelog

## 0.1.8

- Added original Battery Insight logo and Home Assistant app icon.
- Corrected manual local installation path to `/addons/hyundai_battery_insight`.
- Restricted ingress HTTP access to the Home Assistant Supervisor ingress proxy.
- Added recoverable VIN-derived authenticated protection for complete raw vehicle payloads stored in SQLite.
- Added automatic migration of legacy plaintext raw payloads.
- Added migration support for the temporary pre-release v1 encrypted format when its old secret is still available.
- Made raw-data protection reproducible after reinstall without depending on an installation-local random secret.
- Added cold Home Assistant backup mode for safer SQLite backup consistency.
- Added app-level README, DOCS and CHANGELOG files.
- Added repository maintainer metadata, security guidance and CI validation.
- Added repository/app URL metadata.
- Pinned the Home Assistant base image to `ghcr.io/home-assistant/base:3.24`.
- Added `py3-cryptography` for authenticated raw-data protection.

## 0.1.7

- Extended the timeline beyond the period where full raw `vehicle_data` attributes are available by importing separate Home Assistant histories for 12V battery level, HV Battery Level and odometer.
- Added auto-detection of the three history entities, with optional explicit entity configuration.
- Kept raw CCS2 points and reconstructed HA historical correlation points visually and analytically distinct.
- Raw points take precedence when a historical correlation point is close to the same timestamp.
- Removed the standalone **Extern laden** UI metric and obsolete `ev_charging_entity` configuration option.
- Kept raw charging remaining-time as internal/export diagnostic context.
- Added `source_kind` and per-field source timestamps to CSV/JSON exports.

## 0.1.6

- Stopped using the Home Assistant EV-charging switch or connected/disconnected state as evidence of charging.
- Derived charging context from raw CCS2 `Green.ChargingInformation.Charging.RemainTime > 0` when available.
- Kept `ConnectorFastening.State` only as raw diagnostic data.

## 0.1.5

- Renamed the user-facing traction-battery value to **HV Battery Level**.
- Documented the source as `Green.BatteryManagement.BatteryRemain.Ratio`.
- Renamed the public JSON/CSV field from `ev_soc` to `hv_battery_level`.

## 0.1.3

- Added the **Sinds laatste geregistreerde rit** summary.
- Added endpoint-normalized 12V change in percentage points per 24 hours.
- Added interval classification for ride, no-ride decline, no-ride recovery, HV-level increase and stable intervals.
