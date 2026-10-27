local model = assert(loadfile("meross-deck/model.luau"))()
local oldRequire = require
require = function(path) if path == "./model.luau" then return model end return oldRequire(path) end
local now, watchers, shared, calls, encoded = 100, {}, {}, {}, nil
os.time = function() return now end
local config = {interval = 10, cloud_interval = 60, transport = "auto"}
local garage = {uuid = "garage", kind = "garage", name = "Garage", online = true, stale = false, route = "lan", channels = {{channel = 0, open = false, openMs = 12000, closeMs = 14000}}}
local light = {uuid = "lamp", kind = "light", name = "Lamp", online = true, stale = false, route = "lan", channels = {{channel = 0, on = false, luminance = 50, temperature = 40}}}
local result = {ok = true, devices = {garage, light}, account = "test"}
local responses = {poll = result, ack = {ok = true}, failure = {ok = false, kind = "network", error = "Not reachable"}, auth = {ok = false, kind = "auth", error = "Sign in"}}
noctalia = {getConfig = function(k) return config[k] end, pluginDir = function() return "/plugin" end,
  setUpdateInterval = function(n) assert(n == 1000) end,
  state = {set = function(k, v) shared[k] = v end, get = function(k) return shared[k] end, watch = function(k, f) watchers[k] = f end},
  json = {encode = function(v) encoded = v; return "encoded" end, decode = function(v) return responses[v] end},
  runAsync = function(argv, cb) table.insert(calls, {argv = argv, cb = cb}); return true end}
assert(loadfile("meross-deck/service.luau"))()
update(); assert(#calls == 1 and calls[1].argv[3] == "state" and calls[1].argv[4] == "--cloud-max-age")
watchers.command({uuid = "garage", channel = 0, kind = "door", value = true, expires = 106, serial = 1})
assert(#calls == 1) -- no command before initial successful poll
calls[1].cb({exitCode = 0, stdout = "poll"})
assert(shared.status.online and shared.status.route == "lan")
watchers.command({uuid = "garage", channel = 0, kind = "door", value = true, expires = 106, serial = 2})
assert(#calls == 2 and calls[2].argv[3] == "set-json" and encoded.kind == "door")
watchers.command({uuid = "garage", channel = 0, kind = "door", value = true, expires = 106, serial = 3})
assert(#calls == 2 and shared.status.doorPending["garage:0"]) -- never queue a duplicate while in flight
calls[2].cb({exitCode = 0, stdout = "failure"})
assert(shared.status.error:find("check the door", 1, true) and not shared.status.doorPending["garage:0"])
assert(not next(shared.status.motion))
watchers.command({uuid = "garage", channel = 0, kind = "door", value = true, expires = 106, serial = 4})
assert(#calls == 3)
calls[3].cb({exitCode = 0, stdout = "ack"})
assert(shared.status.motion["garage:0"].untilTime == 112)
watchers.command({uuid = "garage", channel = 0, kind = "door", value = true, expires = 106, serial = 5})
assert(#calls == 3) -- no second move while the first is travelling
now = 112; update(); assert(#calls == 4 and calls[4].argv[3] == "state")
calls[4].cb({exitCode = 0, stdout = "poll"})
-- A door confirmation must not survive a long poll.
now = 122; update(); assert(#calls == 5)
watchers.command({uuid = "garage", channel = 0, kind = "door", value = true, expires = 128, serial = 6})
now = 129; calls[5].cb({exitCode = 0, stdout = "poll"})
assert(#calls == 5 and shared.status.error:find("expired", 1, true) and not shared.status.doorPending["garage:0"])
-- Light changes are optimistically displayed and reverted on a failed command.
watchers.command({uuid = "lamp", channel = 0, kind = "switch", value = true, serial = 7})
assert(#calls == 6 and shared.status.devices[2].channels[1].on)
calls[6].cb({exitCode = 0, stdout = "failure"})
assert(not shared.status.devices[2].channels[1].on)
-- Invalid levels, stale devices and an unknown door state never send a command.
watchers.command({uuid = "lamp", channel = 0, kind = "luminance", value = 101, serial = 8})
assert(#calls == 6)
local unknown = model.copy(result.devices); unknown[1].channels[1].open = nil
assert(not model.validCommand(unknown, {uuid = "garage", channel = 0, kind = "door", value = true}))
now = 140; update(); assert(#calls == 7)
calls[7].cb({exitCode = 0, stdout = "auth"})
assert(shared.status.signedOut and not shared.status.online)
now = 200; update(); assert(#calls == 7) -- signed-out services stop polling
watchers.refresh({serial = 9}); assert(#calls == 8)
print("service and garage safety: ok")
