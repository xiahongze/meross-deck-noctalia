#!/usr/bin/env python3
"""Meross Deck helper: talks to Meross devices for the Plasma widget.

Standard library only. Three transports, all documented in docs/API.md:
- cloud HTTP (iotx*.meross.com) for login and the device list
- cloud MQTT (TLS on 443) for reading and controlling devices from anywhere
- LAN HTTP (POST http://<ip>/config) when the PC shares a network with them

Each run prints one JSON object on stdout and exits. Credentials live in
~/.config/meross-deck/session.json (0600) and never go on a command line.
"""

import argparse
import base64
import concurrent.futures
import getpass
import hashlib
import json
import os
import random
import secrets
import socket
import ssl
import string
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

APP_VERSION = "3.42.3"
CLOUD_SECRET = "23x17ahWarFH6w29"
DEFAULT_API = "https://iotx.meross.com"
MQTT_PORT = 443
SESSION_FILE = "session.json"
CACHE_FILE = "devices.json"
COMMAND_LOG = "commands.log"
# apiStatus codes that mean the token is no longer valid.
TOKEN_ERRORS = {1019, 1022, 1201}
# Namespaces an encrypted device still accepts in plain JSON over LAN.
PLAIN_NAMESPACES = {"Appliance.System.All", "Appliance.System.Firmware", "Appliance.System.Ability",
                    "Appliance.Encrypt.Suite", "Appliance.Encrypt.ECDHE"}
# After a device misses on the LAN, go straight to the cloud for this long.
LAN_RETRY_SECONDS = 300
KIND_ORDER = ["garage", "light", "switch", "strip", "plug", "other"]


class MerossError(Exception):
    """An error the widget can show. `kind` is auth, network, device or usage."""

    def __init__(self, message, kind="network", code=None):
        super().__init__(message)
        self.kind = kind
        self.code = code


# ---------------------------------------------------------------- storage

def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "meross-deck")


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "meross-deck")


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data, private=False):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def load_session():
    session = read_json(os.path.join(config_dir(), SESSION_FILE), None)
    if not session or not session.get("token") or not session.get("key"):
        raise MerossError("Not signed in. Run: meross-deck login", "auth")
    return session


def save_session(session):
    write_json(os.path.join(config_dir(), SESSION_FILE), session, private=True)


def load_cache():
    return read_json(os.path.join(cache_dir(), CACHE_FILE), {"devices": {}, "listedAt": 0})


def save_cache(cache):
    write_json(os.path.join(cache_dir(), CACHE_FILE), cache)


# ---------------------------------------------------------------- signing

