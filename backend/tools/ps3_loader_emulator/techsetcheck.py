"""Linked-memory TechniqueSet extraction, layout calibration and donor export.

After ``linkcheck`` has loaded real PS3 zones through the game's own loader,
every MaterialTechniqueSet sits fully linked in emulated memory.  This module

* walks those techsets structurally (26 slots, 0x18 passes, 0x1C vertex
  declarations, 0x0C/0x18 shader roots, Cg binary payloads),
* CALIBRATES every layout assumption the PC-to-RSX compiler makes against
  that retail data - the pixel-argument ``dest`` -> Cg parameter-index model,
  patch sites landing on inline-constant payloads, control bytes matching the
  Cg descriptor, declaration shapes, register files - and reports each rule
  with its measured evidence, and
* exports a DONOR CATALOG: complete retail techsets (payloads included) that
  the porter can transplant into generated zones byte-faithfully.

Nothing here guesses: a techset that does not walk cleanly is reported and
excluded, never silently repaired.
"""

from __future__ import annotations

import hashlib
import json
import struct
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from cod4porter.rsx import cgbinary, nv40  # noqa: E402

TECHSET_TYPE = 0x07
SLOTS = 26
PASS_SIZE = 0x18
DECL_SIZE = 0x1C
DECL_ROUTING = 13
VS_ROOT = 0x0C
PS_ROOT = 0x18
MAX_BLOB = 0x40000

ARG_LITERALS = (1, 7)
ARG_PIXEL_CONSTS = (5, 6, 7)
ARG_VERTEX_CONSTS = (0, 1, 3)
ARG_SAMPLERS = (2, 4)


class TechsetWalkError(RuntimeError):
    pass


def _read_block(memory, address, size):
    return bytes(memory.u8(address + i) for i in range(size))


def walk_techset(memory, cstr, address):
    """Extract one linked techset completely (payload bytes included)."""
    name = cstr(memory.u32(address))
    out = {'name': name, 'address': address,
           'world_vertex_format': memory.u8(address + 4), 'slots': {}}
    shaders = {}
    for slot in range(SLOTS):
        technique = memory.u32(address + 8 + slot * 4)
        if not technique:
            continue
        out['slots'][slot] = _walk_technique(memory, cstr, technique, shaders)
    out['shaders'] = shaders
    return out


def _walk_technique(memory, cstr, technique, shaders):
    name = cstr(memory.u32(technique))
    flags = memory.u16(technique + 4)
    pass_count = memory.u16(technique + 6)
    if not 0 < pass_count <= 16:
        raise TechsetWalkError(f'technique {name!r} pass count {pass_count}')
    passes = []
    for index in range(pass_count):
        base = technique + 8 + index * PASS_SIZE
        decl_address = memory.u32(base)
        vertex_address = memory.u32(base + 4)
        pixel_address = memory.u32(base + 8)
        counts = tuple(memory.u8(base + 12 + i) for i in range(3))
        sampler_flags = memory.u8(base + 15)
        tail = _read_block(memory, base + 16, 4)
        args_address = memory.u32(base + 20)
        args = []
        for i in range(sum(counts)):
            arg = args_address + 8 * i
            arg_type = memory.u16(arg)
            dest = memory.u16(arg + 2)
            value = memory.u32(arg + 4)
            row = {'type': arg_type, 'dest': dest, 'value': value}
            if arg_type in ARG_LITERALS and value:
                row['literal'] = list(struct.unpack('>4f', _read_block(memory, value, 16)))
            args.append(row)
        decl = None
        if decl_address:
            decl_bytes = _read_block(memory, decl_address, DECL_SIZE)
            decl = {'stream_count': decl_bytes[0], 'has_optional': decl_bytes[1],
                    'routing': [(decl_bytes[2 + i * 2], decl_bytes[3 + i * 2])
                                for i in range(DECL_ROUTING)],
                    'raw': decl_bytes.hex()}
        passes.append({
            'decl': decl,
            'vertex_shader': _walk_shader(memory, cstr, vertex_address, 'vertex', shaders),
            'pixel_shader': _walk_shader(memory, cstr, pixel_address, 'pixel', shaders),
            'counts': counts, 'custom_sampler_flags': sampler_flags,
            'pass_tail_bytes': tail.hex(), 'args': args,
        })
    return {'name': name, 'flags': flags, 'passes': passes}


