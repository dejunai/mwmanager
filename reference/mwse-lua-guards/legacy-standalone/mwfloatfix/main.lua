--[[
    mwfloatfix - settle floating loose items

    When a cell loads, every loose item in it (misc items, books, ingredients, potions, weapons, armor,
    clothing, tools...) is checked once: rays are cast straight down from under the item, at its middle
    and towards its four bottom corners. If none of them meets something within a few units, the item
    isn't resting on anything, so it is lowered onto the highest surface below it.

    Left alone: items with nothing below them within reach (no floor found), items under water, items
    up against a wall (mounted weapons, shields) or hanging from something just above, lights (unless turned on in the menu),
    and anything already checked once (each item is checked the first time its cell loads).
    Moves are written to MWSE\logs\mwfloatfix.log.

    Blacklist: look at an object (or click it with the console open) and press the blacklist key
    (Shift+Delete by default). After you confirm, that one object, in that spot, is hidden now and in
    every game from then on: for lanterns and ropes left hanging in the air where another mod took away
    the posts they hung from. The list is MWSE\config\mwfloatfix_blacklist.json; the Mod Config menu
    shows it and can bring the last one back.
]]

local CONFIG_NAME = "mwfloatfix"
local defaults = {
    enabled   = true,
    tolerance = 4,       -- a gap up to this many units still counts as resting on something
    maxDrop   = 160,     -- never lower an item further than this (a gap larger than this is left alone)
    wallGap   = 6,       -- an item this close to a wall counts as mounted on it
    lights    = false,   -- carryable lights too (lanterns often hang on purpose)
    perFrame  = 12,      -- items checked per frame
    log       = true,
    blacklistKey = { keyCode = tes3.scanCode.delete, isShiftDown = true, isAltDown = false, isControlDown = false },
}
local config = mwse.loadConfig(CONFIG_NAME, defaults)

local ITEM_TYPES = {
    [tes3.objectType.miscItem] = true, [tes3.objectType.book] = true, [tes3.objectType.ingredient] = true,
    [tes3.objectType.alchemy] = true, [tes3.objectType.weapon] = true, [tes3.objectType.ammunition] = true,
    [tes3.objectType.armor] = true, [tes3.objectType.clothing] = true, [tes3.objectType.apparatus] = true,
    [tes3.objectType.lockpick] = true, [tes3.objectType.probe] = true, [tes3.objectType.repairItem] = true,
}
local ACTOR_TYPES = {
    [tes3.objectType.npc] = true, [tes3.objectType.creature] = true,
}

local checkedThisSession = setmetatable({}, { __mode = "k" })

local function getRefData(ref, key)
    if not ref or not ref.supportsLuaData then return nil end
    local ok, val = pcall(function()
        local data = ref.data
        return data and data[key] or nil
    end)
    return ok and val or nil
end

local function setRefData(ref, key, val)
    if not ref or not ref.supportsLuaData then return false end
    local ok = pcall(function()
        local data = ref.data
        if not data then
            ref.data = {}
            data = ref.data
        end
        if data then
            data[key] = val
        end
    end)
    return ok
end

local queue, queued = {}, {}
local settleAt = 0                       -- don't start until the cell has finished loading
local DOWN = tes3vector3.new(0, 0, -1)
local START_ABOVE = 6                    -- rays start this far above the item's bottom (inside the item)

local LOG_DIR = "Data Files\\MWSE\\logs"
local LOG_PATH = LOG_DIR .. "\\mwfloatfix.log"
local logFile
local function log(fmt, ...)
    if not config.log then return end
    if not logFile then
        pcall(function() lfs.mkdir(LOG_DIR) end)
        logFile = io.open(LOG_PATH, "w")
        if not logFile then return end
        logFile:write(os.date("mwfloatfix log, started %Y-%m-%d %H:%M:%S"), "\n")
    end
    local ok, line = pcall(string.format, fmt, ...)
    logFile:write(os.date("[%H:%M:%S] "), ok and line or tostring(fmt), "\n")
    logFile:flush()
end

local function isCandidate(ref)
    if not ref or ref.disabled or ref.deleted or not ref.sceneNode then return false end
    if checkedThisSession[ref] then return false end
    local obj = ref.object
    if not obj then return false end
    local t = obj.objectType
    if t == tes3.objectType.light then
        if not (config.lights and obj.canCarry) then return false end
    elseif not ITEM_TYPES[t] then
        return false
    end
    if getRefData(ref, "mwfloatfixChecked") then return false end
    return true
end

