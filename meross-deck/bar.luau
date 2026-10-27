local model = require("./model.luau")
local visibility = require("./visibility.luau")
local hidden = visibility.load()
local function render(s)
  local summary = model.summary(s and s.devices, hidden)
  barWidget.setGlyph("home")
  barWidget.setText(s and s.signedOut and "Sign in" or summary.open > 0 and (tostring(summary.open) .. " open") or "Home")
  barWidget.setTooltip(s and s.error ~= "" and s.error or "Meross Deck")
end
noctalia.state.watch("status", render)
noctalia.state.watch("hidden", function(value) hidden = value or {}; render(noctalia.state.get("status")) end)
function update() render(noctalia.state.get("status")) end
function onClick() noctalia.togglePanel("xiahongze/meross-deck:panel") end
