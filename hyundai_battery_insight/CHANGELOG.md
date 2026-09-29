# Changelog

## 0.1.12

- Adds a sliding timeline window with **Vorige**, **Vandaag** and **Volgende** controls.
- Keeps the existing 1/7/14/30/60/90-day window sizes and shifts by the visible window length.
- Changes the default visible window to 14 days for a less crowded chart.
- Adds custom **Van** / **Tot** date selectors with an explicit **Toon** action.
- Custom ranges can span up to 366 days and cannot end in the future.
- Makes chart/export/rebuild requests use the exact visible date range instead of always using a trailing number of days.
- Makes the graph x-axis represent the full selected window, including periods without measurements.
- Makes **Herbouw HA-historie** rebuild exactly the visible range, including custom date ranges.
- CSV and JSON exports now follow the same visible range.
- Adds automated validation for explicit date ranges and timeline navigation controls.

## 0.1.11

- Makes **Herbouw HA-historie** use the currently selected display period (1/7/14/30/60/90 days) instead of the fixed startup lookback.
- Keeps raw CCS2 snapshots as the preferred source wherever they exist.
- Uses detailed Home Assistant Recorder state history where it is still retained.
- Adds fallback to hourly Home Assistant long-term statistics for the older part of the selected period.
- Keeps long-term-statistics points explicitly marked as `ha_statistics`; they are not presented as exact raw vehicle snapshots or exact state-change timestamps.
- Uses mean values for 12V/HV measurement statistics and state (with safe fallbacks) for odometer statistics.
- Suppresses lower-quality long-term-statistics points when a nearby detailed Recorder point or raw CCS2 snapshot exists.
- Persists the history source type in the app database and portable data backup.
- Adds validation for selected-period rebuilds and long-term-statistics reconstruction.

## 0.1.10

- Changed Home Assistant historical correlation import to additive-only behavior: existing cached `ha_history_points` are never deleted during a Recorder rebuild.
- Added a manual **Herbouw HA-historie** action that re-reads the available 12V battery, HV Battery Level and odometer histories and adds missing correlation points.
- Rebuild results report the number and first/last timestamp of source events found for each of the three sensor streams.
- Correlation upserts preserve existing non-null values and only fill previously missing fields.
- Extended the portable backup format to `hbi-data-backup-v2`, containing both raw `snapshots` and cached `ha_history_points`.
- Kept restore compatibility with existing `hbi-raw-backup-v1` files.
- Renamed the UI controls to **Data backup** and **Data herstel** to reflect the complete persistent timeline backup.
- Added CI tests proving cached historical points survive a rebuild and that both persistent tables round-trip through backup/restore.

## 0.1.9

- Added manual raw snapshot backup and restore from the app UI.
- Raw backup includes all stored raw snapshot rows and keeps protected raw payloads protected.
- Restore merges by vehicle timestamp, updating conflicts without deleting unrelated local snapshots.
- Added format, timestamp, upload-size and vehicle-fingerprint validation for restore.
- Changed the display period selector to 1, 7, 14, 30, 60 and 90 days.
- Allowed 1-day timeline/CSV/JSON API queries.
- Added automated backup/restore round-trip validation.

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
