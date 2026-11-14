--[[
    mwstranded - objects left in the void when another mod rebuilt an interior

    mwdialog_check --cell-conflicts writes MWSE\config\mwstranded_data.json: for each interior a plugin
    rebuilt, the objects another plugin placed for the old layout that are now cut off from the new one
    (nothing joins them to it, and no door in it leads to them). As the interior loads:
      move   people, creatures, containers and loose items are brought to where a door into the place
             lands in the rebuilt layout, spread around it and set down on the floor
             doors and scripted things too (a door still leads where it did; the doors elsewhere that
             arrived by the old spot are sent to the rebuilt layout by the Cell Fixes patch)
      hide   unscripted scenery (walls, rocks, furniture, lights...) is disabled, never deleted
      leave  listed only: the place has no way into its rebuilt layout to bring things to

    Each object is handled once; what it did is kept in your save. Hidden objects show again if they
    drop off the list or you switch it off in the Mod Config menu; moved ones stay where they were put.
    What it does goes to Data Files\MWSE\logs\mwstranded.log.
]]

local CONFIG_NAME = "mwstranded"
local config = mwse.loadConfig(CONFIG_NAME, { enabled = true, move = true, hide = true, log = true })

local LOG_DIR = "Data Files\\MWSE\\logs"
local logFile
local function log(fmt, ...)
    if not config.log then return end
    if not logFile then
        pcall(function() lfs.mkdir(LOG_DIR) end)
        logFile = io.open(LOG_DIR .. "\\mwstranded.log", "w")
        if not logFile then return end
        logFile:write(os.date("mwstranded log, started %Y-%m-%d %H:%M:%S"), "\n")
    end
    local ok, line = pcall(string.format, fmt, ...)
    logFile:write(os.date("[%H:%M:%S] "), ok and line or tostring(fmt), "\n")
    logFile:flush()
end

-- the list: cell id (lower) -> { arrival = {x,y,z,rz} or nil, byId = { id = { entry, ... } } } ----------
local lists = {}
local function loadData()
    lists = {}
    local data = mwse.loadConfig("mwstranded_data")
    if not data or not data.cells then
        log("no MWSE\\config\\mwstranded_data.json yet: run mwdialog_check.bat --cell-conflicts")
        return
    end
    local n = 0
    for cellId, c in pairs(data.cells) do
        local byId = {}
        for _, e in ipairs(c.objects or {}) do
            if e.action ~= "leave" then
                local id = e.id:lower()
                byId[id] = byId[id] or {}
                table.insert(byId[id], e)
                n = n + 1
            end
        end
        lists[cellId:lower()] = { arrival = c.arrival, byId = byId }
    end
    log("list from %s: %d object(s) to move or hide in %d interior(s)", tostring(data.generated), n,
        table.size(lists))
end

local function key(e)
    return string.format("%s|%d|%d|%d", e.id:lower(), e.pos[1], e.pos[2], e.pos[3])
end

local function isActor(ref)
    local t = ref.object.objectType
    return t == tes3.objectType.npc or t == tes3.objectType.creature
end

-- the listed entry this reference is, if any: the same object where the list says (people and
-- creatures may have taken a step or two since the cell was last loaded)
local function entryFor(set, ref)
    local cands = set.byId[ref.object.id:lower()]
    if not cands then return nil end
    local p = ref.position
    local tol = isActor(ref) and 96 or 3
    for _, e in ipairs(cands) do
        local dx, dy, dz = p.x - e.pos[1], p.y - e.pos[2], p.z - e.pos[3]
        if dx * dx + dy * dy + dz * dz <= tol * tol then return e end
    end
end

local function doneIn(cellId)
    local all = tes3.player.data.mwstranded
    if not all then
        all = {}
        tes3.player.data.mwstranded = all
    end
    all[cellId] = all[cellId] or { hidden = {}, moved = {} }
    return all[cellId]
end

