#!/usr/bin/env python3
"""
SCD1 Buddy List Extractor v1.0
Dynamically finds the user block in any SoulseekQt .scd1 file by scanning for
the known binary structure rather than using hardcoded offsets.

Structure per entry:
    <username bytes> | <tag byte> | \\x11\\x00\\x00 | <uint32 LE: next len> | <next username> ...

Usage:
    python scd1_to_nicotine.py your_file.scd1
    python scd1_to_nicotine.py your_file.scd1 --merge
    python scd1_to_nicotine.py your_file.scd1 --merge --config /path/to/config
    python scd1_to_nicotine.py your_file.scd1 --output-dir my_output/
"""

import ast
import os
import re
import struct
import sys
import argparse
from pathlib import Path

# ─────────────────────────────────────────────────────────────
# Dynamic user block detection
# ─────────────────────────────────────────────────────────────

SEPARATOR = b"\x11\x00\x00"  # fixed 3-byte separator after each tag byte


def is_plausible_username(s):
    """Check if a string looks like a real Soulseek username."""
    if not s or len(s) < 2 or len(s) > 50:
        return False
    if not all(0x20 <= ord(c) <= 0x7E for c in s):
        return False
    if not any(c.isalpha() for c in s):
        return False
    # Reject file paths
    if any(s.startswith(p) for p in ("Z:", "C:", "D:", "E:", "/", "\\")):
        return False
    if ":\\" in s or ":/" in s:
        return False
    # Reject settings key fragments (all lowercase + underscore, no spaces)
    if "_" in s and s == s.lower() and " " not in s and len(s.split("_")) >= 2:
        return False
    return True


def find_user_blocks(data):
    """
    Scan for \\x11\\x00\\x00 separator pattern. For each valid hit,
    use the stored next_len to start a block walk directly.
    Returns list of (start_offset, first_username_length) tuples.
    """
    candidates = []
    n = len(data)
    search_start = 0

    while True:
        idx = data.find(SEPARATOR, search_start)
        if idx < 0:
            break

        len_pos = idx + 3
        if len_pos + 4 >= n:
            search_start = idx + 1
            continue

        next_len = struct.unpack_from("<I", data, len_pos)[0]
        if not (2 <= next_len <= 50):
            search_start = idx + 1
            continue

        username_start = len_pos + 4
        if username_start + next_len >= n:
            search_start = idx + 1
            continue

        try:
            username = data[username_start : username_start + next_len].decode(
                "ascii", errors="strict"
            )
        except UnicodeDecodeError:
            search_start = idx + 1
            continue

        if not is_plausible_username(username):
            search_start = idx + 1
            continue

        # Verify ANOTHER separator follows this username (+1 for tag byte)
        next_sep_pos = username_start + next_len + 1
        for next_sep in (b"\x11\x00\x00", b"\x00\x00\x00"):
            if (
                next_sep_pos + 3 < n
                and data[next_sep_pos : next_sep_pos + 3] == next_sep
            ):
                candidates.append((username_start, next_len))
                break

        search_start = idx + 1

    return candidates


def _find_block_start(data, sep_offset):
    """
    Given a separator offset, try to find the start of the user block
    by walking backwards to find the first entry.
    """
    # The entry ending at sep_offset looks like:
    # <username bytes> <tag byte> <sep_offset points here>
    # Walk back to find where printable chars start
    tag_pos = sep_offset - 1
    if tag_pos < 0:
        return None

    # Walk back through username bytes (printable ASCII)
    pos = tag_pos - 1
    while pos >= 0 and 0x20 <= data[pos] <= 0x7E:
        pos -= 1

    # pos now points to byte BEFORE the username
    username_start = pos + 1
    username_len = tag_pos - username_start

    if not (2 <= username_len <= 30):
        return None

    try:
        username = data[username_start:tag_pos].decode("ascii", errors="strict")
        if not is_plausible_username(username):
            return None
    except UnicodeDecodeError:
        return None

    return (username_start, username_len)


