--[[
    mwsoundguard - stops the same sound piling up on the same object

    Some scripts (and the engine, for a light with a sound that a script moves or swings - the chargen
    ship's lanterns with Expanded Sounds) start a sound again and again while it is still playing; each
    start is another copy, and in seconds there are hundreds. Any mod, any script, no patch needed:
    when a sound is about to start on an object that is already playing that very sound, the new copy
    is dropped. The one already playing goes on. And a light with its own sound that a script moves gets
    that sound played once as a loop instead (see 2. below).

    What it stops goes to Data Files\MWSE\logs\mwse_lua_fixes.log: each object + sound once, then a
    count every 60 seconds.
]]

local LOG = "Data Files\\MWSE\\logs\\mwse_lua_fixes.log"
local seen, counts, total = {}, {}, 0

local function log(fmt, ...)
    local f = io.open(LOG, "a")
    if f then
        f:write(os.date("%Y-%m-%d %H:%M:%S") .. " [mwsoundguard] " .. string.format(fmt, ...) .. "\n")
        f:close()
    end
end

-- when each (reference, sound) last started, real time: a looping sound restarted within LOOP_GAP seconds
-- is dropped even when the game says it isn't playing (a script that moves its object - SetAtStart,
-- Rotate - stops the loop, then GetSoundPlaying says no and the script starts it again: every frame,
-- several copies: Expanded Sounds' plx_ES_Water on the chargen boat's lanterns). Any sound at all is
-- dropped when the same one started on the same object less than SAME_GAP seconds ago.
local LOOP_GAP, SAME_GAP = 3.0, 0.1
local lastStart = setmetatable({}, { __mode = "k" })     -- reference -> { sound id -> clock }

local function isLoop(e)
    local ok, v = pcall(function() return e.loop end)
    if ok and v then return true end
    local ok2, fl = pcall(function() return e.flags end)
    return ok2 and type(fl) == "number" and fl % 2 == 1
end

local function onAddSound(e)
    local ref, snd = e.reference, e.sound
    if not ref or not snd then return end
    local okv, vo = pcall(function() return e.isVoiceover end)
    if okv and vo then return end
    local now = os.clock()
    local per = lastStart[ref]
    if not per then per = {}; lastStart[ref] = per end
    local sid = snd.id or tostring(snd)
    local last = per[sid]
    local ok, playing = pcall(tes3.getSoundPlaying, { sound = snd, reference = ref })
    local repeatTooSoon = last and ((isLoop(e) and now - last < LOOP_GAP) or now - last < SAME_GAP)
    if not ((ok and playing) or repeatTooSoon) then
        per[sid] = now
        return
    end
    e.block = true
    local key = (ref.id or "?") .. "|" .. sid
    counts[key] = (counts[key] or 0) + 1
    total = total + 1
    if not seen[key] then
        seen[key] = true
        local cell = ref.cell and ref.cell.editorName or "?"
        local script = ref.object and ref.object.script and ref.object.script.id or "none"
        log("%s on %s in %s (script %s) was started again %s; extra copies are dropped",
            sid, ref.id or "?", cell, script,
            (ok and playing) and "while still playing" or "straight after the last start")
    end
end

-- 2. moving lights: a light with its own sound (a lantern's creak) that a script swings or moves
-- (Expanded Sounds' SignRotate on the chargen ship's lanterns) has that sound started again by the game at
-- every move. Any light seen moving gets its sound played once, as a loop on the light, instead of by
-- the game: the object's own sound is taken off (this session only), so moves no longer restart it.
local watch, moving = {}, {}            -- reference -> {pos, rot}; light object -> its sound

local function playOn(ref, snd)
    pcall(tes3.removeSound, { reference = ref })
    pcall(tes3.playSound, { reference = ref, sound = snd, loop = true })
end

local function onActivated(e)
    local ref = e.reference
    local obj = ref and ref.object
    if not obj or obj.objectType ~= tes3.objectType.light then return end
    local snd = moving[obj]
    if snd then
        playOn(ref, snd)
    elseif obj.sound and obj.script then        -- only a scripted light can be moved by a script
        watch[ref] = { ref.position:copy(), ref.orientation:copy() }
    end
end

local function onDeactivated(e)
    local ref = e.reference
    watch[ref] = nil
    if ref and ref.object and moving[ref.object] then pcall(tes3.removeSound, { reference = ref }) end
end

local function checkMoving()
    for ref, was in pairs(watch) do
        local ok, moved = pcall(function()
            return ref.position:distance(was[1]) > 0.5 or (ref.orientation - was[2]):length() > 0.001
        end)
        if not ok then
            watch[ref] = nil
        elseif moved then
            watch[ref] = nil
            local obj = ref.object
            if not moving[obj] and obj.sound then
                moving[obj] = obj.sound
                obj.sound = nil
                log("%s (sound %s, script %s) is moved by its script; the game restarted its sound at every move. "
                    .. "Its sound now plays once, as a loop on each %s", obj.id, moving[obj].id,
                    obj.script and obj.script.id or "?", obj.id)
                for _, cell in pairs(tes3.getActiveCells()) do
                    for r in cell:iterateReferences(tes3.objectType.light) do
                        if r.object == obj and not r.disabled then
                            playOn(r, moving[obj])
                            watch[r] = nil
                        end
                    end
                end
            end
        end
    end
end

event.register(tes3.event.initialized, function()
    event.register(tes3.event.addSound, onAddSound, { priority = 100 })
    event.register(tes3.event.addTempSound, onAddSound, { priority = 100 })
    event.register(tes3.event.referenceActivated, onActivated)
    event.register(tes3.event.referenceDeactivated, onDeactivated)
    timer.start({ type = timer.real, duration = 0.5, iterations = -1, persist = false, callback = checkMoving })
    timer.start({ type = timer.real, duration = 60, iterations = -1, persist = false, callback = function()
        if total > 0 then
            local parts = {}
            for k, n in pairs(counts) do parts[#parts + 1] = string.format("%s x%d", k, n) end
            table.sort(parts)
            log("in the last minute dropped %d repeat(s): %s", total, table.concat(parts, ", "))
            counts, total = {}, 0
        end
    end })
    mwse.log("[mwsoundguard] active")
end)
