import base64
import io
import json
import os
import struct
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import src.meross_deck as md

FIXTURES = Path(__file__).parent / "fixtures"
SESSION = {"token": "tok", "key": "k" * 32, "userid": "1000001", "domain": "https://iotx-ap.meross.com",
           "mqttDomain": "mqtt-ap-v2.meross.com", "appId": "a" * 32, "email": "me@example.com"}


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def signed_reply(request, payload, key=SESSION["key"], method=None):
    """What a device sends back to `request`."""
    header = dict(request["header"])
    header["method"] = method or header["method"] + "ACK"
    header["sign"] = md.md5(header["messageId"] + key + str(header["timestamp"]))
    return {"header": header, "payload": payload}


def listed_cache():
    devices = fixture("devlist.json")
    return {"devices": {}, "listed": {d["uuid"]: d for d in devices}, "order": [d["uuid"] for d in devices],
            "listedAt": int(time.time())}


def uuid_of(device_type):
    return next(d["uuid"] for d in fixture("devlist.json") if d["deviceType"] == device_type)


class SigningTests(unittest.TestCase):
    def test_cloud_form_matches_offline_vector(self):
        form = md.sign_params({}, 1700000000, "abcdefghij012345")
        self.assertEqual(form, {"sign": "a018f465678293caaf2480d2fcb8713b", "timestamp": "1700000000",
                                "params": "e30=", "nonce": "abcdefghij012345"})

    def test_device_message_sign_round_trips(self):
        msg = md.message("GET", "Appliance.System.All", {}, SESSION["key"], "/app/x/subscribe", "u1", now=1700000000)
        header = msg["header"]
        self.assertEqual(header["sign"], md.md5(header["messageId"] + SESSION["key"] + "1700000000"))
        self.assertEqual((header["payloadVersion"], header["uuid"], header["triggerSrc"]), (1, "u1", "Android"))
        self.assertEqual(md.check_reply(signed_reply(msg, {"ok": 1}), SESSION["key"])["payload"], {"ok": 1})

    def test_bad_sign_and_device_errors_raise(self):
        msg = md.message("SET", "Appliance.Control.ToggleX", {}, SESSION["key"], "o", "u1")
        with self.assertRaisesRegex(md.MerossError, "signature"):
            md.check_reply(signed_reply(msg, {}, key="wrong"), SESSION["key"])
        with self.assertRaises(md.MerossError) as error:
            md.check_reply(signed_reply(msg, {"error": {"code": 5005}}, method="ERROR"), SESSION["key"])
        self.assertEqual((error.exception.kind, error.exception.code), ("device", 5005))


class AesTests(unittest.TestCase):
    def test_fips197_aes256_vector(self):
        key = bytes(range(32))
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        cipher = md.aes_cbc_encrypt(key, plain, iv=bytes(16))
        self.assertEqual(cipher.hex(), "8ea2b7ca516745bfeafc49904b496089")
        self.assertEqual(md.aes_cbc_decrypt(key, cipher, iv=bytes(16)), plain)

    def test_lan_key_uses_ascii_md5(self):
        key = md.lan_key("0123456789abcdef0123456789abcdef", "abcdefghijklmnopqrstuvwxyz012345", "aa:bb:cc:dd:ee:ff")
        self.assertEqual(len(key), 32)
        self.assertEqual(key, md.md5("3456789abcdef012345" + "bcdefghi" + "aa:bb:cc:dd:ee:ff"
                                     + "klmnopqrstuvwxyz01").encode())

    def test_encrypted_lan_request_round_trips(self):
        key = md.lan_key("0" * 32, SESSION["key"], "aa:bb:cc:dd:ee:ff")
        seen = {}

        def opener(request, timeout):
            raw = md.aes_cbc_decrypt(key, base64.b64decode(request.data)).rstrip(b"\0")
            seen["request"] = json.loads(raw)
            seen["type"] = request.get_header("Content-type")
            body = json.dumps(signed_reply(seen["request"], {"togglex": {"onoff": 1}})).encode()
            body += b"\0" * (16 - len(body) % 16)
            return io.BytesIO(base64.b64encode(md.aes_cbc_encrypt(key, body)))

        reply = md.lan_request("10.0.0.2", "0" * 32, "SET", "Appliance.Control.ToggleX", {"togglex": {}},
                               SESSION, crypt_key=key, opener=opener)
        self.assertEqual(reply["payload"], {"togglex": {"onoff": 1}})
        self.assertEqual(seen["type"], "text/plain")
        self.assertEqual(seen["request"]["header"]["from"], "http://10.0.0.2/config")
        self.assertEqual(seen["request"]["header"]["triggerSrc"], "AndroidLocal")


