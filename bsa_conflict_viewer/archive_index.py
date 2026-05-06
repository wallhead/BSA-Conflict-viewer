from __future__ import annotations

import mmap
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Tuple


@dataclass(frozen=True)
class ArchiveEntry:
    path: str
    size: int
    compressed: bool


@dataclass(frozen=True)
class ArchiveIndex:
    path: Path
    version: int
    files: Tuple[ArchiveEntry, ...]


def parse_archive_index(path: Path) -> Optional[ArchiveIndex]:
    try:
        with path.open("rb") as handle:
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
                if data[:4] == b"BTDX":
                    parsed = _parse_ba2(path, data)
                else:
                    parsed = _parse_bsa(path, data)
    except OSError:
        return None

    return parsed


def normalize_asset_path(path: str) -> str:
    return path.replace("\x00", "").replace("/", "\\").strip("\\").lower()


def _parse_ba2(path: Path, data: mmap.mmap) -> Optional[ArchiveIndex]:
    if len(data) < 24:
        return None

    magic, version, archive_type, file_count, name_table_offset = struct.unpack_from(
        "<4sI4sIQ", data, 0
    )
    if magic != b"BTDX":
        return None

    records = []
    pos = 24
    try:
        for _ in range(file_count):
            if archive_type == b"GNRL":
                _, _, _, _, _, packed_size, unpacked_size, _ = struct.unpack_from(
                    "<I4sIIQQII", data, pos
                )
                pos += 44
            elif archive_type == b"DX10":
                _, _, _, _, _, _, packed_size, unpacked_size = struct.unpack_from(
                    "<I4sIIBBII", data, pos
                )
                pos += 32
            else:
                return None
            stored_size = packed_size if packed_size else unpacked_size
            records.append(
                {
                    "size": stored_size,
                    "compressed": packed_size != 0 and packed_size < unpacked_size,
                }
            )

        if name_table_offset <= 0 or name_table_offset >= len(data):
            return ArchiveIndex(path, version, tuple())

        entries = []
        pos = name_table_offset
        for record in records:
            if pos + 2 > len(data):
                break
            name_length = struct.unpack_from("<H", data, pos)[0]
            pos += 2
            if pos + name_length > len(data):
                break
            name = bytes(data[pos : pos + name_length]).decode("utf-8", "ignore")
            pos += name_length
            normalized = normalize_asset_path(name)
            if normalized:
                entries.append(
                    ArchiveEntry(normalized, record["size"], record["compressed"])
                )
    except (struct.error, ValueError):
        return None

    return ArchiveIndex(path, version, tuple(entries))


def _parse_bsa(path: Path, data: mmap.mmap) -> Optional[ArchiveIndex]:
    if len(data) < 36:
        return None

    try:
        (
            magic,
            version,
            _offset,
            archive_flags,
            folder_count,
            file_count,
            _folder_name_length,
            _file_name_length,
            _file_flags,
            _padding,
        ) = struct.unpack_from("<4s7I2H", data, 0)
    except struct.error:
        return None

    if magic != b"BSA\x00":
        return None

    pos = 36
    folder_records = []
    try:
        for _ in range(folder_count):
            if version == 105:
                _name_hash, count, _padding1, offset, _padding2 = struct.unpack_from(
                    "<QIIII", data, pos
                )
                pos += 24
            else:
                _name_hash, count, offset = struct.unpack_from("<QII", data, pos)
                pos += 16
            folder_records.append((count, offset))

        include_dirs = bool(archive_flags & 0x1)
        file_records = []
        for count, _offset in folder_records:
            folder_name = ""
            if include_dirs:
                folder_name, pos = _read_bzstring(data, pos)
            for _ in range(count):
                _name_hash, size_raw, _file_offset = struct.unpack_from(
                    "<QII", data, pos
                )
                pos += 16
                file_records.append(
                    (
                        folder_name,
                        size_raw & 0x3FFFFFFF,
                        bool(size_raw & 0x40000000),
                    )
                )

        file_names = []
        for _ in range(file_count):
            name, pos = _read_cstring(data, pos)
            file_names.append(name)
    except (struct.error, ValueError):
        return None

    entries = []
    for index, (folder_name, size, compressed) in enumerate(file_records):
        file_name = file_names[index] if index < len(file_names) else ""
        full_name = f"{folder_name}\\{file_name}" if folder_name else file_name
        normalized = normalize_asset_path(full_name)
        if normalized:
            entries.append(ArchiveEntry(normalized, size, compressed))

    return ArchiveIndex(path, version, tuple(entries))


def _read_cstring(data: mmap.mmap, pos: int) -> tuple[str, int]:
    end = data.find(b"\x00", pos)
    if end == -1:
        return "", len(data)
    return bytes(data[pos:end]).decode("utf-8", "ignore"), end + 1


def _read_bzstring(data: mmap.mmap, pos: int) -> tuple[str, int]:
    if pos >= len(data):
        return "", pos
    length = data[pos]
    pos += 1
    if length == 0:
        return "", pos
    end = min(pos + length, len(data))
    raw = bytes(data[pos:end])
    if raw.endswith(b"\x00"):
        raw = raw[:-1]
    value = raw.decode("utf-8", "ignore")
    pos = end
    if pos < len(data) and data[pos] == 0:
        pos += 1
    return value, pos


def unique_preserving_order(values: Iterable[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        key = value.lower()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result
