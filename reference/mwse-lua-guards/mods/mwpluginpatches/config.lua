local CONFIG_NAME = "mwpluginpatches"

local config = {}

--- Checks if Morrowind Comes Alive is active.
function config.isMcaActive()
    return tes3.isModActive("MCA.esm")
        or tes3.findGlobal("MCA_Loaded") ~= nil
        or (tes3.getObject("MCA_setup") ~= nil)
end

--- Checks if Ebonheart Underworks is active.
function config.isEbonheartUnderworksActive()
    return tes3.isModActive("Ebonheart_Underworks.esp")
        or tes3.findGlobal("dd19rebirthConfig") ~= nil
        or (tes3.getObject("dd19_rebirthConfigScript") ~= nil)
end

--- Checks if Privileges & Services (P&S) is active.
function config.isPsGuardActive()
    return tes3.isModActive("Privileges and Services.esp")
        or tes3.isModActive("Privileges&Services 1.1.esp")
        or tes3.isModActive("Privileges&Services - Improvement Patch.esp")
        or tes3.isModActive("Privileges&Services - Bugs Patch only.esp")
        or (tes3.findGlobal("ABM_ll_GuardCallCell") ~= nil)
        or (tes3.getObject("ABM_ll_DetectPCCrimeScript") ~= nil)
end

--- Checks if YARR (Yet Another Race Revamp / Ysmir's Shield) is active.
function config.isYsmirActive()
    return tes3.isModActive("YARR - yet another race revamp v1.2.ESP")
        or tes3.isModActive("YARR.esp")
        or (tes3.getObject("exo_nord_ysmirshield") ~= nil)
end

local defaults = {
    -- 1. AutoStartup - MCA
    mcaEnabled = config.isMcaActive(),
    mcaCompanions = true,            -- Enable companions & recruitment
    mcaCourtesans = "all",           -- "all" (both), "none" (disabled), "female" (female only), "male" (male only)
    mcaHostileDrunks = true,         -- Angry tavern patrons / hostile drunks
    mcaRespawnCaves = true,          -- Respawning hostile NPCs in caves & ruins
    mcaBanditAttacksTowns = true,    -- Bandit attacks on towns
    mcaHostileAshlanders = true,     -- Angry Ashlanders at outcast campsites
    mcaCrossroadBandits = true,      -- Bandits / highwaymen at crossroads
    mcaPartySize = 0,                -- 0 = 2-5 enemies (default), 1 = 2-10, 2 = 2-20

    -- 2. AutoStartup - Ebonheart Underworks
    euEnabled = config.isEbonheartUnderworksActive(),
    euRebirth = false,               -- false = No Rebirth (default)
    euThiefBranch = false,           -- false = Standard TG requirement, true = Any class

    -- 3. Privileges & Services Guard Call Fix
    psGuardFixEnabled = config.isPsGuardActive(),

    -- 4. Ysmir Combat Shield (YARR Nord Bubble Fix)
    ysmirShieldEnabled = config.isYsmirActive(),

    -- Diagnostics
    debugMode = false,
}

-- Load existing mwpluginpatches config or migrate from AutoStartupConfig
local loaded = mwse.loadConfig(CONFIG_NAME)
if not loaded then
    loaded = {}
    local oldAuto = mwse.loadConfig("AutoStartupConfig")
    if oldAuto then
        for k, v in pairs(oldAuto) do
            loaded[k] = v
        end
    end
end

-- Merge with dynamic defaults
for k, v in pairs(defaults) do
    if loaded[k] == nil then
        loaded[k] = v
    end
end

-- Re-sync default-to-disabled if plugin is not installed and user hasn't explicitly saved
if not config.isMcaActive() and loaded.mcaEnabled == nil then
    loaded.mcaEnabled = false
end
if not config.isEbonheartUnderworksActive() and loaded.euEnabled == nil then
    loaded.euEnabled = false
end
if not config.isPsGuardActive() and loaded.psGuardFixEnabled == nil then
    loaded.psGuardFixEnabled = false
end
if not config.isYsmirActive() and loaded.ysmirShieldEnabled == nil then
    loaded.ysmirShieldEnabled = false
end

mwse.saveConfig(CONFIG_NAME, loaded)

-- Attach helper methods to returned config table
setmetatable(loaded, { __index = config })

return loaded
