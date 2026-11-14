--[[
  mwguard - guards every other MWSE Lua mod, without editing them (loads first: load-priority 10000).

  1. Every-frame handlers (simulate, enterFrame) run at most every 0.35 s, unless the handler's own code
     looks like it needs every frame (uses e.delta, camera, velocity, position/orientation changes,
     animation, dragging, mouse...) or its mod is in config.exemptMods.
  2. Repeating timers (iterations = -1) faster than 0.35 s are slowed to 0.35 s (same exemptions).
  3. Absurd priorities (|priority| >= 10000) are cut to +-1000 (keeps the sign, so "first"/"last" still hold).
  4. Errors in a handler or timer are logged ONCE with their traceback; repeats are only counted and a
     summary line is written every 60 s (MWSE itself would log every one, every frame).
  5. The same mwse.log line repeated more than 20 times a second is suppressed and counted.
  Every change is written to Data Files\MWSE\logs\mwse_lua_fixes.log (mod, file:line, what, why).
  Settings: Data Files\MWSE\config\mwguard.json (created on first run).
]]
local defaults = {
    enabled = true,
    throttleSeconds = 0.35,
    priorityLimit = 10000,
    priorityClampTo = 1000,
    exemptMods = { "mwguard" },          -- mod folder names (substrings) left completely alone
    forceThrottleMods = {},              -- throttle these even if their code looks per-frame
    perFrameEvents = { "simulate", "enterFrame", "simulated" },
    needsEveryFrame = { "delta", "camera", "velocity", "orientation", "position%s*=", "position:", "rotation",
                        "animat", "drag", "mouse", "lerp", "smooth", "interpolat", "fade", "scale%s*=" },
    errorSummarySeconds = 60,
    logRepeatLimit = 20,
}
local config = mwse.loadConfig("mwguard", defaults)
for k, v in pairs(defaults) do if config[k] == nil then config[k] = v end end
mwse.saveConfig("mwguard", config)
if not config.enabled then
    mwse.log("[mwguard] disabled in config")
    return
end

local LOG_PATH = "Data Files\\MWSE\\logs\\mwse_lua_fixes.log"
local logFile = io.open(LOG_PATH, "a")
local function flog(fmt, ...)
    if not logFile then return end
    local ok, s = pcall(string.format, fmt, ...)
    logFile:write(os.date("%Y-%m-%d %H:%M:%S"), "  ", ok and s or tostring(fmt), "\n")
    logFile:flush()
end
flog("==== session start (MWSE %s) ====", tostring(mwse.buildNumber or "?"))

local origLog = mwse.log
local origRegister, origUnregister, origIsRegistered = event.register, event.unregister, event.isRegistered
local origTimerStart = timer.start

local perFrame = {}
for _, n in ipairs(config.perFrameEvents) do perFrame[n] = true end

