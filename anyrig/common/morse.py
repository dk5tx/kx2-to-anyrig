"""Morse code timing (PARIS standard): durations for status/echo estimates and the DTR fallback.

Units: one dit = 1.2 / wpm seconds; dah = 3; gap inside a character = 1; between characters = 3;
between words = 7.
"""

from __future__ import annotations

CODE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.", "G": "--.", "H": "....",
    "I": "..", "J": ".---", "K": "-.-", "L": ".-..", "M": "--", "N": "-.", "O": "---", "P": ".--.",
    "Q": "--.-", "R": ".-.", "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-", "5": ".....", "6": "-....",
    "7": "--...", "8": "---..", "9": "----.",
    "/": "-..-.", "?": "..--..", ".": ".-.-.-", ",": "--..--", "=": "-...-", "+": ".-.-.", "-": "-....-",
    "'": ".----.", ":": "---...", "(": "-.--.", ")": "-.--.-", '"': ".-..-.", "@": ".--.-.",
    # prosigns as single characters (TS-590SG KY convention)
    "[": "-...-", "_": ".-.-.", "<": ".-...", "#": "........", ">": "...-.-", "]": "-.--.",
    "\\": "-...-.-", "%": "...-.",
}


def char_units(ch: str) -> int:
    """Units of one character without the following gap (0 for unknown characters)."""
    code = CODE.get(ch.upper())
    if not code:
        return 0
    return sum(1 if e == "." else 3 for e in code) + (len(code) - 1)


def units(text: str) -> int:
    """Total units for a text including the gaps between characters and words."""
    total = 0
    prev_char = False
    for ch in text:
        if ch == " ":
            if prev_char:
                total += 7                      # word gap; extra spaces add nothing
            prev_char = False
            continue
        u = char_units(ch)
        if not u:
            continue
        if prev_char:
            total += 3
        total += u
        prev_char = True
    return total


def duration(text: str, wpm: float) -> float:
    """Seconds needed to key ``text`` at ``wpm``."""
    if wpm <= 0:
        return 0.0
    return units(text) * 1.2 / wpm


def elements(text: str) -> list[tuple[bool, int]]:
    """Key-down/key-up sequence in units: [(True, 1), (False, 1), (True, 3), ...] (DTR fallback)."""
    out: list[tuple[bool, int]] = []
    first = True
    for ch in text:
        if ch == " ":
            if out and not out[-1][0]:
                out[-1] = (False, 7)
            continue
        code = CODE.get(ch.upper())
        if not code:
            continue
        if not first:
            out.append((False, 3))
        for i, e in enumerate(code):
            if i:
                out.append((False, 1))
            out.append((True, 1 if e == "." else 3))
        first = False
    return out
