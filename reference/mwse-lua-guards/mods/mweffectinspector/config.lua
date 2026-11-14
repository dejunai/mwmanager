local CONFIG_NAME = "mweffectinspector"

local defaultConfig = {
    enabled = true,
    showVFXNodes = true,          -- Detect and log lingering particle systems and attached VFX nodes
    logToMWSE = true,             -- Mirror inspect summaries to MWSE.log
    interactivePopup = true,      -- Display an in-game MessageBox with Dispel / Purge option
    showSpells = true,            -- Inspect innate spells, abilities, powers, blights, and diseases
    showEquipment = true,         -- Inspect equipped items and their enchantments
    hotkey = {
        keyCode = tes3.scanCode.i,
        isShiftDown = false,
        isAltDown = true,
        isControlDown = false,
    },
}

local config = mwse.loadConfig(CONFIG_NAME, defaultConfig)

function config.save()
    mwse.saveConfig(CONFIG_NAME, config)
end

return config