-- where a function comes from: mod key ("abot/WBSR"), file, first/last line
local function where(fn)
    local info = debug.getinfo(fn, "S")
    if not info then return nil end
    local src = (info.source or ""):gsub("^@", ""):gsub("/", "\\")
    local low = src:lower()
    local i = low:find("\\mwse\\mods\\", 1, true)
    if not i then return nil end                         -- core or lib code: never touched
    local rel = src:sub(i + #"\\mwse\\mods\\")
    local parts = {}
    for p in rel:gmatch("[^\\]+") do parts[#parts + 1] = p end
    local mod = parts[1] or "?"
    if #parts >= 3 and not lfs.fileexists(src:sub(1, i - 1) .. "\\MWSE\\mods\\" .. mod .. "\\main.lua") then
        mod = mod .. "/" .. parts[2]
    end
    return { mod = mod, file = rel, path = src, first = info.linedefined or 0, last = info.lastlinedefined or 0 }
end

local function listed(mod, list)
    local low = mod:lower()
    for _, x in ipairs(list) do
        if low:find(tostring(x):lower(), 1, true) then return true end
    end
    return false
end

local fileCache = {}
local function sourceOf(w)
    local lines = fileCache[w.path]
    if lines == nil then
        lines = {}
        local f = io.open(w.path, "r")
        if f then
            for l in f:lines() do lines[#lines + 1] = l end
            f:close()
        end
        fileCache[w.path] = lines
    end
    return table.concat(lines, "\n", math.max(1, w.first), math.min(#lines, math.max(w.first, w.last)))
end

local function needsEveryFrame(w)
    local body = sourceOf(w):lower()
    for _, pat in ipairs(config.needsEveryFrame) do
        if body:find(pat) then return pat end
    end
    return nil
end

-- 4. errors: once with traceback, then counted
local errors, errorOrder = {}, {}
local function onError(w, kind)
    return function(err)
        local key = tostring(err)
        local rec = errors[key]
        if not rec then
            rec = { count = 0, mod = w and w.mod or "?", kind = kind }
            errors[key] = rec
            errorOrder[#errorOrder + 1] = key
            local tb = debug.traceback("", 2)
            origLog("[mwguard] error in %s (%s): %s (repeats are counted in MWSE\\logs\\mwse_lua_fixes.log)",
                rec.mod, kind, key)
            flog("ERROR %s (%s): %s\n%s", rec.mod, kind, key, tb)
        end
        rec.count = rec.count + 1
        return err
    end
end

local onceNoted = {}
local function note(key, fmt, ...)
    if onceNoted[key] then return end
    onceNoted[key] = true
    flog(fmt, ...)
end

-- wrapper for one registered callback
local wrappers = setmetatable({}, { __mode = "k" })     -- callback -> { [eventType] = wrapper }
local function wrap(eventType, callback, w)
    local byEvent = wrappers[callback]
    if byEvent and byEvent[eventType] then return byEvent[eventType] end
    local throttle = false
    if perFrame[eventType] and w then
        local why = needsEveryFrame(w)
        if why and not listed(w.mod, config.forceThrottleMods) then
            note(w.file .. w.first .. eventType, "KEPT    %s  %s:%d  %s handler runs every frame (its code uses '%s')",
                w.mod, w.file, w.first, eventType, why)
        else
            throttle = true
            note(w.file .. w.first .. eventType, "THROTTLE %s  %s:%d  %s handler: every frame -> every %.2f s",
                w.mod, w.file, w.first, eventType, config.throttleSeconds)
        end
    end
    local handler = onError(w, eventType)
    local acc = 0
    local wrapper
    if throttle then
        local limit = config.throttleSeconds
        wrapper = function(e)
            acc = acc + ((e and e.delta) or 0.016)
            if acc < limit then return end
            acc = 0
            local ok, res = xpcall(callback, handler, e)
            if ok then return res end
        end
    else
        wrapper = function(e)
            local ok, res = xpcall(callback, handler, e)
            if ok then return res end
        end
    end
    wrappers[callback] = wrappers[callback] or {}
    wrappers[callback][eventType] = wrapper
    return wrapper
end

local function fixOptions(eventType, options, w)
    if not options or type(options.priority) ~= "number" then return options end
    local p = options.priority
    if math.abs(p) >= config.priorityLimit then
        local copy = {}
        for k, v in pairs(options) do copy[k] = v end
        copy.priority = (p > 0 and 1 or -1) * config.priorityClampTo
        note((w and w.file or "?") .. eventType .. p, "PRIORITY %s  %s:%d  %s priority %d -> %d",
            w and w.mod or "?", w and w.file or "?", w and w.first or 0, eventType, p, copy.priority)
        return copy
    end
    return options
end

event.register = function(eventType, callback, options)
    if type(callback) ~= "function" or type(eventType) ~= "string" then
        return origRegister(eventType, callback, options)
    end
    local w = where(callback)
    if not w or listed(w.mod, config.exemptMods) then
        return origRegister(eventType, callback, options)
    end
    return origRegister(eventType, wrap(eventType, callback, w), fixOptions(eventType, options, w))
end

local function mapped(eventType, callback)
    local byEvent = type(callback) == "function" and wrappers[callback]
    return byEvent and byEvent[eventType] or callback
end

event.unregister = function(eventType, callback, options)
    local mc = mapped(eventType, callback)
    origUnregister(eventType, mc, options)
    if mc ~= callback then pcall(origUnregister, eventType, callback, options) end   -- registered before mwguard
end

event.isRegistered = function(eventType, callback, options)
    local mc = mapped(eventType, callback)
    return origIsRegistered(eventType, mc, options) or (mc ~= callback and origIsRegistered(eventType, callback, options))
end

-- 2. repeating timers
timer.start = function(params)
    if type(params) == "table" and type(params.callback) == "function" then
        local w = where(params.callback)
        if w and not listed(w.mod, config.exemptMods) then
            local cb = params.callback
            local handler = onError(w, "timer")
            local copy = {}
            for k, v in pairs(params) do copy[k] = v end
            copy.callback = function(e)
                local ok, res = xpcall(cb, handler, e)
                if ok then return res end
            end
            if copy.iterations == -1 and type(copy.duration) == "number" and copy.duration < config.throttleSeconds then
                local why = needsEveryFrame(w)
                if why and not listed(w.mod, config.forceThrottleMods) then
                    note(w.file .. w.first .. "timer", "KEPT    %s  %s:%d  repeating timer every %.3f s (its code uses '%s')",
                        w.mod, w.file, w.first, copy.duration, why)
                else
                    note(w.file .. w.first .. "timer", "TIMER   %s  %s:%d  repeating timer %.3f s -> %.2f s",
                        w.mod, w.file, w.first, copy.duration, config.throttleSeconds)
                    copy.duration = config.throttleSeconds
                end
            end
            return origTimerStart(copy)
        end
    end
    return origTimerStart(params)
end

-- 5. the same log line flooding
local lastMsg, lastCount, windowStart, suppressed = nil, 0, os.clock(), {}
mwse.log = function(fmt, ...)
    local ok, msg = pcall(string.format, tostring(fmt), ...)
    msg = ok and msg or tostring(fmt)
    local now = os.time()
    if msg == lastMsg and now - windowStart < 1 then
        lastCount = lastCount + 1
        if lastCount > config.logRepeatLimit then
            suppressed[msg] = (suppressed[msg] or 0) + 1
            return
        end
    else
        lastMsg, lastCount, windowStart = msg, 1, now
    end
    return origLog("%s", msg)
end

-- periodic summary of counted errors and suppressed lines
local function summary()
    for _, key in ipairs(errorOrder) do
        local rec = errors[key]
        if rec.count > 1 and rec.count ~= rec.reported then
            flog("REPEAT  %s (%s): %d time(s) so far: %s", rec.mod, rec.kind, rec.count, key)
            rec.reported = rec.count
        end
    end
    for msg, n in pairs(suppressed) do
        flog("LOGSPAM %d identical line(s) suppressed: %s", n, msg:sub(1, 200))
        suppressed[msg] = nil
    end
end
origRegister(tes3.event.initialized, function()
    origTimerStart({ type = timer.real, duration = config.errorSummarySeconds, iterations = -1, persist = false,
                     callback = summary })
    origLog("[mwguard] active: every-frame handlers throttled to %.2f s, priorities over %d cut, error/log spam counted "
            .. "(see MWSE\\logs\\mwse_lua_fixes.log)", config.throttleSeconds, config.priorityLimit)
end, { priority = -100000 })
