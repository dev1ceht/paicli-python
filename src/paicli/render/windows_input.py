"""Windows console key encoding helpers for Textual."""

from __future__ import annotations

SHIFT_PRESSED = 0x0010
VK_RETURN = 0x0D


def encode_windows_key(
    *,
    character: str,
    virtual_key_code: int,
    control_key_state: int,
    shift_pressed: bool = False,
) -> str:
    """Encode Windows Shift+Enter so Textual can distinguish it from Enter."""
    if (
        virtual_key_code in {0, VK_RETURN}
        and character in {"\r", "\n"}
        and (control_key_state & SHIFT_PRESSED or shift_pressed)
    ):
        # Kitty keyboard protocol: code point 13 with the Shift modifier.
        return "\x1b[13;2u"
    return character