-- a free spot near the arrival: around it in rings, pulled in where a wall is closer, set on the floor
local function spotNear(arrival, i, ref)
    local ring = math.floor((i - 1) / 8)
    local radius = 72 + 48 * ring
    local angle = (i - 1) * (math.pi / 4) + ring * 0.4 + (arrival[4] or 0)
    local from = tes3vector3.new(arrival[1], arrival[2], arrival[3] + 64)
    local dir = tes3vector3.new(math.sin(angle), math.cos(angle), 0)
    local hit = tes3.rayTest({ position = from, direction = dir, ignore = { ref, tes3.player } })
    if hit and hit.distance and hit.distance < radius + 24 then
        radius = math.max(0, hit.distance - 40)
    end
    local x, y = arrival[1] + dir.x * radius, arrival[2] + dir.y * radius
    local z = arrival[3]
    local down = tes3.rayTest({ position = tes3vector3.new(x, y, arrival[3] + 96),
        direction = tes3vector3.new(0, 0, -1), ignore = { ref, tes3.player } })
    if down and down.intersection and down.distance and down.distance < 400 then
        z = down.intersection.z + 1
    end
    return tes3vector3.new(x, y, z)
end

-- a door brought in: set flat against the nearest wall around the arrival (rays out in 16 directions at
-- chest height; a hit whose surface is upright is a wall), facing out of it, on the floor, kept clear of
-- the arrival spot and of doors already set there. nil when no wall is in reach: it stands free instead.
local function wallSpot(arrival, ref, placed)
    local from = tes3vector3.new(arrival[1], arrival[2], arrival[3] + 64)
    local hits = {}
    for k = 0, 15 do
        local a = k * math.pi / 8
        local dir = tes3vector3.new(math.sin(a), math.cos(a), 0)
        local hit = tes3.rayTest({ position = from, direction = dir, maxDistance = 1024,
            ignore = { ref, tes3.player } })
        if hit and hit.intersection and hit.distance and hit.distance >= 96 and hit.distance <= 1024
            and hit.normal and math.abs(hit.normal.z) < 0.3 then
            hits[#hits + 1] = { hit = hit, dir = dir }
        end
    end
    table.sort(hits, function(p, q) return p.hit.distance < q.hit.distance end)
    -- how thick the door is, and which of its own axes is the thin one
    local bb = ref.object.boundingBox
    local sx = bb and (bb.max.x - bb.min.x) or 16
    local sy = bb and (bb.max.y - bb.min.y) or 16
    local thin, turn = math.min(sx, sy), (sx < sy) and (math.pi / 2) or 0
    for _, h in ipairs(hits) do
        local n = tes3vector3.new(h.hit.normal.x, h.hit.normal.y, 0):normalized()
        local p = h.hit.intersection
        local x, y = p.x + n.x * (thin / 2 + 2), p.y + n.y * (thin / 2 + 2)
        local clear = true
        for _, q in ipairs(placed) do
            if (q.x - x) ^ 2 + (q.y - y) ^ 2 < 128 * 128 then clear = false break end
        end
        if clear then
            local z = arrival[3]
            local down = tes3.rayTest({ position = tes3vector3.new(x, y, p.z + 32),
                direction = tes3vector3.new(0, 0, -1), ignore = { ref, tes3.player } })
            if down and down.intersection and down.distance and down.distance < 400 then
                z = down.intersection.z + 1
            end
            return tes3vector3.new(x, y, z), math.atan2(n.x, n.y) + turn
        end
    end
end

local function settleCell(cell)
    if not cell or not cell.isInterior or not tes3.player then return 0, 0, 0 end
    local cellId = cell.id:lower()
    local set = config.enabled and lists[cellId] or nil
    local done = doneIn(cellId)
    if not set and next(done.hidden) == nil then return 0, 0, 0 end
    local moved, hid, shown, n = 0, 0, 0, 0
    local doorsSet = {}
    for ref in cell:iterateReferences() do
        if ref.object and not ref.deleted then
            local e = set and entryFor(set, ref)
            local k = e and key(e)
            if e and e.action == "hide" and config.hide and not done.hidden[k] and not ref.disabled then
                done.hidden[k] = true
                ref:disable()
                hid = hid + 1
                log("%s: hid %s from %s (cut off from the rebuilt layout)", cell.editorName, ref.object.id, e.from)
            elseif e and e.action == "move" and config.move and not done.moved[k] and set.arrival then
                n = n + 1
                local to, rz = nil, nil
                if ref.object.objectType == tes3.objectType.door then
                    to, rz = wallSpot(set.arrival, ref, doorsSet)
                    if to then doorsSet[#doorsSet + 1] = to end
                end
                to = to or spotNear(set.arrival, n, ref)
                done.moved[k] = true
                if isActor(ref) then
                    tes3.positionCell({ reference = ref, cell = cell, position = to,
                        orientation = tes3vector3.new(0, 0, (set.arrival[4] or 0) + math.pi) })
                else
                    ref.position = to
                    if ref.object.objectType == tes3.objectType.door then
                        -- against a wall, facing out of it; free standing: facing the arrival spot
                        ref.orientation = tes3vector3.new(0, 0,
                            rz or math.atan2(set.arrival[1] - to.x, set.arrival[2] - to.y))
                    end
                    ref.modified = true
                end
                moved = moved + 1
                log("%s: brought %s from %s to %.0f, %.0f, %.0f%s (was at %d, %d, %d)", cell.editorName,
                    ref.object.id, e.from, to.x, to.y, to.z, rz and ", against a wall" or "",
                    e.pos[1], e.pos[2], e.pos[3])
            else
                -- hidden earlier but no longer on the list (or switched off): show it again
                local p = ref.position
                local here = string.format("%s|%d|%d|%d", ref.object.id:lower(), math.floor(p.x + 0.5),
                    math.floor(p.y + 0.5), math.floor(p.z + 0.5))
                if done.hidden[here] and (not e or e.action ~= "hide" or not config.hide) then
                    done.hidden[here] = nil
                    ref:enable()
                    shown = shown + 1
                    log("%s: %s shown again", cell.editorName, ref.object.id)
                end
            end
        end
    end
    return moved, hid, shown
end

local function settleActive()
    local m, h, s = 0, 0, 0
    for _, cell in ipairs(tes3.getActiveCells()) do
        local ok, a, b, c = pcall(settleCell, cell)
        if ok then
            m, h, s = m + (a or 0), h + (b or 0), s + (c or 0)
        else
            log("error in %s: %s", tostring(cell.id), tostring(a))
        end
    end
    return m, h, s
end

event.register(tes3.event.initialized, loadData)
event.register(tes3.event.loaded, function()
    loadData()
    timer.delayOneFrame(settleActive)
end)
event.register(tes3.event.cellChanged, function()
    timer.delayOneFrame(settleActive)
end)

-- Mod Config menu -----------------------------------------------------------------------------------
event.register(tes3.event.modConfigReady, function()
    local template = mwse.mcm.createTemplate({ name = "Stranded Objects (mwstranded)" })
    template:saveOnClose(CONFIG_NAME, config)
    local page = template:createSideBarPage({
        label = "Settings",
        description = "When a mod rebuilds an interior, other mods' objects made for the old layout end up "
            .. "in the void. As such a place loads, this brings the people, creatures, containers and loose "
            .. "items to the way in, and hides the cut-off scenery. The list comes from mwdialog_check "
            .. "--cell-conflicts (MWSE\\config\\mwstranded_data.json).",
    })
    page:createOnOffButton({
        label = "On",
        description = "Off: nothing more is moved or hidden, and what it hid shows again as places load. "
            .. "Moved things stay where they were put.",
        variable = mwse.mcm.createTableVariable({ id = "enabled", table = config }),
        callback = function() if tes3.player then settleActive() end end,
    })
    page:createOnOffButton({
        label = "Bring people, creatures, containers and items to the way in",
        variable = mwse.mcm.createTableVariable({ id = "move", table = config }),
    })
    page:createOnOffButton({
        label = "Hide cut-off scenery",
        variable = mwse.mcm.createTableVariable({ id = "hide", table = config }),
        callback = function() if tes3.player then settleActive() end end,
    })
    page:createButton({
        buttonText = "Re-read the list and apply it here now",
        callback = function()
            loadData()
            if tes3.player then
                local m, h, s = settleActive()
                tes3.messageBox("Stranded objects here: %d brought in, %d hidden, %d shown again.", m, h, s)
            end
        end,
    })
    page:createOnOffButton({
        label = "Write what it does to MWSE\\logs\\mwstranded.log",
        variable = mwse.mcm.createTableVariable({ id = "log", table = config }),
    })
    template:register()
end)
