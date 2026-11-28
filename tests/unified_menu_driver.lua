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
-- the bits of the game's UI tables the menu reads (tab state, heading colour, label fonts)
tes3 = { uiState = { active = 4, normal = 1 }, palette = { headerColor = "headerColor" } }
tes3ui = {
    createTooltipMenu = function()
        return {
            createLabel = function(_, p) note("tooltip", p.text) return {} end,
            updateLayout = function() end,
        }
    end,
    getPalette = function(name) return { name } end,
    registerID = function(name) return name end,
}
function table.size(t) local n = 0 for _ in pairs(t) do n = n + 1 end return n end

-- a UI element that records what is built in it
local function element(kind, text)
    local e = { kind = kind, text = text, children = {}, widget = {} }
    function e:createBlock() local c = element("block") self.children[#self.children + 1] = c return c end
    function e:createLabel(p) local c = element("label", p.text) self.children[#self.children + 1] = c return c end
    function e:createButton(p) local c = element("button", p.text) self.children[#self.children + 1] = c return c end
    function e:destroyChildren() self.children = {} end
    function e:register(name, fn) self[name] = fn if name == "mouseClick" then self.click = fn end end
    function e:getTopLevelMenu() return { updateLayout = function() note("layout") end } end
    return e
end

local menu = assert(loadfile(arg[1]))()

-- each mod's own code, as MWSE would run it from that mod's folder
local saves = {}
local function modRegisters(folder, name, nested)
    local chunk = assert(loadstring(
        "local name, note, saves = ... " ..
        "mwse.registerModConfig(name, { " ..
        "template = { pages = { { components = {} } }, onClose = function() saves[name] = (saves[name] or 0) + 1 end }, " ..
        "onCreate = function(c) note('create', name) c:createBlock() end, " ..
        "onClose = function() note('close', name) end, " ..
        "onSearch = function(t) return t == name end })",
        "@Data Files\\MWSE\\mods\\" .. (nested and "mwsuite\\" or "") .. folder .. "\\main.lua"))
    chunk(name, note, saves)
end
modRegisters("mwcollide", "Doubled Objects")
modRegisters("SomeoneElse", "Other Mod")
modRegisters("mwsaveguard", "mwsaveguard", true)
modRegisters("personalmod", "Standalone Mod")

table.sort(handlers, function(a, b) return a[3] > b[3] end)
for _, h in ipairs(handlers) do if h[1] == "modConfigReady" then h[2]() end end

local entry = registered["MW Conflict Tools"]
assert(entry.template and #entry.template.pages == 2, "core keybind menu can traverse all suite templates")
entry.template.onClose()
assert(saves["Doubled Objects"] == 1 and saves.mwsaveguard == 1, "keybind changes save original templates")
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
tabs[1].help()
tabs[2].click()                                  -- switch to the second tab
note("search", tostring(entry.onSearch("Doubled Objects")), tostring(entry.onSearch("nothing")))
entry.onClose(container)
for _, line in ipairs(log) do print(line) end