def md5(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def random_token(length=16):
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def sign_params(params, timestamp=None, nonce=None):
    """The four form fields of every cloud HTTP request."""
    encoded = base64.b64encode(json.dumps(params or {}, separators=(",", ":")).encode()).decode()
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    nonce = nonce or random_token()
    return {"sign": md5(CLOUD_SECRET + timestamp + nonce + encoded), "timestamp": timestamp,
            "params": encoded, "nonce": nonce}


def message(method, namespace, payload, key, origin, uuid=None, trigger="Android", now=None):
    """A device message, signed with the account key."""
    timestamp = int(time.time() if now is None else now)
    message_id = md5(random_token() + str(timestamp))
    header = {"from": origin, "messageId": message_id, "method": method, "namespace": namespace,
              "payloadVersion": 1, "sign": md5(message_id + key + str(timestamp)),
              "timestamp": timestamp, "triggerSrc": trigger}
    if uuid:
        header["uuid"] = uuid
    return {"header": header, "payload": payload}


def check_reply(reply, key):
    header = reply.get("header") or {}
    expected = md5(str(header.get("messageId", "")) + key + str(header.get("timestamp", "")))
    if header.get("sign") != expected:
        raise MerossError("Device reply had a bad signature", "device")
    if header.get("method") == "ERROR":
        error = (reply.get("payload") or {}).get("error") or {}
        raise MerossError("Device error %s" % error.get("code"), "device", error.get("code"))
    return reply


# ---------------------------------------------------------------- cloud HTTP

def cloud_headers(session=None):
    headers = {"AppVersion": APP_VERSION, "Vendor": "meross", "AppType": "Android", "AppLanguage": "en",
               "Tz": os.environ.get("TZ") or _local_tz_name() or "UTC",
               "Content-Type": "application/x-www-form-urlencoded"}
    if session:
        headers["App-Gray-Id"] = session.get("grayId", "1")
        headers["Authorization"] = "Basic " + session["token"]
    return headers


def _local_tz_name():
    try:
        target = os.path.realpath("/etc/localtime")
        marker = "/zoneinfo/"
        return target.split(marker, 1)[1] if marker in target else ""
    except OSError:
        return ""


def cloud_call(base, path, params=None, session=None, timeout=10, opener=None):
    body = urllib.parse.urlencode(sign_params(params)).encode()
    request = urllib.request.Request(base.rstrip("/") + path, data=body, headers=cloud_headers(session),
                                     method="POST")
    try:
        with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
            reply = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise MerossError("Meross cloud HTTP %s" % e.code) from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise MerossError("Can't reach the Meross cloud (%s)" % _reason(e)) from e
    return reply


def cloud_data(reply):
    status = reply.get("apiStatus")
    if reply.get("sysStatus") not in (0, None) or status != 0:
        kind = "auth" if status in TOKEN_ERRORS else "network"
        info = reply.get("info") or ""
        raise MerossError(("Meross cloud error %s %s" % (status, info)).strip(), kind, status)
    return reply.get("data")


def sign_in(email, password, country="AU", mfa_code=None, base=DEFAULT_API, ask_mfa=None, opener=None):
    """Returns (base, data). Follows the 1030 redirect and the 1033 MFA prompt."""
    params = {"email": email, "password": md5(password), "encryption": 1, "accountCountryCode": country,
              "agree": 1, "mobileInfo": {"resolution": "1920*1080", "carrier": "", "deviceModel": "Linux,KDE Plasma",
                                         "mobileOs": "Android", "mobileOsVersion": "14", "uuid": random_token(32)}}
    for _ in range(4):
        if mfa_code:
            params["mfaCode"] = mfa_code
        reply = cloud_call(base, "/v1/Auth/signIn", params, opener=opener)
        status = reply.get("apiStatus")
        if status == 1030:
            data = reply.get("data") or {}
            data = json.loads(data) if isinstance(data, str) else data
            if not data.get("domain"):
                break
            base = data["domain"]
            time.sleep(0.5)
            continue
        if status == 1033 and not mfa_code and ask_mfa:
            mfa_code = ask_mfa()
            continue
        return base, cloud_data(reply)
    raise MerossError("Sign-in did not settle on a region", "auth")


def device_list(session, opener=None):
    return cloud_data(cloud_call(session["domain"], "/v1/Device/devList", {}, session, opener=opener)) or []


# ---------------------------------------------------------------- MQTT 3.1.1

def _varint(n):
    out = bytearray()
    while True:
        byte, n = n % 128, n // 128
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _utf8(text):
    data = text.encode("utf-8")
    return struct.pack("!H", len(data)) + data


class Mqtt:
    """A minimal MQTT client: one broker, request/response by messageId."""

    def __init__(self, host, session, timeout=10, connect=None):
        self.host = host
        self.session = session
        self.timeout = timeout
        self.userid = str(session["userid"])
        self.reply_topic = "/app/%s-%s/subscribe" % (self.userid, session["appId"])
        self._connect = connect or self._tls_connect
        self.sock = None
        self.buffer = b""
        self.pushes = []

    def _tls_connect(self):
        context = ssl.create_default_context()
        raw = socket.create_connection((self.host, MQTT_PORT), timeout=self.timeout)
        return context.wrap_socket(raw, server_hostname=self.host)

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    def open(self):
        try:
            self.sock = self._connect()
            self.sock.settimeout(self.timeout)
            variable = _utf8("MQTT") + b"\x04\xc2" + struct.pack("!H", 30)
            payload = (_utf8("app:" + self.session["appId"]) + _utf8(self.userid)
                       + _utf8(md5(self.userid + self.session["key"])))
            self._send(0x10, variable + payload)
            kind, body = self._packet()
            if kind != 0x20 or len(body) < 2:
                raise MerossError("Unexpected MQTT reply to connect")
            if body[1] != 0:
                raise MerossError("MQTT login refused (code %d)" % body[1], "auth" if body[1] in (4, 5) else "network")
            topics = _utf8(self.reply_topic) + b"\x01" + _utf8("/app/%s/subscribe" % self.userid) + b"\x01"
            self._send(0x82, struct.pack("!H", 1) + topics)
            while True:
                kind, body = self._packet()
                if kind == 0x90:
                    break
                self._handle(kind, body)
        except (OSError, ssl.SSLError) as e:
            self.close()
            raise MerossError("Can't reach %s (%s)" % (self.host, _reason(e))) from e

    def close(self):
        if self.sock:
            try:
                self.sock.sendall(b"\xe0\x00")
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def _send(self, kind, body):
        self.sock.sendall(bytes([kind]) + _varint(len(body)) + body)

    def _read(self, n):
        while len(self.buffer) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("connection closed")
            self.buffer += chunk
        data, self.buffer = self.buffer[:n], self.buffer[n:]
        return data

    def _packet(self):
        first = self._read(1)[0]
        length, shift = 0, 0
        while True:
            byte = self._read(1)[0]
            length |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break
        return first, self._read(length)

    def _handle(self, kind, body):
        """Returns (topic, json) for a PUBLISH, acking QoS 1."""
        if kind & 0xF0 != 0x30:
            return None
        topic_len = struct.unpack("!H", body[:2])[0]
        topic = body[2:2 + topic_len].decode("utf-8", "replace")
        rest = body[2 + topic_len:]
        if kind & 0x06:
            self._send(0x40, rest[:2])
            rest = rest[2:]
        try:
            return topic, json.loads(rest.decode("utf-8"))
        except ValueError:
            return None

    def request(self, requests):
        """Sends [(uuid, method, namespace, payload)], returns {index: reply or MerossError}."""
        waiting = {}
        for index, (uuid, method, namespace, payload) in enumerate(requests):
            msg = message(method, namespace, payload, self.session["key"], self.reply_topic, uuid)
            waiting[msg["header"]["messageId"]] = index
            body = json.dumps(msg, separators=(",", ":")).encode()
            self._send(0x30, _utf8("/appliance/%s/subscribe" % uuid) + body)
        results = {}
        deadline = time.monotonic() + self.timeout
        try:
            while waiting and time.monotonic() < deadline:
                self.sock.settimeout(max(0.1, deadline - time.monotonic()))
                try:
                    kind, body = self._packet()
                except socket.timeout:
                    break
                published = self._handle(kind, body)
                if not published:
                    continue
                reply = published[1]
                header = reply.get("header") or {}
                index = waiting.pop(header.get("messageId"), None)
                if index is None:
                    if header.get("method") == "PUSH":
                        self.pushes.append(reply)
                    continue
                try:
                    results[index] = check_reply(reply, self.session["key"])
                except MerossError as e:
                    results[index] = e
        except (OSError, ssl.SSLError) as e:
            for index in waiting.values():
                results[index] = MerossError("Lost the cloud connection (%s)" % _reason(e))
            waiting = {}
        for index in waiting.values():
            results[index] = MerossError("The device didn't answer through the cloud")
        return results


# ---------------------------------------------------------------- LAN HTTP

def lan_key(uuid, user_key, mac):
    return md5(uuid[3:22] + user_key[1:9] + mac + user_key[10:28]).encode("ascii")


def lan_request(ip, uuid, method, namespace, payload, session, crypt_key=None, timeout=3, opener=None):
    url = "http://%s/config" % ip
    msg = message(method, namespace, payload, session["key"], url, uuid, trigger="AndroidLocal")
    body = json.dumps(msg, separators=(",", ":")).encode()
    encrypted = crypt_key is not None and namespace not in PLAIN_NAMESPACES
    if encrypted:
        body = base64.b64encode(aes_cbc_encrypt(crypt_key, body + b"\0" * (16 - len(body) % 16)))
    headers = {"Content-Type": "text/plain" if encrypted else "application/json; charset=UTF-8",
               "Connection": "close"}
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
            raw = response.read()
    except (urllib.error.URLError, OSError) as e:
        raise MerossError("No answer on the local network (%s)" % _reason(e)) from e
    try:
        if encrypted:
            raw = aes_cbc_decrypt(crypt_key, base64.b64decode(raw)).rstrip(b"\0")
        reply = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise MerossError("Unreadable reply on the local network", "device") from e
    return check_reply(reply, session["key"])


# ---------------------------------------------------------------- AES-256-CBC
# The LAN encryption uses AES-256-CBC with an ASCII "0000000000000000" IV and
# zero padding. The standard library has no AES, so here is a small one.

_SBOX = bytearray(256)
_INV_SBOX = bytearray(256)


def _build_sbox():
    p = q = 1
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ (q << 1 | q >> 7) ^ (q << 2 | q >> 6) ^ (q << 3 | q >> 5) ^ (q << 4 | q >> 4)
        _SBOX[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    _SBOX[0] = 0x63
    for i, v in enumerate(_SBOX):
        _INV_SBOX[v] = i


_build_sbox()


def _xtime(a):
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else a << 1


def _mul(a, b):
    out = 0
    while b:
        if b & 1:
            out ^= a
        a = _xtime(a)
        b >>= 1
    return out


def _expand_key(key):
    if len(key) != 32:
        raise ValueError("AES-256 needs a 32-byte key")
    words = [list(key[i:i + 4]) for i in range(0, 32, 4)]
    rcon = 1
    for i in range(8, 60):
        word = list(words[i - 1])
        if i % 8 == 0:
            word = [_SBOX[b] for b in word[1:] + word[:1]]
            word[0] ^= rcon
            rcon = _xtime(rcon)
        elif i % 8 == 4:
            word = [_SBOX[b] for b in word]
        words.append([a ^ b for a, b in zip(words[i - 8], word)])
    return [sum(words[r * 4:r * 4 + 4], []) for r in range(15)]


def _encrypt_block(rounds, block):
    s = [b ^ k for b, k in zip(block, rounds[0])]
    for r in range(1, 15):
        s = [_SBOX[b] for b in s]
        s = [s[(i + 4 * (i % 4)) % 16] for i in range(16)]
        if r < 14:
            mixed = []
            for c in range(4):
                a = s[c * 4:c * 4 + 4]
                mixed += [_mul(a[0], 2) ^ _mul(a[1], 3) ^ a[2] ^ a[3],
                          a[0] ^ _mul(a[1], 2) ^ _mul(a[2], 3) ^ a[3],
                          a[0] ^ a[1] ^ _mul(a[2], 2) ^ _mul(a[3], 3),
                          _mul(a[0], 3) ^ a[1] ^ a[2] ^ _mul(a[3], 2)]
            s = mixed
        s = [b ^ k for b, k in zip(s, rounds[r])]
    return bytes(s)


def _decrypt_block(rounds, block):
    s = [b ^ k for b, k in zip(block, rounds[14])]
    for r in range(13, -1, -1):
        s = [s[(i - 4 * (i % 4)) % 16] for i in range(16)]
        s = [_INV_SBOX[b] for b in s]
        s = [b ^ k for b, k in zip(s, rounds[r])]
        if r > 0:
            mixed = []
            for c in range(4):
                a = s[c * 4:c * 4 + 4]
                mixed += [_mul(a[0], 14) ^ _mul(a[1], 11) ^ _mul(a[2], 13) ^ _mul(a[3], 9),
                          _mul(a[0], 9) ^ _mul(a[1], 14) ^ _mul(a[2], 11) ^ _mul(a[3], 13),
                          _mul(a[0], 13) ^ _mul(a[1], 9) ^ _mul(a[2], 14) ^ _mul(a[3], 11),
                          _mul(a[0], 11) ^ _mul(a[1], 13) ^ _mul(a[2], 9) ^ _mul(a[3], 14)]
            s = mixed
    return bytes(s)


LAN_IV = b"0" * 16


def aes_cbc_encrypt(key, data, iv=LAN_IV):
    if len(data) % 16:
        raise ValueError("data must be padded to 16 bytes")
    rounds, prev, out = _expand_key(key), iv, bytearray()
    for i in range(0, len(data), 16):
        prev = _encrypt_block(rounds, bytes(a ^ b for a, b in zip(data[i:i + 16], prev)))
        out += prev
    return bytes(out)


def aes_cbc_decrypt(key, data, iv=LAN_IV):
    if len(data) % 16:
        raise ValueError("ciphertext must be a multiple of 16 bytes")
    rounds, prev, out = _expand_key(key), iv, bytearray()
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        out += bytes(a ^ b for a, b in zip(_decrypt_block(rounds, block), prev))
        prev = block
    return bytes(out)


# ---------------------------------------------------------------- device model

GARAGE_TYPES = {"msg100", "msg150", "msg200"}
# Appliance.Control.Light capacity bits: which fields a SET carries.
LIGHT_CAPACITY = {"rgb": 1, "temperature": 2, "luminance": 4}


def device_kind(device_type):
    t = (device_type or "").lower()
    if t in GARAGE_TYPES:
        return "garage"
    if t.startswith(("msl", "mdl", "mss56", "mss57", "mpd", "hp110")):
        return "light"
    if t.startswith(("mss425", "mss420", "mss426", "sp425", "msp84", "psx")):
        return "strip"
    if t.startswith(("mss5", "mss6", "mss7", "mss8", "mop", "r10", "r11", "r20")):
        return "switch"
    if t.startswith(("mss", "wp110", "p10", "p11")):
        return "plug"
    return "other"


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def channel_names(device):
    """Cloud channel names by index; channel 0 falls back to the device name."""
    names = {}
    for index, channel in enumerate(device.get("channels") or []):
        name = (channel or {}).get("devName") or ""
        if name:
            names[index] = name
    names.setdefault(0, device.get("devName") or device.get("uuid", ""))
    return names


def build_view(listed, cached, all_payload, extras=None):
    """Turns a devList entry plus System.All (and garage config) into the widget's shape."""
    extras = extras or {}
    kind = device_kind(listed.get("deviceType"))
    names = channel_names(listed)
    digest = (all_payload or {}).get("all", {}).get("digest", {}) if all_payload else {}
    view = {"uuid": listed["uuid"], "name": listed.get("devName") or listed["uuid"],
            "type": listed.get("deviceType", ""), "kind": kind,
            "online": listed.get("onlineStatus") in (1, 3), "channels": []}
    if kind == "garage":
        enabled = {c.get("channel"): c.get("doorEnable", 1) for c in _as_list(extras.get("multipleConfig"))}
        config = extras.get("config") or {}
        timings = {c.get("channel"): c for c in _as_list(extras.get("multipleConfig"))}
        for door in _as_list(digest.get("garageDoor")):
            ch = door.get("channel", 0)
            if enabled.get(ch, 1) == 0:
                continue
            timing = timings.get(ch) or config
            view["channels"].append({
                "channel": ch, "kind": "door", "name": names.get(ch, view["name"]),
                "open": bool(door.get("open")), "lmTime": door.get("lmTime", 0),
                "obstruction": door.get("obstruction") == 1,
                "openMs": timing.get("doorOpenDuration") or 15000,
                "closeMs": timing.get("doorCloseDuration") or 15000})
        if listed.get("deviceType") == "msg200":
            view["channels"] = [c for c in view["channels"] if c["channel"] != 0]
    else:
        toggles = _as_list(digest.get("togglex"))
        lights = {l.get("channel", 0): l for l in _as_list(digest.get("light"))}
        if not toggles and "toggle" in digest:
            toggles = [{"channel": 0, "onoff": digest["toggle"].get("onoff")}]
        multi = len(toggles) > 1
        for toggle in toggles:
            ch = toggle.get("channel", 0)
            if multi and ch == 0 and kind == "strip":
                continue  # channel 0 is the strip's master switch
            entry = {"channel": ch, "kind": "light" if kind == "light" else "switch",
                     "name": names.get(ch, "%s %d" % (view["name"], ch)), "on": bool(toggle.get("onoff"))}
            light = lights.get(ch)
            if light:
                entry["capacity"] = light.get("capacity", 0)
                if entry["capacity"] & 4:
                    entry["luminance"] = light.get("luminance")
                if entry["capacity"] & 2:
                    entry["temperature"] = light.get("temperature")
            view["channels"].append(entry)
    view["ip"] = cached.get("ip", "")
    return view


# ---------------------------------------------------------------- orchestration

class Hub:
    """Routes each device over LAN when it answers there, else over MQTT."""

    def __init__(self, session, cache, lan=True, cloud=True, mqtt_factory=Mqtt, lan_call=lan_request):
        self.session = session
        self.cache = cache
        self.lan = lan
        self.cloud = cloud
        self.mqtt_factory = mqtt_factory
        self.lan_call = lan_call

    def crypt_key(self, cached):
        if cached.get("encrypted") and cached.get("mac"):
            return lan_key(cached["uuid"], self.session["key"], cached["mac"])
        return None

    def learn(self, uuid, all_payload, ability=None):
        cached = self.cache["devices"].setdefault(uuid, {"uuid": uuid})
        system = (all_payload or {}).get("all", {}).get("system", {})
        ip = system.get("firmware", {}).get("innerIp")
        mac = system.get("hardware", {}).get("macAddress")
        if ip:
            cached["ip"] = ip
        if mac:
            cached["mac"] = mac
        if ability is not None:
            cached["abilities"] = sorted(ability.keys())
            cached["encrypted"] = "Appliance.Encrypt.Suite" in ability
        return cached

    def run(self, jobs):
        """jobs: {uuid: [(method, namespace, payload)]} -> {uuid: {"route", "replies"|"error"}}."""
        results, cloud_jobs = {}, {}

        now = time.time()

        def try_lan(uuid):
            cached = self.cache["devices"].get(uuid) or {}
            replies = []
            for method, namespace, payload in jobs[uuid]:
                replies.append(self.lan_call(cached["ip"], uuid, method, namespace, payload, self.session,
                                             self.crypt_key(cached), timeout=2.5).get("payload"))
            return replies

        lan_uuids = [u for u in jobs if self.lan and self._lan_ready(u, now)]
        if lan_uuids:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                futures = {pool.submit(try_lan, u): u for u in lan_uuids}
                for future in concurrent.futures.as_completed(futures):
                    uuid = futures[future]
                    cached = self.cache["devices"][uuid]
                    try:
                        results[uuid] = {"route": "lan", "replies": future.result()}
                        cached.pop("lanFailAt", None)
                    except MerossError as e:
                        if e.kind == "device":
                            results[uuid] = {"route": "lan", "error": e}
                        else:
                            cached["lanFailAt"] = int(now)
            # Devices that didn't answer locally fall back to the cloud.
        for uuid in jobs:
            if uuid not in results:
                if not self.cloud:
                    results[uuid] = {"route": "lan", "error": MerossError("Not reachable on the local network")}
                    continue
                domain = self._domain(uuid)
                cloud_jobs.setdefault(domain, []).append(uuid)
        for domain, uuids in cloud_jobs.items():
            flat = [(u, m, n, p) for u in uuids for (m, n, p) in jobs[u]]
            try:
                with self.mqtt_factory(domain, self.session) as client:
                    answers = client.request(flat)
            except MerossError as e:
                for uuid in uuids:
                    results[uuid] = {"route": "cloud", "error": e}
                continue
            cursor = 0
            for uuid in uuids:
                count = len(jobs[uuid])
                chunk = [answers.get(cursor + i) for i in range(count)]
                cursor += count
                failure = next((a for a in chunk if isinstance(a, MerossError)), None)
                results[uuid] = ({"route": "cloud", "error": failure} if failure
                                 else {"route": "cloud", "replies": [a.get("payload") for a in chunk]})
        return results

    def _lan_ready(self, uuid, now):
        cached = self.cache["devices"].get(uuid) or {}
        if not cached.get("ip"):
            return False
        # A LAN-only run always tries; otherwise a recent miss means we're away.
        return not self.cloud or now - cached.get("lanFailAt", 0) > LAN_RETRY_SECONDS

    def _domain(self, uuid):
        listed = self.cache.get("listed", {}).get(uuid) or {}
        return listed.get("domain") or self.session.get("mqttDomain") or "mqtt.meross.com"


def refresh_devices(session, cache, max_age=6 * 3600, force=False, opener=None):
    if force or not cache.get("listed") or time.time() - cache.get("listedAt", 0) > max_age:
        listed = device_list(session, opener=opener)
        cache["listed"] = {d["uuid"]: d for d in listed if d.get("uuid")}
        cache["order"] = [d["uuid"] for d in listed if d.get("uuid")]
        cache["listedAt"] = int(time.time())
    return cache


def state_jobs(listed, cached):
    jobs = [("GET", "Appliance.System.All", {})]
    if "abilities" not in cached:
        jobs.append(("GET", "Appliance.System.Ability", {}))
    if device_kind(listed.get("deviceType")) == "garage" and "garageConfig" not in cached:
        if listed.get("deviceType") == "msg200":
            jobs.append(("GET", "Appliance.GarageDoor.MultipleConfig", {}))
        else:
            jobs.append(("GET", "Appliance.GarageDoor.Config", {}))
    return jobs


def snapshot(session, cache, uuids=None, lan=True, cloud=True, hub=None, force_list=False, cloud_max_age=0):
    """cloud_max_age: devices read through the cloud more recently than this
    many seconds are served from the cache, so a fast LAN refresh rate never
    becomes a fast cloud one."""
    refresh_devices(session, cache, force=force_list)
    hub = hub or Hub(session, cache, lan=lan, cloud=cloud)
    order = [u for u in cache.get("order", []) if not uuids or u in uuids]
    now = time.time()
    reused = set()
    jobs = {}
    for u in order:
        if cache["listed"][u].get("onlineStatus") not in (1, 3):
            continue
        cached = cache["devices"].get(u) or {}
        recent = cloud_max_age and now - cached.get("cloudAt", 0) < cloud_max_age and cached.get("lastState")
        if recent and not (hub.lan and hub._lan_ready(u, now)):
            reused.add(u)
            continue
        jobs[u] = state_jobs(cache["listed"][u], cached)
    results = hub.run(jobs) if jobs else {}
    devices = []
    for uuid in order:
        listed = cache["listed"][uuid]
        result = results.get(uuid)
        cached = cache["devices"].get(uuid) or {}
        all_payload = None
        if result and "replies" in result:
            replies = dict(zip([n for _, n, _ in jobs[uuid]], result["replies"]))
            all_payload = replies.get("Appliance.System.All")
            ability = (replies.get("Appliance.System.Ability") or {}).get("ability")
            cached = hub.learn(uuid, all_payload, ability)
            for ns in ("Appliance.GarageDoor.Config", "Appliance.GarageDoor.MultipleConfig"):
                if ns in replies:
                    cached["garageConfig"] = (replies[ns] or {}).get("config")
            cached["lastState"] = all_payload
            cached["lastSeen"] = int(time.time())
            if result["route"] == "cloud":
                cached["cloudAt"] = int(time.time())
        elif cached.get("lastState"):
            all_payload = cached["lastState"]
        config = cached.get("garageConfig")
        extras = {"multipleConfig": config} if isinstance(config, list) else {"config": config}
        view = build_view(listed, cached, all_payload, extras)
        if uuid in reused:
            view["route"], view["stale"] = "cloud", False
        else:
            view["route"] = result["route"] if result else ""
            view["stale"] = not (result and "replies" in result)
        view["seenAt"] = cached.get("lastSeen", 0)
        if result and "error" in result:
            view["error"] = str(result["error"])
        devices.append(view)
    devices.sort(key=lambda d: (KIND_ORDER.index(d["kind"]), d["name"].lower()))
    return {"ok": True, "devices": devices, "generatedAt": int(time.time()),
            "account": session.get("email", "")}


def command_payload(kind, channel, value, uuid, capacity=0):
    """The SET (namespace, payload) for a widget action."""
    if kind == "door":
        return "Appliance.GarageDoor.State", {"state": {"channel": channel, "open": 1 if value else 0, "uuid": uuid}}
    if kind in ("luminance", "temperature"):
        level = max(1, min(100, int(value)))
        return "Appliance.Control.Light", {"light": {"capacity": LIGHT_CAPACITY[kind], "channel": channel,
                                                     kind: level}}
    return "Appliance.Control.ToggleX", {"togglex": {"channel": channel, "onoff": 1 if value else 0}}


def run_command(session, cache, uuid, kind, channel, value, lan=True, cloud=True, hub=None):
    refresh_devices(session, cache)
    listed = (cache.get("listed") or {}).get(uuid)
    if not listed:
        raise MerossError("Unknown device %s" % uuid, "usage")
    cached = cache["devices"].get(uuid) or {}
    namespace, payload = command_payload(kind, channel, value, uuid)
    abilities = cached.get("abilities") or []
    if namespace == "Appliance.Control.ToggleX" and abilities and namespace not in abilities \
            and "Appliance.Control.Toggle" in abilities:
        namespace, payload = "Appliance.Control.Toggle", {"toggle": {"onoff": 1 if value else 0}, "channel": channel}
    hub = hub or Hub(session, cache, lan=lan, cloud=cloud)
    result = hub.run({uuid: [("SET", namespace, payload)]})[uuid]
    if "error" in result:
        result["error"].route = result["route"]
        result["error"].request = (namespace, payload)
        raise result["error"]
    return {"ok": True, "uuid": uuid, "route": result["route"], "namespace": namespace,
            "payload": payload, "reply": result["replies"][0]}


# ---------------------------------------------------------------- CLI

def _reason(error):
    reason = getattr(error, "reason", None) or error
    return str(reason) or error.__class__.__name__


def cmd_login(args):
    if not sys.stdin.isatty():
        raise MerossError("Run login in a terminal", "usage")
    email = args.email or input("Meross email: ").strip()
    password = getpass.getpass("Meross password: ")
    base, data = sign_in(email, password, country=args.country.upper(), base=args.api,
                         ask_mfa=lambda: input("Authenticator code: ").strip())
    previous = read_json(os.path.join(config_dir(), SESSION_FILE), {}) or {}
    session = {"email": data.get("email") or email, "token": data["token"], "key": data["key"],
               "userid": str(data["userid"]), "domain": data.get("domain") or base,
               "mqttDomain": data.get("mqttDomain") or "mqtt.meross.com",
               # Our own MQTT client id, so the phone's connection is left alone.
               "appId": previous.get("appId") or secrets.token_hex(16),
               "grayId": previous.get("grayId") or str(random.randint(10 ** 13, 10 ** 14)),
               "signedInAt": int(time.time())}
    save_session(session)
    cache = load_cache()
    refresh_devices(session, cache, force=True)
    save_cache(cache)
    return {"ok": True, "email": session["email"], "devices": len(cache.get("order", []))}


def cmd_logout(_args):
    session = load_session()
    try:
        cloud_call(session["domain"], "/v1/Profile/logout", {}, session)
    except MerossError:
        pass
    os.remove(os.path.join(config_dir(), SESSION_FILE))
    return {"ok": True}


def cmd_devices(args):
    session = load_session()
    cache = load_cache()
    refresh_devices(session, cache, force=True)
    save_cache(cache)
    if args.raw:
        return {"ok": True, "devices": [cache["listed"][u] for u in cache["order"]]}
    return {"ok": True, "devices": [{"uuid": u, "name": cache["listed"][u].get("devName"),
                                     "type": cache["listed"][u].get("deviceType"),
                                     "online": cache["listed"][u].get("onlineStatus")} for u in cache["order"]]}


def cmd_cached(_args):
    """Devices from the cache, without touching the network (for the settings page)."""
    cache = load_cache()
    listed = cache.get("listed") or {}
    devices = [{"uuid": u, "name": listed[u].get("devName") or u, "type": listed[u].get("deviceType", ""),
                "kind": device_kind(listed[u].get("deviceType"))} for u in cache.get("order", []) if u in listed]
    devices.sort(key=lambda d: (KIND_ORDER.index(d["kind"]), d["name"].lower()))
    session = read_json(os.path.join(config_dir(), SESSION_FILE), None) or {}
    return {"ok": True, "devices": devices, "account": session.get("email", "")}


def cmd_state(args):
    session = load_session()
    cache = load_cache()
    result = snapshot(session, cache, uuids=set(args.uuid or []) or None, lan=not args.cloud_only,
                      cloud=not args.lan_only, force_list=args.relist, cloud_max_age=args.cloud_max_age)
    save_cache(cache)
    return result


def cmd_probe(args):
    """Read-only: raw replies of the given GET namespaces, for docs and fixtures."""
    session = load_session()
    cache = load_cache()
    refresh_devices(session, cache)
    hub = Hub(session, cache, lan=not args.cloud_only, cloud=not args.lan_only)
    namespaces = args.namespace or ["Appliance.System.All", "Appliance.System.Ability"]
    payload = json.loads(args.payload) if args.payload else {}
    results = hub.run({u: [("GET", n, payload) for n in namespaces] for u in args.uuid})
    out = {}
    for uuid, result in results.items():
        if "replies" in result:
            replies = dict(zip(namespaces, result["replies"]))
            hub.learn(uuid, replies.get("Appliance.System.All"),
                      (replies.get("Appliance.System.Ability") or {}).get("ability"))
        out[uuid] = ({"route": result["route"], "error": str(result["error"])} if "error" in result
                     else {"route": result["route"], "replies": dict(zip(namespaces, result["replies"]))})
    save_cache(cache)
    return {"ok": True, "results": out}


def cmd_set(args):
    request = json.loads(base64.b64decode(args.request).decode("utf-8"))
    cache = load_cache()
    try:
        session = load_session()
        result = run_command(session, cache, request["uuid"], request.get("kind", "switch"),
                             int(request.get("channel", 0)), request.get("value"),
                             lan=not args.cloud_only, cloud=not args.lan_only)
    except MerossError as e:
        log_command(cache, request, None, e)
        raise
    log_command(cache, request, result, None)
    return result


def log_command(cache, request, result, error):
    """One line per command in ~/.cache/meross-deck/commands.log, so what the
    widget sent, and what the device said, can always be checked afterwards."""
    uuid = request.get("uuid", "")
    name = ((cache.get("listed") or {}).get(uuid) or {}).get("devName", "?")
    compact = lambda obj: json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    if error is None:
        outcome = "ok via %s: %s %s -> %s" % (result["route"], result["namespace"], compact(result["payload"]),
                                             compact(result["reply"]))
    else:
        sent = getattr(error, "request", None)
        what = "%s %s" % (sent[0], compact(sent[1])) if sent else compact(request)
        outcome = "FAILED via %s: %s -> %s (%s%s)" % (getattr(error, "route", "-"), what, error, error.kind,
                                                    ", code %s" % error.code if error.code else "")
    line = "%s  %s [%s] ch%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), name, uuid,
                                        request.get("channel", 0), outcome)
    os.makedirs(cache_dir(), exist_ok=True)
    with open(os.path.join(cache_dir(), COMMAND_LOG), "a", encoding="utf-8") as f:
        f.write(line)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="meross-deck", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login", help="sign in (interactive)")
    login.add_argument("--email")
    login.add_argument("--country", default="AU", help="account country code, e.g. AU, US, GB")
    login.add_argument("--api", default=DEFAULT_API)
    sub.add_parser("logout", help="sign out and delete the saved session")
    sub.add_parser("cached", help="cached device list, no network")
    devices = sub.add_parser("devices", help="list devices from the cloud")
    devices.add_argument("--raw", action="store_true")
    for name, helptext in (("state", "read every device"), ("probe", "raw GET replies (read-only)"),
                           ("set", "change a device; takes base64 JSON")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--lan-only", action="store_true")
        p.add_argument("--cloud-only", action="store_true")
        if name == "state":
            p.add_argument("--uuid", action="append")
            p.add_argument("--relist", action="store_true")
            p.add_argument("--cloud-max-age", type=int, default=0, metavar="SECONDS",
                           help="reuse cloud readings younger than this")
        elif name == "probe":
            p.add_argument("uuid", nargs="+")
            p.add_argument("--namespace", "-n", action="append")
            p.add_argument("--payload")
        else:
            p.add_argument("request", help='base64 of {"uuid","kind","channel","value"}')
    args = parser.parse_args(argv)
    handlers = {"login": cmd_login, "logout": cmd_logout, "devices": cmd_devices, "cached": cmd_cached,
                "state": cmd_state,
                "probe": cmd_probe, "set": cmd_set}
    try:
        result = handlers[args.command](args)
        code = 0
    except MerossError as e:
        result = {"ok": False, "error": str(e), "kind": e.kind, "code": e.code}
        code = 0 if args.command in ("state", "set", "cached") else 1
    except KeyboardInterrupt:
        return 130
    print(json.dumps(result, indent=None if args.command in ("state", "set", "cached") else 1))
    return code


if __name__ == "__main__":
    sys.exit(main())
