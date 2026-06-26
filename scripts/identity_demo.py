"""Demonstrate per-transport identity privacy and the HF encryption guard."""

from radio_app.core.compliance import ComplianceGuard
from radio_app.core.message import UnifiedMessage
from radio_app.core.privacy import apply_outbound_privacy
from radio_app.core.station import Station
from radio_app.transports.js8call_transport import JS8CallTransport
from radio_app.transports.reticulum_transport import ReticulumTransport

station = Station(callsign="N0CALL", grid_square="FN31pr")
hf = JS8CallTransport({})
rns = ReticulumTransport({})

print("=== PRIVACY: same message, different identity per transport ===")
msg = UnifiedMessage.direct("me", "K1ABC", "QSL, see you on the net")
msg.metadata = {"callsign": "N0CALL", "grid": "FN31pr", "qth": "Boston"}

hf_out = apply_outbound_privacy(msg, hf, station)
rns_out = apply_outbound_privacy(msg, rns, station)
print(f"  Over HF (js8call): sender={hf_out.sender!r}  metadata={hf_out.metadata}")
print(f"  Over Reticulum   : sender={rns_out.sender!r}  metadata={rns_out.metadata}")
print(f"  Original untouched: metadata={msg.metadata}")

print("\n=== COMPLIANCE: encryption over HF ===")
secret = UnifiedMessage.direct("N0CALL", "K1ABC", "encrypted traffic")
secret.encrypt = True

blocked = ComplianceGuard(allow_encrypted_on_hf=False)
d = blocked.check_outbound(secret, hf)
print(f"  default config        -> allowed={d.allowed}  reason={d.reason}")

declined = ComplianceGuard(allow_encrypted_on_hf=True, confirm=lambda w: False)
d = declined.check_outbound(secret, hf)
print(f"  enabled, user declines -> allowed={d.allowed}  reason={d.reason}")

accepted = ComplianceGuard(allow_encrypted_on_hf=True, confirm=lambda w: True)
d = accepted.check_outbound(secret, hf)
print(f"  enabled, user ACCEPTS  -> allowed={d.allowed}  reason={d.reason}")

plain = UnifiedMessage.direct("N0CALL", "K1ABC", "plain net check-in")
d = blocked.check_outbound(plain, hf)
print(f"  plaintext over HF      -> allowed={d.allowed} (always fine)")

