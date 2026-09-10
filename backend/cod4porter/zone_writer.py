from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import io
import math
import struct
from typing import Dict, List, Optional

PS3_BLOCK_COUNT = 7
POINTER_OFFSET_BITS = 29
POINTER_OFFSET_MASK = (1 << POINTER_OFFSET_BITS) - 1
NULL = 0x00000000
FOLLOWING = 0xFFFFFFFF
INSERT = 0xFFFFFFFE
ZONE_HEADER_SIZE = 36

# Retail oracle (measured 2026-09-03 on real PS3 retail zones):
#   mp_shipment_load.ff  blocks (196, 0, 0, 0, 198, 0, 1223424)
#   mp_shipment.ff       blocks (6512, 165232, 5504, 63333760, 17503405, 0, 6257552)
#   mp_crash.ff          blocks (6256, 169664, 0, 98589696, 24202937, 0, 9305488)
# Block 4 is ODD in both retail main zones, so a declared block size is the exact
# allocator high-water mark and is never rounded.  An earlier fix rounded it up to
# an even boundary because the Fix89B mp_getaway_load baseline declares 198 where
# replaying its content ends at 197; the real mp_shipment_load.ff reaches 198 on its
# own (its rawfile and loadscreen names are one byte longer), which shows the Fix89B
# value was inherited from the Shipment header, not produced by a rounding rule.
TEMP_BLOCK_INDEX = 0
BLOCK_SIZE_GRANULARITY = 1


def declared_block_size(used: int) -> int:
    """A block is declared at exactly its allocator high-water mark."""
    if used < 0:
        raise ValueError('block high-water mark cannot be negative')
    return used


# Ground truth, measured 2026-09-04: stock PC mp_shipment was converted and the result
# compared against the Retail PS3 mp_shipment that Infinity Ward shipped from the same
# map.  Only two blocks came out SMALLER than what the console actually used:
#
#   block            generated      Retail        delta
#   0 TEMP                 936       6,512       -5,576   <-- under-declared
#   1 runtime          165,344     165,232         +112   (0.07% - the model is right)
#   2                        0       5,504       -5,504   <-- under-declared
#   6 physical       7,186,336   6,257,552     +928,784   (over-declared, harmless)
#
# The loader allocates each block from the declared size and then writes into it.
# Declaring more than needed wastes a few bytes; declaring less lets the loader run past
# the end of that block into the next one and corrupt the zone while it is still loading.
# TEMP came out at exactly 936 for mp_getaway and mp_shipment alike, so the model is
# missing loader-side transients it cannot observe from the PC source.
#
# Retail main zones use 6,256 (mp_crash) and 6,512 (mp_shipment) bytes of TEMP.  The load
# zone uses 196 and the porter already reproduces that byte-exactly, so the floor must
# never apply there.  8,192 clears both measured main-zone values and costs 8 KB that is
# released once loading finishes.
RETAIL_PS3_MAIN_ZONE_TEMP_FLOOR = 8192
RETAIL_PS3_MAIN_ZONE_BLOCK2_FLOOR = 8192
_RETAIL_BLOCK_FLOORS = {TEMP_BLOCK_INDEX: RETAIL_PS3_MAIN_ZONE_TEMP_FLOOR,
                        2: RETAIL_PS3_MAIN_ZONE_BLOCK2_FLOOR}


def declared_block_size_with_floor(block: int, used: int, *, apply_floors: bool) -> int:
    """Declared size for one block, never below the measured Retail floor."""
    size = declared_block_size(used)
    if not apply_floors:
        return size
    return max(size, _RETAIL_BLOCK_FLOORS.get(block, 0))


class BlockKind(str, Enum):
    NORMAL = 'normal'
    RUNTIME = 'runtime'
    TEMPORARY = 'temporary'

DEFAULT_BLOCK_KINDS = (
    BlockKind.TEMPORARY,
    BlockKind.RUNTIME,
    BlockKind.RUNTIME,
    BlockKind.NORMAL,
    BlockKind.NORMAL,
    BlockKind.NORMAL,
    BlockKind.NORMAL,
)

TEMP_BLOCK = 0
# Block 1 is loader-allocated runtime memory: Load_Stream memsets it and consumes no
# FastFile bytes, so writers reserve into it to budget the declared block size.
RUNTIME_BLOCK = 1
# IW3 PS3 keeps persistent XStrings, normal arrays and INSERT alias cells in
# block 4.  Block 3 is the delayed-image FIFO that is flushed only after the
# complete normal asset stream has been serialized.
DELAYED_IMAGE_BLOCK = 3
VIRTUAL_BLOCK = 4
INSERT_BLOCK = 4
LARGE_BLOCK = 4
ASSET_ALIAS_BLOCK = 4
PHYSICAL_BLOCK = 5
VERTEX_BLOCK = 6

