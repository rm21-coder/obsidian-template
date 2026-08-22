"""quickadd_patch.py - structurally patch QuickAdd's getFolderPath() to drop
the topItems suggestion in its deterministic fall-through branch.

Called by installers/components/31-quickadd-patch.sh. See that script's
header comment for the full rationale.

The fall-through call is matched structurally, not by a hardcoded literal:
QuickAdd's minifier renames locals on every release (I/G became t/i plus a
new executor: arg as of 2.21.0), so a fixed-string match silently stops
matching the moment upstream re-minifies. The fall-through is the only
getOrCreateFolder(...) call whose first positional argument is the same
identifier as its own allowedRoots value - a structural invariant a
backreference can pin regardless of what the minifier names that identifier
this release. Only the topItems:<name> key is stripped; any other keys in
the object (executor:, or future additions) pass through untouched.

Usage: python3 quickadd_patch.py <path-to-main.js>
Prints exactly one of: PATCHED | ALREADY_PATCHED | NOT_FOUND | AMBIGUOUS:<n>
"""
import re
import sys
import pathlib

CALL_PATTERN = re.compile(r"getOrCreateFolder\((\w+),\{([^{}]*)\}\)")


def main() -> None:
    path = pathlib.Path(sys.argv[1])
    # Encoding and newlines are pinned explicitly because this helper is called
    # from the Windows installer too: there the locale default is cp1252, which
    # blows up on main.js's non-ASCII bytes, and text-mode writes would rewrite
    # every LF in the file as CRLF. Both are no-ops on macOS.
    s = path.read_text(encoding="utf-8")

    needing_patch = []
    already_patched = False

    for m in CALL_PATTERN.finditer(s):
        arg, body = m.group(1), m.group(2)
        if re.search(rf"(?:^|,)allowedRoots:{re.escape(arg)}(?:,|$)", body):
            if re.search(r"(?:^|,)topItems:\w+(?:,|$)", body):
                needing_patch.append(m)
            else:
                already_patched = True

    if len(needing_patch) == 1:
        m = needing_patch[0]
        body = m.group(2)
        new_body = re.sub(r"topItems:\w+,", "", body, count=1)
        if new_body == body:
            new_body = re.sub(r",topItems:\w+", "", body, count=1)
        new_call = f"getOrCreateFolder({m.group(1)},{{{new_body}}})"
        s = s[: m.start()] + new_call + s[m.end() :]
        path.write_text(s, encoding="utf-8", newline="")
        print("PATCHED")
    elif len(needing_patch) == 0 and already_patched:
        print("ALREADY_PATCHED")
    elif len(needing_patch) == 0:
        print("NOT_FOUND")
    else:
        print(f"AMBIGUOUS:{len(needing_patch)}")


if __name__ == "__main__":
    main()
