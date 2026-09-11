"""Owned PS3 MaterialTechniqueSet serialization (compiled PC-to-RSX shaders).

Load semantics follow the PS3 loader family exactly (mirrored from the
IW4Studio fastfile loaders, adjusted to the measured IW3 shapes):

* techset root: TEMP, 0x70 bytes, 26 technique pointer cells at +8;
* everything nested loads inside one LARGE push: the XString name, then each
  owned technique in slot order;
* technique: 8-byte root, ``passCount`` 0x18-byte pass roots, then per pass
  the vertex declaration (0x1C), the two shader assets, and the argument
  table with its literal float4 payloads; the technique name string follows
  the passes;
* shader assets are nested TEMP roots (vertex 0x0C, pixel 0x18 with the
  twelve CgBinaryFragmentProgram control bytes at +0x0C), an INSERT alias
  cell per first occurrence, the XString name in LARGE and the 16-aligned
  CgBinaryProgram payload back in TEMP;
* repeated shaders/declarations serialize once and are referenced by packed
  pointers (shader pointers target the durable INSERT alias cell).

Alignment only ever advances the destination allocator; fastfile bytes stay
physically packed (``align_logical_only``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import struct
import weakref

from ..graph import AssetType, GraphContext, nested_symbol
from ..zone_writer import (
    FOLLOWING, INSERT, LARGE_BLOCK, NULL, RelocatingZoneWriter, TEMP_BLOCK,
)
from ..rsx.techset_compile import CompiledShader, CompiledTechniqueSet, PS3_TECHNIQUE_SLOTS
from ..rsx.techset_parse import ARG_LITERAL_PIXEL_CONST, ARG_LITERAL_VERTEX_CONST

PS3_TECHSET_ROOT_SIZE = 0x70
PS3_TECHNIQUE_ROOT_SIZE = 0x08
PS3_PASS_SIZE = 0x18
PS3_VERTEX_SHADER_ROOT = 0x0C
PS3_PIXEL_SHADER_ROOT = 0x18


@dataclass
class TechsetEmissionRegistry:
    """Cross-techset dedup state shared by all owned techset nodes of a zone.

    The dedup domain is one *write run*: a zone is laid out several times with
    fresh writers (the block-size measurement pass, then the two determinism
    builds), and a symbol defined in one run does not exist in the next.  The
    registry therefore resets itself whenever it sees a new writer; its public
    fields always describe the current (most recent) run.  Holding the writer
    weakly keeps the explicit ``del first; gc.collect()`` memory release in
    ``build_main_fastfile`` effective.
    """

    shader_alias_symbols: dict = field(default_factory=dict)   # content_key -> alias symbol
    shader_names: dict = field(default_factory=dict)           # content_key -> asset name
    decl_symbols: dict = field(default_factory=dict)           # content_key -> payload symbol
    emitted_shaders: int = 0
    reused_shaders: int = 0
    emitted_decls: int = 0
    reused_decls: int = 0
    _run_writer: object = field(default=None, repr=False, compare=False)

    def for_writer(self, writer) -> 'TechsetEmissionRegistry':
        """Bind the registry to ``writer``'s run, resetting stale-run state."""
        current = self._run_writer() if self._run_writer is not None else None
        if current is not writer:
            self._run_writer = weakref.ref(writer)
            self.shader_alias_symbols.clear()
            self.shader_names.clear()
            self.decl_symbols.clear()
            self.emitted_shaders = 0
            self.reused_shaders = 0
            self.emitted_decls = 0
            self.reused_decls = 0
        return self


