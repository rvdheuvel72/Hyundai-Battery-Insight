#!/usr/bin/env python3
"""Raw location freshness and shopping-outing regression checks (synthetic data)."""
import datetime as dt
import importlib.util
import tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("hbi_gps_test", Path("hyundai_battery_insight/app/server.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

def source(gps_date):
    return {"attributes": {"vehicle_data": {
        "Date": "20261020150531.000",
        "Location": {"Date": gps_date,
                     "GeoCoord": {"Latitude": 11.11, "Longitude": 21.21},
                     "Speed": {"Unit": 0, "Value": 0}, "Heading": 320},
        "Drivetrain": {"Odometer": 1000.0, "FuelSystem": {"FuelLevel": 33}},
        "Electronics": {"Battery": {"Level": 72}},
        "Green": {"BatteryManagement": {"BatteryRemain": {"Ratio": 88.5}}},
    }}, "last_updated": "2026-10-20T15:05:31+00:00"}

def point(hour, km, aux, hv, lat, lon):
    stamp = f"2026-10-20T{hour}:00+00:00"
    return {"source_ts": stamp, "source_kind": "raw", "aux_soc": aux,
            "ev_soc": hv, "odometer": km, "charging_remain_time": None,
            "geo_source_ts": stamp, "geo_latitude": lat,
            "geo_longitude": lon, "fuel_level": 33}

def main():
    with tempfile.TemporaryDirectory() as folder:
        m.DATA_DIR = Path(folder)
        m.DB_PATH = Path(folder) / "insight.db"
        m.init_db()
        m.init_db()
        tracker = {'state': 'home', 'last_updated': '2026-10-20T15:05:31+00:00',
                   'attributes': {'latitude': 0, 'longitude': 0}}
        assert m.valid_position(tracker) is None
        tracker['attributes']['latitude'] = 52.0
        tracker['attributes']['longitude'] = 4.0
        assert m.valid_position(tracker) is not None
        stale = m.extract_snapshot(source("20260330102504.607"))
        assert stale["fuel_level"] == 33
        assert stale["geo_source_ts"].startswith("2026-03-30")
        assert stale["source_ts"].startswith("2026-10-20")
        assert m.upsert_snapshot(stale)
        raw = m._raw_rows_range(
            dt.datetime(2026, 10, 19, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 10, 21, tzinfo=dt.timezone.utc),
        )
        ha = {"source_ts": stale["source_ts"], "latitude": 11.11,
              "longitude": 21.21, "source_kind": "ha_tracker"}
        m.attach_fresh_gps(raw, [ha])
        assert "_gps" not in raw[0]
        public = m.public_snapshot(raw[0])
        assert public["fuel_level"] == 33
        assert public["gps_fix_age_hours"] > 4000
        assert "geo_latitude" not in public

        fresh = m.extract_snapshot(source("20261020150531.000"))
        m.attach_fresh_gps([fresh], [ha])
        assert fresh["_gps"]["source_kind"] == "raw_ccs2"
        samples = [
            point("09:00", 100, 63, 90, 11.1, 21.2),
            point("09:12", 108, 65, 81, 11.12, 21.25),
            point("09:52", 108, 65, 81, 11.12, 21.25),
            point("10:05", 116, 68, 72, 11.1, 21.2),
        ]
        m.attach_fresh_gps(samples, [])
        intervals = [m.classify(a, b) for a, b in zip(samples, samples[1:])]
        legs = m.group_journeys(samples, intervals)
        assert len(legs) == 2
        assert [j["distance_km"] for j in legs] == [8, 8]
        assert intervals[1]["event_type"] != "ride"
        assert all("GPS verplaatsing" in j["supporting_signals"] for j in legs)
        outings = m.group_outings(legs, intervals, samples)
        assert len(outings) == 1 and outings[0]["gps_return_to_start"] is True
        doc = m.interval_export_document(
            samples, intervals, dt.datetime(2026, 10, 19, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 10, 21, tzinfo=dt.timezone.utc))
        assert len(doc["journeys"]) == 2 and len(doc["outings"]) == 1
        exported = m.interval_export_rows(samples, intervals)
        assert len(exported) == 3
        assert doc["outings"][0]["gps_return_to_start"] is True
        assert doc["outings"][0]["return_distance_km"] == 0
        assert doc["outings"][0]["confidence"] == "GPS ondersteunt terugkeer"
        for interval in (exported[0], exported[2]):
            assert interval["outing_gps_return_to_start"] is True
            assert interval["outing_return_distance_km"] == 0
            assert "geo_latitude" not in interval

        zero_backup = {'format': m.DATA_BACKUP_FORMAT, 'snapshots': [],
                       'gps_points': [{'source_ts': '2026-10-20T16:00:00+00:00',
                                       'latitude': 0, 'longitude': 0,
                                       'source_kind': 'ha_tracker'}]}
        zero_result = m.restore_raw_backup_document(zero_backup)
        assert zero_result['gps_skipped'] == 1
        assert zero_result['gps_inserted'] == 0
        backup = m.raw_backup_document()
        assert backup["gps_points"][0]["source_kind"] == "raw_ccs2"
        assert "geo_source_ts" in backup["snapshots"][0]

        # A previous-format backup must update available fields without
        # erasing fuel and GPS metadata that did not exist in that format.
        m._current_vehicle_vin = lambda: None
        older = {
            "format": m.RAW_BACKUP_FORMAT,
            "snapshots": [{
                "source_ts": stale["source_ts"], "aux_soc": 64,
                "odometer": 1000.0, "raw_json": None
            }]
        }
        assert m.restore_raw_backup_document(older)["updated"] == 1
        with m.db() as db:
            restored = dict(db.execute(
                "SELECT aux_soc, fuel_level, geo_source_ts, geo_latitude, "
                "geo_longitude, geo_speed, geo_heading FROM snapshots "
                "WHERE source_ts=?", (stale["source_ts"],)
            ).fetchone())
        assert restored["aux_soc"] == 64
        assert restored["fuel_level"] == 33
        assert restored["geo_source_ts"] == stale["geo_source_ts"]
        assert restored["geo_latitude"] == stale["geo_latitude"]
        assert restored["geo_longitude"] == stale["geo_longitude"]
        assert restored["geo_speed"] == 0
        assert restored["geo_heading"] == 320
        print("GPS return evidence / legacy backup preservation / shopping stop: OK")

if __name__ == "__main__":
    main()
