"""Authenticated, streaming encryption for BibiTasks backup bundles.

The key is supplied only through a file path.  Key bytes are never accepted on
the command line or included in manifests/reports.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import stat
import shutil
import tarfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


BACKUP_FORMAT = "bibitasks-encrypted-backup-v1"
KEY_FORMAT = "bibitasks-backup-key-v1"
ENCRYPTION_METHOD = "AES-256-GCM"
KEY_VERSION_RE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PAYLOAD_NAME = "payload.tar.aes256gcm"
DEV_ENVIRONMENTS = {"dev", "development", "test", "testing"}


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def require_explicit_dev_environment(feature: str) -> None:
    environment = (os.environ.get("BIBITASKS_ENVIRONMENT", "") or "").strip().lower()
    if environment not in DEV_ENVIRONMENTS:
        raise RuntimeError(f"{feature} is allowed only in an explicit dev/test environment")


def _mountinfo_unescape(value: str) -> str:
    for encoded, decoded in (("\\040", " "), ("\\011", "\t"),
                             ("\\012", "\n"), ("\\134", "\\")):
        value = value.replace(encoded, decoded)
    return value


def require_memory_backed_temp(path: Path) -> Path:
    """Prove a plaintext scratch root is tmpfs/ramfs in this mount namespace."""
    path = path.expanduser()
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ValueError("plaintext scratch must be an existing absolute non-symlink directory")
    resolved = path.resolve()
    if os.name != "posix" or not Path("/proc/self/mountinfo").is_file():
        raise RuntimeError("memory-backed plaintext scratch cannot be attested")
    matches = []
    try:
        lines = Path("/proc/self/mountinfo").read_text("utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("memory-backed plaintext scratch cannot be attested") from exc
    for line in lines:
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        fields = left.split()
        after = right.split()
        if len(fields) < 6 or not after:
            continue
        mountpoint = Path(_mountinfo_unescape(fields[4])).resolve()
        try:
            resolved.relative_to(mountpoint)
        except ValueError:
            continue
        matches.append((len(mountpoint.parts), mountpoint, after[0], fields[5]))
    if not matches:
        raise RuntimeError("plaintext scratch mount cannot be identified")
    _, mountpoint, fstype, options = max(matches, key=lambda item: item[0])
    if fstype not in {"tmpfs", "ramfs"}:
        raise RuntimeError("plaintext scratch is not memory-backed")
    if "rw" not in set(options.split(",")):
        raise RuntimeError("plaintext scratch mount is not writable")
    if resolved != mountpoint:
        raise RuntimeError("plaintext scratch must be the attested mount root")
    return resolved


def cleanup_private_tree(path: Path, allowed_parent: Path, label: str) -> None:
    """Remove one bounded scratch tree and make cleanup failure explicit."""
    candidate = Path(os.path.abspath(path))
    parent = Path(os.path.abspath(allowed_parent))
    if candidate == parent or not candidate.is_relative_to(parent):
        raise RuntimeError(f"refusing unsafe {label} cleanup")
    try:
        info = candidate.lstat()
    except FileNotFoundError:
        return
    try:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            candidate.unlink()
        else:
            shutil.rmtree(candidate)
    except OSError as exc:
        raise RuntimeError(f"{label} cleanup failed") from exc
    if candidate.exists() or candidate.is_symlink():
        raise RuntimeError(f"{label} cleanup was incomplete")


def _secure_read(path: Path, *, expected_uid: int | None = None) -> bytes:
    """Read a small private regular file without following a swapped symlink."""
    path = path.expanduser()
    if not path.is_absolute():
        raise ValueError("backup key file path must be absolute")
    try:
        before = path.lstat()
    except OSError as exc:
        raise ValueError("backup key file is missing or unreadable") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError("backup key file must be a regular non-symlink file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("backup key file is missing or unreadable") from exc
    try:
        info = os.fstat(descriptor)
        after = path.lstat()
        identity = lambda value: (value.st_dev, value.st_ino)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(after.st_mode)
            or identity(before) != identity(info)
            or identity(after) != identity(info)
            or info.st_nlink != 1
        ):
            raise ValueError("backup key file changed during secure open")
        if info.st_size > 4096:
            raise ValueError("backup key file is unexpectedly large")
        if os.name != "nt":
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise PermissionError("backup key file permissions must be 0600 or stricter")
            if expected_uid is not None and info.st_uid != expected_uid:
                raise PermissionError("backup key file has the wrong owner")
        raw = bytearray()
        while True:
            chunk = os.read(descriptor, 4096)
            if not chunk:
                break
            raw.extend(chunk)
    finally:
        os.close(descriptor)
    return bytes(raw)


def load_backup_key(
    path: Path, *, expected_version: str | None = None,
    expected_uid: int | None = None,
) -> tuple[bytes, str]:
    """Load and validate a versioned 256-bit key from a private JSON file."""
    raw = _secure_read(path, expected_uid=expected_uid)
    try:
        document = json.loads(raw.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("backup key file is not canonical JSON") from exc
    if not isinstance(document, dict) or set(document) != {
        "format", "key_version", "key_b64",
    }:
        raise ValueError("backup key file has an invalid contract")
    version = document.get("key_version")
    if document.get("format") != KEY_FORMAT or not isinstance(version, str):
        raise ValueError("backup key file has an invalid contract")
    if not KEY_VERSION_RE.fullmatch(version):
        raise ValueError("backup key version is invalid")
    if expected_version is not None and version != expected_version:
        raise ValueError("backup key version does not match the requested version")
    encoded = document.get("key_b64")
    if not isinstance(encoded, str):
        raise ValueError("backup key file has an invalid contract")
    try:
        key = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("backup key material is invalid") from exc
    if len(key) != 32:
        raise ValueError("backup key material must be exactly 256 bits")
    return key, version


def key_document(key: bytes, key_version: str) -> bytes:
    """Return canonical bytes for provisioning; callers must write them 0600."""
    if len(key) != 32 or not KEY_VERSION_RE.fullmatch(key_version or ""):
        raise ValueError("invalid backup key or version")
    return canonical_json({
        "format": KEY_FORMAT,
        "key_version": key_version,
        "key_b64": base64.urlsafe_b64encode(key).decode("ascii"),
    }) + b"\n"


def encryption_aad(key_version: str, protected_manifest_sha256: str) -> bytes:
    return canonical_json({
        "format": BACKUP_FORMAT,
        "method": ENCRYPTION_METHOD,
        "key_version": key_version,
        "protected_manifest_sha256": protected_manifest_sha256,
    })


class _EncryptingWriter(io.RawIOBase):
    def __init__(self, destination, encryptor, digest):
        self.destination = destination
        self.encryptor = encryptor
        self.digest = digest
        self.bytes_written = 0

    def writable(self):
        return True

    def write(self, data):
        encrypted = self.encryptor.update(bytes(data))
        if encrypted:
            self.destination.write(encrypted)
            self.digest.update(encrypted)
            self.bytes_written += len(encrypted)
        return len(data)


def encrypt_directory(
    plaintext_root: Path, destination_file: Path, *, key: bytes,
    key_version: str, protected_manifest_sha256: str,
) -> dict:
    nonce = os.urandom(12)
    aad = encryption_aad(key_version, protected_manifest_sha256)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(aad)
    digest = hashlib.sha256()
    destination_file.parent.mkdir(parents=True, exist_ok=True)
    with destination_file.open("xb") as raw:
        writer = _EncryptingWriter(raw, encryptor, digest)
        with tarfile.open(fileobj=writer, mode="w|", format=tarfile.PAX_FORMAT) as archive:
            for source in sorted(plaintext_root.rglob("*")):
                if source.is_symlink():
                    raise ValueError("plaintext backup contains a symbolic link")
                relative = source.relative_to(plaintext_root).as_posix()
                archive.add(source, arcname=relative, recursive=False)
        final = encryptor.finalize()
        if final:
            raw.write(final)
            digest.update(final)
            writer.bytes_written += len(final)
        raw.flush()
        os.fsync(raw.fileno())
    if os.name != "nt":
        destination_file.chmod(0o600)
    return {
        "format": BACKUP_FORMAT,
        "method": ENCRYPTION_METHOD,
        "key_version": key_version,
        "nonce_b64": base64.urlsafe_b64encode(nonce).decode("ascii"),
        "tag_b64": base64.urlsafe_b64encode(encryptor.tag).decode("ascii"),
        "protected_manifest_sha256": protected_manifest_sha256,
        "aad_sha256": sha256_bytes(aad),
        "ciphertext": {
            "path": destination_file.name,
            "bytes": destination_file.stat().st_size,
            "sha256": digest.hexdigest(),
        },
    }


class _DecryptingReader(io.RawIOBase):
    def __init__(self, source, decryptor):
        self.source = source
        self.decryptor = decryptor
        self.buffer = bytearray()
        self.finalized = False

    def readable(self):
        return True

    def readinto(self, target):
        while not self.buffer and not self.finalized:
            chunk = self.source.read(1024 * 1024)
            if chunk:
                self.buffer.extend(self.decryptor.update(chunk))
            else:
                self.buffer.extend(self.decryptor.finalize())
                self.finalized = True
        count = min(len(target), len(self.buffer))
        target[:count] = self.buffer[:count]
        del self.buffer[:count]
        return count

    def verify_complete(self):
        scratch = bytearray(1024 * 1024)
        while self.readinto(scratch):
            pass
        if not self.finalized:
            raise RuntimeError("encrypted backup authentication was not completed")


def _safe_tar_path(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or "\\" in name:
        raise ValueError("encrypted backup contains an unsafe path")
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("encrypted backup contains an unsafe path")
    target = root.joinpath(*relative.parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("encrypted backup contains an unsafe path")
    return target


def decrypt_directory(
    ciphertext_file: Path, destination_root: Path, *, key: bytes,
    encryption: dict,
) -> None:
    """Stream-decrypt a tar into a fresh private directory, fail closed."""
    if destination_root.exists():
        raise FileExistsError("decryption destination already exists")
    if encryption.get("format") != BACKUP_FORMAT or encryption.get("method") != ENCRYPTION_METHOD:
        raise ValueError("unsupported backup encryption contract")
    version = encryption.get("key_version")
    protected = encryption.get("protected_manifest_sha256")
    if not isinstance(version, str) or not KEY_VERSION_RE.fullmatch(version):
        raise ValueError("encrypted backup key version is invalid")
    if not isinstance(protected, str) or len(protected) != 64:
        raise ValueError("encrypted backup manifest binding is invalid")
    try:
        nonce = base64.b64decode(encryption["nonce_b64"], altchars=b"-_", validate=True)
        tag = base64.b64decode(encryption["tag_b64"], altchars=b"-_", validate=True)
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("encrypted backup nonce/tag is invalid") from exc
    if len(nonce) != 12 or len(tag) != 16:
        raise ValueError("encrypted backup nonce/tag is invalid")
    aad = encryption_aad(version, protected)
    if encryption.get("aad_sha256") != sha256_bytes(aad):
        raise ValueError("encrypted backup AAD binding is invalid")
    try:
        total_size = 0
        member_count = 0
        with ciphertext_file.open("rb") as raw:
            before = os.fstat(raw.fileno())
            expected_bytes = (encryption.get("ciphertext") or {}).get("bytes")
            if (
                not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or type(expected_bytes) is not int or before.st_size != expected_bytes
            ):
                raise ValueError("encrypted backup ciphertext changed or is unsafe")
            # Pass one authenticates the entire immutable ciphertext without
            # parsing or writing any attacker-controlled plaintext.
            authenticator = Cipher(
                algorithms.AES(key), modes.GCM(nonce, tag),
            ).decryptor()
            authenticator.authenticate_additional_data(aad)
            for chunk in iter(lambda: raw.read(1024 * 1024), b""):
                authenticator.update(chunk)
            authenticator.finalize()
            raw.seek(0)
            destination_root.mkdir(mode=0o700, parents=True, exist_ok=False)
            decryptor = Cipher(
                algorithms.AES(key), modes.GCM(nonce, tag),
            ).decryptor()
            decryptor.authenticate_additional_data(aad)
            reader = _DecryptingReader(raw, decryptor)
            buffered = io.BufferedReader(reader, buffer_size=1024 * 1024)
            with tarfile.open(fileobj=buffered, mode="r|") as archive:
                for member in archive:
                    member_count += 1
                    if member_count > 1_000_000:
                        raise ValueError("encrypted backup contains too many entries")
                    target = _safe_tar_path(destination_root, member.name)
                    if member.isdir():
                        target.mkdir(mode=0o700, parents=True, exist_ok=True)
                        continue
                    if not member.isfile():
                        raise ValueError("encrypted backup contains a non-regular entry")
                    total_size += int(member.size)
                    if total_size > ciphertext_file.stat().st_size:
                        raise ValueError("encrypted backup expands beyond its safe bound")
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValueError("encrypted backup entry cannot be read")
                    with target.open("xb") as output:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                    if os.name != "nt":
                        target.chmod(0o600)
            reader.verify_complete()
            after = os.fstat(raw.fileno())
            if (
                before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
            ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise RuntimeError("encrypted backup changed during authenticated read")
    except Exception:
        try:
            cleanup_private_tree(
                destination_root, destination_root.parent,
                "decrypted backup scratch",
            )
        except Exception as cleanup_error:
            raise RuntimeError("decrypted backup scratch cleanup failed") from cleanup_error
        raise
