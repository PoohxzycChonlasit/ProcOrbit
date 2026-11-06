from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes


class SingleInstanceGuard:
    """Session-local named mutex plus an auto-reset event for show requests."""

    MUTEX_NAME = "Local\\ProcOrbit.Instance"
    EVENT_NAME = "Local\\ProcOrbit.Show"

    def __init__(self):
        self._kernel32 = None
        self.mutex_handle = None
        self.event_handle = None
        self.is_primary = False

    def acquire(self) -> bool:
        if os.name != "nt":
            # ProcOrbit is Windows-first; keep development imports usable elsewhere.
            self.is_primary = True
            return True

        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateMutexW.argtypes = (
            wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR
        )
        self._kernel32.CreateMutexW.restype = wintypes.HANDLE
        self._kernel32.CreateEventW.argtypes = (
            wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR
        )
        self._kernel32.CreateEventW.restype = wintypes.HANDLE
        self._kernel32.OpenEventW.argtypes = (
            wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR
        )
        self._kernel32.OpenEventW.restype = wintypes.HANDLE
        self._kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
        self._kernel32.SetEvent.restype = wintypes.BOOL
        self._kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        self._kernel32.ReleaseMutex.restype = wintypes.BOOL

        ctypes.set_last_error(0)
        handle = self._kernel32.CreateMutexW(None, True, self.MUTEX_NAME)
        error = ctypes.get_last_error()
        if not handle:
            raise OSError(error, "Could not create ProcOrbit instance mutex")
        if error == 183:  # ERROR_ALREADY_EXISTS
            self._kernel32.CloseHandle(handle)
            self._signal_existing()
            return False

        self.mutex_handle = handle
        self.event_handle = self._kernel32.CreateEventW(
            None, False, False, self.EVENT_NAME
        )
        if not self.event_handle:
            error = ctypes.get_last_error()
            self.close()
            raise OSError(error, "Could not create ProcOrbit show event")
        self.is_primary = True
        return True

    def _signal_existing(self) -> None:
        event = self._kernel32.OpenEventW(0x0002, False, self.EVENT_NAME)
        if not event:
            # The first instance may still be creating its event.
            for _ in range(5):
                time.sleep(0.04)
                event = self._kernel32.OpenEventW(0x0002, False, self.EVENT_NAME)
                if event:
                    break
        if event:
            self._kernel32.SetEvent(event)
            self._kernel32.CloseHandle(event)

    def show_requested(self) -> bool:
        if not self.event_handle or not self._kernel32:
            return False
        return self._kernel32.WaitForSingleObject(self.event_handle, 0) == 0

    def close(self) -> None:
        if not self._kernel32:
            return
        if self.event_handle:
            self._kernel32.CloseHandle(self.event_handle)
            self.event_handle = None
        if self.mutex_handle:
            self._kernel32.ReleaseMutex(self.mutex_handle)
            self._kernel32.CloseHandle(self.mutex_handle)
            self.mutex_handle = None
        self.is_primary = False
