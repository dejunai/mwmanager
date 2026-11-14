local validator = require("mwsaveguard.validator")

-- Compatibility aliases for existing mods
package.loaded["corruptsaveguard.validator"] = validator
package.loaded["corruptsaveguard"] = true

local CONFIG_NAME = "mwsaveguard"
local defaults = {
	enabled = true,
	settleWindow = 5.0,
	validateOnLoad = true,
	logToMWSE = true,
}
local config = mwse.loadConfig(CONFIG_NAME, defaults)

local function logMsg(fmt, ...)
	if config.logToMWSE then
		mwse.log(fmt, ...)
	end
end

----------------------------------------------------------------------------------------------------
-- 1. LOAD PROTECTION: Block corrupt/truncated saves before Morrowind engine loads them
----------------------------------------------------------------------------------------------------

local function restoreMainMenu()
	if tes3.onMainMenu() then
		local mainMenu = tes3ui.findMenu(tes3ui.registerID("MenuOptions"))
		if mainMenu then
			mainMenu.visible = true
		end
		local menuLoad = tes3ui.findMenu(tes3ui.registerID("MenuLoad"))
		if menuLoad then
			menuLoad.visible = true
		end
		tes3ui.enterMenuMode()
	end
end

--- @param e loadEventData
local function onPreLoad(e)
	if not config.enabled or not config.validateOnLoad then
		return
	end

	-- Ignore new game initialization
	if e.newGame then
		return
	end

	local filename = e.filename
	if not filename or filename == "" then
		return
	end

	local resolvedPath = validator.resolveSavePath(filename)
	local isValid, reason = validator.validateSave(resolvedPath)

	if not isValid then
		-- 1. Suppress native engine load call to prevent Morrowind.exe crash / dialog freeze
		e.block = true
		e.claim = true

		-- Notify any UI listeners (such as UI Expansion) that loading was aborted
		event.trigger("loadAborted")

		local saveBaseName = (resolvedPath and resolvedPath:match("([^\\]+)$")) or filename
		mwse.log("[mwsaveguard] INTERCEPTED corrupt save load: '%s' (%s). Blocking load and deleting corrupted file.", tostring(resolvedPath), reason)

		-- 2. Delete the corrupted save file from disk
		if resolvedPath and lfs.fileexists(resolvedPath) then
			local deleted = os.remove(resolvedPath)
			if deleted then
				mwse.log("[mwsaveguard] Successfully removed corrupted save file: '%s'", resolvedPath)
			else
				mwse.log("[mwsaveguard] Warning: Could not remove file: '%s'", resolvedPath)
			end
		end

		-- 3. Restore UI / Main Menu so the user is never stuck or locked out
		restoreMainMenu()

		-- 4. Present user-facing error message
		local okText = "OK"
		local okGmst = tes3.findGMST(tes3.gmst.sOk)
		if okGmst and okGmst.value and #okGmst.value > 0 then
			okText = okGmst.value
		end

		local displayMsg = string.format("Save is corrupted.\n\nFile: %s\nReason: %s\n\nThis corrupted file has been deleted to prevent crashes.", saveBaseName, reason)
		tes3.messageBox({
			message = displayMsg,
			buttons = { okText },
			callback = function()
				restoreMainMenu()
			end
		})

		return false
	end
end

-- Priority -9000 ensures this runs right before native load call
event.register("load", onPreLoad, { priority = -9000 })


----------------------------------------------------------------------------------------------------
-- 2. SAVE STALLING / SETTLING WINDOW: Prevent save crashes during volatile world transitions
----------------------------------------------------------------------------------------------------

local lastCellChangeTime = os.clock()
local lastCharGenFinishTime = os.clock()
local isExecutingDeferredSave = false
local pendingSave = nil
local deferTimer = nil

local function onCellChanged(e)
	lastCellChangeTime = os.clock()
end
event.register("cellChanged", onCellChanged)

local function onCharGenFinished(e)
	lastCharGenFinishTime = os.clock()
end
event.register("charGenFinished", onCharGenFinished)

-- Also monitor on load so newly loaded games settle before autosaving
local function onLoaded(e)
	lastCellChangeTime = os.clock()
	if e and e.newGame then
		lastCharGenFinishTime = 0
	else
		lastCharGenFinishTime = os.clock()
	end
end
event.register("loaded", onLoaded)

-- Forward declaration
local executeDeferredSave

