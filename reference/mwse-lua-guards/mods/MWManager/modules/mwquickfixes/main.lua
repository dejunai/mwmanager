-- MWManager quick fixes consolidated from standalone MWSE utilities.
local print, mwse, mwluafix_log = (function(_p, _m) local S = package.loaded["mwluafix_logshim:DeferredDialogueTeleport"] if type(S) == "table" and S.v == 2 then return S.print, S.mwse, S end S = { v = 2, redirected = 0, dropped = 0, collapsed = 0 } package.loaded["mwluafix_logshim:DeferredDialogueTeleport"] = S local C = { name = "DeferredDialogueTeleport", init = 40, after = 10, keep = { "error", "traceback", "exception", "fail", "warn", "stack", "attempt to", "assert", "invalid", "%.lua:%d+:", "^lua:" }, perSecond = 50, maxBytes = 2097152, keepOld = 1, flushSeconds = 2.0, errorRepeat = 0 } local G, MARK = _G, "mwdialog_luafix" local find, lower, sub, gsub, concat, fmt, date, select, tostring, pcall = string.find, string.lower, string.sub, string.gsub, table.concat, string.format, os.date, select, tostring, pcall local dir = "Data Files\\MWSE\\logs" local path, oldPath = dir .. "\\" .. C.name .. ".log", dir .. "\\" .. C.name .. ".old.log" local t0 = os.time() - math.floor(os.clock()) - 1 local init, run, started, redirected, f, dead, busy, fails = 0, 0, false, false, nil, false, false, 0 local bytes, dirty, last, lastN, sec, secN, dropped, errs, errKeys = 0, false, nil, 0, -1, 0, 0, {}, 0 local function isErr(t) local l = lower(#t > 4096 and sub(t, 1, 4096) or t) for i = 1, #C.keep do local ok, h = pcall(find, l, C.keep[i]) if ok and h then return true end end return false end local function out(s) f:write(date("[%H:%M:%S] "), s, "\n") bytes = bytes + #s + 12 dirty = true end local function pending() if lastN > 0 then out("[previous line repeated " .. lastN .. " more time(s)]") lastN = 0 end if dropped > 0 then out("[" .. dropped .. " line(s) dropped: more than " .. C.perSecond .. " in one second]") dropped = 0 end end local function flushAll() if f then pending() if dirty then dirty = false f:flush() end end end local function rotate() if f then pcall(f.close, f) f = nil end if C.keepOld > 0 then os.remove(oldPath) os.rename(path, oldPath) else os.remove(path) end f = io.open(path, "w") bytes, last = 0, nil if f then pcall(f.setvbuf, f, "full", 16384) f:write(date("[%Y-%m-%d %H:%M:%S] "), C.name, ": lines past its MWSE.log allowance (", MARK, ")\n") end return f end local function open() pcall(function() lfs.mkdir(dir) end) local ok, m = pcall(function() return lfs.attributes(path, "modification") end) if ok and type(m) == "number" and m >= t0 then path, oldPath = dir .. "\\" .. C.name .. ".luafix.log", dir .. "\\" .. C.name .. ".luafix.old.log" end return rotate() end local function toFile(t) if t == last then lastN = lastN + 1 S.collapsed = S.collapsed + 1 return end local s = os.time() if s ~= sec then sec, secN = s, 0 end secN = secN + 1 if C.perSecond > 0 and secN > C.perSecond then dropped = dropped + 1 S.dropped = S.dropped + 1 return end if bytes >= C.maxBytes and not rotate() then dead = true return end pending() last = t out(t) S.redirected = S.redirected + 1 if not started then dirty = false f:flush() end end S.start = function() if started then return end started = true pcall(flushAll) pcall(function() timer.start({ type = timer.real, duration = C.flushSeconds, iterations = -1, persist = false, callback = function() pcall(flushAll) end }) end) end local function route(t, force) if not started then local ok, v = pcall(function() return tes3.isInitialized() end) if ok and v then S.start() end end if force or isErr(t) then if C.errorRepeat > 0 then local k = gsub(sub(t, 1, (find(t, "\n", 1, true) or 201) - 1), "%d+", "#") local n = errs[k] or 0 if n == 0 then if errKeys >= 500 then errs, errKeys = {}, 0 end errKeys = errKeys + 1 end errs[k] = n + 1 if n >= C.errorRepeat then if f then toFile(t) end return false end end if f then pending() out(t) dirty = false f:flush() end return true end local _, nl = gsub(t, "\n", "") nl = nl + 1 if not redirected then if started then if run + nl <= C.after then run = run + nl return true end elseif init + nl <= C.init then init = init + nl return true end redirected = true if not open() then dead = true _p(fmt("[%s] %s writes a lot to MWSE.log, but %s can't be opened: leaving it here", MARK, C.name, path)) return true end _p(fmt("[%s] %s has written %d line(s) to MWSE.log; its further output goes to %s (errors still show here)", MARK, C.name, init + run, path)) end if dead or not f then return true end toFile(t) return false end local function fail(e) fails = fails + 1 if fails >= 10 then dead = true end if fails == 1 then pcall(_p, fmt("[%s] %s: log shim error (the line went to MWSE.log): %s", MARK, C.name, tostring(e))) end end local function decidePrint(n, ...) local t if n <= 1 then t = tostring((...)) else local parts = {} for i = 1, n do parts[i] = tostring((select(i, ...))) end t = concat(parts, "\t") end return route(t, false) end S.print = function(...) if busy or dead then return _p(...) end busy = true local ok, pass = pcall(decidePrint, select("#", ...), ...) busy = false if not ok then fail(pass) return _p(...) end if pass then return _p(...) end end local function decideLog(f0, ...) local ok, t = pcall(fmt, tostring(f0), ...) if not ok then t = tostring(f0) end return route(t, false) end S.mwse = setmetatable({ log = function(f0, ...) if busy or dead then return _m.log(f0, ...) end busy = true local ok, pass = pcall(decideLog, f0, ...) busy = false if not ok then fail(pass) pass = true end if pass then return _m.log(f0, ...) end end }, { __index = _m, __newindex = _m }) local function back(g, ok, ...) if G.print == S.print then G.print = g end if not ok then error((...), 0) end return ... end local wrapped = setmetatable({}, { __mode = "k" }) local LV = { trace = 5, debug = 4, info = 3 } local function wrap(lg) for name, lv in pairs(LV) do local orig = lg[name] if type(orig) == "function" then rawset(lg, name, function(self, ...) if dead or self ~= lg or G.print == S.print then return orig(self, ...) end local sd = type(lg.makeRecord) == "function" and type(lg.writeRecord) == "function" and lg.sharedData if sd and type(sd.level) == "number" then if sd.level < lv then return end local rec = lg:makeRecord(lv) local g = G.print G.print = S.print return back(g, pcall(lg.writeRecord, lg, rec, ...)) end local g = G.print G.print = S.print return back(g, pcall(orig, self, ...)) end) end end end S.wrap = function(lg) if type(lg) == "table" and not wrapped[lg] then wrapped[lg] = true pcall(wrap, lg) end return lg end S.flush = flushAll S.stats = function() return { init = init, run = run, redirected = redirected, dead = dead, path = path, fails = fails, started = started } end pcall(function() event.register("initialized", S.start) for _, ev in ipairs({ "loaded", "save", "menuEnter" }) do event.register(ev, function() pcall(flushAll) end) end end) pcall(function() local p = newproxy(true) getmetatable(p).__gc = function() pcall(flushAll) end S.gc = p end) return S.print, S.mwse, S end)(print, mwse) --[[/mwluafix_logshim]]

local legacyMouseBleedConfig = mwse.loadConfig("mouseBleedFix", {
    enabled = true,
    lockInMenus = true,
    lockInGame = true,
})
local config = mwse.loadConfig("mwquickfixes", {
    corruptSaveGuard = true,
    deferredDialogueTeleport = true,
    suppressBodyPartWarnings = true,
    suppressShutdownWarnings = true,
    companionSneak = true,
    companionSneakIncludeCreatures = true,
    companionSneakIncludeEscorts = true,
    companionSneakDebugMode = false,
    mouseBleedFix = legacyMouseBleedConfig.enabled ~= false,
    mouseBleedFixInMenus = legacyMouseBleedConfig.lockInMenus ~= false,
    mouseBleedFixInGame = legacyMouseBleedConfig.lockInGame ~= false,
    fadeDuration = 0.5,
    logLevel = 1,
})
-- Preserve the enabled-by-default behavior for existing mwquickfixes configs.
if config.companionSneak == nil then config.companionSneak = true end
if config.companionSneakIncludeCreatures == nil then config.companionSneakIncludeCreatures = true end
if config.companionSneakIncludeEscorts == nil then config.companionSneakIncludeEscorts = true end
if config.companionSneakDebugMode == nil then config.companionSneakDebugMode = false end
if config.mouseBleedFix == nil then config.mouseBleedFix = true end
if config.mouseBleedFixInMenus == nil then config.mouseBleedFixInMenus = true end
if config.mouseBleedFixInGame == nil then config.mouseBleedFixInGame = true end

local function readUInt32(bytes, startIdx)
    startIdx = startIdx or 1
    if not bytes or #bytes < (startIdx + 3) then return nil end
    local b1, b2, b3, b4 = bytes:byte(startIdx, startIdx + 3)
    return b1 + b2 * 256 + b3 * 65536 + b4 * 16777216
end

local function resolveSavePath(filename)
    if not filename or filename == "" then return nil end
    local clean = filename:gsub("/", "\\")
    local lower = clean:lower()
    if not (lower:match("%.ess$") or lower:match("%.tes$")) then clean = clean .. ".ess" end
    if lfs.fileexists(clean) then return clean end
    local inSaves = "saves\\" .. clean:gsub("^saves\\\\", "")
    if lfs.fileexists(inSaves) then return inSaves end
    local base = clean:match("([^\\]+)$") or clean
    local inSavesBase = "saves\\" .. base
    if lfs.fileexists(inSavesBase) then return inSavesBase end
    return inSavesBase
end

local function validateSave(filePath)
    local path = resolveSavePath(filePath)
    if not path or not lfs.fileexists(path) then return false, "Save file does not exist on disk" end
    local f, err = io.open(path, "rb")
    if not f then return false, "Could not open save file: " .. tostring(err) end
    local fileSize = f:seek("end")
    if not fileSize or fileSize < 324 then
        f:close()
        return false, string.format("Save file is incomplete or 0 bytes (%s bytes)", tostring(fileSize or 0))
    end
    f:seek("set", 0)
    local recHeader = f:read(16)
    if not recHeader or #recHeader < 16 then f:close(); return false, "Premature end of file reading TES3 header" end
    if recHeader:sub(1, 4) ~= "TES3" then
        f:close(); return false, string.format("Invalid header signature '%s' (expected TES3)", recHeader:sub(1, 4))
    end
    local hdrDataSize = readUInt32(recHeader, 5)
    if not hdrDataSize or hdrDataSize < 308 then f:close(); return false, string.format("Corrupt TES3 header record size (%s)", tostring(hdrDataSize)) end
    local subHeader = f:read(8)
    if not subHeader or #subHeader < 8 then f:close(); return false, "Premature end of file reading HEDR subrecord header" end
    if subHeader:sub(1, 4) ~= "HEDR" then
        f:close(); return false, string.format("Invalid subrecord signature '%s' (expected HEDR)", subHeader:sub(1, 4))
    end
    local subSize = readUInt32(subHeader, 5)
    if subSize ~= 300 then f:close(); return false, string.format("Invalid HEDR subrecord size (%s != 300)", tostring(subSize)) end
    local hedrBody = f:read(8)
    if not hedrBody or #hedrBody < 8 then f:close(); return false, "Premature end of file reading HEDR flags" end
    local saveFlags = readUInt32(hedrBody, 5)
    if saveFlags ~= 32 then f:close(); return false, string.format("Invalid save flags (%s != 32)", tostring(saveFlags)) end
    local offset = 16 + hdrDataSize
    while offset < fileSize do
        f:seek("set", offset)
        local chunk = f:read(8)
        if not chunk or #chunk < 8 then f:close(); return false, string.format("Premature end of file at offset %d", offset) end
        local recDataSize = readUInt32(chunk, 5)
        if not recDataSize then f:close(); return false, string.format("Corrupt record header at offset %d", offset) end
        offset = offset + 16 + recDataSize
        if offset > fileSize then f:close(); return false, string.format("Save was truncated mid-record: record requires %d bytes, but file ends at %d", recDataSize, fileSize) end
    end
    f:close()
    if offset ~= fileSize then return false, string.format("File size mismatch (expected %d bytes, got %d)", offset, fileSize) end
    return true, "Valid save"
end

local function restoreMainMenu()
    if tes3.onMainMenu() then
        local mainMenu = tes3ui.findMenu(tes3ui.registerID("MenuOptions"))
        if mainMenu then mainMenu.visible = true end
        local menuLoad = tes3ui.findMenu(tes3ui.registerID("MenuLoad"))
        if menuLoad then menuLoad.visible = true end
        tes3ui.enterMenuMode()
    end
end

local function onPreLoad(e)
    if not config.corruptSaveGuard or e.newGame then return end
    local filename = e.filename
    if not filename or filename == "" then return end
    local resolvedPath = resolveSavePath(filename)
    local isValid, reason = validateSave(resolvedPath)
    if isValid then return end
    e.block = true
    e.claim = true
    event.trigger("loadAborted")
    local saveBaseName = (resolvedPath and resolvedPath:match("([^\\]+)$")) or filename
    mwse.log("[MWManager:CorruptSaveGuard] INTERCEPTED corrupt save load: '%s' (%s). Blocking load and deleting corrupted file.", tostring(resolvedPath), reason)
    if resolvedPath and lfs.fileexists(resolvedPath) then
        local deleted = os.remove(resolvedPath)
        if deleted then mwse.log("[MWManager:CorruptSaveGuard] Removed corrupted save file: '%s'", resolvedPath)
        else mwse.log("[MWManager:CorruptSaveGuard] Warning: Could not remove file: '%s'", resolvedPath) end
    end
    restoreMainMenu()
    local okText = "OK"
    local okGmst = tes3.findGMST(tes3.gmst.sOk)
    if okGmst and okGmst.value and #okGmst.value > 0 then okText = okGmst.value end
    tes3.messageBox({
        message = string.format("Save is corrupted.\n\nFile: %s\nReason: %s\n\nThis corrupted file has been deleted to prevent crashes.", saveBaseName, reason),
        buttons = { okText }, callback = restoreMainMenu,
    })
end
event.register("load", onPreLoad, { priority = -9000 })

local pendingTeleport
local function parsePositionCell(cmd)
    if not cmd or cmd == "" then return nil end
    if not cmd:lower():find("player%s*%->%s*positioncell") then return nil end
    local journalId, journalIndex
    local jId, jIdx = cmd:match('[Jj][Oo][Uu][Rr][Nn][Aa][Ll]%s+["\']?([^"\'%s\r\n]+)["\']?%s+(%d+)')
    if jId and jIdx then journalId, journalIndex = jId, tonumber(jIdx) end
    local pattern = '[Pp][Ll][Aa][Yy][Ee][Rr]%s*%->%s*[Pp][Oo][Ss][Ii][Tt][Ii][Oo][Nn][Cc][Ee][Ll][Ll]%s+([%-%d%.]+)[%s,]+([%-%d%.]+)[%s,]+([%-%d%.]+)[%s,]+([%-%d%.]+)[%s,]+["]?([^"\r\n]+)["]?'
    local x, y, z, rot, cell = cmd:match(pattern)
    if not (x and y and z and rot and cell) then return nil end
    cell = cell:match("^%s*(.-)%s*$"):gsub('["]', "")
    return { x = tonumber(x), y = tonumber(y), z = tonumber(z), rot = tonumber(rot), cell = cell, journalId = journalId, journalIndex = journalIndex }
end
local function executePendingTeleport()
    if not pendingTeleport then return end
    local tp = pendingTeleport
    pendingTeleport = nil
    if config.logLevel >= 1 then
        mwse.log("[MWManager:DeferredDialogueTeleport] Dialogue closed. Executing teleport to '%s' (pos: %s, %s, %s; rot: %s)", tp.cell, tp.x, tp.y, tp.z, tp.rot)
    end
    timer.frame.delayOneFrame(function()
        local duration = math.max(0, config.fadeDuration or 0.5)
        if duration > 0 then tes3.fadeOut({ duration = duration }) end
        timer.start({ type = timer.simulate, duration = duration, callback = function()
            local rotZ = math.rad((tp.rot or 0) % 360)
            tes3.positionCell({ reference = tes3.player, cell = tp.cell, position = tes3vector3.new(tp.x, tp.y, tp.z), orientation = tes3vector3.new(0, 0, rotZ), teleportCompanions = true })
            if duration > 0 then tes3.fadeIn({ duration = duration }) end
        end })
    end)
end
local function onInfoResponse(e)
    if not e or not config.deferredDialogueTeleport then return end
    if not tes3ui.findMenu("MenuDialog") then return end
    local tp = parsePositionCell(e.command)
    if not tp then return end
    local actor = e.reference or tes3.player
    if not actor then return end
    local actorId = e.reference and e.reference.object and e.reference.object.id or "unknown"
    if config.logLevel >= 1 then mwse.log("[MWManager:DeferredDialogueTeleport] Intercepted PositionCell during dialogue with '%s' to '%s'.", actorId, tp.cell) end
    if tp.journalId and tp.journalIndex then tes3.setJournalIndex({ id = tp.journalId, index = tp.journalIndex }) end
    pendingTeleport = tp
    e.block = true
    tes3.runLegacyScript({ command = "goodbye", reference = actor })
end
local function onMenuDialogActivated(e)
    if not (e and e.element) then return end
    e.element:register(tes3.uiEvent.destroy, executePendingTeleport)
end
local function onMenuExit()
    if pendingTeleport then executePendingTeleport() end
end
local function onLoaded() pendingTeleport = nil end
event.register(tes3.event.infoResponse, onInfoResponse)
event.register(tes3.event.uiActivated, onMenuDialogActivated, { filter = "MenuDialog" })
event.register(tes3.event.menuExit, onMenuExit)
event.register(tes3.event.loaded, onLoaded)

local initCompanionSneak = dofile("Data Files.MWSE.mods.MWManager.modules.mwquickfixes.companion_sneak")
initCompanionSneak(config, mwse)
local initMouseBleedFix = dofile("Data Files.MWSE.mods.MWManager.modules.mwquickfixes.mouse_bleed_fix")
initMouseBleedFix(config, mwse)

local function onInitialized()
    if config.suppressBodyPartWarnings then
        mwse.memory.writeNoOperation({ address = 0x4A1B02, length = 0x21 })
        mwse.log("[MWManager:SuppressBodyPartWarnings] Suppressed missing body part warning spam.")
    end
    if config.suppressShutdownWarnings then
        mwse.memory.writeBytes({ address = 0x483DF6, bytes = { 0xEB, 0x48 } })
        mwse.memory.writeBytes({ address = 0x4EDE64, bytes = { 0xE9, 0xD3, 0x00, 0x00, 0x00, 0x90 } })
        mwse.memory.writeBytes({ address = 0x4EDF44, bytes = { 0xE9, 0xF4, 0x00, 0x00, 0x00, 0x90 } })
        local path = "Warnings.txt"
        local file = io.open(path, "r")
        if file then
            local lines, pruned = {}, 0
            for line in file:lines() do
                if line:find('^Texture ".*" count %d+%.') or line:find("^ModelList still has %d+ %a+ files%.") or line:find("^%s*%d+ %- ") then
                    pruned = pruned + 1
                else table.insert(lines, line) end
            end
            file:close()
            if pruned > 0 then
                if #lines == 0 then os.remove(path)
                else
                    local outFile = io.open(path, "w")
                    if outFile then for _, line in ipairs(lines) do outFile:write(line, "\n") end; outFile:close() end
                end
                mwse.log("[MWManager:SuppressShutdownWarnings] Pruned %d shutdown leak lines from Warnings.txt; retained %d other lines.", pruned, #lines)
            end
        end
        mwse.log("[MWManager:SuppressShutdownWarnings] Shutdown warning suppression patches applied.")
    end
end
event.register(tes3.event.initialized, onInitialized)

mwmanagerMCM.registerTab("mwquickfixes", function(template)
    local page = template:createPage({ label = "mwquickfixes" })
    page:createInfo({ text = "Toggle each quick fix independently. All six modules are enabled by default; changes take effect after restarting the game." })
    page:createOnOffButton({ label = "CorruptSaveGuard — block and remove corrupt saves", variable = mwse.mcm.createTableVariable({ id = "corruptSaveGuard", table = config }) })
    page:createOnOffButton({ label = "DeferredDialogueTeleport — wait until dialogue closes", variable = mwse.mcm.createTableVariable({ id = "deferredDialogueTeleport", table = config }) })
    page:createOnOffButton({ label = "SuppressBodyPartWarnings", variable = mwse.mcm.createTableVariable({ id = "suppressBodyPartWarnings", table = config }) })
    page:createOnOffButton({ label = "SuppressShutdownWarnings", variable = mwse.mcm.createTableVariable({ id = "suppressShutdownWarnings", table = config }) })
    page:createOnOffButton({ label = "CompanionSneak — prevent companions spoiling stealth", variable = mwse.mcm.createTableVariable({ id = "companionSneak", table = config }) })
    page:createOnOffButton({ label = "MouseBleedFix — confine cursor while Morrowind is focused", variable = mwse.mcm.createTableVariable({ id = "mouseBleedFix", table = config }) })
    page:createOnOffButton({ label = "Include creature companions", variable = mwse.mcm.createTableVariable({ id = "companionSneakIncludeCreatures", table = config }) })
    page:createOnOffButton({ label = "Include escort packages", variable = mwse.mcm.createTableVariable({ id = "companionSneakIncludeEscorts", table = config }) })
    page:createOnOffButton({ label = "CompanionSneak debug logging (restart required)", variable = mwse.mcm.createTableVariable({ id = "companionSneakDebugMode", table = config }) })
    page:createSlider({ label = "Deferred teleport fade duration (seconds)", min = 0.0, max = 2.0, step = 0.1, jump = 0.5, decimalPlaces = 1, variable = mwse.mcm.createTableVariable({ id = "fadeDuration", table = config }) })
    page:createDropdown({ label = "Deferred teleport logging", options = { { label = "Disabled", value = 0 }, { label = "Info", value = 1 } }, variable = mwse.mcm.createTableVariable({ id = "logLevel", table = config }) })
end, function() mwse.saveConfig("mwquickfixes", config) end)
