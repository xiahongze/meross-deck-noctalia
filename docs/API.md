# Meross protocol

This documents how the Meross Android app talks to Meross devices, and what Meross Deck uses. It has four parts:

1. [The device message](#1-the-device-message): the JSON envelope every device command uses, whatever the transport.
2. [Cloud HTTP API](#2-cloud-http-api): sign-in and the device list. It also has a small control relay.
3. [Cloud MQTT](#3-cloud-mqtt): reading and controlling devices from anywhere.
4. [LAN HTTP](#4-lan-http): reading and controlling devices on the same network, with no internet needed.

[Namespaces](#5-namespaces) lists the commands for garage doors, lights and plugs. [Routing](#6-how-the-app-chooses-a-transport) covers how the app picks a transport.

**It isn't REST or gRPC.** The cloud HTTP API is form-encoded `POST`s with a signed envelope, and it only handles accounts and inventory. Devices are controlled with a JSON message protocol. Through the cloud it rides on MQTT 3.1.1 over TLS; on the LAN it is posted to `http://<device>/config`.

**How it was worked out.** The app `com.meross.meross` v3.42.3 (versionCode 1043) was pulled from a phone over ADB and decompiled with jadx 1.5.3. Its findings were then checked against a live account on the AP region:

- two MSG100 garage door openers (firmware 4.2.20)
- an MSL610 floor lamp (firmware 4.2.2)
- four MSS210P plugs (offline during testing)

Source references are to jadx output, under `com/meross/...` and `com/reaper/framework/...`.

**Confidence markers**

- **Verified**: observed against live Meross servers or devices.
- **Code**: read in the decompiled app but not exercised live.
- **Inferred**: an educated guess.

**Safety.** The garage door SET was first sent by the owner, watching the door, from the widget. The widget asks for confirmation before every door command.

---

## 1. The device message

Every device command, over MQTT or LAN, is one JSON object. **Verified.**

```json
{
  "header": {
    "from": "/app/1000001-<appId>/subscribe",
    "messageId": "5f0e8c1b9a7d4e62b1c3a8f09d2e4b71",
    "method": "GET",
    "namespace": "Appliance.System.All",
    "payloadVersion": 1,
    "sign": "<md5 hex>",
    "timestamp": 1792199122,
    "triggerSrc": "Android",
    "uuid": "<device uuid>"
  },
  "payload": {}
}
```

| Field | Value |
|---|---|
| `messageId` | `md5(16 random [a-z0-9] chars + str(timestamp))`, lowercase hex. Replies carry the same id. **Code** (`PackageFactory.java:846-859,930`). Any unique 32-hex string works. **Verified** |
| `timestamp` | Unix **seconds**. **Verified** |
| `sign` | `md5(messageId + key + str(timestamp))`, lowercase hex. `key` is the account key returned at sign-in. **Verified** (`PackageFactory.java:967`) |
| `method` | Requests are `GET` or `SET`. Replies are `GETACK` or `SETACK`. Unsolicited state changes are `PUSH`. Failures are `ERROR`. **Verified** for GET/GETACK/SET/SETACK |
| `from` | MQTT: the topic the app wants the reply on. LAN: the request URL, e.g. `http://192.168.1.21/config`. **Code** (`BaseLocal.java:20,51-57`); both **Verified** to work |
| `triggerSrc` | `Android` for MQTT, `AndroidLocal` for LAN, `AndroidWidget` for the home-screen widget. Informational. **Code** |
| `uuid` | The target device. Sent over MQTT; harmless on LAN. **Code** |

**Replies** are signed the same way, using the reply's own `messageId` and `timestamp`. The app rejects a reply whose sign doesn't match, with internal error 1023. **Code** (`ParseFactory.java:9-11`). Meross Deck checks the sign on every reply. **Verified**

**Errors** look like this:

```json
{"header": {"method": "ERROR", ...}, "payload": {"error": {"code": 5005, "detail": "..."}}}
```

- **5005** means "device busy". The app shows it as "No device." **Code**
- **5007** means the device isn't calibrated, which applies to shutters. **Code**
- 5020–5022 are IR errors, 5061 is device offline, and 5071 is abnormal-voltage protection. **Code** (`Constants.java:100-123`)
- 5000–5004 have no special handling and show as a generic "Failure(500x)". **Code**

---

## 2. Cloud HTTP API

### Transport

| | |
|---|---|
| Hosts | Pre-sign-in default: `https://iotx.meross.com`. The account's real host comes back from sign-in, e.g. `https://iotx-ap.meross.com` for Australia. **Verified.** No regional host is hard-coded in the app. **Code** |
| Method | Always `POST`, `application/x-www-form-urlencoded`. **Verified** |
| Body | Exactly four form fields: `params`, `timestamp`, `nonce`, `sign`. **Verified** |
| Timeout | The app uses 10 s. **Code** |

### Signing

```
params    = base64(json(params_object))          # standard alphabet, padded; "{}" when empty
timestamp = str(unix seconds)
nonce     = 16 random chars from [a-z0-9]
sign      = md5_hex("23x17ahWarFH6w29" + timestamp + nonce + params)
```

- The sign covers the base64 string before form URL-encoding. **Verified**
- The secret `23x17ahWarFH6w29` is a constant in `HttpUtils.java:76`. **Code**
- Offline test vector: params `{}` → `e30=`, timestamp `1700000000`, nonce `abcdefghij012345` → sign `a018f465678293caaf2480d2fcb8713b`. This is a unit test in `tests/test_meross_deck.py`.

### Headers

| Header | Value |
|---|---|
| `AppVersion` | `3.42.3` |
| `Vendor` | `meross` |
| `AppType` | `Android` |
| `AppLanguage` | `en` |
| `Tz` | IANA zone, e.g. `Australia/Sydney` |
| `App-Gray-Id` | A stable number. The app derives one from the phone's UUID; any stable value works. **Verified** |
| `Authorization` | `Basic <token>`. This is the **raw** token, not base64 of user:password. Sent only once signed in. **Verified** |

### Response envelope

```json
{"apiStatus": 0, "sysStatus": 0, "info": "Success", "timestamp": 1792200975, "data": {...}}
```

Success is `apiStatus == 0 && sysStatus == 0`. **Verified.** Codes the app handles specially, all **Code** (`ResultFunc1.java`, `MS2FAResultFunc.java`, `Constants.java`):

| apiStatus | Meaning | What to do |
|---|---|---|
| 1019, 1022, 1201 | Token expired or invalid | Sign in again |
| 1030 | Account belongs to another region; `data` (a dict, or a JSON string) has `domain` | Retry on `data.domain` after about 500 ms |
| 1033 | MFA required | Resend the same params plus `mfaCode` (a TOTP code) |
| 1004 | Wrong password | |
| 1035 | Safety limit after repeated failures; wait about 10 minutes | |
| 1028 | Requests too fast | |

### Endpoints

#### `POST /v1/Auth/signIn` (Verified)

```json
{
  "email": "you@example.com",
  "password": "<md5 hex of the password>",
  "encryption": 1,
  "accountCountryCode": "AU",
  "agree": 1,
  "mobileInfo": {"resolution": "1920*1080", "carrier": "", "deviceModel": "Linux,KDE Plasma",
                 "mobileOs": "Android", "mobileOsVersion": "14", "uuid": "<random>"},
  "mfaCode": "123456"
}
```

- The password is sent as `md5(password)`, and `encryption: 1` tells the server so. **Verified**
- `mfaCode` is sent only on the retry after a 1033. **Code**
- No `Authorization` header is sent. **Verified**

The reply `data` is:

```json
{"token": "...", "key": "...", "userid": "1000001", "email": "...",
 "domain": "https://iotx-ap.meross.com", "mqttDomain": "mqtt-ap-v2.meross.com"}
```

**Verified.**

- Use `domain` for every later HTTP call. **Verified**
- `key` signs device messages and derives the MQTT password. **Verified**
- There is no refresh endpoint and no refresh logic in the app. The token lives until the server says otherwise. **Code**
- Whether a new sign-in ends other sessions, such as the phone's, is unknown. Nothing in the app decides it; it is the server's policy. **Inferred**

#### `POST /v1/Auth/countryCodeRegion`

Takes `{"accountCountryCode": "AU"}` and returns `{domain, mqttDomain}` before sign-in, so a client can start on the right region. **Code.** Optional: the 1030 redirect covers the same ground.

#### `POST /v1/Device/devList` (Verified)

Params are `{}` and `Authorization` is required. It returns an array:

```json
{
  "uuid": "<32 hex chars>",
  "devName": "Smart Floor Lamp",
  "deviceType": "msl610",
  "subType": "un",
  "onlineStatus": 1,
  "channels": [{}],
  "domain": "mqtt-ap-v2.meross.com",
  "reservedDomain": "mqtt-ap-v2.meross.com",
  "fmwareVersion": "4.2.2",
  "hdwareVersion": "4.0.0",
  "region": "us",
  "bindTime": 1759475173,
  "devIconId": "device_msl610_un",
  "iconType": 1,
  "hardwareCapabilities": []
}
```

- `onlineStatus`: 1 = online, 2 = offline, 3 = upgrading. The app treats 1 and 3 as controllable. **Verified** for 1/2; **Code** for 3
- `domain` is the **MQTT broker for this device**. **Verified**
- `channels[i].devName` names channel `i`; channel 0 falls back to `devName`. **Code**
- The order of the list isn't stable between calls. **Verified**
- No cloud endpoint returns a device's LAN IP. It comes from the device itself (§5.1). **Code**

#### Other endpoints

| Endpoint | Params | Purpose | |
|---|---|---|---|
| `/v1/Device/devInfo` | `{uuid}` | One device, same shape as above | **Code** |
| `/v1/Hub/getSubDevices` | `{uuid}` | Hub children (sensors, valves) | **Code** |
| `/v1/Device/setDeviceName` | `{uuid, devName}` | Rename | **Code** |
| `/v1/Profile/logout` | `{}` or `{fcmToken}` | Invalidate the token | **Code** |
| `/user/v1/baseInfo` | `{timezone, regionCode}` | Profile, called after sign-in | **Code** |

#### Control through the cloud relay

The app's own Android home-screen widget doesn't use MQTT. It uses two HTTP endpoints (`meross/widgets/WidgetDataSource.java`).

**`POST /v1/mqtt/switchStatus`** (**Verified**, read-only)

```json
{"devices": ["<uuid>", "<uuid>"]}
```

```json
{"devices": {"<uuid>": {"status": 0, "channels": [{"channel": 0, "onoff": 1}]},
             "<offline uuid>": {"status": 1229, "channels": []}}}
```

- `status 0` means the channel data is valid.
- 1229 was returned for an offline plug. **Verified**
- For garage openers, `onoff` follows the door's open state. **Verified** (closed = 0)
- The app batches 20 devices per call. **Code**

**`POST /v1/mqtt/event`** (**Code**, fire-and-forget)

```json
{"event": {"eventType": "DEVICE_EVENT",
           "eventData": {"<uuid>": [{"method": "SET", "namespace": "Appliance.Control.ToggleX",
                                     "payload": {"togglex": {"channel": 0, "onoff": 1}},
                                     "triggerSrc": "AndroidWidget"}]}}}
```

- The cloud forwards the command to the device. The reply has no device answer, so the client never learns whether the command landed. **Code**
- `{"eventType": "SCENE_EVENT", "eventData": {"sceneId": N}}` runs a scene. **Code**

Meross Deck uses MQTT instead, because it gets the device's SETACK back.

---

## 3. Cloud MQTT

### Connection

| | |
|---|---|
| Broker | The device's `domain` from `devList`, e.g. `mqtt-ap-v2.meross.com`. **Verified.** The app keeps one connection per distinct domain. **Code** (`Tcp.java:190`) |
| Port | 443, TLS over raw TCP (no WebSocket). **Verified** |
| TLS | System CAs, no pinning, no client certificate. **Verified** with Python's default context, hostname check on |
| Protocol | MQTT 3.1.1 (level 4); the app falls back to 3.1. Clean session, keepalive 30 s. **Verified** for 3.1.1 |
| Client ID | `app:<appId>`. The app derives the 32-char `appId` from the install. Any stable 32-hex string works. **Verified.** Don't reuse the phone's id, or the broker will drop one of the two connections. **Inferred** |
| Username | `userid` (decimal string). **Verified** |
| Password | `md5(userid + key)`, lowercase hex. **Verified** |

A CONNACK return code of 4 or 5 means bad credentials, so sign in again. **Code**

### Topics

| Direction | Topic | |
|---|---|---|
| Subscribe (QoS 1) | `/app/<userid>-<appId>/subscribe`: replies to this client | **Verified** |
| Subscribe (QoS 1) | `/app/<userid>/subscribe`: pushes for the account | **Code**; subscription accepted, **Verified** |
| Publish (QoS 0) | `/appliance/<uuid>/subscribe`: commands to one device | **Verified** |

- Set the request's `header.from` to your reply topic. **Verified**
- Match replies to requests by `messageId`. The app ignores repeats of its last two ids. **Code** (`TcpMsgManager.java:141`)
- For a `PUSH`, the device uuid is `header.from.split("/")[2]`. **Code**

MQTT payloads are plain JSON; the LAN AES layer is never used here. **Verified**

### Timing

- The app waits 15 s for a reply, and 12 s for a command queued while it reconnects. **Code**
- In testing, one connection plus six GETs (System.All and System.Ability for three devices) took about 2.4 s from Australia. **Verified**
- A malformed GET can end the whole connection: `Appliance.Control.TimerX` with `{"timerx": {"channel": 0}}` made the broker close it rather than return an `ERROR`. **Verified.** Open a new connection after a drop.
- Many clients report that Meross throttles chatty connections. **Inferred**: no limit is visible in the app. Meross Deck polls the cloud once a minute by default and connects only when the LAN doesn't answer.

### Minimal session

```
CONNECT    client "app:<appId>", user "<userid>", pass md5(userid+key), clean, keepalive 30
SUBSCRIBE  /app/<userid>-<appId>/subscribe (QoS 1), /app/<userid>/subscribe (QoS 1)
PUBLISH    /appliance/<uuid>/subscribe  {"header":{..."method":"GET","namespace":"Appliance.System.All"...},"payload":{}}
<- PUBLISH /app/<userid>-<appId>/subscribe  {"header":{..."method":"GETACK"...},"payload":{"all":{...}}}
DISCONNECT
```

`src/meross_deck.py` (class `Mqtt`) implements this in about 150 lines of the standard library.

---

## 4. LAN HTTP

### Transport

| | |
|---|---|
| URL | `POST http://<innerIp>/config`, port 80. **Verified** |
| Body | The JSON message from §1 with `Content-Type: application/json`. The app also sends `Connection: close`. **Verified** |
| Auth | The message sign, using the cloud account key. Without the key, a device can't be controlled. **Verified** |
| Speed | About 0.35 s for three devices in parallel. **Verified.** The app cuts each command off at 1, 3 or 5 s depending on the command. **Code** |

### Finding devices

- **IP address:** `Appliance.System.All` returns `payload.all.system.firmware.innerIp`. Read it once through the cloud, then cache it. **Verified**
- **UDP discovery (Code):** the app binds UDP 9989 and broadcasts `{"id": md5(userid), "devName": "*"}` to `255.255.255.255:9988` five times over 6 s. Devices answer with `{uuid, ip, mac, port, ...}`. This is `UDPBoundDevDiscover.java:10-23`; Meross Deck doesn't use it.
- **mDNS:** none used for control. **Code**

### Encryption (newer firmware)

Devices whose `Appliance.System.Ability` lists `Appliance.Encrypt.Suite` encrypt most LAN messages. **Code** (`LocalEncryptUtils.java`, `ControlDeviceEncryptManager.java`).

- **None of the tested devices list it.** They all answered plain JSON for both System and Control namespaces. **Verified**
- `firmware.encrypt: 1` in `System.All` does **not** mean the LAN is encrypted. **Verified**

How an encrypted device works (**Code**; implemented in Meross Deck and unit-tested, but untested on a device):

- These namespaces always stay plain: `Appliance.System.All`, `.System.Firmware`, `.System.Ability`, `.Encrypt.Suite`, `.Encrypt.ECDHE`.
- Key: `md5_hex(uuid[3:22] + key[1:9] + macAddress + key[10:28])`. The **32 ASCII hex characters** are the AES-256 key. `macAddress` is `system.hardware.macAddress`, used verbatim.
- Cipher: AES-256-CBC. The IV is the ASCII string `"0000000000000000"`, not zero bytes.
- Padding: zero bytes, always 1–16 of them; a full extra block if the length is already a multiple of 16.
- The request body is `base64(ciphertext)` as `text/plain`. The reply is decrypted the same way, with trailing NULs stripped.
- No ECDH is needed for control. ECDH (P-256) is used only while pairing a new device in AP mode.

The standard library has no AES, so `meross_deck.py` contains a small AES-256. It is checked against the FIPS-197 vector and against `openssl enc -aes-256-cbc -nopad`.

---

## 5. Namespaces

### 5.1 `Appliance.System.All` (Verified)

`GET`, payload `{}`. Reply, abbreviated, from an MSG100 (identifiers redacted):

```json
{"all": {
  "system": {
    "hardware": {"type": "msg100", "subType": "us", "version": "4.0.0", "chipType": "MT7686",
                 "uuid": "...", "macAddress": "aa:bb:cc:00:00:00"},
    "firmware": {"version": "4.2.20", "compileTime": "Dec 31 2024 10:45:32", "encrypt": 1,
                 "wifiMac": "...", "innerIp": "192.168.1.21", "server": "mqtt-ap-v2.meross.com",
                 "port": 443, "userId": 1000001},
    "time": {"timestamp": 1792199122, "timezone": "Australia/Sydney", "timeRule": [...]},
    "online": {"status": 1, "bindId": "...", "who": 1}
  },
  "digest": {
    "togglex": [{"channel": 0, "onoff": 0, "lmTime": 0}],
    "garageDoor": [{"channel": 0, "open": 0, "lmTime": 0}],
    "triggerx": [...], "timerx": [...]
  }
}}
```

- On a light, `digest.light` is a **single object**, `{"capacity": 6, "channel": 0, "temperature": 75, "luminance": 80}`, not a list. **Verified** (MSL610)
- Full redacted replies are in `tests/fixtures/`.

### 5.2 `Appliance.System.Ability` (Verified)

`GET`, payload `{}`. It returns `{"ability": {"<namespace>": {...}, ...}}`, listing every namespace the device supports.

- The MSG100 lists `GarageDoor.State`, `GarageDoor.Config`, `Control.ToggleX`, `Control.Multiple`, `Control.TimerX` and others.
- The MSL610 lists `Control.Light` and `Control.ToggleX`.

### 5.3 `Appliance.Control.ToggleX`: on/off for plugs, switches and lights

- **SET:** `{"togglex": {"channel": 0, "onoff": 1}}` → SETACK. **Verified** on the MSL610, both LAN and cloud.
- **GET:** `{"togglex": {"channel": 0}}` → `{"channel": 0, "togglex": {"channel": 0, "onoff": 1, "lmTime": 1792200969}}`. **Verified**
- `lmTime` is the Unix time of the last change. **Verified**
- Bulbs and lamps are switched on and off here, not through `Control.Light`. **Code**, **Verified**
- Power strips (mss425 and similar): channel 0 is the master and 1..n are the outlets. **Code**

### 5.4 `Appliance.Control.Toggle`: legacy plugs

`SET {"toggle": {"onoff": 1}, "channel": 0}`. Note that `channel` sits beside `toggle`, not inside it. It's used for older mss110/210/310 firmware (the app's `PROTOCOL_7688`). **Code.** Meross Deck falls back to it when a device's abilities list `Control.Toggle` but not `Control.ToggleX`.

### 5.5 `Appliance.Control.Light`

`SET {"light": {"capacity": C, "channel": 0, "luminance": 40}}`. **Verified**

`capacity` is a bitmask of the fields the SET carries: 1 = rgb, 2 = temperature, 4 = luminance, 8 = effect. **Verified** for 2 and 4.

| Field | Range | |
|---|---|---|
| `luminance` | 1–100 | **Verified** |
| `temperature` | 1–100, where 1 = warmest and 100 = coolest | **Verified** for the range; direction **Inferred** from the app's UI |
| `rgb` | `0xRRGGBB` as an integer | **Code** |
| `gradual` | 0/1, fade | **Code** |

- What each model supports (`LightCapacityUtil.java:174-226`, **Code**):
  - MSL610, MSL110 and MDL110: 6 (temperature + brightness)
  - MSL120 and MSL430: 7, or 15 on newer firmware (adds rgb and effects)
  - MSL100: 4 (brightness only)
- GET `{"light": {"channel": 0}}` returns the same object. **Verified**

### 5.6 `Appliance.GarageDoor.State`: MSG100, MSG150, MSG200

- **GET:** `{"state": {"channel": 0}}` → `{"state": {"channel": 0, "open": 0, "lmTime": 0}}`, a single object. **Verified** (MSG100)
- **PUSH:** `{"state": [{"channel": 0, "open": 1, "lmTime": ...}]}`, an **array**. **Code**
- **SET:** `{"state": {"channel": 0, "open": 1, "uuid": "<device uuid>"}}`. `open` is 1 to open and 0 to close. **Verified** on an MSG100 over LAN, both directions (`PackageFactory.java:82-90`).
- **SETACK** echoes the state from **before** the command, plus `execute: 1`. Opening a closed door returns `{"state": {"channel": 0, "open": 0, "lmTime": 0, "execute": 1}}`. **Verified.** Don't read the new state from it; re-read after the travel time.

Other fields:

- `obstruction`: 1 = obstruction, 2 = none.
- `signalValid`: 1 = valid, 2 = not.

Both are **Code**; the MSG100 didn't send either.

**There is no moving state.** `open` is a reed switch: it flips to 1 as soon as the door leaves the closed position, and to 0 only when the door is fully closed. **Inferred** from the app's logic. The app's "Opening…/Closing…" is a client-side timer of the door's configured travel time, followed by a re-read (`MSRoomDeviceViewModel.java:77-160`). **Code.** Meross Deck does the same.

Channels:

- MSG100 and MSG150: one door, on channel 0. **Verified**
- MSG200: doors on channels 1–3, with channel 0 the controller. **Code**

### 5.7 `Appliance.GarageDoor.Config`: single-door timing

`GET {}` → `{"config": {"signalDuration": 500, "buzzerEnable": 0, "doorOpenDuration": 12000, "doorCloseDuration": 12000}}`. **Verified**

- Durations are in milliseconds.
- The two tested doors were set to 12 s and 13 s. **Verified**
- The MSG200 uses `Appliance.GarageDoor.MultipleConfig` instead. It returns an array of `{channel, doorEnable, doorOpenDuration, doorCloseDuration, signalOpen, signalClose, buzzerEnable}`; `doorEnable: 0` means the slot has no door. **Code**

### 5.8 Others

| Namespace | Use | |
|---|---|---|
| `Appliance.System.Online` | `GET {}` → `{"online": {"status": 1, "bindId": "...", "who": 1}}` | **Verified** |
| `Appliance.Control.Multiple` | `SET {"multiple": [<message>, ...]}`, where each inner header is just `{namespace, method}`. Batch size is `ability["Appliance.Control.Multiple"].maxCmdNum` | **Code** |
| `Appliance.Control.Electricity` | `GET {"electricity": {"channel": 0}}` → `{channel, current, voltage, power}`. Power is in mW, current probably in mA, voltage in 0.1 V | **Code** / **Inferred** for units |
| `Appliance.Control.Upgrade` | Firmware updates. The app always sends these over MQTT | **Code** |
| `Appliance.Control.TimerX` | On-device schedules. `digest.timerx` lists `{channel, id, count}`. `GET {"timerx": {"id": "<id>"}}` returns the timer, see below. A GET without an `id` was refused | **Verified** |

A timer, from the MSL610:

```json
{"timerx": {"id": "…", "alias": "Lamp on", "type": 1, "enable": 0, "channel": 0, "week": 255,
            "time": 1080, "sunOffset": 0, "duration": 0, "extend": {"toggle": {"onoff": 1, "lmTime": 0}}}}
```

- `time` is minutes after midnight, so 1080 is 18:00. **Verified**
- `week` is a day bitmask; 255 includes every day. **Inferred**
- `extend.toggle.onoff` is the action. **Verified**

---

## 6. How the app chooses a transport

`Tcp.x0` and `DeviceJsonTransportSelector` (**Code**) send a command over the LAN when all of these hold:

1. the device has a cached `innerIp`
2. it has had fewer than three LAN failures
3. it was recently found by UDP, answered a LAN `Appliance.System.Online`, or its Wi-Fi MAC prefix matches the phone's access point

Otherwise the command goes over MQTT.

- A LAN timeout gets a fresh `messageId` and sign and falls back to MQTT.
- An `ERROR` reply from the device does **not** fall back.
- `Appliance.Control.Upgrade` always uses MQTT.

Meross Deck does a simpler version of this:

- Try the LAN with a 2.5 s timeout when the IP is known.
- If that misses, use MQTT, and skip the LAN for that device for the next 5 minutes.
- Device errors never fall back.