-- the item's eight bounding box corners in the world
local function worldCorners(ref)
    local node = ref.sceneNode
    if not node then return nil end
    local bb = (node.createBoundingBox and node:createBoundingBox()) or (ref.object and ref.object.boundingBox)
    if not bb or not bb.min or not bb.max then return nil end
    local t = node.worldTransform
    if not t or not t.rotation or not t.scale or not t.translation then return nil end
    local lo, hi = bb.min, bb.max
    local out = {}
    for _, x in ipairs({ lo.x, hi.x }) do
        for _, y in ipairs({ lo.y, hi.y }) do
            for _, z in ipairs({ lo.z, hi.z }) do
                out[#out + 1] = t.rotation * tes3vector3.new(x, y, z) * t.scale + t.translation
            end
        end
    end
    return out
end

local function bottomOf(corners)
    local z = math.huge
    for _, c in ipairs(corners) do if c.z < z then z = c.z end end
    return z
end

local function isActor(hitRef)
    return hitRef and hitRef.object and ACTOR_TYPES[hitRef.object.objectType]
end

-- distance from the item's bottom down to the first surface under (x, y), or nil if none within reach
local function gapBelow(ref, x, y, bottom)
    local hit = tes3.rayTest({
        position = tes3vector3.new(x, y, bottom + START_ABOVE),
        direction = DOWN,
        maxDistance = config.maxDrop + START_ABOVE + config.tolerance,
        ignore = { ref },
    })
    if not hit or isActor(hit.reference) then return nil end
    return hit.distance - START_ABOVE
end

-- up against a wall (mounted on it, hanging from it): a horizontal ray from the middle meets something
-- just past the item's own edge
local function againstWall(ref, corners, centre)
    local node = ref.sceneNode
    if not node or not node.worldTransform or not node.worldTransform.rotation then return false end
    local rot = node.worldTransform.rotation
    for _, axis in ipairs({ tes3vector3.new(1, 0, 0), tes3vector3.new(-1, 0, 0),
                            tes3vector3.new(0, 1, 0), tes3vector3.new(0, -1, 0) }) do
        local dir = rot * axis
        dir.z = 0
        if dir:length() > 0.01 then
            dir = dir:normalized()
            local reach = 0                  -- how far the item itself extends that way
            for _, c in ipairs(corners) do
                local d = (c - centre):dot(dir)
                if d > reach then reach = d end
            end
            local hit = tes3.rayTest({ position = centre, direction = dir, maxDistance = reach + config.wallGap,
                                       ignore = { ref } })
            if hit and not isActor(hit.reference) and hit.distance > reach * 0.5 then
                return true
            end
        end
    end
    return false
end

local function underWater(ref, bottom)
    local cell = ref.cell
    if not cell then return false end
    if cell.isInterior then
        return cell.hasWater and cell.waterLevel and bottom < cell.waterLevel
    end
    return bottom < (cell.waterLevel or 0)
end

local function settle(ref)
    if not isCandidate(ref) then return end
    checkedThisSession[ref] = true
    setRefData(ref, "mwfloatfixChecked", true)
    local corners = worldCorners(ref)
    if not corners then return end
    local bottom = bottomOf(corners)
    if underWater(ref, bottom) then return end
    local cx, cy, cz = 0, 0, 0
    for _, c in ipairs(corners) do cx, cy, cz = cx + c.x, cy + c.y, cz + c.z end
    cx, cy, cz = cx / 8, cy / 8, cz / 8
    -- middle, and 70% of the way to each bottom corner (the corners themselves overhang edges)
    local samples = { { cx, cy } }
    for _, c in ipairs(corners) do
        if c.z < bottom + 1 then
            samples[#samples + 1] = { cx + (c.x - cx) * 0.7, cy + (c.y - cy) * 0.7 }
        end
    end
    local nearest
    for _, s in ipairs(samples) do
        local g = gapBelow(ref, s[1], s[2], bottom)
        if g then
            if g <= config.tolerance then return end         -- resting on something
            if not nearest or g < nearest then nearest = g end
        end
    end
    if not nearest or nearest > config.maxDrop then return end  -- nothing below within reach
    local centre = tes3vector3.new(cx, cy, cz)
    if againstWall(ref, corners, centre) then return end
    -- hanging: something just above its top (a hook, a beam, a ceiling)
    local top = -math.huge
    for _, c in ipairs(corners) do if c.z > top then top = c.z end end
    local up = tes3.rayTest({ position = centre, direction = tes3vector3.new(0, 0, 1),
                              maxDistance = (top - cz) + config.wallGap, ignore = { ref } })
    if up and not isActor(up.reference) then return end
    local drop = nearest - 0.5                               -- leave half a unit, no sinking in
    setRefData(ref, "mwfloatfixOrigZ", ref.position.z)
    ref.position = tes3vector3.new(ref.position.x, ref.position.y, ref.position.z - drop)
    ref.modified = true
    log("lowered %s by %.0f units in %s (at %.0f, %.0f, %.0f)", ref.object.id, drop,
        ref.cell and ref.cell.editorName or "?", ref.position.x, ref.position.y, ref.position.z)
end


-- blacklist -------------------------------------------------------------------------
-- One entry per object in one spot: the cell, the object id, and where its plugin puts it. So the
-- same entry matches in every new game, and a rope blacklisted on one pier leaves every other rope be.
local BLACKLIST_NAME = "mwfloatfix_blacklist"
local blacklist = mwse.loadConfig(BLACKLIST_NAME, { entries = {} })
blacklist.entries = blacklist.entries or {}
local blackKeys = {}
local function cellKey(cell)
    if not cell then return "?" end
    if cell.isInterior then return cell.id:lower() end
    return string.format("%s (%d, %d)", (cell.name or ""):lower(), cell.gridX, cell.gridY)
end
local function refKey(ref)
    local p = ref.position
    local z = getRefData(ref, "mwfloatfixOrigZ") or (p and p.z or 0)
    return string.format("%s|%s|%d|%d|%d", cellKey(ref.cell), ref.object.id:lower(),
                         math.floor(p.x + 0.5), math.floor(p.y + 0.5), math.floor(z + 0.5))
end
local function rebuildKeys()
    blackKeys = {}
    for _, e in ipairs(blacklist.entries) do blackKeys[e.key] = true end
end
rebuildKeys()

local function applyBlacklist(ref)
    if not ref or ref.disabled or not ref.object or not blackKeys[refKey(ref)] then return false end
    ref:disable()
    setRefData(ref, "mwfloatfixHidden", true)
    log("hid blacklisted %s in %s", ref.object.id, cellKey(ref.cell))
    return true
end

local consoleRef                         -- what the console last had selected
if tes3.event.consoleReferenceChanged then
    event.register(tes3.event.consoleReferenceChanged, function(e) consoleRef = e.reference end)
end

-- what the player means: the console's selection while the console is open, else what the camera is
-- looking at (statics included, so ropes can be picked), up to 3000 units away
local function pickTarget()
    local console = tes3ui.findMenu("MenuConsole")
    if console and console.visible and consoleRef and consoleRef.cell then return consoleRef end
    local hit = tes3.rayTest({ position = tes3.getCameraPosition(), direction = tes3.getCameraVector(),
                               maxDistance = 3000, ignore = { tes3.player } })
    if hit and hit.reference and hit.reference ~= tes3.player then return hit.reference end
    if consoleRef and consoleRef.cell then return consoleRef end
    return nil
end

local function describe(ref)
    local name = ref.object.name
    return (name and name ~= "") and string.format("%s (%s)", name, ref.object.id) or ref.object.id
end

local function addToBlacklist(ref)
    local key = refKey(ref)
    if blackKeys[key] then
        tes3.messageBox("%s is already on the blacklist.", describe(ref))
        return
    end
    local srcMod = ref.sourceMod or (ref.object.sourceMod) or "?"
    table.insert(blacklist.entries, { key = key, id = ref.object.id, cell = cellKey(ref.cell),
                                      mod = srcMod, added = os.date("%Y-%m-%d %H:%M") })
    mwse.saveConfig(BLACKLIST_NAME, blacklist)
    rebuildKeys()
    ref:disable()
    setRefData(ref, "mwfloatfixHidden", true)
    log("blacklisted %s in %s (key %s, from %s)", ref.object.id, cellKey(ref.cell), key, srcMod)
    tes3.messageBox("Hidden in every game from now on: %s", describe(ref))
end

local function onBlacklistKey(e)
    if not tes3.player then return end
    local k = config.blacklistKey
    if e.keyCode ~= k.keyCode or (k.isShiftDown and not e.isShiftDown) or (k.isAltDown and not e.isAltDown)
            or (k.isControlDown and not e.isControlDown) then
        return
    end
    local ref = pickTarget()
    if not ref then
        tes3.messageBox("Nothing to blacklist: look at the object, or click it with the console open.")
        return
    end
    if ref.object.objectType == tes3.objectType.npc or ref.object.objectType == tes3.objectType.creature then
        tes3.messageBox("%s is a character; the blacklist is for things like lanterns and ropes.", describe(ref))
        return
    end
    tes3.messageBox({
        message = string.format("Hide %s here in every game from now on?\n(%s)", describe(ref), cellKey(ref.cell)),
        buttons = { "Hide it", "Cancel" },
        callback = function(b) if b.button == 0 then addToBlacklist(ref) end end,
    })
end
event.register(tes3.event.keyDown, onBlacklistKey)

local function undoLast()
    local e = table.remove(blacklist.entries)
    if not e then return nil end
    mwse.saveConfig(BLACKLIST_NAME, blacklist)
    rebuildKeys()
    for _, cell in ipairs(tes3.getActiveCells()) do         -- bring it back if it's around
        for ref in cell:iterateReferences() do
            if ref.disabled and getRefData(ref, "mwfloatfixHidden") and refKey(ref) == e.key then
                ref:enable()
                setRefData(ref, "mwfloatfixHidden", nil)
            end
        end
    end
    log("removed from the blacklist: %s", e.key)
    return e
end

local function enqueue(ref)
    if not config.enabled or queued[ref] then return end
    if not isCandidate(ref) then return end
    queued[ref] = true
    queue[#queue + 1] = ref
    settleAt = os.clock() + 1.0                     -- wait until the cell around it has loaded
end

event.register(tes3.event.referenceActivated, function(e)
    if applyBlacklist(e.reference) then return end
    enqueue(e.reference)
end)

event.register(tes3.event.cellChanged, function()
    settleAt = os.clock() + 1.0
end)

event.register(tes3.event.loaded, function()
    queue, queued = {}, {}
    checkedThisSession = setmetatable({}, { __mode = "k" })
    settleAt = os.clock() + 2.0
    for _, cell in ipairs(tes3.getActiveCells()) do
        for ref in cell:iterateReferences() do
            if not applyBlacklist(ref) then enqueue(ref) end
        end
    end
end)

event.register(tes3.event.simulate, function()
    if #queue == 0 or os.clock() < settleAt or tes3.menuMode() then return end
    -- lowest items first, so a book resting on a fallen book lands on it, not through it
    if not queue.sorted then
        table.sort(queue, function(a, b)
            local za = a.position and a.position.z or 0
            local zb = b.position and b.position.z or 0
            return za < zb
        end)
        queue.sorted = true
    end
    for _ = 1, config.perFrame do
        local ref = table.remove(queue, 1)
        if not ref then break end
        queued[ref] = nil
        local ok, err = pcall(settle, ref)
        if not ok then log("error checking %s: %s", tostring(ref.object and ref.object.id), tostring(err)) end
    end
    if #queue == 0 then queue.sorted = nil end
end)

-- Mod Config menu ---------------------------------------------------------------
event.register(tes3.event.modConfigReady, function()
    local template = mwse.mcm.createTemplate({ name = "Floating Item Fix" })
    template:saveOnClose(CONFIG_NAME, config)
    local page = template:createSideBarPage({
        label = "Settings",
        description = "Lowers loose items that float above the surface under them, once, the first time "
            .. "their cell loads. Items against a wall, under water, or with no floor within reach are left "
            .. "alone. Each move is written to MWSE\\logs\\mwfloatfix.log.",
    })
    page:createOnOffButton({ label = "Enabled", variable = mwse.mcm.createTableVariable({ id = "enabled", table = config }) })
    page:createSlider({ label = "Still resting when the gap is up to (units)", min = 1, max = 16, step = 1,
        variable = mwse.mcm.createTableVariable({ id = "tolerance", table = config }) })
    page:createSlider({ label = "Lower an item by at most (units)", min = 16, max = 512, step = 8,
        variable = mwse.mcm.createTableVariable({ id = "maxDrop", table = config }) })
    page:createSlider({ label = "Counts as mounted on a wall within (units)", min = 0, max = 32, step = 1,
        variable = mwse.mcm.createTableVariable({ id = "wallGap", table = config }) })
    page:createOnOffButton({ label = "Also carryable lights", variable = mwse.mcm.createTableVariable({ id = "lights", table = config }) })
    page:createOnOffButton({ label = "Write moves to MWSE\\logs\\mwfloatfix.log",
        variable = mwse.mcm.createTableVariable({ id = "log", table = config }) })
    page:createKeyBinder({ label = "Blacklist key (look at an object, or click it in the console, then press)",
        allowCombinations = true, variable = mwse.mcm.createTableVariable({ id = "blacklistKey", table = config }) })

    local bpage = template:createPage({ label = "Blacklist" })
    bpage:createInfo({ text = "Objects hidden in every game. Look at one (or click it with the console open) and "
        .. "press the blacklist key to add it. The list is MWSE\\config\\mwfloatfix_blacklist.json." })
    local listInfo = bpage:createInfo({ text = "" })
    local function refreshList()
        local lines = {}
        for i, e in ipairs(blacklist.entries) do
            lines[#lines + 1] = string.format("%d. %s  -  %s  (%s, added %s)", i, e.id, e.cell, e.mod or "?", e.added or "?")
        end
        listInfo.text = #lines > 0 and table.concat(lines, "\n") or "(empty)"
        if listInfo.elements and listInfo.elements.info then listInfo.elements.info.text = listInfo.text end
    end
    refreshList()
    bpage:createButton({ buttonText = "Bring back the last one", callback = function()
        local e = undoLast()
        tes3.messageBox(e and ("Back in every game: " .. e.id .. " (" .. e.cell .. ")") or "The blacklist is empty.")
        refreshList()
    end })
    template:register()
end)
