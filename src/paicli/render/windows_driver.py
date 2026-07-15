"""Windows Textual driver that preserves Shift+Enter key modifiers."""

from __future__ import annotations

from asyncio import run_coroutine_threadsafe
from ctypes import WinDLL, byref, c_int, c_short, wintypes

from textual import constants
from textual._xterm_parser import XTermParser
from textual.drivers import win32
from textual.drivers.windows_driver import WindowsDriver
from textual.events import Resize
from textual.geometry import Size

from paicli.render.windows_input import VK_RETURN, encode_windows_key

VK_SHIFT = 0x10
_USER32 = WinDLL("user32", use_last_error=True)
_GET_ASYNC_KEY_STATE = _USER32.GetAsyncKeyState
_GET_ASYNC_KEY_STATE.argtypes = [c_int]
_GET_ASYNC_KEY_STATE.restype = c_short


def _is_shift_pressed() -> bool:
    return bool(_GET_ASYNC_KEY_STATE(VK_SHIFT) & 0x8000)


class ModifierAwareEventMonitor(win32.EventMonitor):
    """Textual event monitor that retains Shift on Windows Enter events."""

    def run(self) -> None:
        exit_requested = self.exit_event.is_set
        parser = XTermParser(debug=constants.DEBUG)

        try:
            read_count = wintypes.DWORD(0)
            input_handle = win32.GetStdHandle(win32.STD_INPUT_HANDLE)

            max_events = 1024
            key_event_type = 0x0001
            window_buffer_size_event = 0x0004

            input_records = (win32.INPUT_RECORD * max_events)()
            read_console_input = win32.KERNEL32.ReadConsoleInputW
            keys: list[str] = []

            while not exit_requested():
                for event in parser.tick():
                    self.process_event(event)

                if win32.wait_for_handles([input_handle], 100) is None:
                    continue

                read_console_input(
                    input_handle,
                    byref(input_records),
                    max_events,
                    byref(read_count),
                )
                read_input_records = input_records[: read_count.value]

                keys.clear()
                new_size: tuple[int, int] | None = None

                for input_record in read_input_records:
                    event_type = input_record.EventType

                    if event_type == key_event_type:
                        key_event = input_record.Event.KeyEvent
                        if not key_event.bKeyDown:
                            continue
                        if key_event.dwControlKeyState and key_event.wVirtualKeyCode == 0:
                            continue
                        keys.append(
                            encode_windows_key(
                                character=key_event.uChar.UnicodeChar,
                                virtual_key_code=key_event.wVirtualKeyCode,
                                control_key_state=key_event.dwControlKeyState,
                                shift_pressed=(
                                    key_event.uChar.UnicodeChar in {"\r", "\n"}
                                    and key_event.wVirtualKeyCode in {0, VK_RETURN}
                                    and _is_shift_pressed()
                                ),
                            )
                        )
                    elif event_type == window_buffer_size_event:
                        size = input_record.Event.WindowBufferSizeEvent.dwSize
                        new_size = (size.X, size.Y)

                if keys:
                    encoded_keys = "".join(keys).encode("utf-16", "surrogatepass").decode("utf-16")
                    for event in parser.feed(encoded_keys):
                        self.process_event(event)
                if new_size is not None:
                    self.on_size_change(*new_size)

        except Exception as error:
            self.app.log.error("EVENT MONITOR ERROR", error)

    def on_size_change(self, width: int, height: int) -> None:
        size = Size(width, height)
        event = Resize(size, size)
        run_coroutine_threadsafe(self.app._post_message(event), loop=self.loop)


class PaiWindowsDriver(WindowsDriver):
    """Install the modifier-aware event monitor for PaiCLI on Windows."""

    def start_application_mode(self) -> None:
        original_event_monitor = win32.EventMonitor
        win32.EventMonitor = ModifierAwareEventMonitor
        try:
            super().start_application_mode()
        finally:
            win32.EventMonitor = original_event_monitor
