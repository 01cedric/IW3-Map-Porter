#!/usr/bin/env python3
"""RSX material-preview execution: vectorized executor and material baking."""
import json
import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools' / 'ps3_loader_emulator'))

from cod4porter.rsx import d3d9, interp, nv40
from cod4porter.rsx.d3d9 import assemble, dst, src
from cod4porter.rsx.interp_np import GridSampler, execute_fragment_grid, solid_sampler
from cod4porter.rsx.translate import compile_pixel_shader
from cod4porter.rsx.techset_compile import compile_techniqueset

import gui_shader_preview
from test_rsx_donor_calibration import FakeMemory, build_linked_image, cstr_reader
from test_rsx_techset_pipeline import _pc_techset

R, V, C, T, S = d3d9.REG_TEMP, d3d9.REG_INPUT, d3d9.REG_CONST, d3d9.REG_ADDR, d3d9.REG_SAMPLER


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def test_grid_matches_scalar_interpreter():
    blob = assemble('pixel', 2, 0, [
        ('dcl', 0, 0, dst(T, 0)),
        ('dcl', 0, 0, dst(T, 1)),
        ('def', 0, (0.5, 0.25, 2.0, 1.0)),
        (d3d9.OP_MAD, 0, dst(R, 0), (src(T, 0), src(C, 0), src(T, 1, 'yxwz'))),
        (d3d9.OP_MUL, 0, dst(R, 1, 'xy'), (src(R, 0), src(R, 0, 'wzyx'))),
        (d3d9.OP_MAX, 0, dst(R, 0, 'xy'), (src(R, 1), src(C, 0, 'y'))),
        (d3d9.OP_MOV, 0, dst(d3d9.REG_COLOROUT, 0), (src(R, 0),)),
    ])
    translation = compile_pixel_shader(blob)
    instructions = nv40.decode_fragment_program(translation.ucode)
    rng = np.random.default_rng(5)
    t0 = rng.uniform(-1, 1, size=(3, 4, 4)).astype(np.float32)
    t1 = rng.uniform(-1, 1, size=(3, 4, 4)).astype(np.float32)
    grid = execute_fragment_grid(instructions, (3, 4),
                                 attributes={nv40.FRAG_ATTR_TEX0: t0,
                                             nv40.FRAG_ATTR_TEX0 + 1: t1},
                                 samplers={})
    for y in range(3):
        for x in range(4):
            scalar = interp.run_rsx_fragment(
                instructions,
                attributes={nv40.FRAG_ATTR_TEX0: t0[y, x].tolist(),
                            nv40.FRAG_ATTR_TEX0 + 1: t1[y, x].tolist()})
            for component in range(4):
                check(abs(scalar['color0'][component] - float(grid[y, x, component])) < 1e-4,
                      f'grid/scalar mismatch at {y},{x},{component}')


def test_grid_sampler_and_kill():
    blob = assemble('pixel', 2, 0, [
        ('dcl', 0, 0, dst(T, 0)),
        ('dcl_sampler', d3d9.SAMPLER_2D, dst(S, 0)),
        (d3d9.OP_TEXLD, 0, dst(R, 0), (src(T, 0), src(S, 0))),
        (d3d9.OP_ADD, 0, dst(R, 1), (src(R, 0), src(C, 4))),
        ('def', 4, (-0.5, -0.5, -0.5, 0.0)),
        (d3d9.OP_TEXKILL, 0, dst(R, 1), ()),
        (d3d9.OP_MOV, 0, dst(d3d9.REG_COLOROUT, 0), (src(R, 0),)),
    ])
    translation = compile_pixel_shader(blob)
    instructions = nv40.decode_fragment_program(translation.ucode)
    image = np.zeros((2, 2, 4), dtype=np.float32)
    image[0, 0] = (1.0, 1.0, 1.0, 1.0)   # bright texel -> survives
    image[1, 1] = (0.1, 0.1, 0.1, 1.0)   # dark texel -> killed
    size = 8
    grid_v, grid_u = np.meshgrid(np.linspace(0, 1, size, endpoint=False, dtype=np.float32),
                                 np.linspace(0, 1, size, endpoint=False, dtype=np.float32),
                                 indexing='ij')
    texcoord = np.stack([grid_u, grid_v, np.zeros_like(grid_u), np.ones_like(grid_u)], axis=-1)
    color = execute_fragment_grid(instructions, (size, size),
                                  attributes={nv40.FRAG_ATTR_TEX0: texcoord},
                                  samplers={0: GridSampler(image)})
    killed = (color[..., 3] == 0.0)
    check(killed.any() and not killed.all(), f'kill split {killed.mean()}')


def test_material_bake_from_linked_memory():
    compiled = compile_techniqueset(_pc_techset())
    memory, techset_address = build_linked_image(compiled)

    class FakeMachine:
        m = memory
        cstr = staticmethod(cstr_reader(memory))

    # Material root: techset*, texture table, constant table.
    header = 0x100000
    texture_table = 0x100100
    constant_table = 0x100200
    memory.write(header + 0x70, struct.pack('>I', techset_address))
    memory.data[header + 0x32] = 1
    memory.data[header + 0x33] = 1
    memory.write(header + 0x74, struct.pack('>I', texture_table))
    memory.write(header + 0x78, struct.pack('>I', constant_table))
    memory.write(texture_table, struct.pack('>I', 0xDEADBEEF))
    memory.data[texture_table + 6] = 0
    memory.data[texture_table + 7] = 2
    memory.write(constant_table, struct.pack('>I', 0x11111111))
    memory.write(constant_table + 16, struct.pack('>4f', 0.5, 0.5, 0.5, 1.0))

    import techsetcheck
    walked = techsetcheck.walk_techset(memory, cstr_reader(memory), techset_address)
    texture = np.zeros((4, 4, 4), dtype=np.float32)
    texture[..., 0] = 0.8
    texture[..., 3] = 1.0
    rgba, info = gui_shader_preview.bake_material(FakeMachine, header, walked, {0: texture})
    check(rgba.shape == (gui_shader_preview.BAKE_SIZE, gui_shader_preview.BAKE_SIZE, 4),
          f'bake shape {rgba.shape}')
    check(info['technique_slot'] == 7 and info['technique'] == 'lit', info)
    check(info['samplers'][0]['source'] == 'material', info['samplers'])
    check(info['defaulted_code_consts'] == [1], info['defaulted_code_consts'])
    # Fixture math: tex(0.8) * c0(code, defaults 1.0) then * c1 + c1.w
    # with literal c1 = (0.1,0.2,0.3,0.4): r = 0.8*1*0.1+0.4 = 0.48
    expected = 0.8 * 1.0 * 0.1 + 0.4
    sample = rgba[8, 8, 0] / 255.0
    check(abs(sample - expected) < 2 / 255, f'baked red {sample} != {expected}')
    check(rgba[..., 3].min() > 0, 'alpha preserved')


def main():
    tests = [value for key, value in sorted(globals().items()) if key.startswith('test_')]
    for test in tests:
        test()
    print(json.dumps({'passed': True, 'tests': len(tests)}))


if __name__ == '__main__':
    main()
