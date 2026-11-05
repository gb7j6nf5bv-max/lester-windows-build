"""Makes the RemotePairing key that Lester for iPhone uses, over the USB cable.

The Windows twin of lester_pair.c (the Mac's version, built on the idevice library). It runs
with pymobiledevice3's own Python and writes the key in the idevice format the iPhone app reads:
    public_key, private_key (Ed25519, 32 bytes each), identifier, alt_irk (the iPhone's, 16 bytes)

The key is written only to OUT_FILE, never to pymobiledevice3's own pairing folder: Lester
copies it into the iPhone app and deletes it straight away.

Usage: python lester_pair.py UDID HOSTNAME OUT_FILE
Prints "OK" on success, or "ERROR <stage>: <detail>".
"""
import asyncio
import os
import plistlib
import sys
import uuid


def host_identifier(hostname):
    """Same as idevice's RpPairingFile::generate: a UUID made from the host name."""
    return str(uuid.uuid3(uuid.NAMESPACE_DNS, hostname))


def pairing_file_bytes(record, identifier):
    """The idevice pairing file, from what pymobiledevice3 saves after pairing."""
    out = {"public_key": bytes(record["public_key"]), "private_key": bytes(record["private_key"]),
           "identifier": identifier}
    if record.get("peer_alt_irk"):
        out["alt_irk"] = bytes(record["peer_alt_irk"])
    if len(out["public_key"]) != 32 or len(out["private_key"]) != 32:
        raise ValueError("unexpected key size")
    return plistlib.dumps(out, fmt=plistlib.FMT_XML)


def write_private(path, data):
    """Only this user can read the key: it works like a password for the iPhone."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


async def pair(udid, hostname):
    from pymobiledevice3.exceptions import RemotePairingCompletedError
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.remote.tunnel_service import RemotePairingLockdownService

    class FreshPairing(RemotePairingLockdownService):
        """Always makes a new key under Lester's own name, and keeps it in memory."""
        saved = None

        @property
        def pair_record(self):
            return None   # never reuse pymobiledevice3's own key

        def save_pair_record(self):
            FreshPairing.saved = {
                "public_key": self.ed25519_private_key.public_key().public_bytes_raw(),
                "private_key": self.ed25519_private_key.private_bytes_raw(),
                "peer_alt_irk": self.peer_alt_irk,
            }

    lockdown = await create_using_usbmux(serial=udid, autopair=False)
    service = await FreshPairing.create(lockdown)
    service.identifier = host_identifier(hostname)
    try:
        await service.connect(autopair=True)
    except RemotePairingCompletedError:
        pass   # the iPhone hangs up once pairing is done
    finally:
        try:
            await service.close()
        except Exception:
            pass
    if FreshPairing.saved is None:
        raise RuntimeError("PairingRejected: the iPhone didn't make a new pairing")
    return FreshPairing.saved, service.identifier


def main(argv):
    if len(argv) != 4:
        print("ERROR usage: lester_pair.py UDID HOSTNAME OUT_FILE")
        return 2
    udid, hostname, out = argv[1:]
    try:
        record, identifier = asyncio.run(pair(udid, hostname))
    except Exception as e:
        print(f"ERROR pair: {type(e).__name__}: {e}"[:400])
        return 1
    try:
        write_private(out, pairing_file_bytes(record, identifier))
    except Exception as e:
        print(f"ERROR save: {e}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
