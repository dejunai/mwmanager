-- MouseBleedFix folded into MWManager's mwquickfixes module.
-- The quickfixes MCM toggle controls whether these handlers are registered.
return function(config, mwse)
    if type(config) ~= "table" or not config.mouseBleedFix then return end

    local ok, ffi = pcall(require, "ffi")
    if not ok or type(ffi) ~= "table" then
        mwse.log("[MWManager:MouseBleedFix] LuaJIT FFI is unavailable; cursor clipping was not enabled.")
        return
    end

    pcall(ffi.cdef, [[
        typedef void* MWMBF_HWND;
        typedef unsigned long MWMBF_DWORD;
        typedef int MWMBF_BOOL;
        typedef struct { long left; long top; long right; long bottom; } MWMBF_RECT;
        typedef struct { long x; long y; } MWMBF_POINT;
        MWMBF_HWND __stdcall GetForegroundWindow(void);
        MWMBF_DWORD __stdcall GetCurrentProcessId(void);
        MWMBF_DWORD __stdcall GetWindowThreadProcessId(MWMBF_HWND hWnd, MWMBF_DWORD *lpdwProcessId);
        MWMBF_BOOL __stdcall GetClientRect(MWMBF_HWND hWnd, MWMBF_RECT *lpRect);
        MWMBF_BOOL __stdcall ClientToScreen(MWMBF_HWND hWnd, MWMBF_POINT *lpPoint);
        MWMBF_BOOL __stdcall ClipCursor(const MWMBF_RECT *lpRect);
    ]])

    local loaded, user32, kernel32 = pcall(function()
        return ffi.load("user32"), ffi.load("kernel32")
    end)
    if not loaded or not user32 or not kernel32 then
        mwse.log("[MWManager:MouseBleedFix] Windows cursor APIs could not be loaded; cursor clipping was not enabled.")
        return
    end

    local processId = kernel32.GetCurrentProcessId()
    local clipped, clippedWindow = false, nil
    local clientRect = ffi.new("MWMBF_RECT")
    local screenRect = ffi.new("MWMBF_RECT")
    local topLeft = ffi.new("MWMBF_POINT")
    local processIdBuffer = ffi.new("MWMBF_DWORD[1]")

    local function releaseCursor()
        if not clipped then return end
        pcall(user32.ClipCursor, nil)
        clipped, clippedWindow = false, nil
    end

    local function clipToWindow(window)
        if user32.GetClientRect(window, ffi.cast("MWMBF_RECT *", clientRect)) == 0 then return end
        topLeft.x, topLeft.y = clientRect.left, clientRect.top
        if user32.ClientToScreen(window, ffi.cast("MWMBF_POINT *", topLeft)) == 0 then return end
        screenRect.left, screenRect.top = topLeft.x, topLeft.y
        screenRect.right = topLeft.x + (clientRect.right - clientRect.left)
        screenRect.bottom = topLeft.y + (clientRect.bottom - clientRect.top)
        if screenRect.right <= screenRect.left or screenRect.bottom <= screenRect.top then return end
        if user32.ClipCursor(ffi.cast("const MWMBF_RECT *", screenRect)) ~= 0 then
            clipped, clippedWindow = true, window
        end
    end

    local function updateCursorLock()
        if not config.mouseBleedFix then
            releaseCursor()
            return
        end
        local foreground = user32.GetForegroundWindow()
        if foreground == nil then
            releaseCursor()
            return
        end
        user32.GetWindowThreadProcessId(foreground, processIdBuffer)
        if processIdBuffer[0] ~= processId then
            releaseCursor()
            return
        end
        local inMenu = tes3.menuMode()
        if (inMenu and config.mouseBleedFixInMenus == false)
                or (not inMenu and config.mouseBleedFixInGame == false) then
            releaseCursor()
            return
        end
        if not clipped or foreground ~= clippedWindow then clipToWindow(foreground) end
    end

    local lastCheck = 0
    event.register(tes3.event.enterFrame, function()
        local now = os.clock()
        if now - lastCheck < 0.05 then return end
        lastCheck = now
        pcall(updateCursorLock)
    end)
    event.register(tes3.event.menuEnter, function()
        if config.mouseBleedFixInMenus == false then releaseCursor() end
    end)
    event.register(tes3.event.menuExit, function() pcall(updateCursorLock) end)
    event.register(tes3.event.destroy, releaseCursor)
end
