local config = require("mwpluginpatches.config")

local CONFIG_NAME = "mwpluginpatches"

local template = mwse.mcm.createTemplate({ name = "mwpluginpatches" })
template:saveOnClose(CONFIG_NAME, config)
template.onClose = function()
    mwse.saveConfig(CONFIG_NAME, config)
end

local page = template:createSideBarPage({
    label = "Settings",
    description = "mwpluginpatches unifies targeted fixes for specific plugins into one clean module.\n\nEach patch detects whether its target plugin is active in your load order and defaults to enabled only if installed.\n\nYou can manually enable or disable any patch below.",
})

----------------------------------------------------------------------------------------------------
-- Category 1: Morrowind Comes Alive (MCA)
----------------------------------------------------------------------------------------------------
local mcaInstalled = config.isMcaActive()
local mcaCat = page:createCategory(string.format("Morrowind Comes Alive (MCA.esm)  [%s]",
    mcaInstalled and "Detected Active" or "Not Installed"))

mcaCat:createOnOffButton({
    label = "Enable MCA Startup Questionnaire Auto-Config",
    description = "Silently answers the initial MCA questionnaire upon character creation and applies your chosen settings without intrusive message boxes.\n\nDefault: On if MCA.esm is active, Off if not installed.",
    variable = mwse.mcm.createTableVariable({
        id = "mcaEnabled",
        table = config,
    }),
})

mcaCat:createOnOffButton({
    label = "Enable Companions & Recruitment",
    description = "Allows recruiting MCA companion NPCs and spawns Ordinator Shareel.",
    variable = mwse.mcm.createTableVariable({
        id = "mcaCompanions",
        table = config,
    }),
})

mcaCat:createDropdown({
    label = "Courtesans in Taverns",
    description = "Select which courtesans/hookers appear in taverns.",
    options = {
        { label = "Both Male and Female (Default)", value = "all" },
        { label = "Disabled (None)", value = "none" },
        { label = "Female Only", value = "female" },
        { label = "Male Only", value = "male" },
    },
    variable = mwse.mcm.createTableVariable({
        id = "mcaCourtesans",
        table = config,
    }),
})

mcaCat:createOnOffButton({
    label = "Hostile Drunks in Taverns",
    description = "Allow angry tavern patrons / hostile drunks to spawn in bars.",
    variable = mwse.mcm.createTableVariable({
        id = "mcaHostileDrunks",
        table = config,
    }),
})

mcaCat:createOnOffButton({
    label = "Respawning Hostile Enemies in Caves & Ruins",
    description = "Allow bandit, daedra worshipper, witch, and wizard spawns to respawn in caves and ruins.",
    variable = mwse.mcm.createTableVariable({
        id = "mcaRespawnCaves",
        table = config,
    }),
})

mcaCat:createOnOffButton({
    label = "Bandit Attacks on Towns",
    description = "Allow bandit raiding parties to attack exterior settlements.",
    variable = mwse.mcm.createTableVariable({
        id = "mcaBanditAttacksTowns",
        table = config,
    }),
})

mcaCat:createOnOffButton({
    label = "Hostile Ashlanders at Campsites",
    description = "Allow hostile outcasts and mabrigash to spawn at outcast camps.",
    variable = mwse.mcm.createTableVariable({
        id = "mcaHostileAshlanders",
        table = config,
    }),
})

mcaCat:createOnOffButton({
    label = "Highwaymen at Crossroads",
    description = "Allow bandits and highwaymen to ambush travelers at road forks.",
    variable = mwse.mcm.createTableVariable({
        id = "mcaCrossroadBandits",
        table = config,
    }),
})

mcaCat:createDropdown({
    label = "Hostile Party Size",
    description = "Number of enemies that spawn in bandit or outcast raiding groups.",
    options = {
        { label = "Standard: 2-5 enemies (Default)", value = 0 },
        { label = "Challenging: 2-10 enemies", value = 1 },
        { label = "Extreme: 2-20 enemies", value = 2 },
    },
    variable = mwse.mcm.createTableVariable({
        id = "mcaPartySize",
        table = config,
    }),
})


----------------------------------------------------------------------------------------------------
-- Category 2: Ebonheart Underworks
----------------------------------------------------------------------------------------------------
local euInstalled = config.isEbonheartUnderworksActive()
local euCat = page:createCategory(string.format("Ebonheart Underworks  [%s]",
    euInstalled and "Detected Active" or "Not Installed"))

euCat:createOnOffButton({
    label = "Enable Ebonheart Underworks Startup Auto-Config",
    description = "Silently answers the Ebonheart Underworks startup prompt upon game load.\n\nDefault: On if Ebonheart_Underworks.esp is active, Off if not installed.",
    variable = mwse.mcm.createTableVariable({
        id = "euEnabled",
        table = config,
    }),
})

euCat:createOnOffButton({
    label = "Morrowind Rebirth Compatibility Mode",
    description = "Sets the prompt response for whether Morrowind Rebirth is installed (Off = standard Morrowind).",
    variable = mwse.mcm.createTableVariable({
        id = "euRebirth",
        table = config,
    }),
})

euCat:createOnOffButton({
    label = "Allow Non-Thieves in Guild Branch",
    description = "Off = Standard Thieves Guild class requirement. On = Allows any class to enter the branch.",
    variable = mwse.mcm.createTableVariable({
        id = "euThiefBranch",
        table = config,
    }),
})


----------------------------------------------------------------------------------------------------
-- Category 3: Privileges & Services Guard Call Fix
----------------------------------------------------------------------------------------------------
local psInstalled = config.isPsGuardActive()
local psCat = page:createCategory(string.format("Privileges & Services Guard Fix  [%s]",
    psInstalled and "Detected Active" or "Not Installed"))

psCat:createOnOffButton({
    label = "Enable P&S Guard Call & Black Screen Fix",
    description = "Fixes Privileges & Services screen freeze and blackout bugs in Tamriel Rebuilt and modded dungeons:\n\n1. In non-vanilla cells where P&S does not recognize the area, dynamically assigns regional guards (Hlaalu, Redoran, Telvanni, Temple, Imperial) based on architecture and cell name so guards spawn correctly.\n\n2. Safety net: If no guard spawns, automatically fades the screen back in so the game never gets stuck in a permanent black fadeout.\n\nDefault: On if Privileges & Services is active, Off if not installed.",
    variable = mwse.mcm.createTableVariable({
        id = "psGuardFixEnabled",
        table = config,
    }),
})


----------------------------------------------------------------------------------------------------
-- Category 4: Ysmir's Combat Shield (YARR Nord Bubble Fix)
----------------------------------------------------------------------------------------------------
local ysmirInstalled = config.isYsmirActive()
local ysmirCat = page:createCategory(string.format("Ysmir's Combat Shield (YARR Nord Fix)  [%s]",
    ysmirInstalled and "Detected Active" or "Not Installed"))

ysmirCat:createOnOffButton({
    label = "Enable Ysmir Combat Shield Fix",
    description = "Resolves the persistent Nord visual bubble issue from YARR (Yet Another Race Revamp):\n\n1. Removes the perpetual Shield VFX bubble from the passive racial ability so Nord NPCs in towns do not stand around enclosed in constant magic bubbles.\n\n2. Nords retain their passive Resist Frost (75%) and Resist Shock (50%), while the Shield spell is cast dynamically only during active combat and dismissed when combat ends.\n\nDefault: On if YARR is active, Off if not installed.",
    variable = mwse.mcm.createTableVariable({
        id = "ysmirShieldEnabled",
        table = config,
    }),
})

template:register()