@dataclass
class OwnedTechniqueSetNode:
    compiled: CompiledTechniqueSet
    symbol: str
    registry: TechsetEmissionRegistry
    type: AssetType = field(init=False, default=AssetType.TECHSET)
    root_block: int = field(init=False, default=TEMP_BLOCK)
    dependencies: tuple = field(init=False, default=())

    @property
    def name(self) -> str:
        return self.compiled.name

    @property
    def serialized_name(self) -> str:
        return self.compiled.name

    def write(self, w: RelocatingZoneWriter, c: GraphContext) -> None:
        compiled = self.compiled
        self.registry.for_writer(w)
        if len(compiled.slots) != PS3_TECHNIQUE_SLOTS:
            raise ValueError(f'techset {compiled.name!r} has {len(compiled.slots)} slots')
        root_physical = w.physical_position
        root_location = w.define_symbol(self.symbol)
        c.register_root(self.symbol, root_physical)

        technique_first_slot: dict[str, int] = {}
        root = bytearray(PS3_TECHSET_ROOT_SIZE)
        struct.pack_into('>I', root, 0, FOLLOWING)
        root[4] = compiled.world_vertex_format & 0xFF
        for slot, technique in enumerate(compiled.slots):
            cell = 8 + slot * 4
            if technique is None:
                struct.pack_into('>I', root, cell, NULL)
            elif technique.content_key in technique_first_slot:
                struct.pack_into('>I', root, cell, 0)  # packed, relocated below
            else:
                technique_first_slot[technique.content_key] = slot
                struct.pack_into('>I', root, cell, FOLLOWING)
        w.write_in_block(bytes(root), f'TechniqueSet {compiled.name} root')

        # Packed pointers for repeated slots target the first slot's technique
        # root, which is registered as a symbol when that technique is written.
        for slot, technique in enumerate(compiled.slots):
            if technique is None:
                continue
            first = technique_first_slot[technique.content_key]
            if first != slot:
                w.register_pointer_relocation(
                    root_physical + 8 + slot * 4,
                    nested_symbol(self.symbol, f'technique.{technique.content_key}'),
                    description=f'TechniqueSet {compiled.name} slot {slot} shared technique')

        w.push_block(LARGE_BLOCK)
        name_location = w.current_location
        w.write_latin1z(compiled.name, f'TechniqueSet {compiled.name} name')
        emitted = []
        for slot, technique in enumerate(compiled.slots):
            if technique is None or technique_first_slot[technique.content_key] != slot:
                continue
            emitted.append((slot, technique))
            self._write_technique(w, technique, slot)
        w.pop_block()

        c.register_result('techset.owned:' + self.symbol, {
            'root_physical': root_physical,
            'root_location': root_location,
            'name_location': name_location,
            'serialized_name': compiled.name,
            'owned_techniques': len(emitted),
            'slot_map': {str(slot): technique.name for slot, technique in emitted},
            'estimate_bytes': compiled.serialized_size_estimate(),
        })

    # -- nested payloads ----------------------------------------------------

    def _write_technique(self, w: RelocatingZoneWriter, technique, slot: int) -> None:
        w.align_logical_only(4)
        w.define_symbol(nested_symbol(self.symbol, f'technique.{technique.content_key}'))
        root = bytearray(PS3_TECHNIQUE_ROOT_SIZE)
        struct.pack_into('>I', root, 0, FOLLOWING)
        struct.pack_into('>HH', root, 4, technique.flags & 0xFFFF, len(technique.passes))
        w.write_in_block(bytes(root), f'technique {technique.name} root')

        pass_root_physical = []
        pass_plans = []
        # Plan-time occurrence tracking within THIS technique.  A declaration
        # repeated by a later pass packs backward to the first pass's payload
        # (the loader defers decl/args cell validation until the owning child
        # is materialized, so an earlier-pass inline owner is a legal target).
        # A shader repeated by a later pass of the SAME technique is emitted
        # again under an occurrence-suffixed alias: its pass root is read
        # before the first occurrence's INSERT alias cell exists, so packing
        # backward is not proven safe there.  Across techniques and techsets
        # the alias cell always precedes the referencing pass roots in the
        # stream, and the registry-packed path stays in use.
        local_decl_pending: dict[str, str] = {}
        local_shader_pending: set[str] = set()
        duplicate_ordinal = 0
        for index, pass_ in enumerate(technique.passes):
            physical = w.physical_position
            pass_root_physical.append(physical)
            row = bytearray(PS3_PASS_SIZE)
            plan = {'decl': None, 'vs': None, 'ps': None}
            # vertex declaration cell (+0)
            if pass_.decl is None:
                struct.pack_into('>I', row, 0, NULL)
            elif pass_.decl.content_key in self.registry.decl_symbols:
                plan['decl'] = ('packed', self.registry.decl_symbols[pass_.decl.content_key])
            elif pass_.decl.content_key in local_decl_pending:
                plan['decl'] = ('packed', local_decl_pending[pass_.decl.content_key])
            else:
                struct.pack_into('>I', row, 0, FOLLOWING)
                plan['decl'] = ('inline', pass_.decl)
                local_decl_pending[pass_.decl.content_key] = nested_symbol(
                    self.symbol, f'decl.{pass_.decl.content_key}')
            # shader cells (+4 / +8)
            for offset, key, shader in ((4, 'vs', pass_.vertex_shader), (8, 'ps', pass_.pixel_shader)):
                alias = self.registry.shader_alias_symbols.get(shader.content_key)
                if alias is not None:
                    plan[key] = ('packed', alias, shader)
                elif shader.content_key in local_shader_pending:
                    struct.pack_into('>I', row, offset, INSERT)
                    duplicate_ordinal += 1
                    plan[key] = ('inline_dup', shader, duplicate_ordinal)
                else:
                    struct.pack_into('>I', row, offset, INSERT)
                    plan[key] = ('inline', shader)
                    local_shader_pending.add(shader.content_key)
            row[12] = pass_.per_prim_arg_count
            row[13] = pass_.per_obj_arg_count
            row[14] = pass_.stable_arg_count
            row[15] = pass_.custom_sampler_flags & 0xFF
            struct.pack_into('>I', row, 20, FOLLOWING if pass_.args else NULL)
            w.write_in_block(bytes(row), f'technique {technique.name} pass {index} root')
            pass_plans.append(plan)

        for index, (pass_, plan, physical) in enumerate(zip(technique.passes, pass_plans, pass_root_physical)):
            label = f'{technique.name} pass {index}'
            decl_plan = plan['decl']
            if decl_plan is not None and decl_plan[0] == 'inline':
                decl = decl_plan[1]
                w.align_logical_only(4)
                symbol = nested_symbol(self.symbol, f'decl.{decl.content_key}')
                w.define_symbol(symbol)
                self.registry.decl_symbols[decl.content_key] = symbol
                self.registry.emitted_decls += 1
                w.write_in_block(decl.serialized(), f'{label} vertex declaration')
            elif decl_plan is not None:
                self.registry.reused_decls += 1
                w.register_pointer_relocation(physical + 0, decl_plan[1],
                                              description=f'{label} shared vertex declaration')
            for cell, key in ((4, 'vs'), (8, 'ps')):
                shader_plan = plan[key]
                if shader_plan[0] == 'inline':
                    self._write_shader(w, shader_plan[1], label)
                elif shader_plan[0] == 'inline_dup':
                    self._write_shader(w, shader_plan[1], label,
                                       occurrence=shader_plan[2])
                else:
                    self.registry.reused_shaders += 1
                    w.register_pointer_relocation(physical + cell, shader_plan[1],
                                                  kind='packed_alias',
                                                  description=f'{label} shared {key} asset')
            if pass_.args:
                w.align_logical_only(4)
                literal_args = []
                for argument in pass_.args:
                    record = bytearray(8)
                    struct.pack_into('>HH', record, 0, argument.type, argument.dest)
                    if argument.type in (ARG_LITERAL_VERTEX_CONST, ARG_LITERAL_PIXEL_CONST):
                        if argument.literal is None:
                            struct.pack_into('>I', record, 4, NULL)
                        else:
                            struct.pack_into('>I', record, 4, FOLLOWING)
                            literal_args.append(argument)
                    else:
                        struct.pack_into('>I', record, 4, argument.raw_be)
                    w.write_in_block(bytes(record), f'{label} shader argument')
                for argument in literal_args:
                    w.align_logical_only(16)
                    w.write_in_block(struct.pack('>4f', *argument.literal), f'{label} literal float4')

        w.write_latin1z(technique.name, f'technique {technique.name} name')

    def _write_shader(self, w: RelocatingZoneWriter, shader: CompiledShader, label: str,
                      *, occurrence: int = 0) -> None:
        registry = self.registry
        suffix = f'.occ{occurrence}' if occurrence else ''
        alias_symbol = nested_symbol(self.symbol, f'shader.{shader.content_key}{suffix}')
        w.define_insert_pointer_alias(alias_symbol,
                                      canonical_identity=f'rsx-shader:{shader.content_key}{suffix}',
                                      description=f'{label} {shader.kind} shader alias')
        if not occurrence:
            registry.shader_alias_symbols[shader.content_key] = alias_symbol
            registry.shader_names[shader.content_key] = shader.name
        registry.emitted_shaders += 1

        w.push_block(TEMP_BLOCK)
        w.align_logical_only(4)
        root_size = PS3_VERTEX_SHADER_ROOT if shader.kind == 'vertex' else PS3_PIXEL_SHADER_ROOT
        root = bytearray(root_size)
        struct.pack_into('>I', root, 0, FOLLOWING)
        struct.pack_into('>I', root, 4, FOLLOWING)
        struct.pack_into('>I', root, 8, len(shader.blob))
        if shader.kind == 'pixel':
            control = shader.control_bytes
            if len(control) != 12:
                raise ValueError(f'{label}: pixel control bytes {len(control)}')
            root[0x0C:0x18] = control
        w.write_in_block(bytes(root), f'{label} {shader.kind} shader root')

        w.push_block(LARGE_BLOCK)
        w.write_latin1z(shader.name, f'{label} {shader.kind} shader name')
        w.push_block(TEMP_BLOCK)
        w.align_logical_only(16)
        w.write_in_block(shader.blob, f'{label} {shader.kind} shader Cg binary')
        w.pop_block()
        w.pop_block()
        w.pop_block()
