#!/usr/bin/env python3
"""Compiled TechniqueSet end-to-end: compile -> serialize -> stream readback.

The readback walker consumes the serialized techset exactly the way the PS3
loader family does (root, LARGE children in order, nested TEMP shader roots,
16-aligned Cg payloads) and re-proves the shader blobs by decoding their
microcode and executing it against the PC originals.
"""
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cod4porter.assets.techset import (
    OwnedTechniqueSetNode, PS3_PASS_SIZE, PS3_PIXEL_SHADER_ROOT,
    PS3_TECHNIQUE_ROOT_SIZE, PS3_TECHSET_ROOT_SIZE, PS3_VERTEX_SHADER_ROOT,
    TechsetEmissionRegistry,
)
from cod4porter.graph import write_graph
from cod4porter.zone_writer import FOLLOWING, INSERT, NULL, ZONE_HEADER_SIZE
from cod4porter.rsx import cgbinary, d3d9, interp, nv40
from cod4porter.rsx.d3d9 import assemble, dst, src
from cod4porter.rsx.techset_compile import (
    PS3_SLOT_TO_PC, PS3_TECHNIQUE_SLOTS, compile_techniqueset,
)
from cod4porter.rsx.techset_parse import (
    ARG_CODE_PIXEL_CONST, ARG_CODE_VERTEX_CONST, ARG_LITERAL_PIXEL_CONST,
    ARG_LITERAL_VERTEX_CONST, ARG_MATERIAL_PIXEL_SAMPLER,
    PcArgRecord, PcDeclRecord, PcPassRecord, PcShaderRecord,
    PcTechniqueRecord, PcTechniqueSetFull, parse_full_techniqueset,
)

R, V, C, T, S = d3d9.REG_TEMP, d3d9.REG_INPUT, d3d9.REG_CONST, d3d9.REG_ADDR, d3d9.REG_SAMPLER


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def _vertex_bytecode():
    return assemble('vertex', 2, 0, [
        ('dcl', d3d9.USAGE_POSITION, 0, dst(V, 0)),
        ('dcl', d3d9.USAGE_TEXCOORD, 0, dst(V, 1)),
        ('def', 40, (2.0, 0.5, 0.0, 1.0)),
        (d3d9.OP_M4X4, 0, dst(R, 0), (src(V, 0), src(C, 0))),
        (d3d9.OP_MUL, 0, dst(R, 1, 'xy'), (src(V, 1), src(C, 40, 'x'))),
        (d3d9.OP_MOV, 0, dst(d3d9.REG_RASTOUT, 0), (src(R, 0),)),
        (d3d9.OP_MOV, 0, dst(d3d9.REG_TEXCRDOUT, 0, 'xy'), (src(R, 1, 'xyxx'),)),
    ])


def _pixel_bytecode():
    return assemble('pixel', 2, 0, [
        ('dcl', 0, 0, dst(T, 0)),
        ('dcl_sampler', d3d9.SAMPLER_2D, dst(S, 0)),
        ('def', 1, (0.25, 0.5, 0.75, 1.0)),
        (d3d9.OP_TEXLD, 0, dst(R, 0), (src(T, 0), src(S, 0))),
        (d3d9.OP_MUL, 0, dst(R, 0), (src(R, 0), src(C, 0))),
        (d3d9.OP_MAD, 0, dst(R, 0), (src(R, 0), src(C, 1), src(C, 1, 'wwww'))),
        (d3d9.OP_MOV, 0, dst(d3d9.REG_COLOROUT, 0), (src(R, 0),)),
    ])


def _second_pixel_bytecode():
    return assemble('pixel', 2, 0, [
        ('dcl', 0, 0, dst(T, 0)),
        (d3d9.OP_ADD, 0, dst(R, 0), (src(T, 0), src(C, 2))),
        (d3d9.OP_MOV, 0, dst(d3d9.REG_COLOROUT, 0), (src(R, 0),)),
    ])


def _decl():
    return PcDeclRecord(3, False, ((0, 0), (2, 4), (3, 1)), 0x1000)


