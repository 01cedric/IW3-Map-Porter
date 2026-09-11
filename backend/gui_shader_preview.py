"""RSX pixel-shader execution for material previews.

For every linked material this walks its real PS3 TechniqueSet in emulated
memory, decodes the selected technique's fragment program, applies the same
constant-patching model the console uses (parameter defaults, then material /
literal / code arguments), and executes the microcode over a texture-space
grid with the material's actual sampled images.  The baked RGBA output is the
material's preview texture - real shader math, not a diffuse copy.

Neutral stand-ins are used for values that only exist while the game runs
(code constants such as light colors default to 1.0, code samplers such as
lightmaps default to white) and every stand-in is reported per material.
Materials whose program uses an unmodeled construct keep the plain diffuse
preview and report the exact blocker.
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import numpy as np
from PIL import Image

from cod4porter.rsx import cgbinary, nv40
from cod4porter.rsx.interp_np import GridSampler, RsxExecError, execute_fragment_grid, solid_sampler

BAKE_SIZE = 256
ARG_MATERIAL_VERTEX_CONST = 0
ARG_MATERIAL_PIXEL_SAMPLER = 2
ARG_CODE_PIXEL_SAMPLER = 4
ARG_CODE_PIXEL_CONST = 5
ARG_MATERIAL_PIXEL_CONST = 6
ARG_LITERAL_PIXEL_CONST = 7

# Technique preference for a static preview: the lit world path first, then
# unlit/emissive fallbacks.
TECHNIQUE_PREFERENCE = (7, 8, 4, 5, 6)

_NEUTRAL_CODE_CONST = (1.0, 1.0, 1.0, 1.0)
_LIGHTMAP_NEUTRAL = (0.75, 0.75, 0.75, 1.0)


class _MemoryReader:
    def __init__(self, machine):
        self.machine = machine

    def u8(self, address):
        return self.machine.m.u8(address)

    def u16(self, address):
        return self.machine.m.u16(address)

    def u32(self, address):
        return self.machine.m.u32(address)


def _walk_techset(machine, address, cache):
    walked = cache.get(address)
    if walked is None:
        import techsetcheck
        walked = techsetcheck.walk_techset(_MemoryReader(machine), machine.cstr, address)
        cache[address] = walked
    return walked


def _select_technique(walked):
    for slot in TECHNIQUE_PREFERENCE:
        technique = walked['slots'].get(slot)
        if technique is not None:
            return slot, technique
    for slot in sorted(walked['slots']):
        return slot, walked['slots'][slot]
    return None, None


def _material_constants(machine, header):
    count = machine.m.u8(header + 0x33)
    table = machine.m.u32(header + 0x78)
    out = {}
    for index in range(count):
        entry = table + index * 32
        name_hash = machine.m.u32(entry)
        value = struct.unpack('>4f', bytes(machine.m.u8(entry + 16 + i) for i in range(16)))
        out[name_hash] = value
    return out


def _material_images_by_hash(machine, header):
    count = machine.m.u8(header + 0x32)
    table = machine.m.u32(header + 0x74)
    out = {}
    for index in range(count):
        entry = table + index * 12
        out[machine.m.u32(entry)] = {
            'index': index,
            'sampler': machine.m.u8(entry + 6),
            'semantic': machine.m.u8(entry + 7),
        }
    return out


def _payload_index_map(instructions):
    """ucode-relative inline payload offset -> instruction index."""
    mapping = {}
    cursor = 0
    for index, instruction in enumerate(instructions):
        if instruction.inline_constant is not None:
            mapping[cursor + 16] = index
        cursor += instruction.byte_count
    return mapping


def bake_material(machine, material_header, walked_techset, texture_arrays, *,
                  code_const_values=None):
    """Execute the material's fragment program; returns (rgba_u8, info)."""
    info = {'technique_slot': None, 'technique': None, 'defaulted_code_consts': [],
            'defaulted_code_samplers': [], 'material_consts_bound': 0,
            'literals_bound': 0, 'samplers': {}, 'killed_fraction': 0.0}
    slot, technique = _select_technique(walked_techset)
    if technique is None:
        raise RsxExecError('techset has no technique to execute')
    info['technique_slot'] = slot
    info['technique'] = technique['name']
    pass_ = technique['passes'][0]
    pixel_key = pass_['pixel_shader']
    shader = walked_techset['shaders'].get(pixel_key)
    if shader is None:
        raise RsxExecError('pass has no pixel shader')
    program = cgbinary.parse_cg_binary(shader['blob'])
    instructions = nv40.decode_fragment_program(program.ucode)
    if not instructions or not instructions[-1].end:
        raise RsxExecError('fragment instruction stream is incomplete')
    payload_by_offset = _payload_index_map(instructions)

    constant_values = {}

    def bind_parameter(dest, value, kind):
        if dest >= len(program.parameters):
            raise RsxExecError(f'{kind} argument dest {dest} outside the parameter table')
        parameter = program.parameters[dest]
        for site in parameter.embedded_offsets:
            index = payload_by_offset.get(site)
            if index is None:
                raise RsxExecError(f'{kind} patch site 0x{site:X} is not an inline payload')
            constant_values[index] = tuple(value)

    # Engine load-time behavior: parameter defaults are applied first.
    for ordinal, parameter in enumerate(program.parameters):
        if parameter.default_value is not None and parameter.embedded_offsets:
            bind_parameter(ordinal, parameter.default_value, 'default')

    material_consts = _material_constants(machine, material_header)
    images_by_hash = _material_images_by_hash(machine, material_header)
    samplers = {}
    counts = pass_['counts']
    for argument in pass_['args']:
        arg_type = argument['type']
        if arg_type == ARG_MATERIAL_PIXEL_CONST:
            value = material_consts.get(argument['value'])
            if value is not None:
                bind_parameter(argument['dest'], value, 'material-const')
                info['material_consts_bound'] += 1
        elif arg_type == ARG_LITERAL_PIXEL_CONST:
            literal = argument.get('literal')
            if literal:
                bind_parameter(argument['dest'], literal, 'literal')
                info['literals_bound'] += 1
        elif arg_type == ARG_CODE_PIXEL_CONST:
            code_index = (argument['value'] >> 16) & 0xFFFF
            value = (code_const_values or {}).get(code_index, _NEUTRAL_CODE_CONST)
            bind_parameter(argument['dest'], value, 'code-const')
            info['defaulted_code_consts'].append(code_index)
        elif arg_type == ARG_MATERIAL_PIXEL_SAMPLER:
            binding = images_by_hash.get(argument['value'])
            array = None if binding is None else texture_arrays.get(binding['index'])
            unit = argument['dest']
            if array is not None:
                samplers[unit] = GridSampler(array)
                info['samplers'][unit] = {'source': 'material', 'texture_index': binding['index']}
            else:
                samplers[unit] = solid_sampler((1.0, 1.0, 1.0, 1.0))
                info['samplers'][unit] = {'source': 'neutral-missing-image'}
        elif arg_type == ARG_CODE_PIXEL_SAMPLER:
            unit = argument['dest']
            samplers[unit] = solid_sampler(_LIGHTMAP_NEUTRAL)
            info['samplers'][unit] = {'source': 'neutral-code', 'code_source': argument['value']}
            info['defaulted_code_samplers'].append(argument['value'])

    grid_v, grid_u = np.meshgrid(
        np.linspace(0.0, 1.0, BAKE_SIZE, endpoint=False, dtype=np.float32),
        np.linspace(0.0, 1.0, BAKE_SIZE, endpoint=False, dtype=np.float32),
        indexing='ij')
    texcoord = np.stack([grid_u, grid_v, np.zeros_like(grid_u), np.ones_like(grid_u)], axis=-1)
    attributes = {nv40.FRAG_ATTR_COL0: (1.0, 1.0, 1.0, 1.0),
                  nv40.FRAG_ATTR_COL1: (0.0, 0.0, 0.0, 0.0),
                  nv40.FRAG_ATTR_FOGC: (0.0, 0.0, 0.0, 0.0)}
    for tex in range(10):
        attributes[nv40.FRAG_ATTR_TEX0 + tex] = texcoord

    color = execute_fragment_grid(instructions, (BAKE_SIZE, BAKE_SIZE),
                                  attributes=attributes, samplers=samplers,
                                  constant_values=constant_values)
    info['killed_fraction'] = float((color[..., 3] == 0.0).mean())
    rgba = np.clip(color, 0.0, 1.0)
    return (rgba * 255.0 + 0.5).astype(np.uint8), info


def bake_and_save(machine, material_header, techset_address, texture_arrays, out_dir,
                  *, cache):
    walked = _walk_techset(machine, techset_address, cache)
    rgba, info = bake_material(machine, material_header, walked, texture_arrays)
    digest = hashlib.sha256(rgba.tobytes() + walked['name'].encode('latin-1')).hexdigest()[:20]
    filename = f'shaded_{digest}.png'
    Image.fromarray(rgba, 'RGBA').save(Path(out_dir) / filename)
    info['file'] = filename
    return filename, info
