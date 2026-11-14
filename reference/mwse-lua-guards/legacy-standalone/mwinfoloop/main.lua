--[[
  mwinfoloop - finds dialogue response lists the engine has linked into a circle
  (the cause of "talking to X freezes the game"). At startup it walks every
  topic's response list as the engine built it and writes mwinfoloop.log in the
  Morrowind folder: any topic whose list repeats, with the responses in the
  circle (id, speaker, plugin). Nothing in the game is changed.
]]
local f = io.open("mwinfoloop.log", "w")
local function log(fmt, ...)
    f:write(string.format(fmt, ...), "\n"); f:flush()
end

local function who(info)
    local a = info.actor
    return a and a.id or "-"
end
local function src(info)
    local ok, m = pcall(function() return info.sourceMod end)
    return ok and m or "?"
end

event.register(tes3.event.initialized, function()
    local dialogues = tes3.dataHandler.nonDynamicData.dialogues
    local topics, bad = 0, 0
    for _, dial in ipairs(dialogues) do
        topics = topics + 1
        local seen, order, n = {}, {}, 0
        for _, info in pairs(dial.info) do
            n = n + 1
            local key = tostring(info.id)
            if seen[key] then
                bad = bad + 1
                log("LOOP in topic '%s' after %d responses: response %s (%s, %s) comes round again",
                    dial.id, n - 1, key, who(info), src(info))
                for i = seen[key], #order do
                    local o = order[i]
                    log("    %s  speaker=%s  plugin=%s", tostring(o.id), who(o), src(o))
                end
                break
            end
            seen[key] = n
            order[n] = info
            if n > 200000 then log("topic '%s' has over 200000 responses, stopped", dial.id) break end
        end
    end
    log("checked %d topics, %d with a loop", topics, bad)
    mwse.log("[mwinfoloop] checked %d topics, %d with a loop (see mwinfoloop.log)", topics, bad)
end, { priority = 10000 })

-- Live trace: while the player talks to anyone, log every response the engine
-- considers, the one it picks, and result scripts, flushed per line. If the game
-- freezes, the last lines of mwinfoloop.log show where it was stuck.
local tracing = false
local count = 0
event.register(tes3.event.uiActivated, function(e)
    tracing = true; count = 0
    log("== dialogue menu opened")
end, { filter = "MenuDialog", priority = 10000 })
event.register(tes3.event.menuExit, function() if tracing then log("== menus closed") end tracing = false end)
event.register(tes3.event.activate, function(e)
    if e.activator == tes3.player and e.target and e.target.object.objectType == tes3.objectType.npc then
        tracing = true; count = 0
        log("== activate %s (%s)", e.target.id, e.target.object.name or "")
    end
end, { priority = 10000 })
event.register(tes3.event.infoFilter, function(e)
    if not tracing then return end
    count = count + 1
    if e.passes or count % 500 == 0 then
        log("  consider #%d %s/%s speaker=%s plugin=%s -> %s", count, e.dialogue.id, tostring(e.info.id),
            who(e.info), src(e.info), tostring(e.passes))
    end
end, { priority = -10000 })
event.register(tes3.event.infoResponse, function(e)
    if tracing then log("  result script of %s (%s): %s", tostring(e.info.id), src(e.info),
        (e.command or ""):gsub("[\r\n]+", " | "):sub(1, 300)) end
end, { priority = 10000 })