def extract_usernames_dynamic(data):
    """
    Find all user blocks dynamically and extract usernames from them.
    Returns list of (tag_byte, username) tuples, deduplicated.
    """
    print("   Scanning for user block structure...")
    block_starts = find_user_blocks(data)

    if not block_starts:
        print("   No user blocks found!")
        return []

    # Collect all unique starting points and walk each block
    all_users = []
    seen_names = set()
    seen_starts = set()

    for start_offset, first_len in block_starts:
        if start_offset in seen_starts:
            continue

        # Walk forward from this starting point
        block_users = walk_user_block(data, start_offset, first_len)

        if len(block_users) < 2:
            # Single entry probably isn't a real user block
            continue

        seen_starts.add(start_offset)

        for tag, name in block_users:
            if name not in seen_names:
                all_users.append((tag, name))
                seen_names.add(name)
                seen_starts.add(start_offset)

    return all_users


def walk_user_block(data, start_offset, first_len):
    """
    Walk a user block starting at start_offset with known first_len.
    Returns list of (tag_byte, username) tuples.

    Two separator formats exist in SCD1:
      Format A: <username> <tag> \\x11\\x00\\x00 <uint32 next_len> <next username>
      Format B: <username> <tag> \\x00\\x00\\x00 <uint32 next_len> <next username>

    Username is appended BEFORE reading next_len so the last entry
    in a block is never lost even if next_len is garbage. (fixed)
    """
    pos = start_offset
    next_len = first_len
    users = []
    n = len(data)
    max_users = 500

    while pos < n and 0 < next_len <= 50 and len(users) < max_users:
        if pos + next_len >= n:
            break

        try:
            username = data[pos : pos + next_len].decode("ascii", errors="strict")
        except UnicodeDecodeError:
            break

        # Tag byte immediately follows username
        tag_byte = data[pos + next_len]

        # Try both separator formats
        found = False
        for sep in (b"\x11\x00\x00", b"\x00\x00\x00"):
            sep_pos = pos + next_len + 1
            if sep_pos + 3 > n:
                continue
            if data[sep_pos : sep_pos + 3] != sep:
                continue
            len_pos = sep_pos + 3
            if len_pos + 4 > n:
                # No next length - append current and stop
                if is_plausible_username(username):
                    users.append((tag_byte, username))
                return users

            next_len_val = struct.unpack_from("<I", data, len_pos)[0]

            # Append current username BEFORE advancing
            if is_plausible_username(username):
                users.append((tag_byte, username))

            # If next_len is garbage, stop cleanly
            if not (0 < next_len_val <= 50):
                return users

            next_len = next_len_val
            pos = len_pos + 4
            found = True
            break

        if not found:
            break

    return users


# ─────────────────────────────────────────────────────────────
# Note extraction
# ─────────────────────────────────────────────────────────────


def extract_notes(data, tag_to_username):
    """
    Find user notes and map them to usernames via pointer table.
    Returns dict: {username: note_text}
    """
    note_idx_to_text = {}
    pos = 0
    n = len(data)
    while pos < n - 8:
        idx_val = struct.unpack_from("<I", data, pos)[0]
        if 256 <= idx_val <= 512:
            str_len = struct.unpack_from("<I", data, pos + 4)[0]
            if 1 <= str_len <= 500:
                text_start = pos + 8
                if text_start + str_len <= n:
                    try:
                        text = data[text_start : text_start + str_len].decode(
                            "utf-8", errors="strict"
                        )
                        if all(c.isprintable() or c in "\n\r\t" for c in text):
                            note_idx_to_text[idx_val] = text
                            pos = text_start + str_len
                            continue
                    except (UnicodeDecodeError, ValueError):
                        pass
        pos += 1

    note_idx_to_tag = {}
    pos = 0
    while pos < n - 8:
        idx_val = struct.unpack_from("<I", data, pos)[0]
        if idx_val in note_idx_to_text:
            ptr_val = struct.unpack_from("<I", data, pos + 4)[0]
            low_byte = (ptr_val & 0xFF) + 1
            if low_byte in tag_to_username:
                high_byte = (ptr_val >> 8) & 0xFF
                if high_byte == 0x11:
                    note_idx_to_tag[idx_val] = low_byte
        pos += 1

    username_to_note = {}
    for note_idx, tag_byte in note_idx_to_tag.items():
        username = tag_to_username[tag_byte]
        note_text = note_idx_to_text[note_idx]
        if any(
            note_text.startswith(p)
            for p in ("Z:", "C:", "D:", "/", "http", "ftp", "\\")
        ):
            continue
        if ":\\" in note_text or ":/" in note_text:
            continue
        username_to_note[username] = note_text

    return username_to_note


