local model = assert(loadfile("meross-deck/model.luau"))()
local visibility = {load = function() return {} end, save = function(h) _G.savedHidden = h; noctalia.state.set("hidden", h); return true end}
local oldRequire = require
require = function(path)
  if path == "./model.luau" then return model end
  if path == "./visibility.luau" then return visibility end
  return oldRequire(path)
end
local now, watchers, shared, tree = 100, {}, {}, nil
os.time = function() return now end
shared.status = {online = true, updated = 100, route = "lan", error = "", motion = {}, doorPending = {}, devices = {
  {uuid = "garage", kind = "garage", name = "Garage", online = true, stale = false, channels = {{channel = 0, name = "Garage", open = false}}},
  {uuid = "lamp", kind = "light", name = "Lamp", online = true, stale = false, channels = {{channel = 0, name = "Lamp", on = true, luminance = 50, temperature = 40}}},
  {uuid = "offline", kind = "plug", name = "Offline plug", online = false, stale = true, channels = {}}
}}
noctalia = {getConfig = function(k) if k == "title" then return "Home" end end, setUpdateInterval = function() end,
 state = {get = function(k) return shared[k] end, set = function(k, v) shared[k] = v; if watchers[k] then watchers[k](v) end end, watch = function(k, f) watchers[k] = f end},
 pluginDir = function() return "/plugin" end, runInTerminal = function() return true end}
ui = setmetatable({}, {__index = function(_, kind) return function(props, children) return {kind = kind, props = props or {}, children = children or {}} end end})
panel = {render = function(t) tree = t end}
assert(loadfile("meross-deck/panel.luau"))(); onOpen()
local function find(node, pred)
 if pred(node) then return node end
 for _, c in ipairs(node.children) do local result = find(c, pred); if result then return result end end
end
local function named(text) return assert(find(tree, function(n) return n.props.text == text end), "missing " .. text) end
assert(named("Garage") and named("Offline: Offline plug"))
-- Selected and idle buttons must differ by variant: the theme's secondary colour matches primary.
local homeTab = assert(find(tree, function(n) return n.kind == "button" and n.props.text == "Home" end))
assert(homeTab.props.variant == "primary" and homeTab.props.selected == true)
assert(named("Devices").props.variant == "outline" and named("Devices").props.selected == false)
-- The door action sits in the header row, keeping the card compact.
local doorRow = find(tree, function(n) return n.kind == "row" and #n.children > 0 and n.children[#n.children].props.text == "Open" end)
assert(doorRow and doorRow.children[2].props.text == "Garage")
named("Open").props.onClick(); assert(not shared.command and named("Open door"))
named("Cancel").props.onClick(); assert(not shared.command and named("Open"))
named("Open").props.onClick(); local delayed = named("Open door").props.onClick
now = 106; delayed(); assert(not shared.command) -- confirmation expires, even before the next tick
now = 110; update(); named("Open").props.onClick(); named("Open door").props.onClick()
assert(shared.command.kind == "door" and shared.command.uuid == "garage" and shared.command.value == true and shared.command.expires == 116)
shared.command = nil
named("Open").props.onClick(); local staleClick = named("Open door").props.onClick
shared.status.devices[1].stale = true; watchers.status(shared.status)
staleClick(); assert(not shared.command) -- no command if the device becomes unavailable
shared.status.devices[1].stale = false; watchers.status(shared.status)
local lightToggle = assert(find(tree, function(n) return n.kind == "toggle" end))
lightToggle.props.onChange("false")
assert(shared.command.kind == "switch" and shared.command.uuid == "lamp" and shared.command.value == false)
shared.command = nil
local slider = assert(find(tree, function(n) return n.kind == "slider" end))
slider.props.onChange("72"); assert(not shared.command)
slider.props.onDragEnd("72"); assert(shared.command.kind == "luminance" and shared.command.value == 72)
shared.command = nil
slider.props.onChange("78"); now = now + 1; update(); assert(shared.command.value == 78)
named("Devices").props.onClick()
local function parentWithText(node, text)
 for _, c in ipairs(node.children) do if c.props.text == text then return node end; local p = parentWithText(c,text); if p then return p end end
end
local toggle = find(parentWithText(tree, "Garage"), function(n) return n.kind == "toggle" end)
toggle.props.onChange("false")
assert(savedHidden[1] == "garage" and shared.hidden[1] == "garage")
assert(find(tree, function(n) return n.kind == "button" and n.props.text == "Home" end)).props.onClick(); assert(not find(tree, function(n) return n.props.text == "Garage" end))
print("panel interaction and confirmation: ok")
