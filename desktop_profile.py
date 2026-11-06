"""Resolve the Windows user logged into this process's desktop session."""
import ctypes
from ctypes import wintypes
import os
import winreg


def desktop_profile():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel.ProcessIdToSessionId.restype = wintypes.BOOL
    wts.WTSQuerySessionInformationW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    wts.WTSQuerySessionInformationW.restype = wintypes.BOOL
    wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    adv.LookupAccountNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
    adv.LookupAccountNameW.restype = wintypes.BOOL
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    adv.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    session = wintypes.DWORD()
    if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        raise OSError(ctypes.get_last_error(), "Cannot identify the Windows desktop session")

    def session_text(field):
        ptr, size = ctypes.c_void_p(), wintypes.DWORD()
        if not wts.WTSQuerySessionInformationW(None, session.value, field, ctypes.byref(ptr), ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "Cannot identify the signed-in desktop user")
        try:
            return ctypes.wstring_at(ptr) if ptr.value else ""
        finally:
            wts.WTSFreeMemory(ptr)

    user, domain = session_text(5), session_text(7)  # WTSUserName, WTSDomainName
    if not user:
        raise RuntimeError("No user is signed in to this Windows desktop session.")
    account = f"{domain}\\{user}" if domain else user
    sid_size, domain_size, sid_type = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
    adv.LookupAccountNameW(None, account, None, ctypes.byref(sid_size), None, ctypes.byref(domain_size), ctypes.byref(sid_type))
    if not sid_size.value:
        raise OSError(ctypes.get_last_error(), "Cannot resolve the desktop user's account")
    sid = ctypes.create_string_buffer(sid_size.value)
    ref_domain = ctypes.create_unicode_buffer(max(domain_size.value, 1))
    if not adv.LookupAccountNameW(None, account, sid, ctypes.byref(sid_size), ref_domain, ctypes.byref(domain_size), ctypes.byref(sid_type)):
        raise OSError(ctypes.get_last_error(), "Cannot resolve the desktop user's account")
    text = ctypes.c_void_p()
    if not adv.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise OSError(ctypes.get_last_error(), "Cannot resolve the desktop user's profile")
    try:
        sid_string = ctypes.wstring_at(text)
    finally:
        kernel.LocalFree(text)
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\ProfileList\\" + sid_string) as key:
        profile, _ = winreg.QueryValueEx(key, "ProfileImagePath")
    profile = os.path.expandvars(profile)
    if not os.path.isdir(profile):
        raise FileNotFoundError("The signed-in desktop user's Windows profile was not found.", profile)
    return profile
