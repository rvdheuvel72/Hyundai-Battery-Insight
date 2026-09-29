# Hyundai Battery Insight documentation

## Requirements

- Home Assistant OS.
- Hyundai / Kia Connect installed and configured in Home Assistant.
- A working vehicle entry exposing the required battery/vehicle data.
- Home Assistant Recorder history for extended historical correlation.

Upstream Hyundai / Kia Connect project:

https://github.com/Hyundai-Kia-Connect/kia_uvo

## Configuration

- `raw_entity`: entity containing the nested `vehicle_data` payload. Default: `sensor.tucson_data_2`.
- `aux_soc_entity`: 12V battery percentage entity, or `auto`.
- `hv_battery_entity`: EV/HV battery level entity, or `auto`.
- `odometer_entity`: odometer entity, or `auto`.
- `lookback_days`: how many days of Home Assistant history to correlate.
- `poll_seconds`: polling interval used by this app when reading Home Assistant state.
- `import_on_start`: import available Home Assistant history when the app starts.

The default raw entity is specific to the original development vehicle. Other Hyundai/Kia models and regions may use different entity names or payload structures.

## Data model

### Raw CCS2

A raw CCS2 point is taken from one nested `vehicle_data` object and uses the vehicle timestamp when available.

### HA historical correlation

Older timeline points can be reconstructed by correlating separate Home Assistant histories for 12V battery level, HV Battery Level and odometer.

These correlated values are useful for trends, but they are not treated as equivalent to one raw vehicle snapshot.

### No interpolation

Periods with no uploaded vehicle data remain unknown. The app does not invent intermediate battery measurements.

## Raw-data protection

The complete raw vehicle payload can contain identifiers such as a VIN or registration/licence-plate value.

Starting with version 0.1.8, raw payloads stored in SQLite use authenticated encryption with a key deterministically derived from:

- the vehicle VIN; and
- a fixed Hyundai Battery Insight derivation context.

This design is intentionally **recoverable rather than high-security**. The VIN is not a secret and the derivation method is part of this open-source project. The goals are:

- avoid leaving VIN/registration data readable as plaintext in SQLite;
- make modified ciphertext detectable;
- make the protection key reproducible after reinstall; and
- prevent a lost installation-local secret from making restored data unreadable.

Do **not** treat this as strong protection against a person who knows the VIN and has access to the source code.

### Upgrade and reinstall behaviour

On startup, once a VIN is available, version 0.1.8 checks stored raw rows:

- legacy plaintext `raw_json` rows are converted in place to the recoverable v2 format;
- existing v2 rows are authenticated;
- temporary pre-release v1 encrypted rows are migrated when the old v1 local secret is still available;
- an unreadable or unrecognized row is never automatically deleted.

The key is reproducible after reinstall from the same VIN. However, the SQLite database itself must still be preserved or restored. Use a Home Assistant backup before uninstalling if you need to retain app history.

The app declares `backup: cold`, so Home Assistant stops the app while backing it up to reduce the chance of an inconsistent SQLite backup.

## Network and privacy

- The app does not directly authenticate to Hyundai/Kia.
- It reads Home Assistant through the local Supervisor/Home Assistant API.
- It does not expose a host/LAN port.
- Its HTTP server is ingress-only and rejects requests that do not come from the Home Assistant Supervisor ingress proxy.
- No project telemetry or analytics are sent to an external service.
- CSV/JSON exports contain the analyzed/public fields, not the complete protected raw vehicle payload.

The Hyundai / Kia Connect integration has its own network behaviour; consult that project's documentation for details.

## Interpretation notes

- Odometer movement is used as evidence that driving occurred between two endpoints.
- A 12V rise without odometer movement can be consistent with automatic 12V support or SoC recalibration, but is not treated as proof of a charging cycle.
- HV Battery Level is normally whole-percent data, so a small energy transfer can be invisible.
- Percentage-points per 24h is endpoint normalization, not a continuous current/leakage measurement.
- `Electronics.Battery.Charging.WarningLevel` is shown as raw data only; the app does not assign an undocumented official threshold meaning to it.

## Language

The current user interface is Dutch only.

## Support

Issues and pull requests are welcome, but support and future updates are best-effort only.

The app is provided as-is under the MIT License.
