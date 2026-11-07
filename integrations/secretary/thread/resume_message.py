"""Print the retained thread's resume message: `BRIDGE: <command>`, a blank line, then this directory's guidance.

    python3 -I resume_message.py BRIDGE_COMMAND > resume-message.txt

BRIDGE_COMMAND is the host's command for the installed bridge (its interpreter, `-I` and the bridge's path), the
line the guidance tells the thread to run every command with. The guidance's bytes follow unchanged, read from
GUIDANCE.md beside this file.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

GUIDANCE = Path(__file__).resolve().parent / "GUIDANCE.md"


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: resume_message.py BRIDGE_COMMAND", file=sys.stderr)
        return 2
    command = argv[0]
    if not command:
        print("refused: the BRIDGE command is empty", file=sys.stderr)
        return 2
    if "\n" in command:
        print("refused: the BRIDGE command holds a newline; it must be one line", file=sys.stderr)
        return 2
    message = b"BRIDGE: " + os.fsencode(command) + b"\n\n" + GUIDANCE.read_bytes()
    sys.stdout.buffer.write(message)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