class ViewTests(unittest.TestCase):
    def test_garage_door_view_uses_config_durations(self):
        listed = next(d for d in fixture("devlist.json") if d["deviceType"] == "msg100")
        view = md.build_view(listed, {}, fixture("system_all_msg100.json"),
                             {"config": fixture("garage_config_msg100.json")["config"]})
        self.assertEqual(view["kind"], "garage")
        self.assertEqual(view["channels"], [{"channel": 0, "kind": "door", "name": listed["devName"], "open": False,
                                             "lmTime": 0, "obstruction": False, "openMs": 12000, "closeMs": 12000}])

    def test_lamp_view_has_brightness_and_temperature(self):
        listed = next(d for d in fixture("devlist.json") if d["deviceType"] == "msl610")
        view = md.build_view(listed, {}, fixture("system_all_msl610.json"))
        self.assertEqual(view["kind"], "light")
        channel = view["channels"][0]
        self.assertEqual((channel["kind"], channel["on"], channel["capacity"], channel["luminance"],
                          channel["temperature"]), ("light", False, 6, 80, 75))

    def test_msg200_skips_controller_channel_and_disabled_doors(self):
        listed = {"uuid": "g", "devName": "Garage", "deviceType": "msg200", "onlineStatus": 1,
                  "channels": [{}, {"devName": "Car"}, {"devName": "Bike"}, {"devName": "Spare"}]}
        all_payload = {"all": {"digest": {"garageDoor": [{"channel": c, "open": c == 2} for c in range(4)]}}}
        config = [{"channel": 1, "doorEnable": 1, "doorOpenDuration": 20000}, {"channel": 2, "doorEnable": 1},
                  {"channel": 3, "doorEnable": 0}]
        view = md.build_view(listed, {}, all_payload, {"multipleConfig": config})
        self.assertEqual([(c["name"], c["open"], c["openMs"]) for c in view["channels"]],
                         [("Car", False, 20000), ("Bike", True, 15000)])

    def test_device_kinds(self):
        self.assertEqual([md.device_kind(t) for t in ("msg100", "msg200", "msl610", "mss210p", "mss425f", "mss510x",
                                                     "mdl110m", "xyz")],
                         ["garage", "garage", "light", "plug", "strip", "switch", "light", "other"])

    def test_command_payloads(self):
        self.assertEqual(md.command_payload("door", 0, True, "u"),
                         ("Appliance.GarageDoor.State", {"state": {"channel": 0, "open": 1, "uuid": "u"}}))
        self.assertEqual(md.command_payload("switch", 2, False, "u"),
                         ("Appliance.Control.ToggleX", {"togglex": {"channel": 2, "onoff": 0}}))
        self.assertEqual(md.command_payload("luminance", 0, 140, "u"),
                         ("Appliance.Control.Light", {"light": {"capacity": 4, "channel": 0, "luminance": 100}}))
        self.assertEqual(md.command_payload("temperature", 0, 0, "u"),
                         ("Appliance.Control.Light", {"light": {"capacity": 2, "channel": 0, "temperature": 1}}))


class FakeMqtt:
    """Answers every request from `answers` {uuid: payload or MerossError}."""
    instances = []

    def __init__(self, host, session, answers=None, fail=None):
        self.host, self.answers, self.fail, self.sent = host, answers or {}, fail, []
        FakeMqtt.instances.append(self)

    def __enter__(self):
        if self.fail:
            raise self.fail
        return self

    def __exit__(self, *exc):
        pass

    def request(self, requests):
        self.sent.extend(requests)
        out = {}
        for i, (uuid, method, namespace, payload) in enumerate(requests):
            answer = self.answers.get((uuid, namespace), self.answers.get(uuid, {}))
            out[i] = answer if isinstance(answer, md.MerossError) else {"payload": answer}
        return out


