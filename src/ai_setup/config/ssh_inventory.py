from __future__ import annotations

import base64
import binascii
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from ai_setup.errors import ValidationError

PROTECTED_NAMES = frozenset({"config", "known_hosts", "authorized_keys"})
SUPPORTED_KEY_TYPES = frozenset(
    {
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
    }
)
MAX_PUBLIC_KEY_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class RootIdentity:
    device: int
    inode: int
    owner_uid: int


@dataclass(frozen=True, slots=True)
class EntryIdentity:
    device: int
    inode: int
    mode: int
    owner_uid: int
    link_count: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True, slots=True)
class LocalKey:
    private_name: str
    public_name: str
    fingerprint: str
    root: RootIdentity
    private: EntryIdentity
    public: EntryIdentity
    protected: bool


@dataclass(frozen=True, slots=True)
class UnsafeEntry:
    name: str
    reason: str


@dataclass(frozen=True, slots=True)
class SSHInventory:
    root: RootIdentity | None
    keys: tuple[LocalKey, ...]
    unsafe: tuple[UnsafeEntry, ...]


@dataclass(frozen=True, slots=True)
class RemoteKey:
    key_id: int
    title: str
    fingerprint: str | None


def _error(operation: str, reason: str) -> ValidationError:
    return ValidationError("SSH", operation, reason)


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _root_identity(item: os.stat_result) -> RootIdentity:
    return RootIdentity(item.st_dev, item.st_ino, item.st_uid)


def _entry_identity(item: os.stat_result) -> EntryIdentity:
    return EntryIdentity(
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_uid,
        item.st_nlink,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )


def _validate_home(home: Path, owner_uid: int) -> None:
    try:
        item = home.lstat()
    except OSError as exc:
        raise _error("inspect SSH inventory", f"could not inspect home: {exc}") from exc
    if stat.S_ISLNK(item.st_mode):
        raise _error("inspect SSH inventory", "injected home is a symbolic link")
    if not stat.S_ISDIR(item.st_mode):
        raise _error("inspect SSH inventory", "injected home is not a directory")
    if item.st_uid != owner_uid:
        raise _error("inspect SSH inventory", f"injected home is not owned by uid {owner_uid}")


def open_ssh_root(
    ssh_dir: Path,
    *,
    owner_uid: int,
    missing_ok: bool,
) -> tuple[int | None, RootIdentity | None]:
    if not ssh_dir.is_absolute() or ssh_dir.name != ".ssh":
        raise _error("inspect SSH inventory", "SSH root must be the injected home .ssh directory")
    _validate_home(ssh_dir.parent, owner_uid)
    try:
        observed = ssh_dir.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None, None
        raise _error("revalidate SSH inventory", "SSH root no longer exists") from None
    except OSError as exc:
        raise _error("inspect SSH inventory", f"could not inspect SSH root: {exc}") from exc
    if stat.S_ISLNK(observed.st_mode):
        raise _error("inspect SSH inventory", "SSH root is a symbolic link")
    if not stat.S_ISDIR(observed.st_mode):
        raise _error("inspect SSH inventory", "SSH root is not a directory")
    if observed.st_uid != owner_uid:
        raise _error("inspect SSH inventory", f"SSH root is not owned by uid {owner_uid}")
    try:
        fd = os.open(ssh_dir, _directory_flags())
    except OSError as exc:
        raise _error("inspect SSH inventory", f"could not open SSH root safely: {exc}") from exc
    opened = os.fstat(fd)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_uid != owner_uid
        or (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino)
    ):
        os.close(fd)
        raise _error("inspect SSH inventory", "SSH root changed while it was being opened")
    return fd, _root_identity(opened)


