--[[
    mwpluginpatches
    Author: Antigravity

    Unified collection of targeted fixes and quality-of-life patches for specific ESP/ESM plugins:
      1. AutoStartupConfig:
         - Morrowind Comes Alive (MCA.esm): Silently completes the initial questionnaire with configured spawn/companion settings.
         - Ebonheart Underworks (Ebonheart_Underworks.esp): Silently answers Rebirth and Thief branch prompts.
      2. psguardfix (Privileges & Services Guard Call Fix):
         - Intercepts crime/intruder scripts in non-vanilla cells (TR, modded dungeons) and dynamically assigns
           regional guards (Hlaalu, Redoran, Telvanni, Temple, Imperial) to prevent permanent black screen fadeouts.
      3. YsmirCombatShield (YARR Nord Bubble Fix):
         - Resolves the perpetual visual shield bubble on Nord NPCs by converting the passive shield into an active combat-only spell.

    Each patch automatically detects whether its target plugin is installed and defaults to enabled only if installed.
]]

local config = require("mwpluginpatches.config")
local autoStartup = require("mwpluginpatches.patches.autoStartup")
local psguardfix = require("mwpluginpatches.patches.psguardfix")
local ysmirShield = require("mwpluginpatches.patches.ysmirShield")

local function onInitialized()
    autoStartup.init()
    psguardfix.init()
    ysmirShield.init()
    mwse.log("[mwpluginpatches] Initialized successfully. Active patches: MCA AutoStartup (%s), EU AutoStartup (%s), P&S Guard Fix (%s), Ysmir Shield (%s).",
        (config.mcaEnabled and config.isMcaActive()) and "ON" or "OFF",
        (config.euEnabled and config.isEbonheartUnderworksActive()) and "ON" or "OFF",
        (config.psGuardFixEnabled and config.isPsGuardActive()) and "ON" or "OFF",
        (config.ysmirShieldEnabled and config.isYsmirActive()) and "ON" or "OFF")
end
event.register("initialized", onInitialized)

-- Register Mod Config Menu
local function registerModConfig()
    require("mwpluginpatches.mcm")
end
event.register(tes3.event.modConfigReady, registerModConfig)
