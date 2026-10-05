import hashlib
import hmac
import ipaddress
import secrets

LOCAL_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7",
))


def local_host(hostname):
    if hostname == "localhost":
        return True
    try:
        address = ipaddress.ip_address(hostname or "")
    except ValueError:
        return False
    return address.is_loopback or any(address in network for network in LOCAL_NETWORKS)


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return salt + ":" + digest


def password_matches(password, stored):
    salt, _ = stored.split(":", 1)
    return hmac.compare_digest(password_hash(password, salt), stored)


def media_signature(store, job_id, expires):
    # Separate derived signing key; store root key stays on disk in the persistent volume.
    key = hashlib.sha256((store.root / "encryption.key").read_bytes() + b"relay-media").digest()
    return hmac.new(key, f"{job_id}:{expires}".encode(), hashlib.sha256).hexdigest()