def _stat_entry(root_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _error("inspect SSH inventory", f"could not inspect {name}: {exc}") from exc


def _unsafe_entry_reason(item: os.stat_result | None, owner_uid: int, kind: str) -> str | None:
    if item is None:
        return f"missing {kind}"
    if stat.S_ISLNK(item.st_mode):
        return f"symbolic {kind}"
    if not stat.S_ISREG(item.st_mode):
        return f"non-regular {kind}"
    if item.st_uid != owner_uid:
        return f"wrong-owner {kind}"
    if item.st_nlink != 1:
        return f"hard-linked {kind}"
    return None


def _read_public_key(
    root_fd: int,
    name: str,
    expected: os.stat_result,
    owner_uid: int,
) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(name, flags, dir_fd=root_fd)
    except OSError as exc:
        raise _error("fingerprint public key", f"could not safely open {name}: {exc}") from exc
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != owner_uid
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            raise _error("fingerprint public key", f"{name} changed while it was being opened")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(fd, min(65536, MAX_PUBLIC_KEY_BYTES + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_PUBLIC_KEY_BYTES:
                raise _error("fingerprint public key", f"{name} exceeds the size limit")
        final = os.fstat(fd)
        if _entry_identity(final) != _entry_identity(expected):
            raise _error("fingerprint public key", f"{name} changed while it was being read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _decode_public_key(data: bytes) -> tuple[str, bytes]:
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("public key is not ASCII") from exc
    records = [line for line in text.splitlines() if line.strip()]
    if len(records) != 1:
        raise ValueError("public key must contain exactly one record")
    parts = records[0].split(None, 2)
    if len(parts) < 2 or parts[0] not in SUPPORTED_KEY_TYPES:
        raise ValueError("public key type is unsupported")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("public key base64 is malformed") from exc
    if len(blob) < 4:
        raise ValueError("public key blob is truncated")
    type_length = int.from_bytes(blob[:4], "big")
    if type_length < 1 or 4 + type_length > len(blob):
        raise ValueError("public key type field is malformed")
    try:
        embedded_type = blob[4 : 4 + type_length].decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("embedded public key type is malformed") from exc
    if embedded_type != parts[0]:
        raise ValueError("textual and embedded public key types differ")
    offset = 4 + type_length

    def field() -> bytes:
        nonlocal offset
        if offset + 4 > len(blob):
            raise ValueError("public key payload is truncated")
        length = int.from_bytes(blob[offset : offset + 4], "big")
        offset += 4
        if length < 1 or offset + length > len(blob):
            raise ValueError("public key payload field is malformed")
        value = blob[offset : offset + length]
        offset += length
        return value

    if embedded_type == "ssh-ed25519":
        if len(field()) != 32:
            raise ValueError("Ed25519 public key length is invalid")
    elif embedded_type == "ssh-rsa":
        field()
        field()
    else:
        try:
            curve = field().decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("ECDSA curve name is malformed") from exc
        if curve != embedded_type.removeprefix("ecdsa-sha2-"):
            raise ValueError("ECDSA curve does not match the key type")
        field()
    if offset != len(blob):
        raise ValueError("public key blob contains trailing data")
    return parts[0], blob


def fingerprint_bytes(data: bytes) -> str:
    _, blob = _decode_public_key(data)
    digest = hashlib.sha256(blob).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def fingerprint_text(text: str) -> str | None:
    try:
        return fingerprint_bytes(text.encode("ascii"))
    except (UnicodeEncodeError, ValueError):
        return None


def _protected_identities(root_fd: int, dedicated_name: str) -> frozenset[tuple[int, int]]:
    identities: set[tuple[int, int]] = set()
    for name in (dedicated_name, dedicated_name + ".pub"):
        item = _stat_entry(root_fd, name)
        if item is not None and stat.S_ISREG(item.st_mode):
            identities.add((item.st_dev, item.st_ino))
    return frozenset(identities)


def inventory_local(
    ssh_dir: Path,
    *,
    owner_uid: int | None = None,
    dedicated_name: str = "id_ed25519_ai_github",
) -> SSHInventory:
    expected_uid = os.getuid() if owner_uid is None else owner_uid
    root_fd, root = open_ssh_root(ssh_dir, owner_uid=expected_uid, missing_ok=True)
    if root_fd is None or root is None:
        return SSHInventory(None, (), ())
    try:
        names = tuple(sorted(os.listdir(root_fd), key=os.fsencode))
        candidates = {
            name.removesuffix(".pub") for name in names if name.endswith(".pub") and name != ".pub"
        }
        candidates.update(
            name for name in names if name.startswith("id_") and not name.endswith(".pub")
        )
        protected_names = PROTECTED_NAMES | {dedicated_name, dedicated_name + ".pub"}
        protected_identities = _protected_identities(root_fd, dedicated_name)
        keys: list[LocalKey] = []
        unsafe: list[UnsafeEntry] = []
        for private_name in sorted(candidates, key=os.fsencode):
            public_name = private_name + ".pub"
            private_item = _stat_entry(root_fd, private_name)
            public_item = _stat_entry(root_fd, public_name)
            private_reason = _unsafe_entry_reason(private_item, expected_uid, "private path")
            public_reason = _unsafe_entry_reason(public_item, expected_uid, "public path")
            if private_reason:
                unsafe.append(UnsafeEntry(private_name, private_reason))
                continue
            if public_reason:
                unsafe.append(UnsafeEntry(public_name, public_reason))
                continue
            if private_item is None or public_item is None:
                raise _error("inspect SSH inventory", "entry disappeared during inventory")
            private_identity = (private_item.st_dev, private_item.st_ino)
            public_identity = (public_item.st_dev, public_item.st_ino)
            protected = (
                private_name in protected_names
                or public_name in protected_names
                or private_identity in protected_identities
                or public_identity in protected_identities
            )
            if (
                private_identity in protected_identities or public_identity in protected_identities
            ) and private_name != dedicated_name:
                unsafe.append(UnsafeEntry(private_name, "aliases a protected key"))
                continue
            try:
                fingerprint = fingerprint_bytes(
                    _read_public_key(root_fd, public_name, public_item, expected_uid)
                )
            except (ValidationError, ValueError) as exc:
                reason = exc.reason if isinstance(exc, ValidationError) else str(exc)
                unsafe.append(UnsafeEntry(public_name, f"malformed public key: {reason}"))
                continue
            keys.append(
                LocalKey(
                    private_name,
                    public_name,
                    fingerprint,
                    root,
                    _entry_identity(private_item),
                    _entry_identity(public_item),
                    protected,
                )
            )
        known = (
            {key.private_name for key in keys}
            | {key.public_name for key in keys}
            | {entry.name for entry in unsafe}
        )
        for name in names:
            if name.startswith("id_") and name not in known:
                unsafe.append(UnsafeEntry(name, "unpaired key-like entry"))
        return SSHInventory(
            root,
            tuple(keys),
            tuple(sorted(set(unsafe), key=lambda entry: (os.fsencode(entry.name), entry.reason))),
        )
    finally:
        os.close(root_fd)


def eligible_for_deletion(key: LocalKey, ssh_dir: Path, dedicated: Path) -> bool:
    return (
        ssh_dir.is_absolute()
        and dedicated.parent == ssh_dir
        and key.private_name != dedicated.name
        and key.public_name != dedicated.name + ".pub"
        and key.private_name not in PROTECTED_NAMES
        and key.public_name not in PROTECTED_NAMES
        and not key.protected
        and bool(key.fingerprint)
        and key.private.link_count == 1
        and key.public.link_count == 1
        and stat.S_ISREG(key.private.mode)
        and stat.S_ISREG(key.public.mode)
    )


def github_correlated_local_keys(
    local: tuple[LocalKey, ...], remote: tuple[RemoteKey, ...]
) -> tuple[LocalKey, ...]:
    remote_fingerprints = frozenset(key.fingerprint for key in remote if key.fingerprint)
    return tuple(
        key
        for key in local
        if not key.protected
        and key.fingerprint in remote_fingerprints
        and key.private.owner_uid == key.root.owner_uid
        and key.public.owner_uid == key.root.owner_uid
        and key.private.link_count == 1
        and key.public.link_count == 1
        and stat.S_ISREG(key.private.mode)
        and stat.S_ISREG(key.public.mode)
    )


def _require_same_entry(
    root_fd: int,
    name: str,
    expected: EntryIdentity,
    owner_uid: int,
) -> os.stat_result:
    item = _stat_entry(root_fd, name)
    if (
        item is None
        or _unsafe_entry_reason(item, owner_uid, "key path") is not None
        or _entry_identity(item) != expected
    ):
        raise _error("revalidate SSH inventory", f"{name} changed after inventory")
    return item


def open_revalidated_root(
    ssh_dir: Path,
    keys: tuple[LocalKey, ...],
    *,
    owner_uid: int,
    dedicated: Path,
) -> int:
    root_fd, root = open_ssh_root(ssh_dir, owner_uid=owner_uid, missing_ok=False)
    if root_fd is None or root is None:
        raise _error("revalidate SSH inventory", "SSH root is missing")
    try:
        if any(key.root != root for key in keys):
            raise _error("revalidate SSH inventory", "SSH root changed after inventory")
        protected_identities = _protected_identities(root_fd, dedicated.name)
        for key in keys:
            if not eligible_for_deletion(key, ssh_dir, dedicated):
                raise _error(
                    "revalidate SSH inventory",
                    f"{key.private_name} is protected or ineligible",
                )
            private_item = _require_same_entry(root_fd, key.private_name, key.private, owner_uid)
            public_item = _require_same_entry(root_fd, key.public_name, key.public, owner_uid)
            if (private_item.st_dev, private_item.st_ino) in protected_identities or (
                public_item.st_dev,
                public_item.st_ino,
            ) in protected_identities:
                raise _error(
                    "revalidate SSH inventory",
                    f"{key.private_name} aliases the protected key",
                )
            current_fingerprint = fingerprint_bytes(
                _read_public_key(root_fd, key.public_name, public_item, owner_uid)
            )
            if current_fingerprint != key.fingerprint:
                raise _error(
                    "revalidate SSH inventory",
                    f"{key.public_name} fingerprint changed after inventory",
                )
        return root_fd
    except BaseException:
        os.close(root_fd)
        raise
