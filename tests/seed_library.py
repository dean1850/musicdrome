#!/usr/bin/env python3
"""Generate a deterministic test library.

Playwright needs real audio to exercise playback, and shipping binaries in the
repo is worse than synthesising them: these are short sine-wave WAVs written
with the standard library, then tagged with mutagen so the scanner has real
ID3 frames to parse.
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import wave
from pathlib import Path

from mutagen.id3 import TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK
from mutagen.wave import WAVE

# (artist, album, year, genre, [(track number, title, frequency)])
LIBRARY = [
    (
        "Aurora Fields",
        "Northern Lights",
        2023,
        "Ambient",
        [
            (1, "First Light", 261.63),
            (2, "Glacier Song", 293.66),
            (3, "Polar Drift", 329.63),
            (4, "Midnight Sun", 349.23),
        ],
    ),
    (
        "Aurora Fields",
        "Quiet Machines",
        2025,
        "Ambient",
        [
            (1, "Standby", 392.00),
            (2, "Idle Hum", 440.00),
            (3, "Cold Boot", 493.88),
        ],
    ),
    (
        "The Ledger Lines",
        "Paper Trails",
        2021,
        "Indie Rock",
        [
            (1, "Receipts", 523.25),
            (2, "Small Print", 587.33),
            (3, "Balance Due", 659.25),
            (4, "Audit Season", 698.46),
            (5, "Closing Entry", 783.99),
        ],
    ),
    (
        "Nadia Okonkwo",
        "Salt and Copper",
        2024,
        "Jazz",
        [
            (1, "Brass Morning", 220.00),
            (2, "Copper Rain", 246.94),
            (3, "Salt Flats", 277.18),
        ],
    ),
    (
        "Signal Drift",
        "Test Patterns",
        2022,
        "Electronic",
        [
            (1, "Colour Bars", 174.61),
            (2, "Vertical Hold", 196.00),
            (3, "Static Field", 207.65),
            (4, "Sign Off", 233.08),
        ],
    ),
]


def write_wav(path: Path, seconds: float, frequency: float) -> None:
    frames = bytearray()
    total = int(44100 * seconds)
    for i in range(total):
        # Fade the edges so nothing clicks when a player loops it
        envelope = min(1.0, i / 2000, (total - i) / 2000)
        sample = int(11000 * envelope * math.sin(2 * math.pi * frequency * i / 44100))
        frames += struct.pack("<hh", sample, sample)

    with wave.open(str(path), "w") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(bytes(frames))


def tag(path: Path, *, title: str, artist: str, album: str, track: int, year: int, genre: str) -> None:
    audio = WAVE(str(path))
    if audio.tags is None:
        audio.add_tags()
    audio.tags.add(TIT2(encoding=3, text=title))
    audio.tags.add(TPE1(encoding=3, text=artist))
    audio.tags.add(TPE2(encoding=3, text=artist))
    audio.tags.add(TALB(encoding=3, text=album))
    audio.tags.add(TRCK(encoding=3, text=str(track)))
    audio.tags.add(TDRC(encoding=3, text=str(year)))
    audio.tags.add(TCON(encoding=3, text=genre))
    audio.save()


def build(root: Path, seconds: float = 2.0) -> int:
    root.mkdir(parents=True, exist_ok=True)
    written = 0

    for artist, album, year, genre, tracks in LIBRARY:
        folder = root / artist / album
        folder.mkdir(parents=True, exist_ok=True)

        for number, title, frequency in tracks:
            path = folder / f"{number:02d} - {title}.wav"
            if not path.exists():
                write_wav(path, seconds, frequency)
                tag(
                    path,
                    title=title,
                    artist=artist,
                    album=album,
                    track=number,
                    year=year,
                    genre=genre,
                )
            written += 1

    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the Playwright test library")
    parser.add_argument("root", type=Path, help="directory to write the library into")
    parser.add_argument("--seconds", type=float, default=2.0, help="length of each track")
    args = parser.parse_args()

    count = build(args.root, args.seconds)
    print(f"seeded {count} tracks in {args.root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
