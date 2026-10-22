"""Set the web app's password. Run it yourself, on the tablet:

    python set_password.py

The password is typed hidden and never stored: only a scrypt hash goes into
.env (as WEB_PASSWORD_HASH). Restart Sloth Lobster afterwards. Running it again
replaces the old password and logs out every open session on restart.
"""
import getpass
import os
import sys

from config import ENV_FILE
from web import hash_password

MIN_LENGTH = 12

if __name__ == "__main__":
    pw = getpass.getpass(f"New web app password (at least {MIN_LENGTH} characters): ")
    if len(pw) < MIN_LENGTH:
        sys.exit(f"Too short. Use at least {MIN_LENGTH} characters, e.g. four random words.")
    if getpass.getpass("Type it again: ") != pw:
        sys.exit("They don't match. Nothing was changed.")
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    lines = [l for l in lines if not l.strip().startswith("WEB_PASSWORD_HASH=")]
    lines.append(f"WEB_PASSWORD_HASH={hash_password(pw)}")
    tmp = ENV_FILE.with_name(".env.tmp")           # owner-only from the first byte
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, ENV_FILE)
    print("🔑 Password set (only its hash is stored in .env). Restart Sloth Lobster to use it.")
