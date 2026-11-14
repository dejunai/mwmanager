--[[
    mwwarnsuppression
    Author: Antigravity

    Combines and manages engine warning suppression features:
      1. Body Part Warnings: Suppresses repetitive "Not able to find <BodyPart> part in <Item>"
         engine warning and associated MWSE Lua stack trace spam in MWSE.log.
      2. Shutdown Cache Warnings: Suppresses NetImmerse texture cache refcount leak dumps
         and ModelLoader NIF/KF dumps in Warnings.txt on game exit.

    Both features can be toggled independently via the Mod Config Menu (MCM) or config file.
    Legitimate warnings (missing meshes, textures, dialogue errors, missing masters) remain untouched.
]]--

local CONFIG_NAME = "mwwarnsuppression"
local defaults = {
    suppressBodyPartWarnings = true,
    suppressShutdownWarnings = true,
}
local config = mwse.loadConfig(CONFIG_NAME, defaults)

local origBytes = {}

local function applyBodyPartPatch(enable)
    if enable then
        if not origBytes.bodyPart then
            origBytes.bodyPart = mwse.memory.readBytes({ address = 0x4A1B02, length = 0x21 })
        end
        -- 0x4A1B02: LogWarning call block for "Not able to find %s part in %s.\r\n"
        mwse.memory.writeNoOperation({ address = 0x4A1B02, length = 0x21 })
        mwse.log("[mwwarnsuppression] Memory patch applied: 'Not able to find <BodyPart> part' warnings suppressed.")
    elseif origBytes.bodyPart then
        mwse.memory.writeBytes({ address = 0x4A1B02, bytes = origBytes.bodyPart })
        mwse.log("[mwwarnsuppression] Memory patch reverted: Body part warnings restored.")
    end
end

local function sanitizeWarningsFile()
    local path = "Warnings.txt"
    local file = io.open(path, "r")
    if not file then
        return
    end

    local lines = {}
    local pruned = 0
    for line in file:lines() do
        if line:find('^Texture ".*" count %d+%.') then
            pruned = pruned + 1
        elseif line:find("^ModelList still has %d+ %a+ files%.") then
            pruned = pruned + 1
        elseif line:find("^%s*%d+ %- ") then
            pruned = pruned + 1
        else
            table.insert(lines, line)
        end
    end
    file:close()

    if pruned > 0 then
        if #lines == 0 then
            os.remove(path)
            mwse.log("[mwwarnsuppression] Pruned %d shutdown leak spam lines from Warnings.txt (file was otherwise clean).", pruned)
        else
            local outFile = io.open(path, "w")
            if outFile then
                for _, l in ipairs(lines) do
                    outFile:write(l, "\n")
                end
                outFile:close()
                mwse.log("[mwwarnsuppression] Pruned %d shutdown leak spam lines from Warnings.txt; retained %d genuine warnings.", pruned, #lines)
            end
        end
    end
end

local function applyShutdownPatch(enable)
    if enable then
        if not origBytes.texture then
            origBytes.texture = mwse.memory.readBytes({ address = 0x483DF6, length = 2 })
            origBytes.nif = mwse.memory.readBytes({ address = 0x4EDE64, length = 6 })
            origBytes.kf = mwse.memory.readBytes({ address = 0x4EDF44, length = 6 })
        end
        -- Patch 1: Bypass texture cache shutdown refcount loop at 0x483DF6
        mwse.memory.writeBytes{
            address = 0x483DF6,
            bytes = { 0xEB, 0x48 },
        }
        -- Patch 2: Bypass ModelLoader NIF file dump at 0x4EDE64
        mwse.memory.writeBytes{
            address = 0x4EDE64,
            bytes = { 0xE9, 0xD3, 0x00, 0x00, 0x00, 0x90 },
        }
        -- Patch 3: Bypass ModelLoader KF file dump at 0x4EDF44
        mwse.memory.writeBytes{
            address = 0x4EDF44,
            bytes = { 0xE9, 0xF4, 0x00, 0x00, 0x00, 0x90 },
        }
        sanitizeWarningsFile()
        mwse.log("[mwwarnsuppression] Memory patches applied: shutdown texture and model cache dumps suppressed.")
    else
        if origBytes.texture then
            mwse.memory.writeBytes{ address = 0x483DF6, bytes = origBytes.texture }
        end
        if origBytes.nif then
            mwse.memory.writeBytes{ address = 0x4EDE64, bytes = origBytes.nif }
        end
        if origBytes.kf then
            mwse.memory.writeBytes{ address = 0x4EDF44, bytes = origBytes.kf }
        end
        mwse.log("[mwwarnsuppression] Memory patches reverted: Shutdown cache warnings restored.")
    end
end

local function onInitialized()
    if config.suppressBodyPartWarnings then
        applyBodyPartPatch(true)
    end
    if config.suppressShutdownWarnings then
        applyShutdownPatch(true)
    end
end
event.register("initialized", onInitialized)

-- Mod Config Menu
event.register(tes3.event.modConfigReady, function()
    local template = mwse.mcm.createTemplate({ name = "mwwarnsuppression" })
    template:saveOnClose(CONFIG_NAME, config)
    template.onClose = function()
        mwse.saveConfig(CONFIG_NAME, config)
        applyBodyPartPatch(config.suppressBodyPartWarnings)
        applyShutdownPatch(config.suppressShutdownWarnings)
    end

    local page = template:createSideBarPage({
        label = "Settings",
        description = "mwwarnsuppression cleans harmless engine warning spam from MWSE.log and Warnings.txt without hiding genuine mod issues or errors.\n\nToggle features on or off as desired.",
    })

    page:createOnOffButton({
        label = "Suppress Body Part Missing Warnings",
        description = "Suppresses Morrowind's repetitive 'Not able to find <BodyPart> part in <Item>' warning, which triggers intrusive Lua stack traces in MWSE.log when certain armor or clothing items are equipped or added.",
        variable = mwse.mcm.createTableVariable({ id = "suppressBodyPartWarnings", table = config }),
    })

    page:createOnOffButton({
        label = "Suppress Shutdown Cache Warnings",
        description = "Suppresses benign engine shutdown dumps in Warnings.txt ('Texture count' refcount leaks and ModelLoader NIF/KF dumps) while preserving all genuine in-game warnings.",
        variable = mwse.mcm.createTableVariable({ id = "suppressShutdownWarnings", table = config }),
    })

    template:register()
end)

mwse.log("[mwwarnsuppression] Initialized successfully.")