def _walk_shader(memory, cstr, address, kind, shaders):
    if not address:
        return None
    key = f'{kind}:{address:08x}'
    if key in shaders:
        return key
    name = cstr(memory.u32(address))
    data_pointer = memory.u32(address + 4)
    data_size = memory.u32(address + 8)
    if not 0x40 <= data_size <= MAX_BLOB:
        raise TechsetWalkError(f'{kind} shader {name!r} payload size 0x{data_size:X}')
    blob = _read_block(memory, data_pointer, data_size) if data_pointer else b''
    record = {'kind': kind, 'name': name, 'address': address,
              'data_size': data_size, 'blob_sha256': hashlib.sha256(blob).hexdigest(),
              'blob': blob}
    if kind == 'pixel':
        record['root_control'] = _read_block(memory, address + 0x0C, 12).hex()
    shaders[key] = record
    return key


# --- calibration -------------------------------------------------------------

def _rule(report, name, ok, evidence):
    report['rules'].append({'rule': name, 'passed': bool(ok), 'evidence': evidence})
    if not ok:
        report['failed_rules'].append(name)


def calibrate(techsets):
    """Check every compiler layout assumption against extracted retail data."""
    report = {'schema': 'iw3-ps3-rsx-calibration/v1', 'techsets': len(techsets),
              'rules': [], 'failed_rules': [], 'walk_errors': []}
    profiles = {'vertex': set(), 'pixel': set()}
    control_matches = both = 0
    pixel_dest_checked = pixel_dest_ok = 0
    patch_site_checked = patch_site_ok = 0
    vertex_dest_ok = vertex_dest_checked = 0
    sampler_ok = sampler_checked = 0
    decl_ok = decl_checked = 0
    output_mask_checked = output_mask_ok = 0
    output_mask_mismatches = []
    register_rule = []
    used_slots = set()
    for techset in techsets:
        used_slots.update(int(s) for s in techset['slots'])
        shaders = techset['shaders']
        parsed = {}
        for key, shader in shaders.items():
            try:
                program = cgbinary.parse_cg_binary(shader['blob'])
            except cgbinary.CgBinaryError as error:
                report['walk_errors'].append(f"{techset['name']}/{shader['name']}: {error}")
                continue
            parsed[key] = program
            profiles[shader['kind']].add(program.profile)
            if shader['kind'] == 'pixel':
                both += 1
                expected = cgbinary.fragment_control_bytes(shader['blob']).hex()
                if shader.get('root_control') == expected:
                    control_matches += 1
                register_rule.append((program.descriptor.get('register_count', 0),
                                      _max_register(program)))
            elif shader['kind'] == 'vertex':
                observed = program.descriptor.get('attribute_output_mask')
                if observed is not None:
                    try:
                        computed = _gcm_output_mask(program.ucode)
                    except Exception as error:  # decode failure: report, never guess
                        report['walk_errors'].append(
                            f"{techset['name']}/{shader['name']}: vertex ucode decode: {error}")
                    else:
                        output_mask_checked += 1
                        if observed == computed:
                            output_mask_ok += 1
                        elif len(output_mask_mismatches) < 8:
                            output_mask_mismatches.append({
                                'shader': shader['name'],
                                'observed': f'0x{observed:X}',
                                'computed': f'0x{computed:X}'})
        for technique in techset['slots'].values():
            for pass_ in technique['passes']:
                decl = pass_['decl']
                if decl is not None:
                    decl_checked += 1
                    routing = decl['routing'][:decl['stream_count']]
                    tail = decl['routing'][decl['stream_count']:]
                    sources_ok = all(source <= 8 for source, _ in routing)
                    dests_ok = all(dest <= 15 for _, dest in routing)
                    tail_zero = all(source == 0 and dest == 0 for source, dest in tail)
                    if decl['stream_count'] <= DECL_ROUTING and sources_ok and dests_ok and tail_zero:
                        decl_ok += 1
                pixel_key = pass_['pixel_shader']
                pixel_program = parsed.get(pixel_key)
                pixel_payload_offsets = None
                if pixel_program is not None:
                    pixel_payload_offsets = _inline_payload_offsets(pixel_program.ucode)
                for argument in pass_['args']:
                    arg_type = argument['type']
                    if arg_type in ARG_PIXEL_CONSTS and pixel_program is not None:
                        pixel_dest_checked += 1
                        count = len(pixel_program.parameters)
                        if argument['dest'] < count:
                            pixel_dest_ok += 1
                            parameter = pixel_program.parameters[argument['dest']]
                            for site in parameter.embedded_offsets:
                                patch_site_checked += 1
                                if site in pixel_payload_offsets:
                                    patch_site_ok += 1
                    elif arg_type in (0, 1, 3):
                        vertex_dest_checked += 1
                        if argument['dest'] <= 467:
                            vertex_dest_ok += 1
                    elif arg_type in ARG_SAMPLERS:
                        sampler_checked += 1
                        if argument['dest'] <= 15:
                            sampler_ok += 1
    _rule(report, 'vertex_profile_family', profiles['vertex'] <= {0x1B5B, 0x1B5D, 0x1B59},
          sorted(f'0x{p:04X}' for p in profiles['vertex']))
    _rule(report, 'fragment_profile_family', profiles['pixel'] <= {0x1B5C, 0x1B5E, 0x1807},
          sorted(f'0x{p:04X}' for p in profiles['pixel']))
    _rule(report, 'pixel_root_control_equals_cg_descriptor', both > 0 and control_matches == both,
          {'matching': control_matches, 'pixel_shaders': both})
    _rule(report, 'pixel_const_dest_is_parameter_index',
          pixel_dest_checked > 0 and pixel_dest_ok == pixel_dest_checked,
          {'in_range': pixel_dest_ok, 'checked': pixel_dest_checked})
    _rule(report, 'patch_sites_are_inline_constant_payloads',
          patch_site_checked > 0 and patch_site_ok == patch_site_checked,
          {'matching': patch_site_ok, 'checked': patch_site_checked})
    _rule(report, 'vertex_const_dest_is_rsx_register',
          vertex_dest_checked == 0 or vertex_dest_ok == vertex_dest_checked,
          {'in_range': vertex_dest_ok, 'checked': vertex_dest_checked})
    _rule(report, 'sampler_dest_is_texture_unit',
          sampler_checked == 0 or sampler_ok == sampler_checked,
          {'in_range': sampler_ok, 'checked': sampler_checked})
    _rule(report, 'vertex_decl_13_slot_layout', decl_checked > 0 and decl_ok == decl_checked,
          {'clean': decl_ok, 'checked': decl_checked})
    _rule(report, 'vertex_attribute_output_mask_is_gcm_encoded',
          output_mask_checked > 0 and output_mask_ok == output_mask_checked,
          {'matching': output_mask_ok, 'checked': output_mask_checked,
           'mismatches': output_mask_mismatches})
    _rule(report, 'technique_slots_within_26', all(slot < SLOTS for slot in used_slots),
          sorted(used_slots))
    if register_rule:
        exact = sum(1 for declared, measured in register_rule if declared == measured + 1)
        report['register_count_model'] = {
            'samples': len(register_rule),
            'declared_equals_max_plus_one': exact,
            'pairs_sample': register_rule[:20],
        }
    report['passed'] = not report['failed_rules']
    return report


