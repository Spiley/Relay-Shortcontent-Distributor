"""Local recovery command: docker compose exec relay python -m app.admin"""
import getpass
import os

from .security import password_hash
from .store import Store


def main():
    password = getpass.getpass("New Relay password (at least 12 characters): ")
    if len(password) < 12:
        raise SystemExit("Password must be at least 12 characters.")
    if password != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords did not match.")
    store = Store(os.getenv("DATA_DIR", "data"))
    store.set("password", password_hash(password))
    store.execute("DELETE FROM sessions")
    store.execute("DELETE FROM oauth")
    store.close()
    print("Password updated. Sign in again; existing sessions have been revoked.")


if __name__ == "__main__":
    main()
