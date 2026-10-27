local M = {}
function M.copy(value)
  if type(value) ~= "table" then return value end
  local out = {}; for k, v in pairs(value) do out[k] = M.copy(v) end; return out
end
function M.key(uuid, channel) return tostring(uuid) .. ":" .. tostring(channel) end
function M.channel(devices, uuid, channel)
  for _, d in ipairs(devices or {}) do
    if d.uuid == uuid then
      for _, c in ipairs(d.channels or {}) do if c.channel == channel then return d, c end end
    end
  end
  return nil, nil
end
function M.validCommand(devices, command)
  if type(command) ~= "table" or type(command.uuid) ~= "string" or type(command.channel) ~= "number" or command.channel % 1 ~= 0 then return false end
  local d, ch = M.channel(devices, command.uuid, command.channel)
  if not d or not ch or not d.online or d.stale then return false end
  if command.kind == "door" then return d.kind == "garage" and type(ch.open) == "boolean" and type(command.value) == "boolean" and ch.open ~= command.value end
  if command.kind == "switch" then return d.kind ~= "garage" and type(ch.on) == "boolean" and type(command.value) == "boolean" end
  if command.kind == "luminance" or command.kind == "temperature" then
    return d.kind == "light" and type(ch[command.kind]) == "number" and type(command.value) == "number" and command.value % 1 == 0 and command.value >= 1 and command.value <= 100
  end
  return false
end
function M.route(devices)
  local routes = {}
  for _, d in ipairs(devices or {}) do if d.online and not d.stale and d.route and d.route ~= "" then routes[d.route] = true end end
  if routes.lan and routes.cloud then return "mixed" end
  return routes.lan and "lan" or routes.cloud and "cloud" or ""
end
function M.hiddenMap(hidden)
  local out = {}; for _, uuid in ipairs(hidden or {}) do out[uuid] = true end; return out
end
function M.summary(devices, hidden)
  local open, on, live = 0, 0, 0
  local excluded = M.hiddenMap(hidden)
  for _, d in ipairs(devices or {}) do
    if not excluded[d.uuid] and d.online and not d.stale then
      for _, c in ipairs(d.channels or {}) do
        live = live + 1
        if d.kind == "garage" and c.open then open = open + 1
        elseif d.kind ~= "garage" and c.on then on = on + 1 end
      end
    end
  end
  return {open = open, on = on, live = live}
end
return M
