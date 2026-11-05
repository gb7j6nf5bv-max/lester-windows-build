"""Keeps one location-simulation session open to an iPhone and applies new positions fast.

Used by Lester's joystick. Each `simulate-location set` command takes seconds to connect,
far too slow for steering, so this holds the session and reads commands from stdin:
    set LAT LON   ->  prints "OK" (or "ERR message")
    clear         ->  prints "OK"
Prints "READY" once connected. The location holds while this process runs.
Runs with pymobiledevice3's own Python. Usage: python lester_stream.py UDID
"""
import asyncio
import sys

from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
from pymobiledevice3.tunneld.api import get_tunneld_devices


def reply(text):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


async def main(udid):
    rsds = await get_tunneld_devices()
    rsd = next((r for r in rsds if r.udid == udid), None)
    if rsd is None:
        reply("ERR the iPhone isn't connected to Lester's tunnel")
        return 1
    async with DvtProvider(rsd) as dvt, LocationSimulation(dvt) as sim:
        reply("READY")
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                return 0
            parts = line.split()
            try:
                if parts and parts[0] == "set" and len(parts) == 3:
                    await sim.set(float(parts[1]), float(parts[2]))
                    reply("OK")
                elif parts and parts[0] == "clear":
                    await sim.clear()
                    reply("OK")
                else:
                    reply("ERR unknown command")
            except Exception as e:  # report, keep the session if it's still usable
                reply(f"ERR {e}")
                if "closed" in str(e).lower() or "broken" in str(e).lower():
                    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1])) or 0)
