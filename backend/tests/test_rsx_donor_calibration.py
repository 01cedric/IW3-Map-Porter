#!/usr/bin/env python3
"""Linked-memory techset extraction, calibration rules and donor round trip."""
import json
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools' / 'ps3_loader_emulator'))

import techsetcheck
from cod4porter.assets.techset import OwnedTechniqueSetNode, TechsetEmissionRegistry
from cod4porter.graph import write_graph
from cod4porter.rsx import cgbinary
from cod4porter.rsx.donor import load_donor_catalog
from cod4porter.rsx.techset_compile import compile_techniqueset

from test_rsx_techset_pipeline import _pc_techset, walk_techset


def check(condition, message):
    if not condition:
        raise AssertionError(message)


class FakeMemory:
    def __init__(self):
        self.data = bytearray(0x200000)

    def write(self, address, blob):
        self.data[address:address + len(blob)] = blob

    def u8(self, address):
        return self.data[address]

    def u16(self, address):
        return struct.unpack_from('>H', self.data, address)[0]

    def u32(self, address):
        return struct.unpack_from('>I', self.data, address)[0]


def build_linked_image(compiled):
    """Lay a compiled techset out in fake linked memory the way the loader would."""
    memory = FakeMemory()
    cursor = [0x10000]

    def alloc(size, align=4):
        cursor[0] = (cursor[0] + align - 1) & ~(align - 1)
        address = cursor[0]
        cursor[0] += size
        return address

    def put_cstr(text):
        address = alloc(len(text) + 1, 1)
        memory.write(address, text.encode('latin-1') + b'\0')
        return address

    shader_addresses = {}

    def put_shader(shader):
        if shader.content_key in shader_addresses:
            return shader_addresses[shader.content_key]
        root = alloc(0x18 if shader.kind == 'pixel' else 0x0C)
        blob_address = alloc(len(shader.blob), 16)
        memory.write(blob_address, shader.blob)
        memory.write(root, struct.pack('>3I', put_cstr(shader.name), blob_address, len(shader.blob)))
        if shader.kind == 'pixel':
            memory.write(root + 0x0C, shader.control_bytes)
        shader_addresses[shader.content_key] = root
        return root

    technique_addresses = {}

    def put_technique(technique):
        if technique.content_key in technique_addresses:
            return technique_addresses[technique.content_key]
        root = alloc(8 + len(technique.passes) * 0x18)
        memory.write(root, struct.pack('>IHH', put_cstr(technique.name),
                                       technique.flags, len(technique.passes)))
        for index, pass_ in enumerate(technique.passes):
            base = root + 8 + index * 0x18
            decl_address = 0
            if pass_.decl is not None:
                decl_address = alloc(0x1C)
                memory.write(decl_address, pass_.decl.serialized())
            args_address = 0
            if pass_.args:
                args_address = alloc(len(pass_.args) * 8)
                for i, argument in enumerate(pass_.args):
                    value = argument.raw_be
                    if argument.literal is not None:
                        literal_address = alloc(16, 16)
                        memory.write(literal_address, struct.pack('>4f', *argument.literal))
                        value = literal_address
                    memory.write(args_address + i * 8,
                                 struct.pack('>HHI', argument.type, argument.dest, value))
            memory.write(base, struct.pack('>3I', decl_address,
                                           put_shader(pass_.vertex_shader),
                                           put_shader(pass_.pixel_shader)))
            memory.write(base + 12, bytes((pass_.per_prim_arg_count, pass_.per_obj_arg_count,
                                           pass_.stable_arg_count, pass_.custom_sampler_flags)))
            memory.write(base + 20, struct.pack('>I', args_address))
        technique_addresses[technique.content_key] = root
        return root

    techset = alloc(0x70)
    memory.write(techset, struct.pack('>I', put_cstr(compiled.name)))
    memory.data[techset + 4] = compiled.world_vertex_format
    for slot, technique in enumerate(compiled.slots):
        if technique is not None:
            memory.write(techset + 8 + slot * 4, struct.pack('>I', put_technique(technique)))
    return memory, techset


