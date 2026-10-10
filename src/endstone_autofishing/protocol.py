from __future__ import annotations

from dataclasses import dataclass


class ProtocolError(ValueError):
    pass


@dataclass(slots=True, frozen=True)
class AuthInputView:
    """Only the parts of PlayerAuthInputPacket we need to safely splice.

    BDS 1.26.51 (protocol 2193) starts with 32 fixed bytes, followed by an
    array of signed varints for Input Data. We deliberately keep the actual
    ItemUse transaction opaque: it is learned from a real client packet.
    """

    raw_input_values: tuple[int, ...]
    input_values: tuple[int, ...]
    encoding: str
    input_start: int
    input_end: int
    tail_offset: int


@dataclass(slots=True, frozen=True)
class UseTemplate:
    item_interact_flag: int
    encoding: str
    packed_transaction: bytes
    source: str = "unknown"


_FIXED_PREFIX_SIZE = 32


def read_uvarint(data: bytes, offset: int, max_bytes: int = 10) -> tuple[int, int]:
    value = 0
    shift = 0
    for _ in range(max_bytes):
        if offset >= len(data):
            raise ProtocolError("truncated varint")
        b = data[offset]
        offset += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            return value, offset
        shift += 7
    raise ProtocolError("varint too long")


def write_uvarint(value: int) -> bytes:
    if value < 0:
        raise ProtocolError("negative unsigned varint")
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def zigzag_decode_32(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def zigzag_encode_32(value: int) -> int:
    return (value << 1) ^ (value >> 31)


def _decode_input_values(raw_values: list[int], encoding: str) -> tuple[int, ...]:
    if encoding == "zigzag":
        return tuple(zigzag_decode_32(v) for v in raw_values)
    if encoding == "direct":
        return tuple(raw_values)
    raise ProtocolError(f"unknown input encoding {encoding!r}")


def _detect_input_encoding(raw_values: list[int], item_interact_flag: int) -> str:
    # Stable protocol metadata describes array<int32>; implementations commonly
    # encode signed varints as ZigZag. Detect from the known interaction flag so
    # the plugin works with either representation without guessing.
    direct = item_interact_flag in raw_values
    encoded = zigzag_encode_32(item_interact_flag) in raw_values
    if direct and not encoded:
        return "direct"
    if encoded and not direct:
        return "zigzag"
    if direct and encoded:
        # Extremely unlikely for an interaction packet; stable metadata uses
        # the direct enum value semantically, so prefer direct in ambiguity.
        return "direct"
    raise ProtocolError("item-interaction flag not present")


def parse_auth_input(payload: bytes, *, encoding: str = "zigzag") -> AuthInputView:
    if len(payload) < _FIXED_PREFIX_SIZE + 1:
        raise ProtocolError("PlayerAuthInput payload too short")

    offset = _FIXED_PREFIX_SIZE
    input_start = offset
    count, offset = read_uvarint(payload, offset, 5)
    if count > 128:
        raise ProtocolError(f"unreasonable input flag count: {count}")

    raw_values: list[int] = []
    for _ in range(count):
        value, offset = read_uvarint(payload, offset, 5)
        raw_values.append(value)
    input_end = offset

    # InputMode, PlayMode, NewInteractionModel.
    for _ in range(3):
        _, offset = read_uvarint(payload, offset, 5)

    # InteractRotation Vec2.
    offset += 8
    if offset > len(payload):
        raise ProtocolError("truncated interact rotation")

    # Client tick.
    _, offset = read_uvarint(payload, offset, 10)

    # PosDelta Vec3.
    offset += 12
    if offset > len(payload):
        raise ProtocolError("truncated position delta")

    return AuthInputView(
        raw_input_values=tuple(raw_values),
        input_values=_decode_input_values(raw_values, encoding),
        encoding=encoding,
        input_start=input_start,
        input_end=input_end,
        tail_offset=offset,
    )


def learn_use_template(payload: bytes, item_interact_flag: int = 34) -> UseTemplate:
    """Learn from an auth-input packet when the client actually embeds ItemUse.

    Newer 1.26.x builds serialize optional fields with presence bytes, which makes
    finding the exact end of the variable-sized transaction non-trivial without a
    full item-stack decoder.  This learner therefore supports only the legacy
    flag-gated layout; protocol 2193 primarily learns from InventoryTransaction
    packet 30 using ``learn_use_template_from_inventory_transaction`` below.
    """
    view = parse_auth_input(payload, encoding="direct")
    encoding = _detect_input_encoding(list(view.raw_input_values), item_interact_flag)
    checked = parse_auth_input(payload, encoding=encoding)
    if item_interact_flag not in checked.input_values:
        raise ProtocolError("not an item-interaction auth input")
    tail = payload[checked.tail_offset :]
    if not tail:
        raise ProtocolError("interaction packet has no transaction/trailing payload")
    # BDS 1.26.51 may encode instantaneous item use as the input flag alone:
    # the five optional-presence bytes remain zero and the trailing payload is
    # exactly the 28 bytes of analogue/camera/raw movement vectors. A real rod
    # cast observed in this shape is a complete calibration signal: replay the
    # same PerformItemInteraction flag without fabricating an ItemUse transaction.
    if len(tail) == 33 and all(b in (0, 1) for b in tail[:5]) and tail[0] == 0:
        return UseTemplate(
            item_interact_flag=item_interact_flag,
            encoding=encoding,
            packed_transaction=b"",
            source="auth-flag-only",
        )

    # If an optional ItemUse payload is present we cannot determine its end
    # safely without fully decoding NetworkItemStackDescriptor, so leave that
    # path to packet-30 learning.
    if len(tail) >= 33 and all(b in (0, 1) for b in tail[:5]):
        raise ProtocolError("auth-input carries an opaque ItemUse optional payload")

    return UseTemplate(
        item_interact_flag=item_interact_flag,
        encoding=encoding,
        packed_transaction=tail,
        source="auth",
    )


def learn_use_template_from_inventory_transaction(
    payload: bytes,
    item_interact_flag: int = 34,
    *,
    encoding: str = "zigzag",
) -> UseTemplate:
    """Convert an ItemUse InventoryTransactionPacket payload into the packed
    transaction embedded by PlayerAuthInputPacket.

    With Legacy Request ID == 0, packet 30 is:
        legacy_request_id, transaction_type, ItemUseInventoryTransaction
    while PlayerAuthInput embeds:
        legacy_request_id, ItemUseInventoryTransaction

    So conversion is lossless: validate transaction_type == 2 and remove only
    that discriminator.  Non-zero legacy request IDs are rejected conservatively
    because they carry an additional variable LegacySetSlot array.
    """
    if not payload:
        raise ProtocolError("empty InventoryTransaction payload")

    legacy_raw, offset = read_uvarint(payload, 0, 5)
    if legacy_raw != 0:
        raise ProtocolError(f"unsupported non-zero legacy request id: {legacy_raw}")

    # 1.26.40+ cereal writes the optional LegacySetItemSlots field with an
    # explicit one-byte presence marker even when Legacy Request ID is zero.
    # A real 1.26.51 rod-use packet begins `00 00 02 ...`:
    #   00 = legacy request id
    #   00 = no legacy set-item-slots payload
    #   02 = ComplexInventoryTransaction::ItemUseTransaction
    if offset >= len(payload):
        raise ProtocolError("missing legacy-slot presence byte")
    legacy_slots_present = payload[offset]
    offset += 1
    if legacy_slots_present not in (0, 1):
        raise ProtocolError(f"invalid legacy-slot presence byte: {legacy_slots_present}")
    if legacy_slots_present:
        # This is not expected for ordinary fishing and requires decoding the
        # variable LegacySetSlot array before the transaction discriminator.
        raise ProtocolError("legacy set-item-slots payload is unsupported")

    type_offset = offset
    transaction_type, after_type = read_uvarint(payload, type_offset, 5)
    if transaction_type != 2:
        raise ProtocolError(f"not an ItemUse transaction: type={transaction_type}")
    if after_type >= len(payload):
        raise ProtocolError("truncated ItemUse transaction")

    # PackedItemUseLegacyInventoryTransaction has exactly the same prefix and
    # ItemUse body, but no ComplexInventoryTransaction type discriminator.
    packed = payload[:type_offset] + payload[after_type:]
    return UseTemplate(
        item_interact_flag=item_interact_flag,
        encoding=encoding,
        packed_transaction=packed,
        source="inventory30",
    )

def _encode_input_value(value: int, encoding: str) -> int:
    if encoding == "zigzag":
        return zigzag_encode_32(value)
    if encoding == "direct":
        return value
    raise ProtocolError(f"unknown input encoding {encoding!r}")


def inject_learned_use(
    payload: bytes,
    template: UseTemplate,
    *,
    blocked_flags: set[int] | frozenset[int] = frozenset(),
) -> bytes:
    view = parse_auth_input(payload, encoding=template.encoding)
    flags = set(view.input_values)

    if template.item_interact_flag in flags:
        raise ProtocolError("current tick already contains item interaction")
    if flags.intersection(blocked_flags):
        raise ProtocolError("current tick contains incompatible optional input fields")

    new_values = sorted(flags | {template.item_interact_flag})
    encoded_values = [_encode_input_value(v, template.encoding) for v in new_values]

    rebuilt = bytearray()
    rebuilt += payload[:_FIXED_PREFIX_SIZE]
    rebuilt += write_uvarint(len(encoded_values))
    for value in encoded_values:
        rebuilt += write_uvarint(value)
    rebuilt += payload[view.input_end : view.tail_offset]

    tail = payload[view.tail_offset :]
    if not tail:
        raise ProtocolError("auth-input has no trailing fields")

    # BDS 1.26.51.1 uses five optional presence bytes before the final
    # AnalogMoveVector/CameraOrientation/RawMoveVector fields. An idle packet
    # therefore has a 33-byte tail: 5 presence bytes + 28 bytes of vectors.
    # Preserve every current-tick field, flip only ItemUse presence, and insert
    # the genuine packed transaction captured from packet 30.
    if len(tail) >= 33 and all(b in (0, 1) for b in tail[:5]):
        if template.source == "auth-flag-only":
            # This is the exact shape emitted by the native client for the
            # successful manual rod cast: only InputData changes. Preserve all
            # optional presence bytes and movement vectors verbatim.
            rebuilt += tail
        else:
            if tail[0] != 0:
                raise ProtocolError("current tick already has ItemUse optional payload")
            rebuilt.append(1)
            rebuilt += template.packed_transaction
            rebuilt += tail[1:]
    else:
        # Older flag-gated layout: the transaction is serialized directly at
        # this position only when PerformItemInteraction is present. A flag-only
        # template has no extra bytes to insert.
        rebuilt += template.packed_transaction
        rebuilt += tail

    return bytes(rebuilt)

def parse_actor_event(payload: bytes) -> tuple[int, int]:
    runtime_id, offset = read_uvarint(payload, 0, 10)
    if offset >= len(payload):
        raise ProtocolError("truncated ActorEvent payload")
    return runtime_id, payload[offset]
