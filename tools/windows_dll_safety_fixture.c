#include <windows.h>
#include <mmsystem.h>

static HMODULE system_winmm(void) {
    static HMODULE module = NULL;
    if (!module) {
        WCHAR path[MAX_PATH];
        UINT length = GetSystemDirectoryW(path, MAX_PATH);
        if (length > 0 && length + 10 < MAX_PATH) {
            lstrcatW(path, L"\\winmm.dll");
            module = LoadLibraryW(path);
        }
    }
    return module;
}

__declspec(dllexport) MMRESULT WINAPI timeKillEvent(UINT timer_id) {
    typedef MMRESULT (WINAPI *Function)(UINT);
    Function function = (Function)GetProcAddress(system_winmm(), "timeKillEvent");
    return function ? function(timer_id) : MMSYSERR_ERROR;
}

__declspec(dllexport) MMRESULT WINAPI timeSetEvent(
    UINT delay, UINT resolution, LPTIMECALLBACK callback,
    DWORD_PTR user, UINT event) {
    typedef MMRESULT (WINAPI *Function)(UINT, UINT, LPTIMECALLBACK,
                                       DWORD_PTR, UINT);
    Function function = (Function)GetProcAddress(system_winmm(), "timeSetEvent");
    return function ? function(delay, resolution, callback, user, event)
                    : MMSYSERR_ERROR;
}

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID reserved) {
    (void)instance;
    (void)reserved;

    if (reason == DLL_PROCESS_ATTACH) {
        WCHAR marker[MAX_PATH];
        DWORD length = GetEnvironmentVariableW(
            L"GAMEINSTALLER_DLL_FIXTURE_MARKER", marker, MAX_PATH);
        if (length > 0 && length < MAX_PATH) {
            HANDLE file = CreateFileW(marker, GENERIC_WRITE, 0, NULL,
                                      CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
            if (file != INVALID_HANDLE_VALUE) {
                CloseHandle(file);
            }
        }
    }

    return TRUE;
}