def _pc_techset(name='wc_l_custom_t0c0', *, second_pass_shader=None):
    vertex = PcShaderRecord('vertex', 'custom_world_vs', _vertex_bytecode(), 0, 0x2000)
    pixel = PcShaderRecord('pixel', 'custom_world_ps', _pixel_bytecode(), 0, 0x3000)
    args = (
        PcArgRecord(ARG_CODE_VERTEX_CONST, 0, (4 << 24) | 0, None),           # viewproj rows c0..c3
        PcArgRecord(ARG_LITERAL_VERTEX_CONST, 41, 0, (1.0, 2.0, 3.0, 4.0)),   # dropped: never read
        PcArgRecord(ARG_MATERIAL_PIXEL_SAMPLER, 0, 0xDEADBEEF, None),
        PcArgRecord(ARG_CODE_PIXEL_CONST, 0, (7 << 16) | (0 << 8) | 1, None), # c0 <- code row 7
        PcArgRecord(ARG_LITERAL_PIXEL_CONST, 1, 0, (0.1, 0.2, 0.3, 0.4)),     # c1 literal
        PcArgRecord(ARG_LITERAL_PIXEL_CONST, 9, 0, (9.0, 9.0, 9.0, 9.0)),     # dropped: c9 unread
    )
    passes = [PcPassRecord(_decl(), vertex, pixel, 0, 0, len(args) - 0, 0x5, args)]
    if second_pass_shader is not None:
        passes.append(PcPassRecord(_decl(), vertex, second_pass_shader, 0, 0, 1,
                                   0, (PcArgRecord(ARG_CODE_PIXEL_CONST, 2, (3 << 16) | 1, None),)))
    technique = PcTechniqueRecord('lit', 2, tuple(passes), 0x4000)
    unlit = PcTechniqueRecord('unlit', 0, (PcPassRecord(_decl(), vertex, pixel, 0, 0, 4, 0, args[:4] + args[4:5]),), 0x5000)
    slots = [None] * 34
    slots[4] = unlit          # TECHNIQUE_UNLIT
    slots[7] = technique      # TECHNIQUE_LIT
    slots[8] = technique      # TECHNIQUE_LIT_SUN shares the same technique
    slots[0x0E] = unlit       # instanced family: dropped on PS3
    return PcTechniqueSetFull(name, 2, tuple(slots), 55)


def test_compile_shapes():
    compiled = compile_techniqueset(_pc_techset())
    check(compiled.name == 'wc_l_custom_t0c0', compiled.name)
    check(len(compiled.slots) == PS3_TECHNIQUE_SLOTS, 'slot count')
    check(compiled.slots[4] is not None and compiled.slots[4].name == 'unlit', 'unlit slot')
    check(compiled.slots[7] is not None and compiled.slots[7].name == 'lit', 'lit slot')
    check(compiled.slots[8] is compiled.slots[7], 'shared technique object')
    check(compiled.dropped_pc_slots == (0x0E,), f'dropped {compiled.dropped_pc_slots}')
    lit = compiled.slots[7].passes[0]
    # dropped: unread vertex literal c41 and unread pixel literal c9
    check(len(lit.dropped_args) == 2, f'dropped args {lit.dropped_args}')
    check(lit.per_prim_arg_count == 0 and lit.per_obj_arg_count == 0, 'arg slices')
    check(lit.stable_arg_count == 4, f'stable args {lit.stable_arg_count}')
    types = [a.type for a in lit.args]
    check(types == [ARG_CODE_VERTEX_CONST, ARG_MATERIAL_PIXEL_SAMPLER,
                    ARG_CODE_PIXEL_CONST, ARG_LITERAL_PIXEL_CONST], f'arg order {types}')
    # pixel const destinations are parameter ordinals of the fragment blob
    pixel = lit.pixel_shader
    check(set(pixel.parameter_index) == {0, 1}, f'params {pixel.parameter_index}')
    code_arg = lit.args[2]
    check(code_arg.dest == pixel.parameter_index[0], 'code pixel const param index')
    literal_arg = lit.args[3]
    check(literal_arg.dest == pixel.parameter_index[1], 'literal pixel const param index')
    check(literal_arg.literal == (0.1, 0.2, 0.3, 0.4), 'literal value kept')
    # vertex code const kept with register dest
    check(lit.args[0].dest == 0 and lit.args[0].raw_be >> 24 == 0, 'vertex code const dest')
    check((lit.args[0].raw_be & 0xFF) == 4, 'row count in BE union')
    # declaration destinations were translated to RSX attributes
    check(lit.decl.routing == ((0, 0), (2, 8), (3, 2)), f'decl routing {lit.decl.routing}')


