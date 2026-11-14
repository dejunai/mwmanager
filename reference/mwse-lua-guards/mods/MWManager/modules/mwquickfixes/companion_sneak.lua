-- CompanionSneak integrated into the MWManager quick-fixes package.
return function(config, mwseApi)
    local log = mwseApi.Logger.new({
        name = "CompanionSneak",
        logLevel = config.companionSneakDebugMode and "DEBUG" or "INFO",
    })

    -- Optional integrations. Companion detection works without either mod.
    local soInterop
    pcall(function() soInterop = require("StormAtronach.SO.interop") end)
    local attendMe
    pcall(function() attendMe = require("AttendMeMWSE.followers") end)

    local function clearSuspicion(ref)
        if not ref or not soInterop or type(soInterop.isEnabled) ~= "function"
                or type(soInterop.clearSuspicion) ~= "function" then return end
        local ok, enabled = pcall(soInterop.isEnabled)
        if ok and enabled then pcall(soInterop.clearSuspicion, ref) end
    end

    local function actorFightsPlayer(mobile)
        if not mobile or not mobile.inCombat then return false end
        local hostiles = mobile.hostileActors
        if hostiles then
            for _, actor in ipairs(hostiles) do
                if actor and actor.reference == tes3.player then return true end
            end
        end
        return false
    end

    local function isCompanion(mobile)
        if not mobile or mobile == tes3.mobilePlayer then return false end
        local ref = mobile.reference
        if not ref then return false end
        local okValid, valid = pcall(function() return ref:isValid() end)
        if not okValid or not valid or ref.disabled or mobile.isDead then return false end
        if actorFightsPlayer(mobile) then return false end
        if not config.companionSneakIncludeCreatures and mobile.actorType == tes3.actorType.creature then
            return false
        end

        local planner = mobile.aiPlanner
        if planner then
            local ok, package = pcall(function() return planner:getActivePackage() end)
            if ok and package then
                local ptype = package.type
                local isFollow = ptype == tes3.aiPackage.follow
                local isEscort = config.companionSneakIncludeEscorts and ptype == tes3.aiPackage.escort
                if isFollow or isEscort then
                    local targetActor = package.targetActor
                    if targetActor == tes3.mobilePlayer or (targetActor and targetActor.reference == tes3.player) then
                        return true
                    end
                    local target = package.target
                    if target == tes3.player or target == tes3.mobilePlayer then return true end
                end
            end
        end

        local player = tes3.mobilePlayer
        if player and player.friendlyActors then
            for friendly in tes3.iterate(player.friendlyActors) do
                if friendly == mobile then return true end
            end
        end

        local okData, data = pcall(function() return ref.data end)
        if okData and data and data.attendMeCompanion == true then return true end

        if attendMe and type(attendMe.isFollower) == "function" then
            local ok, follows = pcall(attendMe.isFollower, ref)
            if ok and follows then return true end
        end

        if mobile.actorType == tes3.actorType.npc then
            local base = ref.baseObject
            if base and base.script then
                local s = base.script
                local hasCompanionVar = false
                local lists = { s.shortVariableNames, s.longVariableNames, s.shortVarNames, s.longVarNames }
                for _, list in ipairs(lists) do
                    if type(list) == "table" then
                        for _, name in pairs(list) do
                            if type(name) == "string" and name:lower() == "companion" then
                                hasCompanionVar = true
                                break
                            end
                        end
                    end
                    if hasCompanionVar then break end
                end
                if hasCompanionVar and ref.context then
                    local ok, value = pcall(function() return ref.context.companion end)
                    if ok and value == 1 then return true end
                end
            end
        end
        return false
    end

    local function onDetectSneak(e)
        if not config.companionSneak or not e or e.target ~= tes3.mobilePlayer then return end
        local detector = e.detector
        if not detector or not isCompanion(detector) then return end
        e.isDetected = false
        detector.isPlayerDetected = false
        detector.isPlayerHidden = true
        local ref = detector.reference
        clearSuspicion(ref)
        log:debug("Suppressed sneak detection from companion: %s", ref and ref.id or "unknown")
        return false
    end
    event.register(tes3.event.detectSneak, onDetectSneak, { priority = 2000 })

    local function onSweep()
        if not config.companionSneak or tes3.menuMode() then return end
        local player = tes3.mobilePlayer
        if not player or not player.isSneaking then return end
        local friendlies = player.friendlyActors
        if not friendlies then return end
        for mobile in tes3.iterate(friendlies) do
            if isCompanion(mobile) then
                mobile.isPlayerDetected = false
                mobile.isPlayerHidden = true
                clearSuspicion(mobile.reference)
            end
        end
    end

    local sweepStarted = false
    event.register(tes3.event.loaded, function()
        if not config.companionSneak or sweepStarted then return end
        sweepStarted = true
        timer.start({ type = timer.simulate, duration = 0.35, iterations = -1, callback = onSweep })
        log:info("CompanionSneak initialized successfully.")
    end)
end
