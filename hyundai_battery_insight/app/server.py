import base64
import bisect
import csv
import datetime as dt
import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from cryptography.fernet import Fernet, InvalidToken
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8099
DATA_DIR = Path('/data')
DB_PATH = DATA_DIR / 'hyundai_battery_insight.db'
OPTIONS_PATH = DATA_DIR / 'options.json'
APP_DIR = Path('/app')
HA_BASE = 'http://supervisor/core/api'
RAW_SECRET_PATH = DATA_DIR / 'raw_payload_secret.bin'
RAW_FORMAT = 'hbi-fernet-v2'
LEGACY_RAW_FORMAT = 'hbi-fernet-v1'
RAW_KDF_CONTEXT = b'Hyundai Battery Insight recoverable raw payload v2\0'
RAW_BACKUP_FORMAT = 'hbi-raw-backup-v1'
RAW_BACKUP_MAX_BYTES = 50 * 1024 * 1024
RAW_BACKUP_COLUMNS = (
    'source_ts', 'received_ts', 'aux_soc', 'ev_soc', 'ev_soh', 'odometer',
    'ev_range', 'warning_level', 'sensor_reliability', 'aux_fail_warning',
    'battery_pre_warning', 'power_state_class_c', 'sleep_mode', 'driving_ready',
    'accessory', 'ignition1', 'ignition3', 'connector_fastening',
    'charging_remain_time', 'ev_charging', 'raw_json',
)
INGRESS_PROXY_IP = '172.30.32.2'
VIN_KEY_NAMES = {'vin', 'vehiclevin', 'vinnumber', 'vehicleidentificationnumber'}
VIN_RE = re.compile(r'^[A-HJ-NPR-Z0-9]{17}$', re.IGNORECASE)


def load_supervisor_token():
    # s6-overlay can reset the service environment. Home Assistant injects the
    # API token into the container environment, and s6 keeps a persisted copy.
    for name in ('SUPERVISOR_TOKEN', 'HASSIO_TOKEN'):
        value = os.environ.get(name, '').strip()
        if value:
            return value, f'env:{name}'
    for name in ('SUPERVISOR_TOKEN', 'HASSIO_TOKEN'):
        path = Path('/run/s6/container_environment') / name
        try:
            value = path.read_text(encoding='utf-8', errors='ignore').strip('\x00\r\n ')
        except OSError:
            continue
        if value:
            return value, f's6:{name}'
    return '', 'missing'


TOKEN, TOKEN_SOURCE = load_supervisor_token()

DEFAULTS = {
    'raw_entity': 'sensor.tucson_data_2',
    'aux_soc_entity': 'auto',
    'hv_battery_entity': 'auto',
    'odometer_entity': 'auto',
    'lookback_days': 120,
    'poll_seconds': 300,
    'import_on_start': True,
}


def load_options():
    opts = DEFAULTS.copy()
    try:
        with OPTIONS_PATH.open('r', encoding='utf-8') as f:
            opts.update(json.load(f))
    except Exception:
        pass
    opts['lookback_days'] = max(7, min(365, int(opts.get('lookback_days', 120))))
    opts['poll_seconds'] = max(60, min(3600, int(opts.get('poll_seconds', 300))))
    # Old 0.1.x installations may still have this setting in options.json.
    # It is intentionally ignored from 0.1.7 onward.
    opts.pop('ev_charging_entity', None)
    return opts