class StreamReader:
    def __init__(self, zone, offset):
        self.z = zone
        self.p = offset

    def u8(self):
        value = self.z[self.p]; self.p += 1; return value

    def u16(self):
        value = struct.unpack_from('>H', self.z, self.p)[0]; self.p += 2; return value

    def u32(self):
        value = struct.unpack_from('>I', self.z, self.p)[0]; self.p += 4; return value

    def take(self, count):
        data = bytes(self.z[self.p:self.p + count]); self.p += count; return data

    def cstr(self):
        end = self.z.index(b'\0', self.p)
        value = self.z[self.p:end].decode('latin-1'); self.p = end + 1; return value


def _read_shader(reader, expect_inline):
    root_marker = None
    marker = struct.unpack_from('>I', reader.z, reader.p - 4)[0]  # caller consumed cell
    kind_root = reader.p
    return None


def walk_techset(zone, root_physical, compiled, pre_seeded=()):
    """Walk the serialized techset stream, mirroring the loader order."""
    reader = StreamReader(zone, root_physical)
    root = reader.take(PS3_TECHSET_ROOT_SIZE)
    check(struct.unpack_from('>I', root, 0)[0] == FOLLOWING, 'root name marker')
    check(root[4] == compiled.world_vertex_format, 'world vertex format')
    slot_cells = [struct.unpack_from('>I', root, 8 + i * 4)[0] for i in range(PS3_TECHNIQUE_SLOTS)]
    name = reader.cstr()
    check(name == compiled.name, f'techset name {name}')
    shaders = {key: None for key in pre_seeded}
    first_slots = {}
    for slot, technique in enumerate(compiled.slots):
        cell = slot_cells[slot]
        if technique is None:
            check(cell == NULL, f'slot {slot} null')
            continue
        if technique.content_key in first_slots:
            check(cell not in (NULL, FOLLOWING, INSERT), f'slot {slot} packed to shared technique')
            continue
        first_slots[technique.content_key] = slot
        check(cell == FOLLOWING, f'slot {slot} FOLLOWING')
        reader.p = (reader.p + 0) if True else reader.p
        walk_technique(reader, technique, shaders)
    return shaders


