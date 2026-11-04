#!/usr/bin/python3
"""6000D per-user desktop helper; this is NOT part of the F1 release package.

Try the corporate share once with existing persistent credentials, without a
password dialog. A failed/interrupted attempt is latched until credential
metadata changes or the user runs --reset. Existing mounts are never removed,
including mounts that may be stale after a network outage.

Requires Python 3 and the system python3-gi/GVFS packages. --status only reads
metadata; --reset only clears the failure latch. Neither action mounts a share.
No Secret Service GetSecret/GetSecrets/OpenSession/Unlock methods are used.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import tempfile
import threading
import time
from urllib.parse import quote, unquote, urlsplit


SERVER = "company.example.invalid"
SHARE = "机器人软件共享盘"
SHARE_URI = f"smb://{SERVER}/{quote(SHARE, safe='')}"
LOGIN_COLLECTION = "/org/freedesktop/secrets/collection/login"
SECRET_SERVICE = "org.freedesktop.secrets"
STATE_PATH = Path.home() / ".local/state/hq-share-autoconnect.json"
REASONS = {
    "attempt_in_progress", "authentication_required", "question_required",
    "process_confirmation_required", "timeout", "permission_denied",
    "network_error", "cancelled", "mount_failed",
}


def display_metadata() -> dict:
    display = os.environ.get("DISPLAY", "")
    match = re.fullmatch(r":(\d+)(?:\.\d+)?", display)
    if not match:
        return {"active": False, "reason": "local_display_missing"}
    try:
        info = Path(f"/tmp/.X11-unix/X{int(match.group(1))}").stat()
    except OSError:
        return {"active": False, "reason": "display_socket_missing"}
    owned_socket = stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid()
    return {
        "active": owned_socket,
        "reason": "ready" if owned_socket else "display_socket_not_owned",
    }


def force_user_bus() -> str | None:
    """Set the address before importing GI or constructing any GIO object."""
    runtime = Path(f"/run/user/{os.getuid()}")
    bus = runtime / "bus"
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    os.environ["XDG_RUNTIME_DIR"] = str(runtime)
    try:
        info = bus.stat()
    except OSError:
        return None
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        return None
    return os.environ["DBUS_SESSION_BUS_ADDRESS"]


def read_state(path: Path = STATE_PATH) -> tuple[str, dict | None]:
    try:
        with path.open(encoding="utf-8") as stream:
            item = json.load(stream)
    except FileNotFoundError:
        return "absent", None
    except (OSError, UnicodeError, ValueError):
        return "unavailable", None
    if not isinstance(item, dict) or item.get("schema") != 1:
        return "invalid", None
    credentials = item.get("credentials")
    if (
        item.get("reason") not in REASONS
        or not isinstance(credentials, list)
        or not credentials
        or any(
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or not entry["path"].startswith(LOGIN_COLLECTION + "/")
            or type(entry.get("modified")) is not int
            for entry in credentials
        )
        or item.get("credential_fingerprint") != credential_fingerprint(credentials)
    ):
        return "invalid", None
    # Never echo arbitrary data from a state file in --status output.
    return "valid", {
        "schema": 1,
        "reason": item["reason"],
        "credentials": [
            {"path": entry["path"], "modified": entry["modified"]}
            for entry in credentials
        ],
        "credential_fingerprint": item["credential_fingerprint"],
    }


def credential_fingerprint(credentials: list[dict]) -> str:
    normalized = sorted(
        ({"path": item["path"], "modified": item["modified"]} for item in credentials),
        key=lambda item: item["path"],
    )
    data = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def write_state(credentials: list[dict], reason: str, path: Path = STATE_PATH) -> None:
    if reason not in REASONS:
        raise ValueError("unsupported_reason")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = {
        "schema": 1,
        "reason": reason,
        "credentials": credentials,
        "credential_fingerprint": credential_fingerprint(credentials),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".hq-share-", dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            json.dump(record, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def clear_state(path: Path = STATE_PATH) -> None:
    path.unlink(missing_ok=True)


def matches_share(uri: str) -> bool:
    try:
        parsed = urlsplit(uri)
        return (
            parsed.scheme.lower() == "smb"
            and (parsed.hostname or "").lower() == SERVER
            and unquote(parsed.path).strip("/") == SHARE
        )
    except ValueError:
        return False


def mounted_metadata(Gio) -> dict:
    try:
        mounted = any(
            matches_share(mount.get_root().get_uri())
            for mount in Gio.VolumeMonitor.get().get_mounts()
        )
    except Exception:
        return {"known": False, "mounted": False}
    # Enumeration only: no IO health probe and no stale-mount teardown.
    return {"known": True, "mounted": mounted}


def keyring_metadata(bus, Gio, GLib) -> tuple[dict, list[dict]]:
    metadata = {
        "owner": False, "login_locked": None, "saved_count": 0,
        "usable_saved_count": 0, "reason": "service_unavailable",
    }

    deadline = time.monotonic() + 5.0

    def call(destination, path, interface, method, args, signature):
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            raise TimeoutError("metadata_timeout")
        return bus.call_sync(
            destination, path, interface, method, args,
            GLib.VariantType.new(signature), Gio.DBusCallFlags.NO_AUTO_START,
            min(2000, remaining_ms), None,
        ).unpack()

    def prop(path, interface, key):
        return call(
            SECRET_SERVICE, path, "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", (interface, key)), "(v)",
        )[0]

    try:
        metadata["owner"] = bool(call(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "NameHasOwner", GLib.Variant("(s)", (SECRET_SERVICE,)), "(b)",
        )[0])
        if not metadata["owner"]:
            return metadata, []
        metadata["login_locked"] = bool(prop(
            LOGIN_COLLECTION, "org.freedesktop.Secret.Collection", "Locked",
        ))
        unlocked, locked = call(
            SECRET_SERVICE, "/org/freedesktop/secrets", "org.freedesktop.Secret.Service",
            "SearchItems", GLib.Variant("(a{ss})", ({"server": SERVER, "protocol": "smb"},)),
            "(aoao)",
        )
        persistent = sorted({
            path for path in unlocked + locked
            if path.startswith(LOGIN_COLLECTION + "/")
        })
        metadata["saved_count"] = len(persistent)
        if metadata["login_locked"]:
            metadata["reason"] = "login_locked"
            return metadata, []
        # Bound total metadata work; don't authenticate with an incomplete view.
        if len(persistent) > 32:
            metadata["reason"] = "too_many_credentials"
            return metadata, []
        credentials = []
        for path in persistent:
            if path in locked or prop(path, "org.freedesktop.Secret.Item", "Locked"):
                continue
            credentials.append({
                "path": path,
                "modified": int(prop(path, "org.freedesktop.Secret.Item", "Modified")),
            })
        metadata["usable_saved_count"] = len(credentials)
        metadata["reason"] = "ready" if credentials else "no_usable_credentials"
        return metadata, credentials
    except Exception:
        metadata["reason"] = "metadata_unavailable"
        return metadata, []


def network_available() -> bool:
    # Socket timeouts alone do not bound DNS resolution. The daemon worker
    # bounds this probe to two seconds including DNS; it never authenticates.
    done = threading.Event()
    result = {"available": False}

    def probe():
        try:
            with socket.create_connection((SERVER, 445), timeout=2.0):
                result["available"] = True
        except OSError:
            pass
        finally:
            done.set()

    threading.Thread(target=probe, daemon=True).start()
    return done.wait(2.0) and result["available"]


def mount_once(Gio, GLib) -> tuple[bool, str | None]:
    loop = GLib.MainLoop()
    operation = Gio.MountOperation()
    cancellable = Gio.Cancellable()
    outcome = {"success": False, "reason": None}

    def abort_prompt(_operation, *_args, reason):
        outcome["reason"] = reason
        operation.reply(Gio.MountOperationResult.ABORTED)

    for signal, reason in (
        ("ask-password", "authentication_required"),
        ("ask-question", "question_required"),
        ("show-processes", "process_confirmation_required"),
    ):
        operation.connect(signal, lambda *args, reason=reason: abort_prompt(*args, reason=reason))

    def completed(source, result, _data=None):
        try:
            outcome["success"] = bool(source.mount_enclosing_volume_finish(result))
        except GLib.Error as error:
            if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.ALREADY_MOUNTED):
                outcome["success"] = True
            elif outcome["reason"] is None:
                if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.PERMISSION_DENIED):
                    outcome["reason"] = "permission_denied"
                elif error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                    outcome["reason"] = "cancelled"
                elif any(error.matches(Gio.io_error_quark(), code) for code in (
                    Gio.IOErrorEnum.HOST_NOT_FOUND, Gio.IOErrorEnum.HOST_UNREACHABLE,
                    Gio.IOErrorEnum.NETWORK_UNREACHABLE, Gio.IOErrorEnum.CONNECTION_REFUSED,
                    Gio.IOErrorEnum.TIMED_OUT,
                )):
                    outcome["reason"] = "network_error"
                else:
                    outcome["reason"] = "mount_failed"
        except Exception:
            outcome["reason"] = "mount_failed"
        loop.quit()

    def timed_out():
        outcome["reason"] = "timeout"
        cancellable.cancel()
        loop.quit()
        return GLib.SOURCE_REMOVE

    timeout_id = GLib.timeout_add(15000, timed_out)
    try:
        Gio.File.new_for_uri(SHARE_URI).mount_enclosing_volume(
            Gio.MountMountFlags.NONE, operation, cancellable, completed, None,
        )
        loop.run()
    except Exception:
        outcome["reason"] = "mount_failed"
    finally:
        if outcome["reason"] != "timeout":
            GLib.source_remove(timeout_id)
    return bool(outcome["success"]), outcome["reason"] or "mount_failed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--status", action="store_true", help="Read metadata only; do not mount or update state")
    actions.add_argument("--reset", action="store_true", help="Clear the failure latch only; do not mount")
    args = parser.parse_args()
    if args.reset:
        try:
            STATE_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with STATE_PATH.with_suffix(".lock").open("a", encoding="utf-8") as lock:
                os.fchmod(lock.fileno(), 0o600)
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                clear_state()
        except BlockingIOError:
            print(json.dumps({"reset": False, "reason": "attempt_running"}))
            return 1
        except OSError:
            print(json.dumps({"reset": False, "reason": "state_unavailable"}))
            return 1
        print(json.dumps({"reset": True}))
        return 0

    address = force_user_bus()
    display = display_metadata()
    state_status, failure = read_state()
    report = {
        "display": display, "user_bus_available": bool(address),
        "mount": {"known": False, "mounted": False},
        "keyring": {"reason": "not_checked"},
        "failure_state": state_status, "failure": failure,
    }
    if not address or (not args.status and not display["active"]):
        if args.status:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
        bus = Gio.DBusConnection.new_for_address_sync(
            address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None,
        )
        report["mount"] = mounted_metadata(Gio)
        report["keyring"], credentials = keyring_metadata(bus, Gio, GLib)
    except Exception:
        report["keyring"] = {"reason": "metadata_unavailable"}
        if args.status:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    if args.status:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0

    # A user timer and a manual invocation must not authenticate concurrently.
    try:
        STATE_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with STATE_PATH.with_suffix(".lock").open("a", encoding="utf-8") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            mounted = mounted_metadata(Gio)
            if not mounted["known"]:
                return 0
            if mounted["mounted"]:
                clear_state()
                return 0
            if not credentials:
                return 0
            state_status, failure = read_state()
            if state_status not in {"absent", "valid"}:
                return 0
            if failure and failure["credential_fingerprint"] == credential_fingerprint(credentials):
                return 0
            if not network_available():
                return 0
            # Persist before authentication: a crash or unwritable state must
            # never turn the next timer tick into an uncontrolled retry.
            write_state(credentials, "attempt_in_progress")
            success, reason = mount_once(Gio, GLib)
            if success:
                clear_state()
            else:
                write_state(credentials, reason)
    except OSError:
        # Includes lock contention and state-write failures. Do not authenticate.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
