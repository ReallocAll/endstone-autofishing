import importlib.util
import struct
import sys
from pathlib import Path

path = Path(__file__).resolve().parents[2] / "src/endstone_autofishing/protocol.py"
spec = importlib.util.spec_from_file_location("af_protocol", path)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

inject_learned_use = mod.inject_learned_use
learn30 = mod.learn_use_template_from_inventory_transaction
parse_actor_event = mod.parse_actor_event
parse_auth_input = mod.parse_auth_input
read_uvarint = mod.read_uvarint
write_uvarint = mod.write_uvarint
zigzag_encode_32 = mod.zigzag_encode_32


def auth_packet(flags, tail, tick=1234):
    out = bytearray(struct.pack("<8f", 1, 2, 3, 4, 0, 0, 5, 6))
    assert len(out) == 32
    out += write_uvarint(len(flags))
    for f in flags:
        out += write_uvarint(zigzag_encode_32(f))
    # input mode, play mode, interaction model
    out += write_uvarint(1) + write_uvarint(0) + write_uvarint(0)
    out += struct.pack("<2f", 7, 8)
    out += write_uvarint(tick)
    out += struct.pack("<3f", 9, 10, 11)
    out += tail
    return bytes(out)


# Packet 30 live layout: legacy id 0, absent legacy-slots optional, type 2 (ItemUse).
item_use_body = b"ITEM_USE_BODY_2193"
packet30 = b"\x00\x00" + write_uvarint(2) + item_use_body
template = learn30(packet30, 34, encoding="zigzag")
assert template.source == "inventory30"
assert template.item_interact_flag == 34
assert template.packed_transaction == b"\x00\x00" + item_use_body

# 1.26.51.1 optional-presence layout: five zero presence bytes + 28 bytes vectors.
trailing_vectors = bytes(range(28))
idle_tail = b"\x00\x00\x00\x00\x00" + trailing_vectors
assert len(idle_tail) == 33
current = auth_packet([6, 20], idle_tail, tick=101)
modified = inject_learned_use(current, template, blocked_flags={35, 36, 45})
view = parse_auth_input(modified, encoding="zigzag")
assert 34 in view.input_values
new_tail = modified[view.tail_offset:]
assert new_tail[0] == 1
assert new_tail[1:1 + len(template.packed_transaction)] == template.packed_transaction
assert new_tail[1 + len(template.packed_transaction):] == idle_tail[1:]

# Current tick's fixed player state must not be replaced by calibration data.
assert modified[:32] == current[:32]

# Block optional fields that would require structural merging.
for bad_flag in (35, 36, 45):
    bad = auth_packet([bad_flag], idle_tail)
    try:
        inject_learned_use(bad, template, blocked_flags={35, 36, 45})
    except mod.ProtocolError:
        pass
    else:
        raise AssertionError(f"blocked flag {bad_flag} was injected")

# Inventory packet types other than ItemUse must be rejected.
try:
    learn30(b"\x00\x00" + write_uvarint(3) + b"x", 34)
except mod.ProtocolError:
    pass
else:
    raise AssertionError("non-ItemUse packet30 accepted")

runtime = 0x123456789
actor_payload = write_uvarint(runtime) + bytes([13]) + b"\x00"
assert parse_actor_event(actor_payload) == (runtime, 13)
print("protocol tests: OK")

# Native BDS 1.26.51 rod-use shape observed in the field: interaction flag is
# present while all optional payloads remain absent (33-byte tail).
flag_only_cast = auth_packet([6, 20, 34], idle_tail, tick=202)
flag_only_template = mod.learn_use_template(flag_only_cast, 34)
assert flag_only_template.source == "auth-flag-only"
assert flag_only_template.packed_transaction == b""
flag_only_idle = auth_packet([6, 20], idle_tail, tick=203)
flag_only_modified = inject_learned_use(flag_only_idle, flag_only_template, blocked_flags={35, 36, 45})
flag_only_view = parse_auth_input(flag_only_modified, encoding=flag_only_template.encoding)
assert 34 in flag_only_view.input_values
assert flag_only_modified[flag_only_view.tail_offset:] == idle_tail
print("flag-only tests: OK")