def walk_technique(reader, technique, shaders):
    root = reader.take(PS3_TECHNIQUE_ROOT_SIZE)
    check(struct.unpack_from('>I', root, 0)[0] == FOLLOWING, 'technique name marker')
    flags, pass_count = struct.unpack_from('>HH', root, 4)
    check(flags == technique.flags, 'technique flags')
    check(pass_count == len(technique.passes), 'pass count')
    pass_roots = [reader.take(PS3_PASS_SIZE) for _ in range(pass_count)]
    for pass_, row in zip(technique.passes, pass_roots):
        decl_cell, vs_cell, ps_cell = struct.unpack_from('>3I', row, 0)
        check(row[12] == pass_.per_prim_arg_count and row[13] == pass_.per_obj_arg_count
              and row[14] == pass_.stable_arg_count, 'arg counts')
        check(row[15] == pass_.custom_sampler_flags, 'sampler flags')
        args_cell = struct.unpack_from('>I', row, 20)[0]
        if pass_.decl is not None:
            if decl_cell == FOLLOWING:
                decl = reader.take(2 + 13 * 2)
                check(decl == pass_.decl.serialized(), 'decl bytes')
            else:
                check(decl_cell not in (NULL, INSERT), 'decl packed')
        for cell, shader in ((vs_cell, pass_.vertex_shader), (ps_cell, pass_.pixel_shader)):
            if shader.content_key in shaders:
                if cell == INSERT:
                    # A repeat within the same technique is emitted again
                    # (occurrence-suffixed alias); consume and verify the copy.
                    root_size = PS3_VERTEX_SHADER_ROOT if shader.kind == 'vertex' else PS3_PIXEL_SHADER_ROOT
                    dup_root = reader.take(root_size)
                    check(struct.unpack_from('>I', dup_root, 8)[0] == len(shader.blob), 'dup blob size')
                    check(reader.cstr() == shader.name, 'dup shader name')
                    check(reader.take(len(shader.blob)) == shader.blob, 'dup blob bytes')
                    continue
                check(cell not in (NULL, FOLLOWING, INSERT), f'{shader.kind} packed reuse')
                continue
            check(cell == INSERT, f'{shader.kind} INSERT first occurrence')
            root_size = PS3_VERTEX_SHADER_ROOT if shader.kind == 'vertex' else PS3_PIXEL_SHADER_ROOT
            shader_root = reader.take(root_size)
            check(struct.unpack_from('>I', shader_root, 0)[0] == FOLLOWING, 'shader name marker')
            check(struct.unpack_from('>I', shader_root, 4)[0] == FOLLOWING, 'shader loadDef marker')
            declared = struct.unpack_from('>I', shader_root, 8)[0]
            check(declared == len(shader.blob), 'shader blob size')
            if shader.kind == 'pixel':
                check(shader_root[0x0C:0x18] == shader.control_bytes, 'pixel control bytes')
            shader_name = reader.cstr()
            check(shader_name == shader.name, f'shader asset name {shader_name}')
            blob = reader.take(declared)
            check(blob == shader.blob, 'shader blob bytes')
            shaders[shader.content_key] = blob
        if pass_.args:
            check(args_cell == FOLLOWING, 'args FOLLOWING')
            literal_queue = []
            for argument in pass_.args:
                record = reader.take(8)
                arg_type, arg_dest = struct.unpack_from('>HH', record, 0)
                union = struct.unpack_from('>I', record, 4)[0]
                check(arg_type == argument.type and arg_dest == argument.dest, 'arg row')
                if argument.type in (ARG_LITERAL_VERTEX_CONST, ARG_LITERAL_PIXEL_CONST):
                    check(union == FOLLOWING, 'literal marker')
                    literal_queue.append(argument)
                else:
                    check(union == argument.raw_be, 'arg union value')
            for argument in literal_queue:
                literal = reader.take(16)
                check(literal == struct.pack('>4f', *argument.literal), 'literal payload')
        else:
            check(args_cell == NULL, 'args NULL')
    technique_name = reader.cstr()
    check(technique_name == technique.name, 'technique name string')


def test_serialize_and_walk():
    compiled = compile_techniqueset(_pc_techset())
    registry = TechsetEmissionRegistry()
    node = OwnedTechniqueSetNode(compiled, 'techset:test:wc_l_custom_t0c0', registry)
    result = write_graph([node])
    zone = result.zone.zone_bytes
    info = result.context.results['techset.owned:' + node.symbol]
    shaders = walk_techset(zone, info['root_physical'], compiled)
    check(len(shaders) == 2, f'unique shader payloads {len(shaders)}')
    check(registry.emitted_shaders == 2 and registry.reused_shaders == 2,
          f'dedup counts {registry.emitted_shaders}/{registry.reused_shaders}')

    # blob-level proof: parse, decode, execute against the PC original
    for pass_ in (compiled.slots[7].passes[0],):
        vertex_program = cgbinary.parse_cg_binary(pass_.vertex_shader.blob)
        check(vertex_program.profile == cgbinary.PROFILE_VERTEX, 'vertex profile')
        embedded = [p for p in vertex_program.parameters
                    if p.variability == cgbinary.CG_VARIABILITY_CONSTANT]
        check(len(embedded) == 1 and embedded[0].resource_index == 40, 'embedded def c40')
        decoded = nv40.decode_vertex_program(vertex_program.ucode)
        check(decoded == pass_.vertex_shader.translation.instructions, 'vertex ucode roundtrip')

        fragment_program = cgbinary.parse_cg_binary(pass_.pixel_shader.blob)
        check(fragment_program.is_fragment, 'fragment profile')
        check(fragment_program.descriptor['register_count'] >= 2, 'register count')
        control = cgbinary.fragment_control_bytes(pass_.pixel_shader.blob)
        check(len(control) == 12, 'control bytes present')
        uniforms = [p for p in fragment_program.parameters
                    if p.variability == cgbinary.CG_VARIABILITY_UNIFORM]
        check([p.resource_index for p in uniforms] == [0, 1], 'fragment param registers')
        for parameter in uniforms:
            check(parameter.embedded_offsets, f'{parameter.name} has patch sites')
            for site in parameter.embedded_offsets:
                decoded_at = nv40.decode_fragment_program(fragment_program.ucode)
                payloads = []
                pc = 0
                for instruction in decoded_at:
                    if instruction.inline_constant is not None:
                        payloads.append(pc + 16)
                    pc += instruction.byte_count
                check(site in payloads, f'patch site 0x{site:X} targets an inline payload')

    # execution equivalence through the serialized bytes
    blob = compiled.slots[7].passes[0].pixel_shader.blob
    program = cgbinary.parse_cg_binary(blob)
    instructions = nv40.decode_fragment_program(program.ucode)
    pc_program = d3d9.disassemble(_pixel_bytecode())
    inputs = {(T, 0): [0.3, 0.7, 0.1, 1.0]}
    d3d_result = interp.run_d3d9(pc_program, inputs=inputs)
    rsx_result = interp.run_rsx_fragment(
        instructions, attributes={nv40.FRAG_ATTR_TEX0: inputs[(T, 0)]})
    expected = d3d_result['outputs'][(d3d9.REG_COLOROUT, 0)]
    for component in range(4):
        check(abs(expected[component] - rsx_result['color0'][component]) < 1e-4,
              f'serialized execution equivalence [{component}]')