# ─────────────────────────────────────────────────────────────
# Nicotine+ config
# ─────────────────────────────────────────────────────────────


def find_nicotine_config():
    candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            candidates.append(Path(appdata) / "nicotine" / "config")
    elif sys.platform == "darwin":
        candidates.append(
            Path.home() / "Library" / "Application Support" / "nicotine" / "config"
        )
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", "")
        if xdg:
            candidates.append(Path(xdg) / "nicotine" / "config")
        candidates.append(Path.home() / ".config" / "nicotine" / "config")
    for path in candidates:
        if path.exists():
            return path
    return None


def parse_userlist_from_config(config_path):
    content = config_path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^userlist\s*=\s*(\[.*\])\s*$", content, re.MULTILINE)
    if not match:
        return []
    try:
        return ast.literal_eval(match.group(1))
    except Exception as e:
        print(f"   Could not parse userlist: {e}")
        return []


def merge_userlists(existing_entries, new_usernames, username_to_note):
    existing_lower = {e[0].lower(): e for e in existing_entries}
    merged = list(existing_entries)
    added = []
    for username in new_usernames:
        if username.lower() not in existing_lower:
            note = username_to_note.get(username, "")
            merged.append([username, note, False, False, False, "Never seen", ""])
            existing_lower[username.lower()] = True
            added.append((username, note))
    return sorted(merged, key=lambda e: e[0].lower()), added


