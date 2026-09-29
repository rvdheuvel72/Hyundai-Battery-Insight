# Hyundai Battery Insight

Local Home Assistant app that correlates Hyundai / Kia vehicle battery information already present in Home Assistant. It never calls Hyundai/Bluelink directly and does not request a vehicle refresh.

## Requirements

- **Home Assistant OS**. Home Assistant Apps are available on Home Assistant OS.
- **Hyundai / Kia Connect** installed and configured in Home Assistant:
  - GitHub: https://github.com/Hyundai-Kia-Connect/kia_uvo
  - The upstream project is installed through HACS; its current instructions use the name **Kia Uvo** in HACS and in **Settings → Devices & Services → Integrations**.
- A working Hyundai / Kia Connect vehicle entry exposing the battery/vehicle entities used by this app.
- Home Assistant Recorder history is required for the extended historical correlation view.

Hyundai Battery Insight reads data already available in Home Assistant. It does **not** authenticate against Hyundai/Kia itself.

## Installation

### Recommended: add this repository to Home Assistant

This repository is structured as a Home Assistant third-party App repository. For Home Assistant to fetch it directly from GitHub, this repository must be publicly accessible.

Repository URL:

```text
https://github.com/rvdheuvel72/Hyundai-Battery-Insight
```

1. In Home Assistant, go to **Settings → Apps**.
2. Select **Install app**.
3. Open the **⋮** menu in the top-right corner and select **Repositories**.
4. Paste the repository URL above and select **Add**.
5. Return to the App store.
6. Open **Hyundai Battery Insight** and select **Install**.
7. After installation, review the app configuration.
8. Start the app.
9. Open **Web UI**. Optionally enable **Show in sidebar**.

If the repository or app does not appear immediately, refresh the Home Assistant browser UI. For repository errors, check **Settings → System → Logs** and select the Supervisor log.

### Manual / local installation

You can also install the app without adding the GitHub repository.

Copy the contents of:

```text
hyundai_battery_insight/
```

from this repository to:

```text
/local_apps/hyundai_battery_insight
```

Using Terminal & SSH, reload the local App store:

```sh
ha store reload
```

Then go to **Settings → Apps → Install app**. The app should appear under **Local apps** as **Hyundai Battery Insight**.

## Configuration

Version 0.1.7 can auto-detect the relevant Home Assistant sensors. The app configuration also allows explicit entity IDs:

- `raw_entity`: Hyundai / Kia Connect entity containing the nested `vehicle_data` payload. Default: `sensor.tucson_data_2`.
- `aux_soc_entity`: 12V battery percentage sensor, or `auto`.
- `hv_battery_entity`: EV/HV battery level sensor, or `auto`.
- `odometer_entity`: odometer sensor, or `auto`.
- `lookback_days`: historical lookback period.
- `poll_seconds`: Home Assistant polling interval used by this app.
- `import_on_start`: import available Home Assistant history when the app starts.

The default `raw_entity` is specific to the original development vehicle. If your Hyundai / Kia Connect entity IDs differ, set the appropriate entity explicitly in the app configuration.

## Evidence model

- **Raw CCS2 points** come from one nested `vehicle_data` payload and use the CCS2 vehicle timestamp.
- **HA historical correlation points** extend the timeline with the separate Home Assistant 12V, HV Battery Level and odometer histories when older raw attributes are no longer available.
- HA historical correlation is explicitly marked as lower evidentiary quality than one raw vehicle snapshot; values can be carried from the most recent state of a separate sensor.
- Gaps while the car sleeps are never interpolated as measurements.
- Odometer movement is used as evidence that a drive occurred between points.
- A 12V rise without odometer movement is labelled only as possible automatic support/recalibration, not proven charging.
- `Electronics.Battery.Charging.WarningLevel` is displayed only when raw CCS2 data is present and is not treated as an official Aux Battery Saver threshold.

## History entities

For the original development vehicle, auto-detection is expected to find the 12V sensor by its `Car Battery` / `12V` naming, and the known EV Battery Level and Odometer sensors.

Because Hyundai / Kia Connect entity naming can differ by vehicle, account, integration version and region, verify the detected entities in your own Home Assistant installation.

## 0.1.7

- Extends the timeline beyond the period where full raw `vehicle_data` attributes are available by importing the separate Home Assistant histories for 12V battery level, HV Battery Level and odometer.
- Auto-detects those three history entities, with optional explicit entity configuration.
- Keeps raw CCS2 points and reconstructed HA historical correlation points visually and analytically distinct.
- Raw points take precedence when a historical correlation point is close to the same timestamp.
- Removes the standalone **Extern laden** UI metric and the obsolete `ev_charging_entity` configuration option.
- Keeps raw charging remaining-time only as internal/export diagnostic context; it is no longer a separate visible dashboard item.
- CSV/JSON exports include `source_kind` and per-field source timestamps for historical correlation transparency.

## 0.1.6

- Stops using the Home Assistant EV-charging switch or connected/disconnected state as evidence of charging.
- Derives charging context from raw CCS2 `Green.ChargingInformation.Charging.RemainTime > 0` when available.
- Keeps `ConnectorFastening.State` only as raw diagnostic data because it has been observed to update unreliably on this vehicle.

## 0.1.5

- Renames the user-facing traction-battery value to **HV Battery Level**.
- Documents the source as `Green.BatteryManagement.BatteryRemain.Ratio`; it is a reported remaining-battery percentage, not explicitly named State of Charge by the CCS2 field.
- Renames the public JSON/CSV field from `ev_soc` to `hv_battery_level`; the legacy internal SQLite column remains unchanged for upgrade compatibility.

## 0.1.3

- Adds a dedicated **Sinds laatste geregistreerde rit** summary.
- Adds endpoint-normalized 12V change in percentage points per 24 hours.
- Classifies ride, no-ride decline, no-ride recovery, HV-level increase and stable intervals.
- Explicitly notes whole-percent HV Battery Level resolution and hidden sleep-gap uncertainty.
