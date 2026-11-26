# Copyright (c) 2026 NVX One (nvxone.com). All rights reserved. Proprietary - see LICENSE. No copying or reuse without written permission.
"""NVX Assistant drone link: find the aircraft on USB, sign its Service unlock, read firmware and logs.

No UI code here. One DroneLink object owns the serial port; call its methods from one worker thread.
"""
import json
import os
import re
import secrets
import time
import urllib.request

os.environ['MAVLINK20'] = '1'
from pymavlink import mavutil  # noqa: E402
from serial.tools import list_ports  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402

PX4_USB_VID = 0x26AC
SAM_PAYLOAD_TYPE = 20054
SERVICE_SECONDS = 15 * 60
LOG_CHUNK = 90
SHELL = mavutil.mavlink.SERIAL_CONTROL_DEV_SHELL
SHELL_FLAGS = (mavutil.mavlink.SERIAL_CONTROL_FLAG_RESPOND
               | mavutil.mavlink.SERIAL_CONTROL_FLAG_EXCLUSIVE)


class LinkError(Exception):
    pass


def find_drone_port():
    """COM port of a PX4 flight controller on USB, or None."""
    for port in list_ports.comports():
        if port.vid == PX4_USB_VID:
            return port.device
    return None


class ChallengeSigner:
    """Signs the aircraft's Service challenge with the NVX Ed25519 key kept on this computer."""

    def __init__(self, key_path):
        self.key_path = key_path
        self._key = None

    def available(self):
        return bool(self.key_path) and os.path.isfile(self.key_path)

    def sign(self, request_id, nonce):
        if self._key is None:
            with open(self.key_path, 'rb') as f:
                self._key = serialization.load_pem_private_key(f.read(), password=None)
        # Same 48 bytes nvx_security.cpp verifies: domain, request id, mode, version, nonce.
        message = b'NVX-SAM-V1' + request_id.to_bytes(4, 'little') + bytes([1, 1]) + nonce
        return self._key.sign(message)


