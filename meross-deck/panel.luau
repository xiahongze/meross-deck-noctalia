local model = require("./model.luau")
local visibility = require("./visibility.luau")
local status, hidden, page, serial = nil, visibility.load(), "home", 0
local confirmation, levels, uiError = nil, {}, ""
local render
local function label(text, size, color)
  return ui.label({text = tostring(text or ""), fontSize = size or 13, color = color or "on_surface"})
end
local function row(children) return ui.row({gap = 8, align = "center"}, children) end
local function card(children) return ui.column({gap = 8, padding = 14, radius = 16, fill = "on_surface/0.06"}, children) end
local function button(text, action, selected, enabled)
  return ui.button({text = text, variant = selected and "primary" or "secondary", controlSize = "sm", enabled = enabled ~= false, onClick = action})
end
local function send(uuid, channel, kind, value, expires)
  if not status or not status.online then return end
  serial = serial + 1
  noctalia.state.set("command", {uuid = uuid, channel = channel, kind = kind, value = value, expires = expires, serial = serial})
end
local function refresh()
  serial = serial + 1; noctalia.state.set("refresh", {serial = serial, time = os.time()})
end
local function shellQuote(s) return "\x27" .. tostring(s):gsub("\x27", "\x27\\\x27\x27") .. "\x27" end
local function signIn()
  local dir = noctalia.pluginDir()
  if not dir then uiError = "Plugin directory unavailable"; render(); return end
  if not noctalia.runInTerminal("python3 " .. shellQuote(dir .. "/bridge.py") .. " login") then
    uiError = "Could not open a terminal. See the README for manual sign-in."; render()
  end
end
local function canUse(device, channel)
  return status and status.online and device and device.online and not device.stale and channel ~= nil
end
local function doorCard(device, channel)
  local key = channel and model.key(device.uuid, channel.channel) or ""
  local enabled = canUse(device, channel) and type(channel.open) == "boolean" and not ((status.motion or {})[key]) and not ((status.doorPending or {})[key])
  local moving = channel and (status.motion or {})[key]
  local description = (not channel or type(channel.open) ~= "boolean") and "No door state available" or device.stale and "Unavailable · last known state" or moving and ((moving.open and "Opening" or "Closing") .. " · " .. tostring(math.max(0, moving.untilTime - os.time())) .. "s") or channel.open and "Open" or "Closed"
  local children = {row({ui.glyph({name = "door", size = 22, color = channel and channel.open and "error" or "primary"}), label(channel and channel.name or device.name, 15), ui.spacer({flexGrow = 1}), label(description, 12, channel and channel.open and "error" or "on_surface/0.65")})}
  if channel and channel.obstruction then table.insert(children, label("Obstruction reported", 12, "error")) end
  if confirmation and confirmation.key == key and confirmation.untilTime > os.time() and enabled then
    local requested = confirmation.value
    table.insert(children, row({label(requested and "Open this door?" or "Close this door?", 13), ui.spacer({flexGrow = 1}),
      button("Cancel", function() confirmation = nil; render() end),
      button(requested and "Open door" or "Close door", function()
        local pending = confirmation
        local currentDevice, currentChannel = model.channel(status and status.devices, device.uuid, channel.channel)
        confirmation = nil
        if pending and pending.key == key and pending.untilTime > os.time() and canUse(currentDevice, currentChannel)
          and currentChannel.open ~= pending.value and not (status.motion or {})[key] and not (status.doorPending or {})[key] then
          send(device.uuid, channel.channel, "door", pending.value, os.time() + 6)
        end
        render()
      end, true, enabled)}))
  else
    table.insert(children, row({ui.spacer({flexGrow = 1}), button(channel and channel.open and "Close" or "Open", function()
      if not enabled then return end
      confirmation = {key = key, value = not channel.open, untilTime = os.time() + 6}; render()
    end, false, enabled)}))
  end
  return card(children)
end
local function levelControl(device, channel, field, text)
  local id = model.key(device.uuid, channel.channel) .. ":" .. field
  local value = levels[id] and levels[id].value or tonumber(channel[field]) or 1
  return ui.column({gap = 4}, {row({label(text, 12, "on_surface/0.65"), ui.spacer({flexGrow = 1}), label(tostring(value) .. (field == "temperature" and " / 100" or "%"), 12)}),
    ui.slider({key = "level-" .. id, min = 1, max = 100, step = 1, value = value, enabled = canUse(device, channel),
      onChange = function(v)
        local n = math.max(1, math.min(100, math.floor((tonumber(v) or value) + 0.5)))
        levels[id] = {uuid = device.uuid, channel = channel.channel, field = field, value = n, due = os.time() + 1}
      end,
      onDragEnd = function(v)
        local item = levels[id]
        if item then
          local n = tonumber(v); if n then item.value = math.max(1, math.min(100, math.floor(n + 0.5))) end
          levels[id] = nil; send(item.uuid, item.channel, item.field, item.value)
        end
      end})})
