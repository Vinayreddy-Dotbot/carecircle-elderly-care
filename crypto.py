"""Encryption at rest, keyed lookups and secret hashing.

- Field encryption: AES-256-GCM, stored as "enc1:<key id>:<base64(nonce + ciphertext)>".
- Blind index: HMAC-SHA256 so an encrypted email can still be looked up without storing it in clear.
- Access codes: scrypt with a per-member random salt.
- Session tokens and one-time codes: random, stored only as keyed hashes.

The master key comes from DATA_KEY (base64, 32 bytes). Previous keys can be listed in DATA_KEY_PREVIOUS
(comma separated) while `python manage.py rotate-key` re-encrypts the data.
In development, if DATA_KEY is unset, a key file is created next to the database. Production refuses to start without DATA_KEY.
"""
import base64
import functools
import hashlib
import hmac
import os
import secrets
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

import config

PREFIX = "enc1:"
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1


class DecryptionError(Exception):
    pass


def generate_key():
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def parse_key(raw):
    raw = raw.strip()
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception:
        raise ValueError("it must be base64") from None
    if len(key) != 32:
        raise ValueError("it must decode to exactly 32 bytes")
    return key


def _dev_key_path():
    return config.db_path() + ".devkey"


_dev_cache = {}


def _read_dev_key(path):
    """Contents of the development key file, or None. Cached so it isn't read from disk on every field."""
    if path not in _dev_cache:
        if not os.path.exists(path):
            return None
        with open(path) as f:
            _dev_cache[path] = f.read().strip()
    return _dev_cache[path]


def _raw_master():
    raw = config.get("DATA_KEY")
    if raw:
        return raw.strip()
    if config.is_production():
        raise RuntimeError("DATA_KEY is required in production")
    path = _dev_key_path()
    existing = _read_dev_key(path)
    if existing:
        return existing
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(generate_key())
    except FileExistsError:  # another process created it first
        time.sleep(0.2)
    _dev_cache.pop(path, None)
    return _read_dev_key(path)


def key_source():
    return (f"DATA_KEY in {config.source('DATA_KEY')}" if config.get("DATA_KEY")
            else f"the development key file {_dev_key_path()}")


def dev_key_alongside():
    """True when DATA_KEY is set in development but data was first created with the development key file."""
    return not config.is_production() and bool(config.get("DATA_KEY")) and bool(_read_dev_key(_dev_key_path()))


@functools.lru_cache(maxsize=8)
def _derive(raw):
    master = parse_key(raw)

    def sub(info):
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(master)

    kid = hashlib.sha256(master).hexdigest()[:8]
    return kid, AESGCM(sub(b"carecircle field encryption v1")), sub(b"carecircle blind index v1")


def _current():
    return _derive(_raw_master())


def previous_keys():
    """Keys that may still be needed to read older data. Not config.get_list: base64 keys are case-sensitive.
    In development, the key file made before DATA_KEY was set is included, so adding DATA_KEY later doesn't lock
    you out of existing data (run `python manage.py rotate-key` to move it all to DATA_KEY)."""
    keys = [p.strip() for p in config.get("DATA_KEY_PREVIOUS").split(",") if p.strip()]
    if dev_key_alongside():
        dev = _read_dev_key(_dev_key_path())
        if dev not in keys:
            keys.append(dev)
    return keys


def kid_of(raw):
    return _derive(raw)[0]


def stored_kid(value):
    """The key id a stored value was encrypted with, or None."""
    if not is_encrypted(value):
        return None
    return value[len(PREFIX):].split(":", 1)[0]


def mismatch_message(expected_kid):
    prev = []
    for raw in previous_keys():
        try:
            prev.append(kid_of(raw))
        except Exception:
            pass
    return (f"The database was encrypted with key {expected_kid}, but the key in use is {key_id()} "
            f"(from {key_source()})" + (f", and the previous keys are {', '.join(prev)}" if prev else "") + ".")


def _all_keys():
    keys = {}
    kid, aes, _ = _current()
    keys[kid] = aes
    for raw in previous_keys():
        k, a, _ = _derive(raw)
        keys.setdefault(k, a)
    return keys


def key_id():
    return _current()[0]


@functools.lru_cache(maxsize=8)
def _backup_aead(raw):
    master = parse_key(raw)
    sub = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"carecircle backup v1").derive(master)
    return hashlib.sha256(master).hexdigest()[:8], AESGCM(sub)


def backup_key():
    """(key id, AESGCM) for encrypting backup files with the current key."""
    return _backup_aead(_raw_master())


def backup_key_for(kid):
    for raw in [_raw_master(), *previous_keys()]:
        k, aead = _backup_aead(raw)
        if k == kid:
            return aead
    raise DecryptionError(f"this backup was made with key {kid}, which is not DATA_KEY or in DATA_KEY_PREVIOUS")


def enc(value):
    """Encrypt a text value. None and empty strings are stored as they are."""
    if value is None or value == "":
        return value
    value = str(value)
    if value.startswith(PREFIX):
        return value
    kid, aes, _ = _current()
    nonce = os.urandom(12)
    ct = aes.encrypt(nonce, value.encode("utf-8"), None)
    return f"{PREFIX}{kid}:{base64.b64encode(nonce + ct).decode()}"


def is_encrypted(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def dec(value):
    if not is_encrypted(value):
        return value
    try:
        kid, blob = value[len(PREFIX):].split(":", 1)
        cur_kid, cur_aes, _ = _current()
        aes = cur_aes if kid == cur_kid else _all_keys().get(kid)
        if aes is None:
            raise DecryptionError(f"No available key matches key {kid}. Check DATA_KEY and DATA_KEY_PREVIOUS")
        raw = base64.b64decode(blob)
        return aes.decrypt(raw[:12], raw[12:], None).decode("utf-8")
    except DecryptionError:
        raise
    except Exception as e:
        raise DecryptionError(f"could not decrypt a stored value ({type(e).__name__}); check DATA_KEY") from None


def needs_reencrypt(value):
    return is_encrypted(value) and not value.startswith(f"{PREFIX}{key_id()}:")


def index(value):
    """Deterministic keyed hash for lookups (emails, dedupe keys, one-time codes)."""
    _, _, ikey = _current()
    return hmac.new(ikey, str(value).encode("utf-8"), hashlib.sha256).hexdigest()


def index_with(raw_key, value):
    _, _, ikey = _derive(raw_key)
    return hmac.new(ikey, str(value).encode("utf-8"), hashlib.sha256).hexdigest()


def normalize_email(email):
    return (email or "").strip().lower()


def email_index(email):
    e = normalize_email(email)
    return index("email:" + e) if e else ""


# ---------------------------------------------------------------- secrets
def hash_secret(secret):
    salt = os.urandom(16)
    dk = hashlib.scrypt(secret.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(SCRYPT_N, SCRYPT_R, SCRYPT_P,
                                          base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


_DUMMY = None


def verify_secret(secret, stored):
    """Constant-time check. Runs a full scrypt even when `stored` is missing, so timing does not reveal it."""
    global _DUMMY
    if not stored:
        if _DUMMY is None:
            _DUMMY = hash_secret("dummy-value")
        stored, secret = _DUMMY, secret + "x"
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        assert algo == "scrypt"
        test = hashlib.scrypt(secret.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                              dklen=len(base64.b64decode(dk)))
        return hmac.compare_digest(test, base64.b64decode(dk))
    except Exception:
        return False


def token():
    return secrets.token_urlsafe(32)


def token_hash(tok):
    return hashlib.sha256(tok.encode()).hexdigest()