@dataclass(frozen=True, order=True)
class PackedOffset:
    block: int
    offset: int

    def encode(self) -> int:
        if not 0 <= self.block < PS3_BLOCK_COUNT:
            raise ValueError('block index outside PS3 range')
        if not 0 <= self.offset <= POINTER_OFFSET_MASK:
            raise ValueError('packed offset outside PS3 range')
        return ((self.block << POINTER_OFFSET_BITS) | self.offset) + 1

@dataclass(frozen=True)
class Relocation:
    physical_field_offset: int
    target_symbol: str
    target_byte_offset: int = 0
    kind: str = 'packed_pointer'
    description: str = 'packed pointer'

@dataclass(frozen=True)
class ResolvedRelocation:
    physical_field_offset: int
    target_symbol: str
    target: PackedOffset
    serialized_pointer: int
    kind: str
    description: str

@dataclass
class SerializationOptions:
    block_kinds: tuple[BlockKind, ...] = DEFAULT_BLOCK_KINDS
    insert_block_index: int = INSERT_BLOCK
    external_memory_size: int = 0
    zone_memory_size_override: Optional[int] = None
    expected_block_sizes: Optional[tuple[int, ...]] = None
    requested_physical_capacity_bytes: int = 0
    enable_trace: bool = False
    # Main zones raise blocks 0 and 2 to the measured Retail floor; the load zone, which
    # is reproduced byte-exactly, must keep its own high-water marks.
    apply_retail_block_floors: bool = False

    def validate(self) -> None:
        if len(self.block_kinds) != PS3_BLOCK_COUNT:
            raise ValueError('exactly seven block kinds are required')
        if not 0 <= self.insert_block_index < PS3_BLOCK_COUNT:
            raise ValueError('invalid insert block')
        if self.block_kinds[self.insert_block_index] is not BlockKind.NORMAL:
            raise ValueError('insert block must be a normal serialized block')
        if self.expected_block_sizes is not None and len(self.expected_block_sizes) != PS3_BLOCK_COUNT:
            raise ValueError('expected block sizes must contain seven entries')

@dataclass
class _BlockFrame:
    index: int
    kind: BlockKind
    temporary_position: int = 0

@dataclass
class CanonicalAllocation:
    identity: str
    block: int
    offset: int
    size: int
    alignment: int

@dataclass
class _DeferredPayload:
    block: int
    data: bytes
    alignment: int
    description: str
    result_row: Optional[dict] = None

@dataclass
class ZoneWriteResult:
    zone_bytes: bytes
    block_sizes: tuple[int, ...]
    symbols: Dict[str, PackedOffset]
    relocations: tuple[ResolvedRelocation, ...]
    trace: tuple[str, ...]
    canonical_allocations: tuple[CanonicalAllocation, ...]
    requested_capacity: int
    remaining_capacity: int