class HubTests(unittest.TestCase):
    def setUp(self):
        FakeMqtt.instances = []
        self.cache = listed_cache()
        self.lamp = uuid_of("msl610")
        self.cache["devices"][self.lamp] = {"uuid": self.lamp, "ip": "10.0.0.5"}

    def hub(self, lan_call, answers=None, **kw):
        return md.Hub(SESSION, self.cache, mqtt_factory=lambda h, s: FakeMqtt(h, s, answers), lan_call=lan_call, **kw)

    def test_lan_answer_skips_cloud(self):
        hub = self.hub(lambda ip, uuid, *a, **k: {"payload": {"via": ip}})
        result = hub.run({self.lamp: [("GET", "Appliance.System.All", {})]})
        self.assertEqual(result[self.lamp], {"route": "lan", "replies": [{"via": "10.0.0.5"}]})
        self.assertEqual(FakeMqtt.instances, [])

    def test_lan_miss_falls_back_to_cloud_and_backs_off(self):
        calls = []

        def unreachable(*args, **kw):
            calls.append(args)
            raise md.MerossError("timeout")
        hub = self.hub(unreachable, {self.lamp: {"via": "cloud"}})
        result = hub.run({self.lamp: [("GET", "Appliance.System.All", {})]})
        self.assertEqual(result[self.lamp], {"route": "cloud", "replies": [{"via": "cloud"}]})
        self.assertEqual(FakeMqtt.instances[0].host, "mqtt-ap-v2.meross.com")
        hub.run({self.lamp: [("GET", "Appliance.System.All", {})]})
        self.assertEqual(len(calls), 1, "second run should skip the LAN after a recent miss")

    def test_device_error_on_lan_does_not_retry_in_cloud(self):
        def busy(*args, **kw):
            raise md.MerossError("Device error 5005", "device", 5005)
        result = self.hub(busy).run({self.lamp: [("SET", "Appliance.Control.ToggleX", {})]})
        self.assertEqual(result[self.lamp]["route"], "lan")
        self.assertEqual(result[self.lamp]["error"].code, 5005)
        self.assertEqual(FakeMqtt.instances, [])

    def test_cloud_failure_marks_each_device(self):
        hub = md.Hub(SESSION, self.cache, lan=False,
                     mqtt_factory=lambda h, s: FakeMqtt(h, s, fail=md.MerossError("broker down")))
        garage = uuid_of("msg100")
        result = hub.run({garage: [("GET", "Appliance.System.All", {})], self.lamp: [("GET", "x", {})]})
        self.assertEqual({u: str(r["error"]) for u, r in result.items()}, {garage: "broker down", self.lamp: "broker down"})