def fetch_update_manifest(url, timeout=8):
    """Latest published aircraft update from the NVX server, or None when offline."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8')).get('aircraft')
    except Exception:
        return None


def update_status(firmware, aircraft_update):
    """Plain-language comparison of the installed build and the published one."""
    if not aircraft_update:
        return 'Update server not reachable'
    latest = aircraft_update.get('version', '?')
    # Published versions end in the build date (1.15.4.20261104); the aircraft reports its build date.
    match = re.search(r'\.(\d{8})$', latest)
    built = firmware.get('build_yyyymmdd')
    if match and built:
        if built >= match.group(1):
            return f'Up to date (latest {latest})'
        return f'Update available: {latest}'
    return f'Latest on server: {latest}'


class DroneLink:
    def __init__(self, port, signer):
        self.port = port
        self.signer = signer
        self.m = mavutil.mavlink_connection(port, baud=115200, source_system=255, source_component=190)
        if self.m.wait_heartbeat(timeout=8) is None:
            self.close()
            raise LinkError('No heartbeat from the aircraft')
        self.unlocked_at = 0.0

    def close(self):
        try:
            self.m.close()
        except Exception:
            pass

    # ---------- Service unlock ----------

    def _sam(self, opcode, request_id, extra=b''):
        payload = bytes([78, 86, 83, 49, 1, opcode, 1, 0]) + request_id.to_bytes(4, 'little') + bytes(4) + extra
        self.m.mav.tunnel_send(self.m.target_system, 0, SAM_PAYLOAD_TYPE, len(payload),
                               list(payload) + [0] * (128 - len(payload)))

    def _wait_sam(self, opcode, request_id, seconds=5):
        end = time.time() + seconds
        while time.time() < end:
            msg = self.m.recv_match(type='TUNNEL', blocking=True, timeout=1)
            if msg is None or msg.payload_type != SAM_PAYLOAD_TYPE:
                continue
            p = bytes(msg.payload).ljust(128, b'\0')
            if p[5] == opcode and int.from_bytes(p[8:12], 'little') == request_id:
                return p
        return None

    def security_mode(self):
        """'Production', 'Service' or 'Manufacturer' (None when the aircraft does not answer)."""
        request_id = secrets.randbelow(0x7ffffffe) + 1
        self._sam(1, request_id)
        reply = self._wait_sam(0x81, request_id, 3)
        return None if reply is None else ['Production', 'Service', 'Manufacturer'][min(reply[6], 2)]

    def unlock(self):
        """Signed Service unlock. Raises LinkError with a readable reason."""
        if not self.signer.available():
            raise LinkError('Signing key not found on this computer')
        request_id = secrets.randbelow(0x7ffffffe) + 1
        self._sam(2, request_id)
        reply = self._wait_sam(0x82, request_id)
        if reply is None:
            raise LinkError('Aircraft did not answer the unlock request (old firmware?)')
        if reply[7] != 0:
            raise LinkError(f'Aircraft refused a challenge (status {reply[7]})')
        nonce = reply[16:48]
        self._sam(3, request_id, nonce + self.signer.sign(request_id, nonce))
        result = self._wait_sam(0x83, request_id)
        if result is None or result[7] != 0:
            reasons = {4: 'aircraft is armed', 5: 'signature rejected (wrong key)', 3: 'challenge expired'}
            code = None if result is None else result[7]
            raise LinkError('Unlock failed: ' + reasons.get(code, f'status {code}'))
        self.unlocked_at = time.time()

    def keep_unlocked(self):
        """Call regularly; signs again before the 15-minute window closes."""
        if time.time() - self.unlocked_at > SERVICE_SECONDS - 180:
            self.unlock()

    # ---------- firmware ----------

    def shell(self, command, seconds=5):
        data = (command + '\n').encode()
        self.m.mav.serial_control_send(SHELL, SHELL_FLAGS, 0, 0, len(data), list(data) + [0] * (70 - len(data)))
        out = b''
        end = time.time() + seconds
        quiet = time.time()
        while time.time() < end and time.time() - quiet < 1.5:
            self.m.mav.serial_control_send(SHELL, SHELL_FLAGS, 0, 0, 0, [0] * 70)
            msg = self.m.recv_match(type='SERIAL_CONTROL', blocking=True, timeout=0.2)
            while msg is not None:
                if msg.count:
                    out += bytes(msg.data[:msg.count])
                    quiet = time.time()
                msg = self.m.recv_match(type='SERIAL_CONTROL', blocking=False)
        return out.decode(errors='replace').replace('\r', '').replace('\x1b[K', '')

    def firmware(self):
        """Dict: version, git, branch, build, build_yyyymmdd, board, serial."""
        self.shell('', 1)
        text = self.shell('ver all', 6)

        def field(label):
            match = re.search(rf'{label}:\s*(.+)', text)
            return match.group(1).strip() if match else ''

        info = {
            'version': field('PX4 version').split(' ')[0],
            'git': field('PX4 git-hash')[:10],
            'branch': field('PX4 git-branch'),
            'build': field('Build datetime'),
            'board': field('HW arch'),
            'serial': '',
            'build_yyyymmdd': '',
        }
        try:
            info['build_yyyymmdd'] = time.strftime('%Y%m%d', time.strptime(info['build'][:11].replace('  ', ' '), '%b %d %Y'))
        except ValueError:
            pass
        serial = self.shell('cat /fs/microsd/nvx_serial.txt', 3)
        match = re.search(r'\b(NVX[A-Z0-9-]{3,15})\b', serial)
        if match:
            info['serial'] = match.group(1)
        return info

    # ---------- flight logs ----------

    def list_logs(self, progress=None):
        """All logs as [{'id', 'size', 'utc'}], newest first."""
        logs = {}
        total = None
        start = 0
        idle_rounds = 0
        while idle_rounds < 4 and (total is None or len(logs) < total):
            self.m.mav.log_request_list_send(self.m.target_system, self.m.target_component, start, 0xffff)
            before = len(logs)
            idle = time.time()
            while time.time() - idle < 1.5:
                msg = self.m.recv_match(type='LOG_ENTRY', blocking=True, timeout=0.5)
                if msg is None:
                    continue
                idle = time.time()
                total = msg.num_logs
                logs[msg.id] = {'id': msg.id, 'size': msg.size, 'utc': msg.time_utc}
                if progress and len(logs) % 25 == 0:
                    progress(len(logs), total)
                if len(logs) >= total:
                    break
            idle_rounds = idle_rounds + 1 if len(logs) == before else 0
            # Continue after the first gap.
            start = next((i for i in range(total or 0) if i not in logs), 0)
        return sorted(logs.values(), key=lambda entry: entry['utc'], reverse=True)

    def download_log(self, log_id, size, path, progress=None, cancelled=lambda: False):
        """Downloads one log to path. Returns False when cancelled."""
        data = bytearray(size)
        got = bytearray((size + LOG_CHUNK - 1) // LOG_CHUNK)
        first = 0
        received = 0
        started = time.time()
        last_report = 0.0
        while first < len(got):
            if cancelled():
                return False
            self.keep_unlocked()
            self.m.mav.log_request_data_send(self.m.target_system, self.m.target_component, log_id,
                                             first * LOG_CHUNK, size - first * LOG_CHUNK)
            idle = time.time()
            while time.time() - idle < 1.5 and first < len(got):
                msg = self.m.recv_match(type='LOG_DATA', blocking=True, timeout=0.5)
                if msg is None or msg.id != log_id or msg.count == 0:
                    continue
                idle = time.time()
                block = msg.ofs // LOG_CHUNK
                if not got[block]:
                    data[msg.ofs:msg.ofs + msg.count] = bytes(msg.data[:msg.count])
                    got[block] = 1
                    received += 1
                while first < len(got) and got[first]:
                    first += 1
                if block - first > 400:
                    break  # a gap opened behind the stream: ask again from the gap
                if progress and time.time() - last_report > 0.5:
                    last_report = time.time()
                    progress(received * LOG_CHUNK, size, received * LOG_CHUNK / max(0.1, time.time() - started))
                if cancelled() or time.time() - self.unlocked_at > SERVICE_SECONDS - 180:
                    break
        self.m.mav.log_request_end_send(self.m.target_system, self.m.target_component)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)
        return True


def summarize_log(path):
    """Short flight summary of a .ulg file (best effort; a log cut by power loss still parses)."""
    import io
    import contextlib
    import numpy as np
    from pyulog import ULog

    with contextlib.redirect_stdout(io.StringIO()):
        u = ULog(path, ['vehicle_status', 'battery_status', 'vehicle_local_position', 'vehicle_gps_position',
                        'vehicle_land_detected'])
    t0 = u.start_timestamp
    lines = []

    def topic(name):
        return next((d.data for d in u.data_list if d.name == name), None)

    def seconds(d):
        t = (d['timestamp'].astype('int64') - t0) / 1e6
        return t

    status = topic('vehicle_status')
    if status is not None:
        t = seconds(status)
        good = (t >= 0) & (t < 86400)
        armed = status['arming_state'][good] == 2
        lines.append(f'Log length: {t[good].max() / 60:.1f} min, armed for {armed.sum() / max(1, len(armed)) * t[good].max() / 60:.1f} min')
    battery = topic('battery_status')
    if battery is not None and battery['voltage_v'].max() > 6:
        v = battery['voltage_v']
        lines.append(f'Battery: start {v[0]:.1f} V, lowest {v.min():.1f} V, end {v[-1]:.1f} V, peak {battery["current_a"].max():.0f} A')
    position = topic('vehicle_local_position')
    if position is not None:
        lines.append(f'Max height {(-position["z"]).max():.1f} m, max speed '
                     f'{np.hypot(position["vx"], position["vy"]).max():.1f} m/s, fastest descent {position["vz"].max():.1f} m/s')
    gps = topic('vehicle_gps_position')
    if gps is not None:
        fixed = np.flatnonzero(gps['fix_type'] >= 3)
        if len(fixed):
            i = fixed[-1]
            lat = gps['latitude_deg'][i] if 'latitude_deg' in gps else gps['lat'][i] / 1e7
            lon = gps['longitude_deg'][i] if 'longitude_deg' in gps else gps['lon'][i] / 1e7
            lines.append(f'Last GPS position: {lat:.7f}, {lon:.7f} ({int(gps["satellites_used"][i])} satellites)')
        else:
            lines.append('No GPS fix in this log')
    notable = [m.message.strip() for m in u.logged_messages
               if m.log_level_str() in ('WARNING', 'ERROR', 'CRITICAL', 'EMERGENCY')][-6:]
    if notable:
        lines.append('Warnings: ' + ' | '.join(notable))
    return '\n'.join(lines) or 'No summary data in this log'
