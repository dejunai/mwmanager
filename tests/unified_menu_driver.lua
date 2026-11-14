-- Run by test_unified_menu.py: luajit unified_menu_driver.lua <path to mwmanagermenu\main.lua>
-- Stands in for MWSE: three mods register a settings page (two suite mods, one other), then the menu is used.
local log = {}
local function note(...) log[#log + 1] = table.concat({ ... }, " ") end

local registered = {}
mwse = {
    registerModConfig = function(name, package) registered[name] = package note("registered", name) end,
    log = function() end,
}
local handlers = {}
event = { register = function(name, fn, options) handlers[#handlers + 1] = { name, fn, options and options.priority or 0 } end }
function table.size(t) local n = 0 for _ in pairs(t) do n = n + 1 end return n end

-- a UI element that records what is built in it
local function element(kind, text)
    local e = { kind = kind, text = text, children = {}, widget = {} }
    function e:createBlock() local c = element("block") self.children[#self.children + 1] = c return c end
    function e:createButton(p) local c = element("button", p.text) self.children[#self.children + 1] = c return c end
    function e:destroyChildren() self.children = {} end
    function e:register(_, fn) self.click = fn end
    function e:getTopLevelMenu() return { updateLayout = function() note("layout") end } end
    return e
end

local menu = assert(loadfile(arg[1]))()

-- each mod's own code, as MWSE would run it from that mod's folder
local function modRegisters(folder, name)
    local chunk = assert(loadstring(
        "local name, note = ... " ..
        "mwse.registerModConfig(name, { " ..
        "onCreate = function(c) note('create', name) c:createBlock() end, " ..
        "onClose = function() note('close', name) end, " ..
        "onSearch = function(t) return t == name end })",
        "@Data Files\\MWSE\\mods\\" .. folder .. "\\main.lua"))
    chunk(name, note)
end
modRegisters("mwcollide", "Doubled Objects")
modRegisters("SomeoneElse", "Other Mod")
modRegisters("mwsaveguard", "mwsaveguard")

table.sort(handlers, function(a, b) return a[3] > b[3] end)
for _, h in ipairs(handlers) do if h[1] == "modConfigReady" then h[2]() end end

local entry = registered["MWManager"]
local container = element("container")
entry.onCreate(container)
local tabs = {}
for _, row in ipairs(container.children) do
    for _, c in ipairs(row.children) do
        if c.kind == "button" then tabs[#tabs + 1] = c end
    end
end
note("tabs", #tabs, tabs[1] and tabs[1].text or "-", tabs[2] and tabs[2].text or "-")
note("open tab state", tostring(tabs[1].widget.state), tostring(tabs[2].widget.state))
tabs[2].click()                                  -- switch to the second tab
note("search", tostring(entry.onSearch("Doubled Objects")), tostring(entry.onSearch("nothing")))
entry.onClose(container)
for _, line in ipairs(log) do print(line) end