class SnapshotTests(unittest.TestCase):
    def test_snapshot_learns_ip_and_keeps_offline_devices(self):
        cache = listed_cache()
        garage, lamp = uuid_of("msg100"), uuid_of("msl610")
        answers = {lamp: fixture("system_all_msl610.json"),
                   (lamp, "Appliance.System.Ability"): fixture("ability_msl610.json")}
        for door in (d["uuid"] for d in fixture("devlist.json") if d["deviceType"] == "msg100"):
            answers.update({door: fixture("system_all_msg100.json"),
                            (door, "Appliance.System.Ability"): fixture("ability_msg100.json"),
                            (door, "Appliance.GarageDoor.Config"): fixture("garage_config_msg100.json")})
        hub = md.Hub(SESSION, cache, lan=False, mqtt_factory=lambda h, s: FakeMqtt(h, s, answers))
        result = md.snapshot(SESSION, cache, hub=hub)
        kinds = [d["kind"] for d in result["devices"]]
        self.assertEqual(kinds, sorted(kinds, key=md.KIND_ORDER.index))
        by_uuid = {d["uuid"]: d for d in result["devices"]}
        self.assertEqual(by_uuid[garage]["route"], "cloud")
        self.assertEqual(by_uuid[garage]["channels"][0]["openMs"], 12000)
        self.assertTrue(cache["devices"][garage]["ip"].startswith("192.168.1."))
        self.assertEqual(cache["devices"][lamp]["encrypted"], False)
        plug = next(d for d in result["devices"] if d["kind"] == "plug")
        self.assertEqual((plug["online"], plug["channels"], plug["stale"]), (False, [], True))
        # The second pass reuses the cached ability and config: one System.All per online device.
        sent = sum(len(m.sent) for m in FakeMqtt.instances)
        md.snapshot(SESSION, cache, hub=hub)
        self.assertEqual(sum(len(m.sent) for m in FakeMqtt.instances) - sent, 3)

    def test_recent_cloud_readings_are_reused(self):
        cache = listed_cache()
        lamp = uuid_of("msl610")
        answers = {lamp: fixture("system_all_msl610.json"), (lamp, "Appliance.System.Ability"): fixture("ability_msl610.json")}
        hub = md.Hub(SESSION, cache, lan=False, mqtt_factory=lambda h, s: FakeMqtt(h, s, answers))
        md.snapshot(SESSION, cache, uuids={lamp}, hub=hub, cloud_max_age=60)
        sent = sum(len(m.sent) for m in FakeMqtt.instances)
        again = md.snapshot(SESSION, cache, uuids={lamp}, hub=hub, cloud_max_age=60)
        self.assertEqual(sum(len(m.sent) for m in FakeMqtt.instances), sent, "no second cloud read")
        view = next(d for d in again["devices"] if d["uuid"] == lamp)
        self.assertEqual((view["route"], view["stale"], view["channels"][0]["luminance"]), ("cloud", False, 80))
        cache["devices"][lamp]["cloudAt"] -= 61
        md.snapshot(SESSION, cache, uuids={lamp}, hub=hub, cloud_max_age=60)
        self.assertGreater(sum(len(m.sent) for m in FakeMqtt.instances), sent)

    def test_legacy_toggle_used_when_togglex_is_missing(self):
        cache = listed_cache()
        plug = uuid_of("mss210p")
        cache["devices"][plug] = {"uuid": plug, "abilities": ["Appliance.Control.Toggle"]}
        hub = md.Hub(SESSION, cache, lan=False, mqtt_factory=lambda h, s: FakeMqtt(h, s, {plug: {}}))
        result = md.run_command(SESSION, cache, plug, "switch", 0, True, hub=hub)
        self.assertEqual(result["namespace"], "Appliance.Control.Toggle")
        self.assertEqual(FakeMqtt.instances[-1].sent[0][3], {"toggle": {"onoff": 1}, "channel": 0})


class FakeBroker:
    """A socket that speaks just enough MQTT to test the client."""

    def __init__(self, session, reply_with=None, connack=0):
        self.session, self.reply_with, self.inbox = session, reply_with or {}, b""
        self.outbox = bytes([0x20, 2, 0, connack]) + bytes([0x90, 3, 0, 1, 1])
        self.topics = []

    def settimeout(self, _):
        pass

    def sendall(self, data):
        if data[0] == 0x30:
            length, i, shift = 0, 1, 0
            while True:
                length |= (data[i] & 0x7F) << shift
                shift += 7
                i += 1
                if not data[i - 1] & 0x80:
                    break
            body = data[i:i + length]
            n = struct.unpack("!H", body[:2])[0]
            self.topics.append(body[2:2 + n].decode())
            request = json.loads(body[2 + n:])
            push = {"header": {"method": "PUSH", "messageId": "p", "from": "/appliance/x/publish"}, "payload": {}}
            for msg in (push, signed_reply(request, self.reply_with.get(request["header"]["namespace"], {}))):
                self._publish("/app/%s-%s/subscribe" % (self.session["userid"], self.session["appId"]), msg, qos=1)

    def _publish(self, topic, msg, qos=0):
        payload = json.dumps(msg).encode()
        variable = md._utf8(topic) + (b"\x00\x07" if qos else b"")
        self.outbox += bytes([0x30 | (qos << 1)]) + md._varint(len(variable) + len(payload)) + variable + payload

    def recv(self, n):
        if not self.outbox:
            import socket
            raise socket.timeout()
        data, self.outbox = self.outbox[:n], self.outbox[n:]
        return data

    def close(self):
        pass