executeDeferredSave = function()
	deferTimer = nil
	if not pendingSave then
		return
	end

	-- Do not save on main menu
	if tes3.onMainMenu() then
		pendingSave = nil
		return
	end

	-- Wait if any background thread is loading or engine is in the middle of a transition
	local dh = tes3.dataHandler
	local isBackground = dh and dh.backgroundThreadRunning
	local ndd = dh and dh.nonDynamicData
	local isSavingOrLoading = ndd and ndd.isSavingOrLoading
	local inMenu = tes3ui.menuMode()

	if inMenu or isBackground or isSavingOrLoading then
		-- Re-check in 1.0 second until menus close and the engine is quiet
		deferTimer = timer.start({
			duration = 1.0,
			type = timer.real,
			callback = executeDeferredSave,
		})
		return
	end

	-- Check player vitality
	local mobile = tes3.mobilePlayer
	if not mobile or mobile.isDead or (mobile.health and mobile.health.current <= 0) then
		pendingSave = nil
		return
	end

	local target = pendingSave
	pendingSave = nil

	isExecutingDeferredSave = true
	logMsg("[mwsaveguard] World settled. Executing deferred save: '%s'...", tostring(target.filename or "save"))

	local saveParams = {}
	if target.filename and target.filename ~= "" then
		saveParams.file = target.filename
	end
	if target.name and target.name ~= "" then
		saveParams.name = target.name
	end

	local ok, err = pcall(tes3.saveGame, saveParams)
	isExecutingDeferredSave = false

	if not ok then
		mwse.log("[mwsaveguard] Error executing deferred save: %s", tostring(err))
	end
end

--- @param e saveEventData
local function onSave(e)
	if not config.enabled then
		return
	end

	-- If our own deferred execution triggered this, allow it straight through
	if isExecutingDeferredSave then
		return
	end

	local window = tonumber(config.settleWindow) or 5.0
	local now = os.clock()
	local cellElapsed = now - lastCellChangeTime
	local charGenElapsed = now - lastCharGenFinishTime

	-- Calculate the minimum elapsed time across all settling triggers
	local minElapsed = math.min(cellElapsed, charGenElapsed)

	if minElapsed < window then
		-- Suppress immediate engine save
		e.block = true
		e.claim = true

		local remaining = math.max(0.5, window - minElapsed)
		pendingSave = {
			filename = e.filename,
			name = e.name,
		}

		logMsg("[mwsaveguard] Intercepted save '%s' during world settling window (%.1fs remaining). Deferring save...", tostring(e.filename), remaining)

		if deferTimer then
			deferTimer:cancel()
		end

		deferTimer = timer.start({
			duration = remaining,
			type = timer.real,
			callback = executeDeferredSave,
		})

		return false
	end
end

-- Priority 10000 ensures this intercepts before any other save handlers (like SSS or native)
event.register("save", onSave, { priority = 10000 })


----------------------------------------------------------------------------------------------------
-- 3. MOD CONFIG MENU
----------------------------------------------------------------------------------------------------

event.register(tes3.event.modConfigReady, function()
	local template = mwse.mcm.createTemplate({ name = "mwsaveguard" })
	template:saveOnClose(CONFIG_NAME, config)
	template.onClose = function()
		mwse.saveConfig(CONFIG_NAME, config)
	end

	local page = template:createSideBarPage({
		label = "Settings",
		description = "mwsaveguard is a core protection tool for Morrowind saves:\n\n1. Settling Window: Defers autosaves or quicksaves following cell transitions and chargen completion until actors, inventories, and custom Lua data stabilize, eliminating save-time freeze and crash bugs.\n\n2. Load Integrity: Pre-scans save files to intercept and quarantine truncated or corrupted files before the engine attempts to load them.",
	})

	page:createOnOffButton({
		label = "Enable Save Guard",
		description = "Enables or disables mwsaveguard protection.",
		variable = mwse.mcm.createTableVariable({ id = "enabled", table = config }),
	})

	page:createSlider({
		label = "World Settling Window (seconds)",
		description = "How long mwsaveguard will defer saves following cell change or chargen completion while the scene stabilizes (default: 5.0 seconds).",
		min = 1.0,
		max = 10.0,
		step = 0.5,
		decimalPlaces = 1,
		variable = mwse.mcm.createTableVariable({ id = "settleWindow", table = config }),
	})

	page:createOnOffButton({
		label = "Validate Saves on Load",
		description = "Validates save files on load to catch corrupted saves before they crash Morrowind.",
		variable = mwse.mcm.createTableVariable({ id = "validateOnLoad", table = config }),
	})

	page:createOnOffButton({
		label = "Log Notices to MWSE.log",
		description = "Outputs notices to MWSE.log when a save is deferred or an issue is caught.",
		variable = mwse.mcm.createTableVariable({ id = "logToMWSE", table = config }),
	})

	template:register()
end)

mwse.log("[mwsaveguard] Initialized successfully. Core save integrity and settling delay guard active.")
