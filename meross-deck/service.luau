local model = require("./model.luau")
local devices, account, lastUpdated, failures = {}, "", 0, 0
local errorKind, lastError, busy, nextPoll, generation = "", "", false, 0, 0
local interval, cloudInterval, transport = 10, 60, "auto"
local queue, pending, motion, settled, doorPending = {}, {}, {}, {}, {}
local forceNext = false

local function shown()
  local out = model.copy(devices)
  for _, d in ipairs(out) do
    for _, c in ipairs(d.channels or {}) do
      local edit = pending[model.key(d.uuid, c.channel)]
      if edit and edit.expires > os.time() then for field, value in pairs(edit.values) do c[field] = value end end
    end
  end
  return out
end
local function publish()
  noctalia.state.set("status", {devices = shown(), account = account, updated = lastUpdated,
    online = lastUpdated > 0 and failures < 3 and errorKind ~= "auth" and errorKind ~= "setup",
    signedOut = errorKind == "auth" or errorKind == "setup", stale = failures > 0,
    error = lastError, route = model.route(devices), motion = model.copy(motion), doorPending = model.copy(doorPending), busy = busy})
end
local function configure()
  generation = generation + 1
  interval = math.max(5, tonumber(noctalia.getConfig("interval")) or 10)
  cloudInterval = math.max(30, tonumber(noctalia.getConfig("cloud_interval")) or 60)
  transport = noctalia.getConfig("transport") or "auto"
  devices, account, lastUpdated, failures, errorKind, lastError = {}, "", 0, 0, "", ""
  busy, queue, pending, motion, settled, doorPending, nextPoll, forceNext = false, {}, {}, {}, {}, {}, 0, false
  publish()
end
local function flags(args)
  if transport == "lan" then table.insert(args, "--lan-only")
  elseif transport == "cloud" then table.insert(args, "--cloud-only") end
  return args
end
local function run(args, done)
  local dir = noctalia.pluginDir()
  if not dir or dir == "" then done(false, {kind = "setup", error = "Plugin directory unavailable"}); return end
  local argv = {"python3", dir .. "/bridge.py"}
  for _, arg in ipairs(args) do table.insert(argv, tostring(arg)) end
  busy = true
  local g = generation
  local accepted = noctalia.runAsync(argv, function(result)
    if g ~= generation then return end
    busy = false
    local obj = not result.stdoutTruncated and noctalia.json.decode(result.stdout or "") or nil
    if result.timedOut then done(false, {kind = "network", error = "The request timed out; refresh before retrying"})
    elseif result.exitCode ~= 0 or type(obj) ~= "table" or obj.ok ~= true then
      done(false, {kind = type(obj) == "table" and obj.kind or "network", error = type(obj) == "table" and obj.error or "Meross helper failed"})
    else done(true, obj) end
  end)
  if not accepted then busy = false; done(false, {kind = "setup", error = "Could not start the Meross helper"}) end
end
local function reconcile()
  for key, edit in pairs(pending) do
    local _, ch = model.channel(devices, edit.uuid, edit.channel)
    local matches = ch ~= nil
    if matches then for field, value in pairs(edit.values) do if ch[field] ~= value then matches = false end end end
    if matches or edit.expires <= os.time() then pending[key] = nil end
  end
end
local function noticeDoors(before, after)
  if #before == 0 then return end
  for _, d in ipairs(after) do
    if d.kind == "garage" and not d.stale then
      for _, c in ipairs(d.channels or {}) do
        local key = model.key(d.uuid, c.channel)
        local _, old = model.channel(before, d.uuid, c.channel)
        if old and not old.open and c.open and not motion[key] and (not settled[key] or os.time() - settled[key] > 60) then
          motion[key] = {open = true, untilTime = os.time() + math.max(1, math.ceil((tonumber(c.openMs) or 15000) / 1000))}
        end
      end
    end
  end