end
local function deviceCard(device, channel)
  local enabled = canUse(device, channel)
  local items = {row({ui.glyph({name = device.kind == "light" and "bulb" or "plug", size = 20, color = "primary"}),
    label(channel.name or device.name, 15), ui.spacer({flexGrow = 1}), ui.toggle({checked = channel.on == true, enabled = enabled,
      onChange = function(v) send(device.uuid, channel.channel, "switch", v == "true") end})})}
  if device.kind == "light" and channel.on then
    if type(channel.luminance) == "number" then table.insert(items, levelControl(device, channel, "luminance", "Brightness")) end
    if type(channel.temperature) == "number" then table.insert(items, levelControl(device, channel, "temperature", "Colour temperature")) end
  end
  return card(items)
end
local function deviceSettings()
  local excluded = model.hiddenMap(hidden)
  local nodes = {label("Visible devices", 18), label("Choose which devices appear in Meross Deck.", 12, "on_surface/0.65")}
  for _, device in ipairs(status and status.devices or {}) do
    local d = device
    table.insert(nodes, card({row({label(d.name, 14), ui.spacer({flexGrow = 1}), ui.toggle({checked = not excluded[d.uuid],
      onChange = function(v)
        local nextHidden = {}
        for _, id in ipairs(hidden) do if id ~= d.uuid then table.insert(nextHidden, id) end end
        if v ~= "true" then table.insert(nextHidden, d.uuid) end
        if visibility.save(nextHidden) then hidden = nextHidden; uiError = "" else uiError = "Could not save device visibility" end
        render()
      end})})}))
  end
  if status and #status.devices == 0 then table.insert(nodes, label("No devices found yet.", 13, "on_surface/0.65")) end
  return nodes
end
local function home()
  local nodes, offline, doors, others = {}, {}, {}, {}
  local excluded = model.hiddenMap(hidden)
  for _, device in ipairs(status and status.devices or {}) do
    if not excluded[device.uuid] then
      if device.kind == "garage" then
        if #(device.channels or {}) == 0 then table.insert(doors, doorCard(device, nil))
        else for _, channel in ipairs(device.channels) do table.insert(doors, doorCard(device, channel)) end end
      elseif not device.online or device.stale or #(device.channels or {}) == 0 then
        table.insert(offline, device.name or "Device")
      else
        for _, channel in ipairs(device.channels) do table.insert(others, deviceCard(device, channel)) end
      end
    end
  end
  if #doors > 0 then table.insert(nodes, label("GARAGE", 11, "on_surface/0.65")); for _, item in ipairs(doors) do table.insert(nodes, item) end end
  if #others > 0 then table.insert(nodes, label("LIGHTS & PLUGS", 11, "on_surface/0.65")); for _, item in ipairs(others) do table.insert(nodes, item) end end
  if #offline > 0 then table.insert(nodes, label("Offline: " .. table.concat(offline, ", "), 12, "on_surface/0.65")) end
  if #doors == 0 and #others == 0 and #offline == 0 and status and status.online then table.insert(nodes, label("No visible devices.", 14, "on_surface/0.65")) end
  return nodes
end
render = function()
  local s = status or {devices = {}, online = false, error = "Connecting…"}
  local text = s.signedOut and "SIGN IN" or not s.online and "OFFLINE" or s.stale and "RETRYING" or s.route == "lan" and "LOCAL" or s.route == "cloud" and "CLOUD" or "LIVE"
  local nodes = page == "devices" and deviceSettings() or home()
  if s.signedOut then
    table.insert(nodes, 1, card({label("Sign in to your Meross account in a terminal.", 13), row({button("Sign in…", signIn), button("Refresh", refresh)})}))
  elseif not s.updated or s.updated == 0 then
    table.insert(nodes, 1, label("Connecting to Meross…", 14, "on_surface/0.65"))
  end
  if (s.error or "") ~= "" then table.insert(nodes, 1, label(s.error, 12, "error")) end
  if uiError ~= "" then table.insert(nodes, 1, label(uiError, 12, "error")) end
  panel.render(ui.column({gap = 12, padding = 16, flexGrow = 1}, {
    row({ui.glyph({name = "home", size = 20, color = "primary"}), label(noctalia.getConfig("title") or "Home", 16), ui.spacer({flexGrow = 1}), label(text, 11, "primary")}),
    row({button("Home", function() page = "home"; confirmation = nil; render() end, page == "home"), button("Devices", function() page = "devices"; confirmation = nil; render() end, page == "devices"), ui.spacer({flexGrow = 1}), button("Refresh", refresh)}),
    ui.scroll({gap = 10, flexGrow = 1}, nodes),
    label(s.updated and s.updated > 0 and ("Updated " .. tostring(math.max(0, os.time() - s.updated)) .. "s ago") or "", 11, "on_surface/0.65")
  }))
end
noctalia.state.watch("status", function(s) status = s; render() end)
noctalia.state.watch("hidden", function(h) hidden = h or {}; render() end)
function onOpen() status = noctalia.state.get("status"); hidden = visibility.load(); noctalia.setUpdateInterval(1000); render() end
function onClose() confirmation = nil; levels = {} end
function update()
  local needsRender = false
  if confirmation and os.time() >= confirmation.untilTime then confirmation = nil; needsRender = true end
  for id, item in pairs(levels) do
    if os.time() >= item.due then levels[id] = nil; send(item.uuid, item.channel, item.field, item.value); needsRender = true end
  end
  if confirmation or (status and next(status.motion or {})) then needsRender = true end
  if needsRender then render() end
end
