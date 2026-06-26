"""Smoke test the real Reticulum adapter (no RNode needed in this sandbox).

Starts RNS+LXMF in a throwaway config dir, prints the anonymous LXMF address and
where the persistent identity is stored, then stops. On your machine with the
RNode configured, the same path will transmit/receive over LoRa.

Note: RNS is a process-singleton, so this runs a single start/stop. Identity
persists across separate process runs via the identity file shown below.
"""

import asyncio
import os
import tempfile

from radio_app.transports.reticulum_transport import ReticulumTransport


async def main():
    tmp = tempfile.mkdtemp(prefix="radioapp_rns_")
    cfg = {"config_path": tmp, "announce_on_start": False, "display_name": ""}

    t = ReticulumTransport(cfg)
    await t.start()
    print("running        :", t.running)
    print("LXMF address   :", t.local_identity())
    caps = t.capabilities()
    print("anonymous?     :", not caps.carries_operator_identity)
    print("encryption     :", caps.supports_encryption)
    id_file = os.path.join(t._storage_dir(), "identity")
    print("identity file  :", id_file, "(exists:", os.path.isfile(id_file), ")")
    await t.stop()


asyncio.run(main())



