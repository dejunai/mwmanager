-- MWManager's MWSE utilities load as one mod and register one MCM entry.
local tabOrder = { "mwquickfixes", "mwhangwatch", "mwinfoloop", "mwsoundguard", "mwperformers",
    "mwcollide", "mwfloatfix", "mwstranded", "mwguard" }
local loadOrder = { "mwguard", "mwquickfixes", "mwhangwatch", "mwinfoloop", "mwsoundguard", "mwperformers",
    "mwcollide", "mwfloatfix", "mwstranded" }

mwmanagerMCM = { tabs = {}, saveCallbacks = {} }
function mwmanagerMCM.registerTab(id, builder, saveCallback)
    assert(not mwmanagerMCM.tabs[id], "Duplicate MWManager MCM tab: " .. tostring(id))
    mwmanagerMCM.tabs[id] = builder
    mwmanagerMCM.saveCallbacks[id] = saveCallback
end

for _, id in ipairs(loadOrder) do
    dofile("Data Files.MWSE.mods.MWManager.modules." .. id .. ".main")
end

event.register(tes3.event.modConfigReady, function()
    local template = mwse.mcm.createTemplate({ name = "MWManager" })
    for _, id in ipairs(tabOrder) do
        assert(mwmanagerMCM.tabs[id], "Missing MWManager MCM tab: " .. id)
        mwmanagerMCM.tabs[id](template)
    end
    template.onClose = function()
        for _, id in ipairs(tabOrder) do
            local save = mwmanagerMCM.saveCallbacks[id]
            if save then save() end
        end
    end
    template:register()
end)