def _max_register(program):
    highest = -1
    for instruction in nv40.decode_fragment_program(program.ucode):
        if not instruction.no_dest and not instruction.dest_fp16:
            highest = max(highest, instruction.dest_reg)
    return highest


def _gcm_output_mask(ucode):
    """CELL_GCM_ATTRIB_OUTPUT_MASK_* bits for the results a vertex ucode writes."""
    from cod4porter.rsx.translate import RESULT_TO_GCM_OUTPUT_BIT
    mask = 0
    for instruction in nv40.decode_vertex_program(ucode):
        if ((instruction.vec_result or instruction.sca_result)
                and instruction.result != nv40.VERT_RESULT_NONE):
            mask |= RESULT_TO_GCM_OUTPUT_BIT.get(instruction.result, 0)
    return mask


def _inline_payload_offsets(ucode):
    offsets = set()
    pc = 0
    for instruction in nv40.decode_fragment_program(ucode):
        if instruction.inline_constant is not None:
            offsets.add(pc + 16)
        pc += instruction.byte_count
    return offsets


# --- donor catalog -----------------------------------------------------------

def export_donor_catalog(techsets, out_json, out_bin, *, source_zones):
    """Write the donor manifest + payload bin the porter transplants from."""
    blob_file = Path(out_bin)
    manifest = {'schema': 'iw3-ps3-techset-donor/v1', 'source_zones': source_zones,
                'payload_file': blob_file.name, 'techsets': []}
    payloads = bytearray()
    blob_windows = {}

    def store(blob):
        key = hashlib.sha256(blob).hexdigest()
        window = blob_windows.get(key)
        if window is None:
            window = {'offset': len(payloads), 'size': len(blob), 'sha256': key}
            payloads.extend(blob)
            blob_windows[key] = window
        return dict(window)

    for techset in techsets:
        row = {'name': techset['name'],
               'world_vertex_format': techset['world_vertex_format'], 'slots': {}}
        shader_rows = {}
        for key, shader in techset['shaders'].items():
            shader_rows[key] = {
                'kind': shader['kind'], 'name': shader['name'],
                'payload': store(shader['blob']),
                'root_control': shader.get('root_control'),
            }
        row['shaders'] = shader_rows
        for slot, technique in techset['slots'].items():
            row['slots'][str(slot)] = {
                'name': technique['name'], 'flags': technique['flags'],
                'passes': [{
                    'decl_raw': pass_['decl']['raw'] if pass_['decl'] else None,
                    'vertex_shader': pass_['vertex_shader'],
                    'pixel_shader': pass_['pixel_shader'],
                    'counts': list(pass_['counts']),
                    'custom_sampler_flags': pass_['custom_sampler_flags'],
                    'args': pass_['args'],
                } for pass_ in technique['passes']],
            }
        manifest['techsets'].append(row)
    blob_file.write_bytes(bytes(payloads))
    Path(out_json).write_text(json.dumps(manifest, indent=1) + '\n', encoding='utf-8')
    return {'techsets': len(manifest['techsets']), 'payload_bytes': len(payloads),
            'unique_payloads': len(blob_windows)}


def run(machine, out_dir, *, source_label):
    """Extract, calibrate and export for one linked Machine.  Returns paths."""
    out = Path(out_dir)
    inventory = machine.inventory()
    techsets = []
    errors = []
    for (asset_type, name), header in sorted(inventory.items()):
        if asset_type != TECHSET_TYPE:
            continue
        try:
            techsets.append(walk_techset(machine.m, machine.cstr, header))
        except (TechsetWalkError, Exception) as error:  # noqa: BLE001
            errors.append(f'{name}: {error}')
    calibration = calibrate(techsets)
    calibration['walk_errors'] = errors + calibration['walk_errors']
    calibration_path = out / 'rsx-calibration.json'
    calibration_path.write_text(json.dumps(calibration, indent=1) + '\n', encoding='utf-8')
    donor_json = out / 'techset-donors.json'
    donor_bin = out / 'techset-donors.bin'
    summary = export_donor_catalog(techsets, donor_json, donor_bin,
                                   source_zones=[source_label])
    return {'calibration': calibration_path, 'donors': donor_json,
            'donor_payloads': donor_bin, 'summary': summary,
            'calibration_passed': calibration['passed'],
            'techsets_extracted': len(techsets), 'walk_errors': len(errors)}