def cstr_reader(memory):
    def cstr(address, limit=256):
        end = memory.data.index(b'\0', address)
        return memory.data[address:end].decode('latin-1')
    return cstr


def test_walk_and_calibrate():
    compiled = compile_techniqueset(_pc_techset())
    memory, address = build_linked_image(compiled)
    walked = techsetcheck.walk_techset(memory, cstr_reader(memory), address)
    check(walked['name'] == compiled.name, walked['name'])
    check(set(walked['slots']) == {slot for slot, t in enumerate(compiled.slots) if t is not None},
          f'slots {sorted(walked["slots"])}')
    lit = walked['slots'][7]
    check(lit['name'] == 'lit' and len(lit['passes']) == 1, 'lit walk')
    pixel_key = lit['passes'][0]['pixel_shader']
    check(walked['shaders'][pixel_key]['blob'] == compiled.slots[7].passes[0].pixel_shader.blob,
          'pixel blob extraction')

    calibration = techsetcheck.calibrate([walked])
    check(calibration['passed'], f'calibration failed: {calibration["failed_rules"]} '
                                 f'{[r for r in calibration["rules"] if not r["passed"]]}')
    rule_names = {r['rule'] for r in calibration['rules']}
    check('pixel_const_dest_is_parameter_index' in rule_names, 'dest rule present')
    check('patch_sites_are_inline_constant_payloads' in rule_names, 'patch rule present')


def test_calibration_fails_closed():
    compiled = compile_techniqueset(_pc_techset())
    memory, address = build_linked_image(compiled)
    walked = techsetcheck.walk_techset(memory, cstr_reader(memory), address)
    # Corrupt one pixel-const dest so it indexes outside the parameter table.
    for technique in walked['slots'].values():
        for pass_ in technique['passes']:
            for argument in pass_['args']:
                if argument['type'] in (5, 7):
                    argument['dest'] = 250
    calibration = techsetcheck.calibrate([walked])
    check(not calibration['passed'], 'corrupted dest must fail calibration')
    check('pixel_const_dest_is_parameter_index' in calibration['failed_rules'],
          calibration['failed_rules'])


def test_donor_roundtrip():
    compiled = compile_techniqueset(_pc_techset())
    memory, address = build_linked_image(compiled)
    walked = techsetcheck.walk_techset(memory, cstr_reader(memory), address)
    with tempfile.TemporaryDirectory() as folder:
        out_json = Path(folder) / 'techset-donors.json'
        out_bin = Path(folder) / 'techset-donors.bin'
        summary = techsetcheck.export_donor_catalog([walked], out_json, out_bin,
                                                    source_zones=['fake'])
        check(summary['techsets'] == 1, summary)
        catalog = load_donor_catalog(out_json)
        check(compiled.name in catalog, 'catalog lookup')
        donor = catalog.techniqueset(compiled.name)
        check(donor.world_vertex_format == compiled.world_vertex_format, 'donor vf')
        check([s is not None for s in donor.slots] == [s is not None for s in compiled.slots],
              'donor slot shape')
        donor_pass = donor.slots[7].passes[0]
        original_pass = compiled.slots[7].passes[0]
        check(donor_pass.pixel_shader.blob == original_pass.pixel_shader.blob, 'donor pixel blob')
        check(donor_pass.pixel_shader.control_bytes == original_pass.pixel_shader.control_bytes,
              'donor control bytes')
        check(donor_pass.decl.serialized() == original_pass.decl.serialized(), 'donor decl bytes')
        check([(a.type, a.dest) for a in donor_pass.args] ==
              [(a.type, a.dest) for a in original_pass.args], 'donor args')

        registry = TechsetEmissionRegistry()
        node = OwnedTechniqueSetNode(donor, 'techset:test:donor', registry)
        result = write_graph([node])
        info = result.context.results['techset.owned:' + node.symbol]
        walk_techset(result.zone.zone_bytes, info['root_physical'], donor)


def main():
    tests = [value for key, value in sorted(globals().items()) if key.startswith('test_')]
    for test in tests:
        test()
    print(json.dumps({'passed': True, 'tests': len(tests)}))


if __name__ == '__main__':
    main()