def test_multipass_shared_shader_and_decl_within_technique():
    # Audit finding 1: two passes of ONE technique sharing decl + pixel shader.
    pixel = PcShaderRecord('pixel', 'shared_ps', _pixel_bytecode(), 0, 0x3000)
    full = _pc_techset(second_pass_shader=pixel)
    compiled = compile_techniqueset(full)
    lit = compiled.slots[7]
    check(len(lit.passes) == 2, 'two passes')
    check(lit.passes[0].pixel_shader.content_key == lit.passes[1].pixel_shader.content_key,
          'shared pixel shader fixture')
    check(lit.passes[0].decl.content_key == lit.passes[1].decl.content_key, 'shared decl fixture')
    registry = TechsetEmissionRegistry()
    node = OwnedTechniqueSetNode(compiled, 'techset:test:multipass', registry)
    result = write_graph([node])
    info = result.context.results['techset.owned:' + node.symbol]
    walk_techset(result.zone.zone_bytes, info['root_physical'], compiled)
    # Second decl occurrence packs backward; second shader occurrence re-emits.
    check(registry.reused_decls >= 1, f'decl packed backward ({registry.reused_decls})')


def test_shader_reuse_across_techsets():
    registry = TechsetEmissionRegistry()
    first = OwnedTechniqueSetNode(compile_techniqueset(_pc_techset('wc_l_custom_t0c0')),
                                  'techset:test:a', registry)
    second = OwnedTechniqueSetNode(compile_techniqueset(_pc_techset('mc_l_custom_t0c0')),
                                   'techset:test:b', registry)
    result = write_graph([first, second])
    check(registry.emitted_shaders == 2, f'first zone-wide emission {registry.emitted_shaders}')
    check(registry.reused_shaders == 6, f'cross-techset reuse {registry.reused_shaders}')
    info = result.context.results['techset.owned:' + second.symbol]
    walk_techset(result.zone.zone_bytes, info['root_physical'],
                 second.compiled, pre_seeded=set(registry.shader_alias_symbols))


def test_repeated_write_runs_share_one_registry():
    # Regression for the 22.2.1 mp_gulag failure: a port lays the zone out
    # several times with fresh writers (block-size measurement pass, then the
    # two determinism builds) while the nodes keep ONE TechsetEmissionRegistry.
    # 22.2.1 left the dedup dicts populated across runs, so the next run
    # planned 'packed' references to symbols only defined in the previous
    # writer: "relocation target 'techset:...::decl....' was never defined".
    registry = TechsetEmissionRegistry()
    nodes = [OwnedTechniqueSetNode(compile_techniqueset(_pc_techset('wc_l_custom_t0c0')),
                                   'techset:test:a', registry),
             OwnedTechniqueSetNode(compile_techniqueset(_pc_techset('mc_l_custom_t0c0')),
                                   'techset:test:b', registry)]
    zones = []
    for run in range(3):  # measure + first build + determinism build
        result = write_graph(nodes)
        zones.append(result.zone.zone_bytes)
        check(registry.emitted_shaders == 2 and registry.reused_shaders == 6,
              f'run {run} counters describe one run, not a sum '
              f'({registry.emitted_shaders}/{registry.reused_shaders})')
        info = result.context.results['techset.owned:' + nodes[1].symbol]
        walk_techset(result.zone.zone_bytes, info['root_physical'],
                     nodes[1].compiled, pre_seeded=set(registry.shader_alias_symbols))
    check(zones[0] == zones[1] == zones[2], 'write runs must be byte-identical')