class RelocatingZoneWriter:
    '''Python port of FIX114 RelocatingZoneWriter's production pointer/block semantics.

    This intentionally keeps physical serialization order independent from logical block
    cursors. TEMP frames restart at zero at top level; INSERT alias cells grow VIRTUAL block
    logical memory without emitting source bytes.
    '''
    def __init__(self, options: SerializationOptions | None = None):
        self.options = options or SerializationOptions()
        self.options.validate()
        self.physical = bytearray(b'\0' * ZONE_HEADER_SIZE)
        self.block_positions = [0] * PS3_BLOCK_COUNT
        self.stack: List[_BlockFrame] = []
        self.symbols: Dict[str, PackedOffset] = {}
        self.relocations: List[Relocation] = []
        # Relocation fields are unique by physical pointer slot. CP11 linearly rescanned the
        # entire relocation list for every ClipMap/GfxWorld pointer, making 40k+ relocations
        # quadratic. Keep an O(1) membership index without changing serialized bytes.
        self._relocation_fields: set[int] = set()
        self.trace_enabled = bool(self.options.enable_trace)
        self.trace: List[str] = [f'reserve raw zone header: {ZONE_HEADER_SIZE} bytes'] if self.trace_enabled else []
        self.canonical: Dict[tuple[int, str], CanonicalAllocation] = {}
        self.deferred_payloads: List[_DeferredPayload] = []
        self.deferred_flushed = False
        self.built = False

    @property
    def physical_position(self) -> int:
        return len(self.physical)

    @property
    def current_frame(self) -> _BlockFrame:
        if not self.stack:
            raise RuntimeError('push a zone block before writing block data')
        return self.stack[-1]

    @property
    def current_block_index(self) -> int:
        return self.current_frame.index

    def _position(self, frame: _BlockFrame | None = None) -> int:
        frame = frame or self.current_frame
        return frame.temporary_position if frame.kind is BlockKind.TEMPORARY else self.block_positions[frame.index]

    def _set_position(self, value: int) -> None:
        if not 0 <= value <= POINTER_OFFSET_MASK + 1:
            raise ValueError('zone block cursor exceeds pointer address range')
        f = self.current_frame
        if f.kind is BlockKind.TEMPORARY:
            f.temporary_position = value
        else:
            self.block_positions[f.index] = value

    @property
    def current_location(self) -> PackedOffset:
        p = self._position()
        if p > POINTER_OFFSET_MASK:
            raise ValueError('current block cursor exceeds pointer address range')
        return PackedOffset(self.current_block_index, p)

    def push_block(self, block: int) -> None:
        if not 0 <= block < PS3_BLOCK_COUNT:
            raise ValueError('invalid block')
        kind = self.options.block_kinds[block]
        temp_position = 0
        if kind is BlockKind.TEMPORARY:
            for parent in reversed(self.stack):
                if parent.index == block and parent.kind is kind:
                    temp_position = parent.temporary_position
                    break
        self.stack.append(_BlockFrame(block, kind, temp_position))
        if self.trace_enabled:self.trace.append(f'push block {block} ({kind.value}) at 0x{self._position():X}')

    def pop_block(self) -> int:
        if not self.stack:
            raise RuntimeError('no active block')
        frame = self.stack.pop()
        pos = frame.temporary_position if frame.kind is BlockKind.TEMPORARY else self.block_positions[frame.index]
        if frame.kind is BlockKind.TEMPORARY:
            # TEMP is stack storage. A nested frame contributes only to the
            # global high-water mark; PopBlock restores the parent's saved
            # cursor instead of advancing it to the child end.
            self.block_positions[frame.index] = max(self.block_positions[frame.index], frame.temporary_position)
        if self.trace_enabled:self.trace.append(f'pop block {frame.index} at 0x{pos:X}')
        return frame.index

    def align(self, alignment: int) -> None:
        if alignment <= 0 or alignment & (alignment - 1):
            raise ValueError('alignment must be a positive power of two')
        before = self._position()
        after = (before + alignment - 1) & ~(alignment - 1)
        padding = after - before
        if padding and self.current_frame.kind is not BlockKind.RUNTIME:
            self.physical.extend(b'\0' * padding)
        self._set_position(after)
        if padding:
            if self.trace_enabled:self.trace.append(f'align block {self.current_block_index}: 0x{before:X}->0x{after:X} ({alignment})')

    def align_logical_only(self, alignment: int) -> None:
        if alignment <= 0 or alignment & (alignment - 1):
            raise ValueError('alignment must be a positive power of two')
        before = self._position()
        after = (before + alignment - 1) & ~(alignment - 1)
        self._set_position(after)
        if after != before:
            if self.trace_enabled:self.trace.append(f'align logical block {self.current_block_index}: 0x{before:X}->0x{after:X} ({alignment})')

    def write_raw(self, data: bytes, description: str = 'raw data') -> int:
        off = len(self.physical)
        self.physical.extend(data)
        if self.trace_enabled:self.trace.append(f'write raw 0x{off:X}: {len(data)} bytes ({description})')
        return off

    def write_in_block(self, data: bytes, description: str = 'block data') -> PackedOffset:
        loc = self.current_location
        if self.current_frame.kind is not BlockKind.RUNTIME:
            self.physical.extend(data)
        self._set_position(self._position() + len(data))
        if self.trace_enabled:self.trace.append(f'write block {loc.block} @0x{loc.offset:X}: {len(data)} bytes ({description})')
        return loc

    def reserve_in_block(self, count: int, description: str = 'reserved block data') -> PackedOffset:
        if count < 0:
            raise ValueError('negative allocation')
        if self.current_frame.kind is BlockKind.RUNTIME:
            loc = self.current_location
            self._set_position(self._position() + count)
            return loc
        return self.write_in_block(b'\0' * count, description)

    def reserve_logical_only(self, count: int, description: str = 'logical-only allocation') -> PackedOffset:
        if count < 0:
            raise ValueError('negative allocation')
        loc = self.current_location
        self._set_position(self._position() + count)
        if self.trace_enabled:self.trace.append(f'reserve logical block {loc.block} @0x{loc.offset:X}: {count} bytes ({description})')
        return loc

    def define_symbol(self, name: str) -> PackedOffset:
        return self.define_symbol_at(name, self.current_location)

    def define_symbol_at(self, name: str, loc: PackedOffset) -> PackedOffset:
        if not name or name in self.symbols:
            raise ValueError(f'duplicate/empty relocation symbol {name!r}')
        self.symbols[name] = loc
        if self.trace_enabled:self.trace.append(f'define symbol {name} = block {loc.block}:0x{loc.offset:X}')
        return loc

    def define_insert_pointer_alias(self, symbol: str, canonical_identity: str | None = None, description: str = 'insert-pointer alias') -> PackedOffset:
        canonical_identity = canonical_identity or symbol
        key = (self.options.insert_block_index, canonical_identity)
        if key in self.canonical:
            c = self.canonical[key]
            loc = PackedOffset(c.block, c.offset)
            self.define_symbol_at(symbol, loc)
            if self.trace_enabled:self.trace.append(f'reuse insert alias {canonical_identity} = block {loc.block}:0x{loc.offset:X}')
            return loc
        block = self.options.insert_block_index
        # Persistent global alias allocation is independent from the current active block.
        current = self.block_positions[block]
        start = (current + 3) & ~3
        self.block_positions[block] = start + 4
        loc = PackedOffset(block, start)
        self.canonical[key] = CanonicalAllocation(canonical_identity, block, start, 4, 4)
        self.define_symbol_at(symbol, loc)
        if self.trace_enabled:self.trace.append(f'create insert alias {canonical_identity} = block {block}:0x{start:X}')
        return loc

    def _write(self, fmt: str, value, desc: str) -> PackedOffset:
        return self.write_in_block(struct.pack('>' + fmt, value), desc)

    def write_byte(self, v: int, desc='byte'): return self._write('B', v, desc)
    def write_u16(self, v: int, desc='uint16'): return self._write('H', v, desc)
    def write_i16(self, v: int, desc='int16'): return self._write('h', v, desc)
    def write_u32(self, v: int, desc='uint32'): return self._write('I', v & 0xFFFFFFFF, desc)
    def write_i32(self, v: int, desc='int32'): return self._write('i', v, desc)
    def write_u64(self, v: int, desc='uint64'): return self._write('Q', v & 0xFFFFFFFFFFFFFFFF, desc)
    def write_f32(self, v: float, desc='float'):
        if not math.isfinite(v): raise ValueError(f'{desc} must be finite')
        return self._write('f', v, desc)

    def write_null_pointer(self, desc='null pointer'): return self.write_u32(NULL, desc)
    def write_following_pointer(self, desc='following pointer'):
        marker = INSERT if self.current_frame.kind is BlockKind.TEMPORARY else FOLLOWING
        return self.write_u32(marker, desc)
    def write_insert_pointer(self, desc='insert pointer'): return self.write_u32(INSERT, desc)
    def write_packed_pointer(self, target: PackedOffset, desc='packed pointer'): return self.write_u32(target.encode(), desc)

    def write_pointer_to(self, target_symbol: str, target_byte_offset: int = 0, kind: str = 'packed_pointer', description: str = 'packed pointer') -> PackedOffset:
        if self.current_frame.kind is BlockKind.RUNTIME:
            raise RuntimeError('cannot serialize pointer field into runtime block')
        physical = len(self.physical)
        loc = self.write_u32(0, description)
        if physical in self._relocation_fields:
            raise ValueError(f'pointer field 0x{physical:X} already has relocation')
        self._relocation_fields.add(physical)
        self.relocations.append(Relocation(physical, target_symbol, target_byte_offset, kind, description))
        return loc

    def register_pointer_relocation(self, physical_field_offset: int, target_symbol: str, target_byte_offset: int = 0, kind: str = 'packed_pointer', description: str = 'structured packed pointer') -> None:
        if physical_field_offset < ZONE_HEADER_SIZE or physical_field_offset + 4 > len(self.physical):
            raise ValueError('relocation field outside serialized payload')
        if physical_field_offset in self._relocation_fields:
            raise ValueError(f'pointer field 0x{physical_field_offset:X} already has relocation')
        self._relocation_fields.add(physical_field_offset)
        self.relocations.append(Relocation(physical_field_offset, target_symbol, target_byte_offset, kind, description))

    def write_latin1z(self, value: str, desc='Latin-1 string') -> PackedOffset:
        if '\0' in value:
            raise ValueError('embedded NUL')
        try: data = value.encode('latin-1') + b'\0'
        except UnicodeEncodeError as e: raise ValueError('string is not Latin-1') from e
        return self.write_in_block(data, desc)

    def defer_payload(
        self,
        data: bytes,
        *,
        block: int = DELAYED_IMAGE_BLOCK,
        alignment: int = 0x80,
        description: str = 'deferred payload',
        result_row: Optional[dict] = None,
    ) -> int:
        """Queue bytes for the global tail FIFO without emitting them in-place.

        Delayed GfxImage data is encountered while its shell is loaded, but the
        PS3 loader consumes the payloads only after the normal XAsset stream.
        The logical block alignment still advances the destination allocator;
        there is deliberately no physical padding between already padded image
        resources in the FastFile tail.
        """
        if self.built or self.deferred_flushed:
            raise RuntimeError('cannot queue payload after deferred FIFO flush')
        if not data:
            raise ValueError('deferred payload cannot be empty')
        if not 0 <= block < PS3_BLOCK_COUNT:
            raise ValueError('invalid deferred payload block')
        if self.options.block_kinds[block] is not BlockKind.NORMAL:
            raise ValueError('deferred payload block must be normal')
        if alignment <= 0 or alignment & (alignment - 1):
            raise ValueError('deferred payload alignment must be a positive power of two')
        index = len(self.deferred_payloads)
        self.deferred_payloads.append(_DeferredPayload(block, bytes(data), alignment, description, result_row))
        if result_row is not None:
            result_row.update({'fifo_index': index, 'resource_physical': None})
        return index

    def flush_deferred_payloads(self) -> None:
        if self.deferred_flushed:
            return
        if self.stack:
            raise RuntimeError('deferred FIFO can only be flushed with no active block')
        self.deferred_flushed = True
        for index, item in enumerate(self.deferred_payloads):
            self.push_block(item.block)
            self.align_logical_only(item.alignment)
            logical = self.current_location
            physical = self.physical_position
            self.write_in_block(item.data, item.description)
            self.pop_block()
            if item.result_row is not None:
                item.result_row.update({
                    'fifo_index': index,
                    'resource_physical': physical,
                    'resource_logical_block': logical.block,
                    'resource_logical_offset': logical.offset,
                })

    def build(self) -> ZoneWriteResult:
        if self.built: raise RuntimeError('writer already built')
        if self.stack: raise RuntimeError('all pushed blocks must be popped')
        self.flush_deferred_payloads()
        self.built = True
        # The loader allocates each block from the declared size, so the
        # declaration must never be smaller than the block's high-water mark.
        block_sizes = tuple(
            declared_block_size_with_floor(i, x, apply_floors=self.options.apply_retail_block_floors)
            for i, x in enumerate(self.block_positions)
        )
        if self.options.expected_block_sizes is not None and block_sizes != self.options.expected_block_sizes:
            raise ValueError(f'block-size verification failed: {block_sizes} != {self.options.expected_block_sizes}')
        zone = bytearray(self.physical)
        resolved: List[ResolvedRelocation] = []
        for r in self.relocations:
            if r.target_symbol not in self.symbols:
                raise ValueError(f'relocation target {r.target_symbol!r} was never defined')
            s = self.symbols[r.target_symbol]
            target = PackedOffset(s.block, s.offset + r.target_byte_offset)
            if target.offset >= block_sizes[target.block]:
                raise ValueError(f'relocation target {target} outside declared block')
            raw = target.encode()
            struct.pack_into('>I', zone, r.physical_field_offset, raw)
            resolved.append(ResolvedRelocation(r.physical_field_offset, r.target_symbol, target, raw, r.kind, r.description))
        declared = sum(block_sizes)
        calc = max(len(zone), min(declared, 0xFFFFFFFF))
        memory = self.options.zone_memory_size_override if self.options.zone_memory_size_override is not None else calc
        struct.pack_into('>II', zone, 0, memory, self.options.external_memory_size)
        for i, size in enumerate(block_sizes):
            struct.pack_into('>I', zone, 8 + i * 4, size)
        if self.options.requested_physical_capacity_bytes and len(zone) > self.options.requested_physical_capacity_bytes:
            raise ValueError('serialized zone exceeds requested physical capacity')
        remaining = 0 if not self.options.requested_physical_capacity_bytes else self.options.requested_physical_capacity_bytes - len(zone)
        return ZoneWriteResult(bytes(zone), block_sizes, dict(self.symbols), tuple(resolved), tuple(self.trace), tuple(sorted(self.canonical.values(), key=lambda x:(x.block,x.offset,x.identity))), self.options.requested_physical_capacity_bytes, remaining)