end
local pump
local function poll(fresh)
  if busy then forceNext = forceNext or fresh; return end
  local args = flags({"state"})
  if not fresh then table.insert(args, "--cloud-max-age"); table.insert(args, tostring(math.max(0, cloudInterval - 2))) end
  run(args, function(ok, result)
    if ok and type(result.devices) == "table" then
      local before = devices
      devices = result.devices; account = result.account or ""; lastUpdated = os.time(); failures = 0; lastError = ""; errorKind = ""
      noticeDoors(before, devices); reconcile()
      nextPoll = os.time() + ((transport == "cloud" or model.route(devices) == "cloud") and cloudInterval or interval)
    else
      failures = failures + 1; errorKind = result.kind or "network"; lastError = result.error or "Meross refresh failed"
      nextPoll = os.time() + interval
    end
    publish(); pump()
  end)
end
pump = function()
  if busy then return end
  local cmd = table.remove(queue, 1)
  if not cmd then
    if forceNext then forceNext = false; poll(true) end
    return
  end
  local key = model.key(cmd.uuid, cmd.channel)
  if not model.validCommand(devices, cmd) or (cmd.kind == "door" and os.time() > cmd.expires) then
    if cmd.kind == "door" then doorPending[key] = nil else pending[key] = nil end
    lastError = cmd.kind == "door" and "Door request expired or its state changed. Check it and try again." or "Device is no longer available. Refresh and try again."
    publish(); pump(); return
  end
  local payload = noctalia.json.encode({uuid = cmd.uuid, kind = cmd.kind, channel = cmd.channel, value = cmd.value})
  if not payload then lastError = "Could not encode the command"; pending[key] = nil; doorPending[key] = nil; publish(); pump(); return end
  run(flags({"set-json", payload}), function(ok, result)
    if cmd.kind == "door" then doorPending[key] = nil end
    if not ok then
      if pending[key] and pending[key].serial == cmd.serial then pending[key] = nil end
      lastError = (result.error or "Command failed") .. (cmd.kind == "door" and "; check the door before retrying" or "")
    elseif cmd.kind == "door" then
      local _, channel = model.channel(devices, cmd.uuid, cmd.channel)
      local duration = channel and (cmd.value and channel.openMs or channel.closeMs) or 15000
      motion[key] = {open = cmd.value, untilTime = os.time() + math.max(1, math.ceil((tonumber(duration) or 15000) / 1000))}
      lastError = ""
    else lastError = "" end
    nextPoll = os.time() + 2
    publish(); pump()
  end)
end
function update()
  local expired = false
  for key, value in pairs(motion) do
    if os.time() >= value.untilTime then motion[key] = nil; settled[key] = os.time(); expired = true end
  end
  if expired then forceNext = true; publish() end
  if busy then return end
  if #queue > 0 then pump(); return end
  if forceNext then forceNext = false; poll(true); return end
  if errorKind ~= "auth" and errorKind ~= "setup" and os.time() >= nextPoll then poll(false) end
end
function onConfigChanged() configure() end
noctalia.state.watch("refresh", function()
  errorKind = ""; lastError = ""; forceNext = true; pump()
end)
noctalia.state.watch("command", function(cmd)
  if not cmd or lastUpdated == 0 or failures >= 3 or errorKind == "auth" or errorKind == "setup" or not model.validCommand(devices, cmd) then return end
  if cmd.kind == "door" then
    local key = model.key(cmd.uuid, cmd.channel)
    if motion[key] or doorPending[key] or type(cmd.expires) ~= "number" or os.time() > cmd.expires or cmd.expires > os.time() + 6 then return end
    doorPending[key] = true
  else
    local values = cmd.kind == "switch" and {on = cmd.value} or {[cmd.kind] = cmd.value, on = true}
    pending[model.key(cmd.uuid, cmd.channel)] = {uuid = cmd.uuid, channel = cmd.channel, values = values, expires = os.time() + 15, serial = cmd.serial}
  end
  table.insert(queue, model.copy(cmd)); publish(); pump()
end)
configure(); noctalia.setUpdateInterval(1000)
