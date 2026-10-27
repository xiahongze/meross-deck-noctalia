local model = require("./model.luau")
local visibility = require("./visibility.luau")
local hidden = visibility.load()
local function render(s)
  local counts = model.summary(s and s.devices, hidden)
  local state = s and s.signedOut and "SIGN IN" or s and s.online and "LIVE" or "OFFLINE"
  desktopWidget.render(ui.column({padding = 16, gap = 8, fill = "surface", radius = 18}, {
    ui.row({align = "center", gap = 8}, {ui.glyph({name = "home", color = "primary", size = 22}), ui.label({text = "MEROSS DECK", fontWeight = "bold", fontSize = 13}), ui.spacer({flexGrow = 1}), ui.label({text = state, color = "primary", fontSize = 11})}),
    ui.label({text = tostring(counts.open) .. " doors open  ·  " .. tostring(counts.on) .. " devices on", fontSize = 16}),
    ui.button({text = "Open controls", variant = "secondary", onClick = "openControls"})
  }))
end
noctalia.state.watch("status", render)
noctalia.state.watch("hidden", function(value) hidden = value or {}; render(noctalia.state.get("status")) end)
function update() render(noctalia.state.get("status")) end
function openControls() noctalia.togglePanel("xiahongze/meross-deck:panel") end