class MqttTests(unittest.TestCase):
    def test_request_matches_reply_and_collects_pushes(self):
        broker = FakeBroker(SESSION, {"Appliance.System.All": {"all": {"ok": 1}}})
        with md.Mqtt("broker", SESSION, timeout=1, connect=lambda: broker) as client:
            answers = client.request([("dev1", "GET", "Appliance.System.All", {})])
            self.assertEqual(len(client.pushes), 1)
        self.assertEqual(answers[0]["payload"], {"all": {"ok": 1}})
        self.assertEqual(broker.topics, ["/appliance/dev1/subscribe"])

    def test_refused_login_is_an_auth_error(self):
        with self.assertRaises(md.MerossError) as error:
            md.Mqtt("broker", SESSION, timeout=1, connect=lambda: FakeBroker(SESSION, connack=5)).open()
        self.assertEqual(error.exception.kind, "auth")

    def test_silence_times_out_per_request(self):
        broker = FakeBroker(SESSION)
        broker.sendall = lambda data: None
        with md.Mqtt("broker", SESSION, timeout=0.2, connect=lambda: broker) as client:
            answers = client.request([("dev1", "GET", "Appliance.System.All", {})])
        self.assertIsInstance(answers[0], md.MerossError)


class CloudTests(unittest.TestCase):
    def test_sign_in_follows_redirect_then_mfa(self):
        replies = [{"apiStatus": 1030, "sysStatus": 0, "data": json.dumps({"domain": "https://iotx-ap.meross.com"})},
                   {"apiStatus": 1033, "sysStatus": 0, "data": None},
                   {"apiStatus": 0, "sysStatus": 0, "data": {"token": "t", "key": "k", "userid": 7}}]
        seen = []

        def opener(request, timeout):
            form = urllib.parse.parse_qs(request.data.decode())
            params = json.loads(base64.b64decode(form["params"][0]))
            seen.append((request.full_url, params.get("mfaCode"), params["password"], request.get_header("Authorization")))
            return io.BytesIO(json.dumps(replies.pop(0)).encode())
        with patch("time.sleep"):
            base, data = md.sign_in("me@example.com", "secret", ask_mfa=lambda: "123456", opener=opener)
        self.assertEqual(base, "https://iotx-ap.meross.com")
        self.assertEqual(data["token"], "t")
        self.assertEqual([s[0] for s in seen], ["https://iotx.meross.com/v1/Auth/signIn"]
                         + ["https://iotx-ap.meross.com/v1/Auth/signIn"] * 2)
        self.assertEqual([s[1] for s in seen], [None, None, "123456"])
        self.assertTrue(all(s[2] == md.md5("secret") and s[3] is None for s in seen))

    def test_expired_token_is_an_auth_error(self):
        with self.assertRaises(md.MerossError) as error:
            md.cloud_data({"apiStatus": 1019, "sysStatus": 0, "info": "Token expired"})
        self.assertEqual(error.exception.kind, "auth")


class CliTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": home.name + "/c", "XDG_CACHE_HOME": home.name + "/k"})
        env.start()
        self.addCleanup(env.stop)

    def run_cli(self, *argv):
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = md.main(list(argv))
        return code, json.loads(out.getvalue())

    def test_state_without_session_reports_auth(self):
        code, result = self.run_cli("state")
        self.assertEqual((code, result["ok"], result["kind"]), (0, False, "auth"))

    def test_every_command_is_logged_once(self):
        md.save_session(SESSION)
        cache = listed_cache()
        md.save_cache(cache)
        lamp = uuid_of("msl610")
        request = base64.b64encode(json.dumps({"uuid": lamp, "kind": "switch", "channel": 0, "value": True}).encode()).decode()
        real_hub = md.Hub
        with patch.object(md, "Hub", lambda session, cache, **kw: real_hub(
                session, cache, lan=False, mqtt_factory=lambda h, s: FakeMqtt(h, s, {lamp: {}}))):
            code, result = self.run_cli("set", request)
        self.assertTrue(result["ok"])
        unknown = base64.b64encode(json.dumps({"uuid": "nope", "kind": "door", "value": True}).encode()).decode()
        code, result = self.run_cli("set", unknown)
        self.assertEqual((result["ok"], result["kind"]), (False, "usage"))
        lines = Path(md.cache_dir(), md.COMMAND_LOG).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("Smart Floor Lamp", lines[0])
        self.assertIn('ok via cloud: Appliance.Control.ToggleX {"togglex":{"channel":0,"onoff":1}}', lines[0])
        self.assertIn("FAILED", lines[1])
        self.assertIn("usage", lines[1])

    def test_session_file_is_private(self):
        md.save_session(SESSION)
        mode = os.stat(os.path.join(md.config_dir(), md.SESSION_FILE)).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        code, result = self.run_cli("cached")
        self.assertEqual((code, result["account"]), (0, "me@example.com"))


if __name__ == "__main__":
    unittest.main()
