import sys

import pytest
from textual._xterm_parser import XTermParser

from paicli.render.tui_app import PaiCliApp
from paicli.render.windows_input import SHIFT_PRESSED, encode_windows_key


def test_windows_shift_enter_preserves_shift_modifier_for_textual_parser():
    encoded = encode_windows_key(
        character="\r",
        virtual_key_code=0x0D,
        control_key_state=SHIFT_PRESSED,
    )

    events = list(XTermParser().feed(encoded))

    assert [event.key for event in events] == ["shift+enter"]


def test_windows_plain_enter_remains_plain_enter():
    encoded = encode_windows_key(
        character="\r",
        virtual_key_code=0x0D,
        control_key_state=0,
    )

    events = list(XTermParser().feed(encoded))

    assert [event.key for event in events] == ["enter"]


def test_windows_vt_shift_enter_uses_live_shift_state_when_record_loses_modifiers():
    encoded = encode_windows_key(
        character="\r",
        virtual_key_code=0,
        control_key_state=0,
        shift_pressed=True,
    )

    events = list(XTermParser().feed(encoded))

    assert [event.key for event in events] == ["shift+enter"]


def test_windows_vt_plain_enter_stays_plain_when_live_shift_is_not_pressed():
    encoded = encode_windows_key(
        character="\r",
        virtual_key_code=0,
        control_key_state=0,
        shift_pressed=False,
    )

    events = list(XTermParser().feed(encoded))

    assert [event.key for event in events] == ["enter"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows driver selection")
def test_paicli_uses_modifier_aware_textual_driver_on_windows():
    app = PaiCliApp(cwd=".")

    assert app.driver_class.__name__ == "PaiWindowsDriver"
