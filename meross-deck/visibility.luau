local M = {}
function M.load()
  local shared = noctalia.state.get("hidden")
  if type(shared) == "table" then return shared end
  local dir = noctalia.pluginDataDir()
  if not dir then return {} end
  local raw = noctalia.readFile(dir .. "/visibility.json")
  local value = raw and noctalia.json.decode(raw) or nil
  return type(value) == "table" and value or {}
end
function M.save(hidden)
  local dir = noctalia.pluginDataDir()
  if not dir then return false end
  local data = noctalia.json.encode(hidden)
  if not data then return false end
  local ok = noctalia.writeFile(dir .. "/visibility.json", data)
  if ok then noctalia.state.set("hidden", hidden) end
  return ok == true
end
return M