OPTIONS = load_options()
RESOLVED_ENTITIES = {'aux_soc': None, 'hv_battery': None, 'odometer': None}


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.execute('''
        CREATE TABLE IF NOT EXISTS snapshots (
            source_ts TEXT PRIMARY KEY,
            received_ts TEXT,
            aux_soc REAL,
            ev_soc REAL,
            ev_soh REAL,
            odometer REAL,
            ev_range REAL,
            warning_level REAL,
            sensor_reliability INTEGER,
            aux_fail_warning INTEGER,
            battery_pre_warning INTEGER,
            power_state_class_c INTEGER,
            sleep_mode INTEGER,
            driving_ready INTEGER,
            accessory INTEGER,
            ignition1 INTEGER,
            ignition3 INTEGER,
            connector_fastening INTEGER,
            charging_remain_time REAL,
            ev_charging TEXT,
            raw_json TEXT
        )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_snapshots_source_ts ON snapshots(source_ts)')
        c.execute('''
        CREATE TABLE IF NOT EXISTS ha_history_points (
            source_ts TEXT PRIMARY KEY,
            aux_soc REAL,
            ev_soc REAL,
            odometer REAL,
            aux_source_ts TEXT,
            ev_source_ts TEXT,
            odometer_source_ts TEXT,
            imported_ts TEXT
        )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_ha_history_source_ts ON ha_history_points(source_ts)')
        c.commit()


def api_get(path):
    if not TOKEN:
        raise RuntimeError('SUPERVISOR_TOKEN is missing; homeassistant_api must be enabled')
    req = urllib.request.Request(
        HA_BASE + path,
        headers={'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode('utf-8'))


def get_path(obj, *path, default=None):
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def num(v):
    if v is None or v == '':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def intval(v):
    if v is None or v == '':
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_vehicle_ts(value, fallback=None):
    if value:
        s = str(value).strip().replace("'", '')
        try:
            base = s.split('.')[0]
            t = dt.datetime.strptime(base[:14], '%Y%m%d%H%M%S').replace(tzinfo=dt.timezone.utc)
            return t.isoformat()
        except Exception:
            pass
        try:
            d = dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
            return d.astimezone(dt.timezone.utc).isoformat()
        except Exception:
            pass
    if fallback:
        try:
            d = dt.datetime.fromisoformat(str(fallback).replace('Z', '+00:00'))
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
            return d.astimezone(dt.timezone.utc).isoformat()
        except Exception:
            return str(fallback)
    return None


def iso_to_dt(s):
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(str(s).replace('Z', '+00:00'))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc)
    except Exception:
        return None


def _normalized_key(value):
    return re.sub(r'[^a-z0-9]', '', str(value).lower())


def find_vin(obj):
    """Find a standard 17-character VIN in a nested vehicle/state payload."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if _normalized_key(key) in VIN_KEY_NAMES and isinstance(value, str):
                candidate = value.strip().upper()
                if VIN_RE.fullmatch(candidate):
                    return candidate
        for value in obj.values():
            found = find_vin(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_vin(value)
            if found:
                return found
    return None


def _vin_fingerprint(vin):
    return hashlib.sha256(vin.upper().encode('utf-8')).hexdigest()[:16]


def _raw_fernet(vin):
    """Recoverable authenticated protection derived deterministically from the VIN.

    This is deliberately not presented as high-security encryption: the VIN is
    not secret and the derivation context is part of this open-source project.
    The goal is to avoid plaintext vehicle identifiers in SQLite, preserve
    recoverability across reinstalls, and detect modified ciphertext.
    """
    digest = hashlib.sha256(
        RAW_KDF_CONTEXT + vin.upper().encode('utf-8')
    ).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _legacy_v1_fernet(vin):
    """Return the temporary v1 Fernet key if its installation secret survives."""
    try:
        secret = RAW_SECRET_PATH.read_bytes()
    except OSError:
        return None
    if len(secret) < 32:
        return None
    digest = hmac.new(
        secret[:32],
        b'Hyundai Battery Insight raw payload v1\0' + vin.upper().encode('utf-8'),
        hashlib.sha256,
    ).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def protect_raw_payload(payload, vin=None):
    """Protect the complete raw vehicle payload for recoverable local storage."""
    vin = (vin or find_vin(payload) or '').strip().upper()
    if not VIN_RE.fullmatch(vin):
        return None
    plaintext = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')
    ).encode('utf-8')
    token = _raw_fernet(vin).encrypt(plaintext).decode('ascii')
    return json.dumps({
        'format': RAW_FORMAT,
        'vin_fingerprint': _vin_fingerprint(vin),
        'ciphertext': token,
    }, separators=(',', ':'))


def decrypt_raw_payload(value, vin):
    """Decrypt and authenticate a recoverable v2 raw payload."""
    envelope = json.loads(value)
    if envelope.get('format') != RAW_FORMAT:
        raise ValueError('Unsupported raw payload format')
    if envelope.get('vin_fingerprint') != _vin_fingerprint(vin):
        raise ValueError('VIN fingerprint does not match this payload')
    plaintext = _raw_fernet(vin).decrypt(envelope['ciphertext'].encode('ascii'))
    return json.loads(plaintext.decode('utf-8'))


def decrypt_legacy_v1_payload(value, vin):
    """Decrypt the temporary v1 format when its old local secret still exists."""
    envelope = json.loads(value)
    if envelope.get('format') != LEGACY_RAW_FORMAT:
        raise ValueError('Not a legacy v1 payload')
    if envelope.get('vin_fingerprint') != _vin_fingerprint(vin):
        raise ValueError('VIN fingerprint does not match this payload')
    fernet = _legacy_v1_fernet(vin)
    if fernet is None:
        raise ValueError('Legacy v1 installation secret is unavailable')
    plaintext = fernet.decrypt(envelope['ciphertext'].encode('ascii'))
    return json.loads(plaintext.decode('utf-8'))


def migrate_raw_payloads(vin_hint=None):
    """Migrate plaintext and recoverable v1 rows to the reinstall-safe v2 format."""
    plaintext_migrated = 0
    v1_migrated = 0
    v2_verified = 0
    invalid = 0
    skipped = 0

    with db() as conn:
        rows = conn.execute(
            'SELECT source_ts, raw_json FROM snapshots WHERE raw_json IS NOT NULL'
        ).fetchall()

        for row in rows:
            value = row['raw_json']
            try:
                parsed = json.loads(value)
            except Exception:
                invalid += 1
                continue

            # Legacy plaintext vehicle_data object.
            if isinstance(parsed, dict) and 'format' not in parsed:
                vin = find_vin(parsed) or vin_hint
                protected = protect_raw_payload(parsed, vin)
                if protected is None:
                    skipped += 1
                    continue
                conn.execute(
                    'UPDATE snapshots SET raw_json=? WHERE source_ts=?',
                    (protected, row['source_ts']),
                )
                plaintext_migrated += 1
                continue

            if not isinstance(parsed, dict):
                invalid += 1
                continue

            fmt = parsed.get('format')
            if fmt == RAW_FORMAT:
                if not vin_hint:
                    skipped += 1
                    continue
                try:
                    decrypt_raw_payload(value, vin_hint)
                    v2_verified += 1
                except (InvalidToken, ValueError, KeyError, TypeError, json.JSONDecodeError):
                    invalid += 1
                continue

            if fmt == LEGACY_RAW_FORMAT:
                if not vin_hint:
                    skipped += 1
                    continue
                try:
                    plaintext = decrypt_legacy_v1_payload(value, vin_hint)
                    protected = protect_raw_payload(plaintext, vin_hint)
                    if protected is None:
                        skipped += 1
                        continue
                    conn.execute(
                        'UPDATE snapshots SET raw_json=? WHERE source_ts=?',
                        (protected, row['source_ts']),
                    )
                    v1_migrated += 1
                except (InvalidToken, ValueError, KeyError, TypeError, json.JSONDecodeError):
                    # Never delete or overwrite unreadable legacy data.
                    skipped += 1
                continue

            skipped += 1

        conn.commit()

    print(
        '[raw protection] '
        f'plaintext→v2={plaintext_migrated}, '
        f'v1→v2={v1_migrated}, '
        f'v2 verified={v2_verified}, '
        f'invalid={invalid}, skipped={skipped}',
        flush=True,
    )


def extract_snapshot(state_obj):
    attrs = state_obj.get('attributes') or {}
    vd = attrs.get('vehicle_data')
    if not isinstance(vd, dict):
        return None

    electronics = get_path(vd, 'Electronics', default={}) or {}
    battery = electronics.get('Battery') or {}
    green = vd.get('Green') or {}
    bm = green.get('BatteryManagement') or {}
    drivetrain = vd.get('Drivetrain') or {}
    fuel = drivetrain.get('FuelSystem') or {}
    power = electronics.get('PowerSupply') or {}
    remote = vd.get('RemoteControl') or {}
    ci = green.get('ChargingInformation') or {}

    source_ts = parse_vehicle_ts(vd.get('Date'), state_obj.get('last_updated'))
    if not source_ts:
        return None

    charging_remain_time = num(get_path(ci, 'Charging', 'RemainTime'))
    if charging_remain_time is None:
        charging_state = None
    elif charging_remain_time > 0:
        charging_state = 'active'
    else:
        charging_state = 'inactive'

    vin = find_vin(state_obj) or find_vin(vd)
    protected_raw = protect_raw_payload(vd, vin)

    return {
        'source_ts': source_ts,
        'received_ts': parse_vehicle_ts(None, state_obj.get('last_updated')),
        'aux_soc': num(battery.get('Level')),
        'ev_soc': num(get_path(bm, 'BatteryRemain', 'Ratio')),
        'ev_soh': num(get_path(bm, 'SoH', 'Ratio')),
        'odometer': num(drivetrain.get('Odometer')),
        'ev_range': num(get_path(fuel, 'DTE', 'EV')),
        'warning_level': num(get_path(battery, 'Charging', 'WarningLevel')),
        'sensor_reliability': intval(battery.get('SensorReliability')),
        'aux_fail_warning': intval(get_path(battery, 'Auxiliary', 'FailWarning')),
        'battery_pre_warning': intval(get_path(electronics, 'AutoCut', 'BatteryPreWarning')),
        'power_state_class_c': intval(get_path(battery, 'PowerStateAlert', 'ClassC')),
        'sleep_mode': intval(remote.get('SleepMode')),
        'driving_ready': intval(vd.get('DrivingReady', drivetrain.get('DrivingReady'))),
        'accessory': intval(power.get('Accessory')),
        'ignition1': intval(power.get('Ignition1')),
        'ignition3': intval(power.get('Ignition3')),
        # Diagnostic only. It is not used to determine whether charging is active.
        'connector_fastening': intval(get_path(ci, 'ConnectorFastening', 'State')),
        'charging_remain_time': charging_remain_time,
        'ev_charging': charging_state,
        # The complete raw snapshot is retained for diagnostics/evidence, but is
        # stored only as authenticated ciphertext when a VIN is available.
        'raw_json': protected_raw,
    }


def upsert_snapshot(s):
    if not s:
        return False
    cols = list(s.keys())
    placeholders = ','.join('?' for _ in cols)
    updates = ','.join(
        ('raw_json=COALESCE(excluded.raw_json,snapshots.raw_json)'
         if c == 'raw_json' else f'{c}=excluded.{c}')
        for c in cols if c != 'source_ts'
    )
    with db() as c:
        before = c.execute('SELECT 1 FROM snapshots WHERE source_ts=?', (s['source_ts'],)).fetchone()
        c.execute(
            f"INSERT INTO snapshots ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(source_ts) DO UPDATE SET {updates}",
            [s[c] for c in cols],
        )
        c.commit()
        return before is None


def upsert_history_point(p):
    cols = list(p.keys())
    placeholders = ','.join('?' for _ in cols)
    updates = ','.join(f'{c}=excluded.{c}' for c in cols if c != 'source_ts')
    with db() as c:
        c.execute(
            f"INSERT INTO ha_history_points ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(source_ts) DO UPDATE SET {updates}",
            [p[c] for c in cols],
        )
        c.commit()


def get_current_state(entity_id):
    if not entity_id:
        return None
    try:
        return api_get('/states/' + urllib.parse.quote(entity_id, safe='._'))
    except Exception:
        return None


def poll_once():
    raw = get_current_state(OPTIONS.get('raw_entity', ''))
    if raw:
        snap = extract_snapshot(raw)
        if snap:
            upsert_snapshot(snap)
            return find_vin(raw)
    return None


def history_chunk(entity, start, end):
    start_s = start.astimezone(dt.timezone.utc).isoformat()
    q = urllib.parse.urlencode({
        'filter_entity_id': entity,
        'end_time': end.astimezone(dt.timezone.utc).isoformat(),
    })
    path = '/history/period/' + urllib.parse.quote(start_s, safe=':+-T') + '?' + q
    data = api_get(path)
    if isinstance(data, list) and data:
        return data[0] if isinstance(data[0], list) else []
    return []


def _candidate_score(state, role):
    entity_id = str(state.get('entity_id') or '').lower()
    attrs = state.get('attributes') or {}
    name = str(attrs.get('friendly_name') or '').lower()
    unit = str(attrs.get('unit_of_measurement') or '').lower()
    device_class = str(attrs.get('device_class') or '').lower()
    text = f'{entity_id} {name}'
    score = 0

    if role == 'aux_soc':
        if 'car_battery' in entity_id:
            score += 130
        if 'car battery' in name:
            score += 120
        if '12v' in text:
            score += 100
        if 'battery soc' in name and 'ev' not in name:
            score += 70
        if 'ev battery' in text or 'state of health' in text or 'soh' in entity_id:
            score -= 180
        if unit == '%':
            score += 20
        if device_class == 'battery':
            score += 10
    elif role == 'hv_battery':
        if 'ev_battery_level' in entity_id:
            score += 150
        if 'ev battery level' in name:
            score += 130
        if 'hv battery' in name:
            score += 110
        if 'state of health' in text or 'soh' in entity_id or 'charging current limit' in text:
            score -= 180
        if unit == '%':
            score += 20
        if device_class == 'battery':
            score += 10
    elif role == 'odometer':
        if 'odometer' in entity_id:
            score += 150
        if 'odometer' in name:
            score += 130
        if unit in ('km', 'mi'):
            score += 20
        if device_class == 'distance':
            score += 10
    return score


def resolve_history_entities():
    try:
        states = api_get('/states')
    except Exception as e:
        print(f'[history] cannot discover sensor entities: {e}', flush=True)
        return RESOLVED_ENTITIES.copy()

    mapping = {
        'aux_soc': OPTIONS.get('aux_soc_entity', 'auto'),
        'hv_battery': OPTIONS.get('hv_battery_entity', 'auto'),
        'odometer': OPTIONS.get('odometer_entity', 'auto'),
    }
    by_id = {s.get('entity_id'): s for s in states if isinstance(s, dict)}
    resolved = {}
    for role, configured in mapping.items():
        configured = str(configured or 'auto').strip()
        if configured.lower() != 'auto':
            resolved[role] = configured if configured in by_id else configured
            continue
        candidates = sorted(
            ((_candidate_score(s, role), s.get('entity_id')) for s in states if isinstance(s, dict)),
            reverse=True,
        )
        best_score, best_id = candidates[0] if candidates else (0, None)
        resolved[role] = best_id if best_score >= 80 else None

    RESOLVED_ENTITIES.update(resolved)
    print(
        '[history] resolved entities: '
        f"12V={resolved.get('aux_soc') or 'not found'}, "
        f"HV={resolved.get('hv_battery') or 'not found'}, "
        f"odometer={resolved.get('odometer') or 'not found'}",
        flush=True,
    )
    return resolved


def _history_events(entity, start, now):
    if not entity:
        return []
    events_by_ts = {}
    cur = start
    while cur < now:
        end = min(cur + dt.timedelta(days=14), now)
        try:
            rows = history_chunk(entity, cur, end)
            for row in rows:
                value = num(row.get('state'))
                if value is None:
                    continue
                ts = parse_vehicle_ts(None, row.get('last_updated') or row.get('last_changed'))
                t = iso_to_dt(ts)
                if not ts or not t or t < start or t > now:
                    continue
                events_by_ts[ts] = {'ts': ts, 'dt': t, 'value': value}
        except Exception as e:
            print(f'[history:{entity}] {cur.isoformat()}..{end.isoformat()}: {e}', flush=True)
        cur = end

    events = sorted(events_by_ts.values(), key=lambda x: x['dt'])
    # Recorder can contain repeated updates with the same state. For a correlation
    # timeline, keep the initial state and actual value transitions only.
    compressed = []
    for e in events:
        if compressed and abs(e['value'] - compressed[-1]['value']) < 1e-9:
            continue
        compressed.append(e)
    return compressed


def _cluster_event_times(event_lists, seconds=120):
    times = sorted(e['dt'] for events in event_lists for e in events)
    if not times:
        return []
    clusters = [[times[0]]]
    for t in times[1:]:
        if (t - clusters[-1][-1]).total_seconds() <= seconds:
            clusters[-1].append(t)
        else:
            clusters.append([t])
    return [max(c) for c in clusters]


def _value_at(events, t, near_seconds=120):
    if not events:
        return None, None
    dts = [e['dt'] for e in events]
    i = bisect.bisect_right(dts, t) - 1
    prev = events[i] if i >= 0 else None
    nxt = events[i + 1] if i + 1 < len(events) else None
    # Same vehicle refresh can update separate sensor entities milliseconds apart.
    # Prefer a near-future value if it is within the clustering window.
    if nxt and 0 <= (nxt['dt'] - t).total_seconds() <= near_seconds:
        if prev is None or abs((nxt['dt'] - t).total_seconds()) < abs((t - prev['dt']).total_seconds()):
            return nxt['value'], nxt['ts']
    if prev:
        return prev['value'], prev['ts']
    return None, None


def import_correlated_history(start, now):
    entities = resolve_history_entities()
    aux_events = _history_events(entities.get('aux_soc'), start, now)
    hv_events = _history_events(entities.get('hv_battery'), start, now)
    odo_events = _history_events(entities.get('odometer'), start, now)

    if not aux_events:
        print('[history] no numeric 12V sensor history found; raw CCS2 history remains available', flush=True)
        return

    cluster_times = _cluster_event_times([aux_events, hv_events, odo_events])
    imported_ts = dt.datetime.now(dt.timezone.utc).isoformat()
    with db() as c:
        c.execute('DELETE FROM ha_history_points WHERE source_ts >= ?', (start.isoformat(),))
        c.commit()

    count = 0
    last_signature = None
    for t in cluster_times:
        aux, aux_ts = _value_at(aux_events, t)
        hv, hv_ts = _value_at(hv_events, t)
        odo, odo_ts = _value_at(odo_events, t)
        if aux is None:
            continue
        signature = (aux, hv, odo)
        # Skip a cluster that changes no tracked value after carry-forward.
        if signature == last_signature:
            continue
        last_signature = signature
        p = {
            'source_ts': t.astimezone(dt.timezone.utc).isoformat(),
            'aux_soc': aux,
            'ev_soc': hv,
            'odometer': odo,
            'aux_source_ts': aux_ts,
            'ev_source_ts': hv_ts,
            'odometer_source_ts': odo_ts,
            'imported_ts': imported_ts,
        }
        upsert_history_point(p)
        count += 1
    print(f'[history] built {count} HA historical correlation points', flush=True)


def import_raw_history(start, now):
    entity = OPTIONS.get('raw_entity', '')
    if not entity:
        return
    cur = start
    count = 0
    while cur < now:
        end = min(cur + dt.timedelta(days=14), now)
        try:
            rows = history_chunk(entity, cur, end)
            for row in rows:
                snap = extract_snapshot(row)
                if snap:
                    upsert_snapshot(snap)
                    count += 1
        except Exception as e:
            print(f'[history:raw] {cur.isoformat()}..{end.isoformat()}: {e}', flush=True)
        cur = end
    print(f'[history] processed {count} raw CCS2 history states', flush=True)


def import_history():
    now = dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(days=OPTIONS['lookback_days'])
    import_raw_history(start, now)
    import_correlated_history(start, now)


def poll_loop():
    while True:
        try:
            poll_once()
        except Exception as e:
            print(f'[poll] {e}', flush=True)
        time.sleep(OPTIONS['poll_seconds'])


def charging_active(row):
    value = row.get('charging_remain_time')
    if value is None:
        return None
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return None


def classify(prev, cur):
    def delta(a, b):
        return None if a is None or b is None else round(b - a, 3)

    aux_d = delta(prev.get('aux_soc'), cur.get('aux_soc'))
    ev_d = delta(prev.get('ev_soc'), cur.get('ev_soc'))
    odo_d = delta(prev.get('odometer'), cur.get('odometer'))
    t1, t2 = iso_to_dt(prev.get('source_ts')), iso_to_dt(cur.get('source_ts'))
    hours = round((t2 - t1).total_seconds() / 3600, 2) if t1 and t2 else None
    drove = odo_d is not None and odo_d > 0.05
    raw_pair = prev.get('source_kind') == 'raw' and cur.get('source_kind') == 'raw'
    charge_seen = charging_active(prev) is True or charging_active(cur) is True
    aux_rate_24h = None
    if aux_d is not None and hours is not None and hours > 0:
        aux_rate_24h = round(aux_d * 24.0 / hours, 2)

    facts = []
    if aux_d is not None:
        facts.append(f'12V SoC {aux_d:+g} pp')
    if ev_d is not None:
        facts.append(f'HV Battery Level {ev_d:+g} pp')
    if odo_d is not None:
        facts.append(f'odometer {odo_d:+.1f} km')
    if hours is not None:
        facts.append(f'{hours:g} h tussen meetpunten')
    if aux_rate_24h is not None and not drove:
        facts.append(f'netto omgerekend {aux_rate_24h:+g} pp/24h')
    if not raw_pair:
        facts.append('bron: HA historical correlation')

    if drove:
        event_type = 'ride'
        label = 'Rit tussen meetpunten'
        inference = 'De odometerstijging ondersteunt dat er tussen deze meetpunten is gereden; 12V kan daarbij normaal zijn bijgeladen.'
    elif ev_d is not None and ev_d >= 1:
        event_type = 'hv_increase_no_ride'
        label = 'HV Battery Level hoger zonder geregistreerde rit'
        inference = 'Dit is verenigbaar met laden tussen de meetpunten; de oorzaak is uit alleen deze endpoints niet definitief vast te stellen.'
    elif aux_d is not None and aux_d >= 3:
        event_type = 'recovery_no_ride'
        label = '12V-herstel zonder geregistreerde rit'
        if ev_d is not None and ev_d <= -1:
            inference = ('De combinatie van 12V-herstel, geen odometertoename en een lager HV Battery Level '
                         'is verenigbaar met HV→12V-bijlading, maar bewijst geen causale laadcyclus.')
        else:
            inference = 'Mogelijke automatische 12V-bijlading of SoC-herkalibratie; met endpoints alleen niet te onderscheiden.'
    elif aux_d is not None and aux_d <= -2:
        event_type = 'decline_no_ride'
        label = 'Lagere 12V-SoC bij volgend meetpunt'
        inference = 'Netto daling tussen meetpunten zonder geregistreerde rit. Het tussentijdse verloop is onbekend.'
    else:
        event_type = 'stable_no_ride'
        label = 'Geen grote 12V-verandering'
        inference = 'Geen sterke conclusie uit dit interval.'

    if charge_seen:
        inference += ' Een raw CCS2-endpoint in dit interval rapporteerde een positieve resterende laadtijd.'

    unknown_parts = [
        'Gebeurtenissen tussen de meetpunten kunnen ontbreken; een slapende auto uploadt niet continu.'
    ]
    if not raw_pair:
        unknown_parts.append(
            'Dit interval bevat apart opgeslagen Home Assistant-sensorhistorie; waarden hoeven niet uit exact dezelfde voertuigsnapshot te komen.'
        )
    if not drove and ev_d is not None and abs(ev_d) < 0.5:
        unknown_parts.append(
            'Een ongewijzigd HV Battery Level op hele procenten sluit een kleine HV→12V-energieoverdracht niet uit.'
        )
    if aux_rate_24h is not None and not drove:
        unknown_parts.append(
            'De pp/24h-waarde is alleen endpoint-normalisatie; het is geen gemeten continue ontlaadsnelheid.'
        )

    return {
        'from': prev.get('source_ts'), 'to': cur.get('source_ts'), 'hours': hours,
        'aux_delta': aux_d, 'hv_battery_level_delta': ev_d, 'odo_delta': odo_d,
        'aux_rate_24h': aux_rate_24h, 'event_type': event_type,
        'source_quality': 'raw' if raw_pair else 'ha_history',
        'label': label, 'facts': '; '.join(facts), 'inference': inference,
        'unknown': ' '.join(unknown_parts),
    }


def _raw_rows(days):
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    with db() as c:
        rows = [dict(r) for r in c.execute(
            'SELECT * FROM snapshots WHERE source_ts >= ? ORDER BY source_ts', (cutoff.isoformat(),)
        ).fetchall()]
    for r in rows:
        r['source_kind'] = 'raw'
        r['aux_source_ts'] = r['source_ts']
        r['ev_source_ts'] = r['source_ts']
        r['odometer_source_ts'] = r['source_ts']
    return rows


def _history_rows(days):
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    with db() as c:
        rows = [dict(r) for r in c.execute(
            'SELECT * FROM ha_history_points WHERE source_ts >= ? ORDER BY source_ts', (cutoff.isoformat(),)
        ).fetchall()]
    out = []
    for r in rows:
        out.append({
            'source_ts': r['source_ts'], 'received_ts': None,
            'aux_soc': r['aux_soc'], 'ev_soc': r['ev_soc'], 'odometer': r['odometer'],
            'ev_soh': None, 'ev_range': None, 'warning_level': None,
            'sensor_reliability': None, 'aux_fail_warning': None,
            'battery_pre_warning': None, 'power_state_class_c': None,
            'sleep_mode': None, 'driving_ready': None, 'accessory': None,
            'ignition1': None, 'ignition3': None, 'connector_fastening': None,
            'charging_remain_time': None, 'ev_charging': None, 'raw_json': None,
            'source_kind': 'ha_history',
            'aux_source_ts': r['aux_source_ts'], 'ev_source_ts': r['ev_source_ts'],
            'odometer_source_ts': r['odometer_source_ts'],
        })
    return out


def query_timeline(days):
    raw = _raw_rows(days)
    hist = _history_rows(days)
    raw_times = sorted(iso_to_dt(r['source_ts']) for r in raw if iso_to_dt(r['source_ts']))

    def near_raw(t, seconds=300):
        if not raw_times or not t:
            return False
        i = bisect.bisect_left(raw_times, t)
        candidates = []
        if i < len(raw_times):
            candidates.append(raw_times[i])
        if i > 0:
            candidates.append(raw_times[i - 1])
        return any(abs((x - t).total_seconds()) <= seconds for x in candidates)

    combined = list(raw)
    for r in hist:
        if not near_raw(iso_to_dt(r['source_ts'])):
            combined.append(r)
    combined.sort(key=lambda r: r['source_ts'])
    intervals = [classify(combined[i - 1], combined[i]) for i in range(1, len(combined))]
    return combined, intervals, raw


def since_last_trip_context(rows, intervals):
    if len(rows) < 2 or not intervals:
        return {'available': False, 'reason': 'Nog onvoldoende meetpunten.'}

    last_ride_i = None
    for i, interval in enumerate(intervals):
        if interval.get('event_type') == 'ride':
            last_ride_i = i
    if last_ride_i is None:
        return {'available': False, 'reason': 'Geen odometertoename gevonden binnen het gekozen tijdvenster.'}

    baseline_index = last_ride_i + 1
    baseline = rows[baseline_index]
    latest = rows[-1]
    t0, t1 = iso_to_dt(baseline['source_ts']), iso_to_dt(latest['source_ts'])
    hours = round((t1 - t0).total_seconds() / 3600, 2) if t0 and t1 else None

    def delta(a, b, digits=3):
        return None if a is None or b is None else round(b - a, digits)

    aux_d = delta(baseline.get('aux_soc'), latest.get('aux_soc'))
    ev_d = delta(baseline.get('ev_soc'), latest.get('ev_soc'))
    odo_d = delta(baseline.get('odometer'), latest.get('odometer'))
    rate = round(aux_d * 24.0 / hours, 2) if aux_d is not None and hours and hours > 0 else None
    post_intervals = intervals[last_ride_i + 1:]
    recoveries = sum(1 for x in post_intervals if x.get('event_type') == 'recovery_no_ride')
    declines = sum(1 for x in post_intervals if x.get('event_type') == 'decline_no_ride')
    point_count = len(rows) - baseline_index
    last_ride = intervals[last_ride_i]

    facts = []
    if aux_d is not None:
        facts.append(f'12V {baseline.get("aux_soc"):g}% → {latest.get("aux_soc"):g}% ({aux_d:+g} pp)')
    if ev_d is not None:
        facts.append(f'HV Battery Level {baseline.get("ev_soc"):g}% → {latest.get("ev_soc"):g}% ({ev_d:+g} pp netto)')
    if odo_d is not None:
        facts.append(f'odometer na rit {odo_d:+.1f} km')
    if hours is not None:
        facts.append(f'{hours:g} h sinds rit-meetpunt')
    if rate is not None:
        facts.append(f'netto omgerekend {rate:+g} pp/24h')

    uses_history = any(r.get('source_kind') != 'raw' for r in rows[baseline_index:])
    unknown = (
        'Niet-geüploade laadcycli en SoC-herkalibraties tijdens slaap blijven onbekend. '
        'HV Battery Level wordt in hele procentpunten weergegeven, waardoor kleine HV→12V-energieoverdracht onzichtbaar kan blijven. '
        'De pp/24h-waarde is endpoint-normalisatie en geen gemeten continue lekstroom.'
    )
    if uses_history:
        unknown += ' Een deel van deze periode is uit afzonderlijke Home Assistant-sensorhistorie gecorreleerd en is dus geen enkele raw voertuigsnapshot.'

    return {
        'available': True,
        'baseline_ts': baseline['source_ts'], 'latest_ts': latest['source_ts'],
        'baseline_source_kind': baseline.get('source_kind'), 'latest_source_kind': latest.get('source_kind'),
        'hours': hours,
        'aux_start': baseline.get('aux_soc'), 'aux_end': latest.get('aux_soc'),
        'aux_delta': aux_d, 'aux_rate_24h': rate,
        'hv_start': baseline.get('ev_soc'), 'hv_end': latest.get('ev_soc'), 'hv_delta': ev_d,
        'odo_start': baseline.get('odometer'), 'odo_end': latest.get('odometer'), 'odo_delta': odo_d,
        'last_trip_distance': last_ride.get('odo_delta'),
        'data_points': point_count,
        'recovery_events': recoveries, 'decline_events': declines,
        'uses_history_correlation': uses_history,
        'facts': '; '.join(facts),
        'inference': ('Opeenvolgende endpoints na de laatst geregistreerde rit kunnen een netto 12V-trend tijdens stilstand laten zien. '
                      'Dit beschrijft de bekende endpoints, niet het verborgen verloop ertussen.'),
        'unknown': unknown,
    }


def raw_backup_document():
    """Build a portable backup of all raw snapshot rows.

    The complete raw_json field remains in its protected-at-rest representation.
    Extracted timeline fields such as battery percentages, odometer and timestamps
    are intentionally included so the snapshot rows can be reconstructed exactly.
    """
    with db() as conn:
        rows = [
            dict(row) for row in conn.execute(
                'SELECT * FROM snapshots ORDER BY source_ts'
            ).fetchall()
        ]

    vehicle_fingerprint = None
    for row in reversed(rows):
        value = row.get('raw_json')
        if not value:
            continue
        try:
            envelope = json.loads(value)
        except Exception:
            continue
        if isinstance(envelope, dict) and envelope.get('vin_fingerprint'):
            vehicle_fingerprint = envelope.get('vin_fingerprint')
            break

    return {
        'format': RAW_BACKUP_FORMAT,
        'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'vehicle_fingerprint': vehicle_fingerprint,
        'snapshot_count': len(rows),
        'snapshots': [
            {column: row.get(column) for column in RAW_BACKUP_COLUMNS}
            for row in rows
        ],
    }


def _current_vehicle_vin():
    raw = get_current_state(OPTIONS.get('raw_entity', ''))
    return find_vin(raw) if raw else None


def restore_raw_backup_document(document):
    """Merge a manual raw backup into the snapshots table by source timestamp."""
    if not isinstance(document, dict) or document.get('format') != RAW_BACKUP_FORMAT:
        raise ValueError('Ongeldig raw-backupformaat.')

    snapshots = document.get('snapshots')
    if not isinstance(snapshots, list):
        raise ValueError('Raw-backup bevat geen geldige snapshots-lijst.')

    current_vin = _current_vehicle_vin()
    backup_fingerprint = document.get('vehicle_fingerprint')
    if current_vin and backup_fingerprint:
        if backup_fingerprint != _vin_fingerprint(current_vin):
            raise ValueError('Deze raw-backup hoort bij een ander voertuig (VIN-fingerprint wijkt af).')

    inserted = 0
    updated = 0
    skipped = 0
    cols = list(RAW_BACKUP_COLUMNS)
    placeholders = ','.join('?' for _ in cols)
    updates = ','.join(f'{col}=excluded.{col}' for col in cols if col != 'source_ts')

    with db() as conn:
        for source in snapshots:
            if not isinstance(source, dict):
                skipped += 1
                continue

            row = {column: source.get(column) for column in cols}
            source_ts = row.get('source_ts')
            if not isinstance(source_ts, str) or iso_to_dt(source_ts) is None:
                skipped += 1
                continue

            raw_value = row.get('raw_json')
            if raw_value is not None and not isinstance(raw_value, str):
                skipped += 1
                continue

            existed = conn.execute(
                'SELECT 1 FROM snapshots WHERE source_ts=?', (source_ts,)
            ).fetchone() is not None

            conn.execute(
                f"INSERT INTO snapshots ({','.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(source_ts) DO UPDATE SET {updates}",
                [row[column] for column in cols],
            )
            if existed:
                updated += 1
            else:
                inserted += 1
        conn.commit()

    # This also upgrades any legacy plaintext rows from older backups.
    if current_vin:
        migrate_raw_payloads(current_vin)

    return {
        'ok': True,
        'inserted': inserted,
        'updated': updated,
        'skipped': skipped,
        'total_in_file': len(snapshots),
    }


def public_snapshot(r):
    return {
        'source_ts': r.get('source_ts'),
        'source_kind': r.get('source_kind', 'raw'),
        'aux_soc': r.get('aux_soc'),
        'hv_battery_level': r.get('ev_soc'),
        'odometer': r.get('odometer'),
        'warning_level': r.get('warning_level'),
        'sleep_mode': r.get('sleep_mode'),
        'sensor_reliability': r.get('sensor_reliability'),
        'aux_fail_warning': r.get('aux_fail_warning'),
        'battery_pre_warning': r.get('battery_pre_warning'),
        'aux_source_ts': r.get('aux_source_ts'),
        'hv_source_ts': r.get('ev_source_ts'),
        'odometer_source_ts': r.get('odometer_source_ts'),
        # Kept in exports/API as diagnostics, but deliberately not shown as a standalone UI metric.
        'charging_remaining_minutes': r.get('charging_remain_time'),
    }


def current_context(raw_rows):
    if not raw_rows:
        return {}
    r = raw_rows[-1]
    ts = iso_to_dt(r['source_ts'])
    age_h = None
    if ts:
        age_h = round((dt.datetime.now(dt.timezone.utc) - ts).total_seconds() / 3600, 1)
    return {
        'source_ts': r['source_ts'], 'snapshot_age_hours': age_h,
        'aux_soc': r['aux_soc'], 'hv_battery_level': r['ev_soc'], 'odometer': r['odometer'],
        'warning_level': r['warning_level'], 'sleep_mode': r['sleep_mode'],
        'sensor_reliability': r['sensor_reliability'], 'aux_fail_warning': r['aux_fail_warning'],
        'battery_pre_warning': r['battery_pre_warning'],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print('[http] ' + (fmt % args), flush=True)

    def client_allowed(self):
        # This app is ingress-only: accept HTTP only from the Supervisor ingress proxy.
        return self.client_address[0] == INGRESS_PROXY_IP

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json_download(self, obj, filename):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.client_allowed():
            self.send_json({'error': 'forbidden'}, 403)
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip('/') or '/'
        qs = urllib.parse.parse_qs(parsed.query)
        if path == '/api/data':
            days = max(1, min(365, int(qs.get('days', [OPTIONS['lookback_days']])[0])))
            rows, intervals, raw_rows = query_timeline(days)
            self.send_json({
                'options': OPTIONS,
                'resolved_entities': RESOLVED_ENTITIES,
                'current': current_context(raw_rows),
                'since_last_trip': since_last_trip_context(rows, intervals),
                'snapshots': [public_snapshot(r) for r in rows],
                'intervals': intervals,
                'coverage': {
                    'raw_points': sum(1 for r in rows if r.get('source_kind') == 'raw'),
                    'ha_history_points': sum(1 for r in rows if r.get('source_kind') == 'ha_history'),
                },
                'method': {
                    'vehicle_wake': 'No direct Hyundai/Bluelink calls are made.',
                    'raw_snapshot': 'Raw points use the CCS2 vehicle timestamp and one nested vehicle_data payload.',
                    'history_correlation': 'Older points can combine separate Home Assistant histories for 12V, HV Battery Level and odometer. These are marked as HA historical correlation, not as one vehicle snapshot.',
                    'gap_rule': 'No interpolation is treated as a measurement; events while asleep can be invisible.',
                    'warning_level': 'Displayed as an API field; its exact Hyundai semantic is not assumed.',
                    'hv_battery_level_resolution': 'Whole-percent HV Battery Level can hide small HV-to-12V energy transfers.',
                    'rate_24h': 'Percentage-points per 24h is endpoint-normalized, not a continuous discharge-current measurement.',
                    'raw_storage': 'Full raw vehicle payloads are stored locally using recoverable authenticated protection derived from the VIN. It is intended to avoid plaintext identifiers and detect modified ciphertext, not as high-security encryption.',
                },
            })
            return
        if path == '/api/refresh':
            try:
                poll_once()
                self.send_json({'ok': True})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)}, 500)
            return
        if path == '/api/export.csv':
            days = max(1, min(365, int(qs.get('days', [OPTIONS['lookback_days']])[0])))
            rows, _, _ = query_timeline(days)
            output = io.StringIO()
            fields = [
                'source_ts', 'source_kind', 'aux_soc', 'hv_battery_level', 'odometer',
                'warning_level', 'sleep_mode', 'sensor_reliability', 'aux_fail_warning',
                'battery_pre_warning', 'aux_source_ts', 'hv_source_ts', 'odometer_source_ts',
                'charging_remaining_minutes'
            ]
            w = csv.DictWriter(output, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow(public_snapshot(r))
            body = output.getvalue().encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv; charset=utf-8')
            self.send_header('Content-Disposition', 'attachment; filename="hyundai_battery_insight.csv"')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == '/api/export.json':
            days = max(1, min(365, int(qs.get('days', [OPTIONS['lookback_days']])[0])))
            rows, intervals, _ = query_timeline(days)
            self.send_json({
                'snapshots': [public_snapshot(r) for r in rows],
                'intervals': intervals,
                'since_last_trip': since_last_trip_context(rows, intervals),
                'resolved_entities': RESOLVED_ENTITIES,
            })
            return
        if path == '/api/raw/backup':
            backup = raw_backup_document()
            stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
            self.send_json_download(
                backup, f'hyundai-battery-insight-raw-backup-{stamp}.json'
            )
            return

        try:
            body = (APP_DIR / 'index.html').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def do_POST(self):
        if not self.client_allowed():
            self.send_json({'error': 'forbidden'}, 403)
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip('/') or '/'
        if path != '/api/raw/restore':
            self.send_json({'error': 'not found'}, 404)
            return

        try:
            length = int(self.headers.get('Content-Length') or '0')
        except ValueError:
            length = 0
        if length <= 0:
            self.send_json({'ok': False, 'error': 'Lege restore-aanvraag.'}, 400)
            return
        if length > RAW_BACKUP_MAX_BYTES:
            self.send_json({'ok': False, 'error': 'Raw-backupbestand is te groot.'}, 413)
            return

        try:
            body = self.rfile.read(length)
            document = json.loads(body.decode('utf-8'))
            result = restore_raw_backup_document(document)
            self.send_json(result)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as e:
            self.send_json({'ok': False, 'error': str(e)}, 400)
        except Exception as e:
            self.send_json({'ok': False, 'error': str(e)}, 500)


def main():
    init_db()
    print(f'[startup] Home Assistant API token source: {TOKEN_SOURCE}', flush=True)

    vin_hint = None
    try:
        # Reading the current Home Assistant state is local and does not request a
        # Hyundai/Bluelink vehicle refresh.
        vin_hint = poll_once()
    except Exception as e:
        print(f'[startup poll] {e}', flush=True)

    if vin_hint:
        migrate_raw_payloads(vin_hint)
    else:
        print(
            '[raw protection] no VIN found in the current vehicle state; '
            'legacy raw payload migration is deferred until a VIN is available',
            flush=True,
        )

    if OPTIONS.get('import_on_start', True):
        print(f"[startup] importing up to {OPTIONS['lookback_days']} days of HA Recorder history", flush=True)
        import_history()
    else:
        # Resolve names anyway so the UI can report which entities would be used.
        resolve_history_entities()

    threading.Thread(target=poll_loop, daemon=True).start()
    print(f'[startup] listening on :{PORT}; raw entity={OPTIONS.get("raw_entity")}', flush=True)
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()


if __name__ == '__main__':
    main()