def test_pc_parse_roundtrip():
    """Craft a PC-layout byte image plus resolver and re-parse it."""
    zone = bytearray(0x8000)
    pointers = {}

    def put_cstr(offset, text):
        zone[offset:offset + len(text) + 1] = text.encode() + b'\0'

    vertex_bytecode = _vertex_bytecode()
    pixel_bytecode = _pixel_bytecode()
    vs_root, ps_root = 0x1000, 0x1100
    vs_prog, ps_prog = 0x1200, 0x1200 + len(vertex_bytecode)
    zone[vs_prog:vs_prog + len(vertex_bytecode)] = vertex_bytecode
    zone[ps_prog:ps_prog + len(pixel_bytecode)] = pixel_bytecode
    struct.pack_into('<HH', zone, vs_root + 12, len(vertex_bytecode) // 4, 0)
    struct.pack_into('<HH', zone, ps_root + 12, len(pixel_bytecode) // 4, 0)
    pointers[vs_root] = 'sh_vs'
    pointers[vs_root + 8] = vs_prog
    pointers[ps_root] = 'sh_ps'
    pointers[ps_root + 8] = ps_prog

    decl_off = 0x2000
    zone[decl_off] = 2
    zone[decl_off + 1] = 0
    zone[decl_off + 4:decl_off + 8] = bytes((0, 0, 2, 4))

    lit_off = 0x2100
    struct.pack_into('<4f', zone, lit_off, 5.0, 6.0, 7.0, 8.0)

    args_off = 0x2200
    struct.pack_into('<HHI', zone, args_off, ARG_CODE_VERTEX_CONST, 0, (4 << 24))
    struct.pack_into('<HHI', zone, args_off + 8, ARG_LITERAL_PIXEL_CONST, 1, 0)
    pointers[args_off + 12] = lit_off

    tech_off = 0x3000
    struct.pack_into('<HH', zone, tech_off + 4, 1, 1)   # flags=1, passCount=1
    pointers[tech_off] = 'my_technique'
    pass_off = tech_off + 8
    pointers[pass_off + 0] = decl_off
    pointers[pass_off + 4] = vs_root
    pointers[pass_off + 8] = ps_root
    zone[pass_off + 12] = 0
    zone[pass_off + 13] = 0
    zone[pass_off + 14] = 2
    zone[pass_off + 15] = 9
    pointers[pass_off + 16] = args_off

    ts_root = 0x4000
    for slot in range(34):
        pointers[ts_root + 12 + slot * 4] = tech_off if slot == 7 else None

    class FakeTechset:
        root_offset = ts_root
        name = 'wc_test'
        world_vertex_format = 3
        source_asset_index = 9

    full = parse_full_techniqueset(bytes(zone), lambda f: pointers.get(f), FakeTechset())
    check(full.name == 'wc_test' and full.world_vertex_format == 3, 'techset head')
    technique = full.techniques[7]
    check(technique is not None and technique.name == 'my_technique', 'technique parse')
    check(technique.flags == 1 and len(technique.passes) == 1, 'technique fields')
    pass_ = technique.passes[0]
    check(pass_.decl.routing == ((0, 0), (2, 4)), 'decl routing parse')
    check(pass_.vertex_shader.bytecode == vertex_bytecode, 'vertex bytecode parse')
    check(pass_.pixel_shader.name == 'sh_ps', 'pixel name parse')
    check(pass_.args[0].code_const == (0, 0, 4), 'code const parse')
    check(pass_.args[1].literal == (5.0, 6.0, 7.0, 8.0), 'literal parse')
    check(pass_.custom_sampler_flags == 9, 'custom sampler flags')

    compiled = compile_techniqueset(full)
    check(compiled.slots[7] is not None, 'compiled from parsed input')


def main():
    tests = [value for key, value in sorted(globals().items()) if key.startswith('test_')]
    for test in tests:
        test()
    print(json.dumps({'passed': True, 'tests': len(tests)}))


if __name__ == '__main__':
    main()
