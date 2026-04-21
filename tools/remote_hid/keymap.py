"""Static table: macOS NSEvent virtual keyCode -> Linux EV_KEY code.

Translation happens on the client (Mac) so the server receives
ready-to-emit Linux input codes.  This is the correct split because
the server side is a thin uinput shim that knows nothing about
macOS.  The table covers the ANSI physical layout plus the standard
modifier / navigation / function keys.  Keys not in the table are
silently dropped at the client.

Sources for the two sides:
  - macOS virtual keyCodes: <HIToolbox/Events.h> (kVK_* constants).
  - Linux EV_KEY codes:    <linux/input-event-codes.h> (KEY_* constants).

The F13 keyCode (105) is deliberately NOT forwarded — it is the
capture-toggle hotkey on the client and must never reach the server
or it would toggle state on the remote kernel unpredictably.
"""

__version__ = "1.0.0"

from typing import Dict, Optional

# Linux EV_KEY codes inlined so this module has no runtime dep on
# python-evdev (the client runs on Mac where evdev isn't available).
# Values are stable kernel ABI; safe to hard-code.
_L = {
    "ESC": 1, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8,
    "8": 9, "9": 10, "0": 11, "MINUS": 12, "EQUAL": 13, "BACKSPACE": 14,
    "TAB": 15, "Q": 16, "W": 17, "E": 18, "R": 19, "T": 20, "Y": 21,
    "U": 22, "I": 23, "O": 24, "P": 25, "LEFTBRACE": 26, "RIGHTBRACE": 27,
    "ENTER": 28, "LEFTCTRL": 29, "A": 30, "S": 31, "D": 32, "F": 33,
    "G": 34, "H": 35, "J": 36, "K": 37, "L": 38, "SEMICOLON": 39,
    "APOSTROPHE": 40, "GRAVE": 41, "LEFTSHIFT": 42, "BACKSLASH": 43,
    "Z": 44, "X": 45, "C": 46, "V": 47, "B": 48, "N": 49, "M": 50,
    "COMMA": 51, "DOT": 52, "SLASH": 53, "RIGHTSHIFT": 54,
    "LEFTALT": 56, "SPACE": 57, "CAPSLOCK": 58,
    "F1": 59, "F2": 60, "F3": 61, "F4": 62, "F5": 63, "F6": 64,
    "F7": 65, "F8": 66, "F9": 67, "F10": 68,
    "F11": 87, "F12": 88,
    "KPENTER": 96, "RIGHTCTRL": 97, "RIGHTALT": 100,
    "HOME": 102, "UP": 103, "PAGEUP": 104, "LEFT": 105, "RIGHT": 106,
    "END": 107, "DOWN": 108, "PAGEDOWN": 109, "INSERT": 110, "DELETE": 111,
    "MUTE": 113, "VOLUMEDOWN": 114, "VOLUMEUP": 115,
    "LEFTMETA": 125, "RIGHTMETA": 126,
    "F13": 183, "F14": 184, "F15": 185, "F16": 186,
    "F17": 187, "F18": 188, "F19": 189, "F20": 190,
}

# macOS NSEvent keyCode -> Linux EV_KEY.  See module docstring.
NSEVENT_TO_EVKEY: Dict[int, int] = {
    0: _L["A"], 1: _L["S"], 2: _L["D"], 3: _L["F"], 4: _L["H"],
    5: _L["G"], 6: _L["Z"], 7: _L["X"], 8: _L["C"], 9: _L["V"],
    11: _L["B"], 12: _L["Q"], 13: _L["W"], 14: _L["E"], 15: _L["R"],
    16: _L["Y"], 17: _L["T"],
    18: _L["1"], 19: _L["2"], 20: _L["3"], 21: _L["4"],
    22: _L["6"], 23: _L["5"],
    24: _L["EQUAL"], 25: _L["9"], 26: _L["7"], 27: _L["MINUS"],
    28: _L["8"], 29: _L["0"],
    30: _L["RIGHTBRACE"], 31: _L["O"], 32: _L["U"],
    33: _L["LEFTBRACE"], 34: _L["I"], 35: _L["P"],
    36: _L["ENTER"], 37: _L["L"], 38: _L["J"], 39: _L["APOSTROPHE"],
    40: _L["K"], 41: _L["SEMICOLON"], 42: _L["BACKSLASH"],
    43: _L["COMMA"], 44: _L["SLASH"], 45: _L["N"], 46: _L["M"],
    47: _L["DOT"], 48: _L["TAB"], 49: _L["SPACE"], 50: _L["GRAVE"],
    51: _L["BACKSPACE"], 53: _L["ESC"],
    # macOS Command -> Linux META (left/right).
    54: _L["RIGHTMETA"], 55: _L["LEFTMETA"],
    56: _L["LEFTSHIFT"], 57: _L["CAPSLOCK"],
    58: _L["LEFTALT"], 59: _L["LEFTCTRL"],
    60: _L["RIGHTSHIFT"], 61: _L["RIGHTALT"], 62: _L["RIGHTCTRL"],
    # F-row.  kVK_F13 (105) is the toggle; deliberately absent.
    96: _L["F5"], 97: _L["F6"], 98: _L["F7"], 99: _L["F3"],
    100: _L["F8"], 101: _L["F9"], 103: _L["F11"],
    107: _L["F14"], 109: _L["F10"], 111: _L["F12"], 113: _L["F15"],
    117: _L["DELETE"],
    118: _L["F4"], 120: _L["F2"], 122: _L["F1"],
    # Navigation.
    115: _L["HOME"], 116: _L["PAGEUP"], 119: _L["END"],
    121: _L["PAGEDOWN"],
    123: _L["LEFT"], 124: _L["RIGHT"], 125: _L["DOWN"], 126: _L["UP"],
    # Media keys.
    72: _L["VOLUMEUP"], 73: _L["VOLUMEDOWN"], 74: _L["MUTE"],
    # Keypad Enter.
    76: _L["KPENTER"],
}

# macOS keyCode reserved as the capture-toggle.  kVK_RightOption = 0x3D.
# Chosen because Right Option is present on every Mac keyboard, is
# rarely used for shortcuts (people reach for Left Option), and has
# no latching state like Caps Lock.  Consequence: the client never
# forwards Right Alt to the server — if you need Right Alt on the
# remote, use Left Alt (kVK_Option = 58 -> Linux LEFTALT).  The client
# accepts --toggle-key to override.
TOGGLE_KEYCODE: int = 61


def translate(nsevent_keycode: int) -> Optional[int]:
    """Return the Linux EV_KEY for a macOS keyCode, or None if unmapped.

    The capture-toggle keyCode returns None so it is never forwarded
    to the server — its effect is purely client-local.
    """
    if nsevent_keycode == TOGGLE_KEYCODE:
        return None
    return NSEVENT_TO_EVKEY.get(nsevent_keycode)


def all_mapped_evkeys() -> list:
    """Return every EV_KEY the server needs to register on its uinput
    device.  Used to build the keyboard half of the composite HID.
    """
    return sorted(set(NSEVENT_TO_EVKEY.values()))