def format_nicotine_userlist(entries):
    parts = []
    for e in entries:

        def fmt(v):
            if isinstance(v, bool):
                return str(v)
            if isinstance(v, str):
                return "'" + v.replace("'", "\\'") + "'"
            return str(v)

        parts.append("[" + ", ".join(fmt(f) for f in e) + "]")
    return "userlist = [" + ", ".join(parts) + "]"


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Extract Soulseek buddy list + notes from SCD1 -> Nicotine+ config"
    )
    parser.add_argument("scd1_file", help="Path to your .scd1 file")
    parser.add_argument(
        "--output-dir",
        "-o",
        default="scd1_extracted",
        help="Output directory (default: scd1_extracted/)",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="Merge with existing Nicotine+ config (auto-detected)",
    )
    parser.add_argument(
        "--config", help="Path to Nicotine+ config file (overrides auto-detect)"
    )
    args = parser.parse_args()

    scd1_path = Path(args.scd1_file)
    if not scd1_path.exists():
        print(f"File not found: {scd1_path}")
        return

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nSCD1 Buddy Extractor v12")
    print(f"  File:       {scd1_path}")
    print(f"  Output dir: {output_dir.resolve()}\n")

    with open(scd1_path, "rb") as f:
        data = f.read()
    print(f"  File size: {len(data):,} bytes\n")
    print("-" * 55)

    # Extract usernames
    print("Extracting usernames...")
    user_tuples = extract_usernames_dynamic(data)
    tag_to_username = {tag: name for tag, name in user_tuples}
    usernames = [name for _, name in user_tuples]

    if not usernames:
        print("  No usernames found. The file format may be unsupported.")
        print("  Please open an issue with your SCD1 file version.")
        return

    for name in usernames:
        print(f"   + {name}")

    # Extract notes
    print(f"\nExtracting user notes...")
    username_to_note = extract_notes(data, tag_to_username)
    if username_to_note:
        for username, note in username_to_note.items():
            print(f"   + {username}: {repr(note)}")
    else:
        print("   (no notes found)")

    # Merge if requested
    existing_entries = []
    if args.merge:
        config_path = Path(args.config) if args.config else find_nicotine_config()
        if config_path and config_path.exists():
            print(f"\nMerging with Nicotine+ config: {config_path}")
            existing_entries = parse_userlist_from_config(config_path)
            print(f"   Existing buddies: {len(existing_entries)}")
        elif args.config:
            print(f"\nConfig not found at: {args.config}")
        else:
            print(f"\nNicotine+ config not found automatically.")
            print(f"Try: --config /path/to/nicotine/config")

    # Build final list
    sorted_usernames = sorted(set(usernames), key=str.lower)

    if existing_entries:
        final_entries, added = merge_userlists(
            existing_entries, sorted_usernames, username_to_note
        )
        print(f"\n   Added {len(added)} new users from SCD1:")
        for username, note in added:
            print(f"     + {username}" + (f"  (note: {repr(note)})" if note else ""))
        print(f"   Total after merge: {len(final_entries)}")
    else:
        final_entries = [
            [u, username_to_note.get(u, ""), False, False, False, "Never seen", ""]
            for u in sorted_usernames
        ]

    # Plain list
    plain_path = output_dir / "buddies.txt"
    with open(plain_path, "w", encoding="utf-8") as f:
        f.write("# Soulseek buddy list extracted from SCD1\n")
        f.write("# Sorted A-Z, one username per line.\n\n")
        for u in sorted_usernames:
            note = username_to_note.get(u, "")
            if note:
                f.write(f"{u}  # {note}\n")
            else:
                f.write(f"{u}\n")
    print(f"\n  Plain list:      {plain_path}")

    # Nicotine+ userlist line
    userlist_line = format_nicotine_userlist(final_entries)
    nicotine_path = output_dir / "nicotine_userlist.txt"
    with open(nicotine_path, "w", encoding="utf-8") as f:
        f.write("# Paste this line into your Nicotine+ config file\n")
        f.write(
            "# under the [server] section, replacing the existing userlist = line.\n"
        )
        f.write("#\n")
        f.write("# BACK UP YOUR CONFIG FILE FIRST!\n")
        f.write("# Windows: %APPDATA%\\nicotine\\config\n")
        f.write("# Linux:   ~/.config/nicotine/config\n")
        f.write("# Mac:     ~/Library/Application Support/nicotine/config\n")
        f.write("#\n")
        f.write("# Nicotine+ must be CLOSED when you edit the config,\n")
        f.write("# otherwise it overwrites your changes on exit.\n\n")
        f.write(userlist_line + "\n")
    print(f"  Nicotine config: {nicotine_path}")

    print(f"\n{'-' * 55}")
    print(f"  {len(sorted_usernames)} usernames extracted (sorted A-Z)\n")
    for u in sorted_usernames:
        note = username_to_note.get(u, "")
        if note:
            print(f"  {u:<25}  # {note}")
        else:
            print(f"  {u}")

    print(f"\n{'-' * 55}")
    print("HOW TO IMPORT INTO NICOTINE+:")
    print("  1. Close Nicotine+ completely")
    print("  2. Open your config file in a text editor")
    print("  3. Find the [server] section")
    print(
        "  4. Replace the 'userlist = [...]' line with the line from nicotine_userlist.txt"
    )
    print("  5. Save and reopen Nicotine+")
    print("  6. Buddy list populated!\n")


if __name__ == "__main__":
    main()
