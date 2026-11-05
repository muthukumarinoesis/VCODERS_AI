"""Builds minimal OLE (Compound File Binary, v3) files for tests.

Stands in for the "small checked-in samples" of spec 10, since no Office or
LibreOffice is available to make real ones. Streams are padded to 4 KB so
they live in normal sectors (no mini stream needed). Spec 12.12 still asks
for the encryption markers to be confirmed with real encrypted files.
"""

import struct

SECTOR = 512
FREE, END, FATSECT, NOSTREAM = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFF


def _dir_entry(name, kind, child=NOSTREAM, right=NOSTREAM, start=END, size=0):
    raw = (name + "\x00").encode("utf-16-le") if name else b""
    return struct.pack(
        "<64sHBBIII16sIQQIII",
        raw,
        len(raw),
        kind,
        1,
        NOSTREAM,
        right,
        child,
        b"\x00" * 16,
        0,
        0,
        0,
        start,
        size,
        0,
    )


def make_ole(streams: dict[str, bytes], storages: tuple = ()) -> bytes:
    """streams: name -> data (top level only). storages: empty storage names."""
    items = [(n, d.ljust(4096, b"\x00")) for n, d in streams.items()]
    data_sectors = [-(-len(d) // SECTOR) for _, d in items]
    n_entries = 1 + len(items) + len(storages)
    n_dir = -(-n_entries * 128 // SECTOR)
    n_fat = 1
    while n_fat * 128 < n_fat + n_dir + sum(data_sectors):
        n_fat += 1
    fat = [FATSECT] * n_fat
    dir_start = len(fat)
    fat += [dir_start + i + 1 for i in range(n_dir - 1)] + [END]
    starts = []
    for count in data_sectors:
        start = len(fat)
        starts.append(start)
        fat += [start + i + 1 for i in range(count - 1)] + [END]
    fat += [FREE] * (n_fat * 128 - len(fat))

    names = [n for n, _ in items] + list(storages)
    entries = [_dir_entry("Root Entry", 5, child=1 if names else NOSTREAM)]
    for i, (name, d) in enumerate(items):
        right = i + 2 if i + 2 <= len(names) else NOSTREAM
        entries.append(_dir_entry(name, 2, right=right, start=starts[i], size=len(d)))
    for j, name in enumerate(storages):
        i = len(items) + j
        right = i + 2 if i + 2 <= len(names) else NOSTREAM
        entries.append(_dir_entry(name, 1, right=right))
    directory = b"".join(entries).ljust(n_dir * SECTOR, b"\x00")
    # unused directory slots must be empty entries
    directory = directory[: n_entries * 128] + b"".join(
        _dir_entry("", 0) for _ in range((len(directory) - n_entries * 128) // 128)
    )

    difat = list(range(n_fat)) + [FREE] * (109 - n_fat)
    header = struct.pack(
        "<8s16sHHHHH6sIIIIIIIII109I",
        bytes.fromhex("D0CF11E0A1B11AE1"),
        b"\x00" * 16,
        0x3E,
        3,
        0xFFFE,
        9,
        6,
        b"\x00" * 6,
        0,
        n_fat,
        dir_start,
        0,
        4096,
        END,
        0,
        END,
        0,
        *difat,
    )
    body = struct.pack(f"<{len(fat)}I", *fat) + directory + b"".join(d for _, d in items)
    return header + body


def word_stream(encrypted=False, text="") -> bytes:
    flags = 0x0100 if encrypted else 0
    fib = struct.pack("<HHHHHH", 0xA5EC, 0xC1, 0, 0, 0, flags)
    return fib + b"\x00" * 500 + text.encode("utf-16-le")


def xls_stream(encrypted=False) -> bytes:
    bof = struct.pack("<HH", 0x0809, 16) + b"\x00" * 16
    filepass = struct.pack("<HH", 0x002F, 6) + b"\x00" * 6 if encrypted else b""
    other = struct.pack("<HH", 0x00E1, 2) + b"\x00\x00"
    eof = struct.pack("<HH", 0x000A, 0)
    return bof + filepass + other + eof
