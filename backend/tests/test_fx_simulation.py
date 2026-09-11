#!/usr/bin/env python3
"""Deterministic FX particle simulation over linked-memory FxEffectDef data."""
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools' / 'ps3_loader_emulator'))

import fx_simulation
from test_rsx_donor_calibration import FakeMemory, cstr_reader


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def put_f32(memory, address, *values):
    for index, value in enumerate(values):
        memory.write(address + index * 4, struct.pack('>f', value))


def build_effect(memory):
    base = 0x40000
    name = 0x40100
    memory.write(name, b'smoke/test_fx\0')
    elems = 0x41000
    # effect: 1 looping + 1 oneshot + 0 emission
    memory.write(base, struct.pack('>8I', name, 0, 0, 5000, 1, 1, 0, elems))

    def build_elem(address, *, looping, elem_type=fx_simulation.ELEM_SPRITE_BILLBOARD):
        memory.write(address, struct.pack('>I', 0))                      # flags
        if looping:
            memory.write(address + 4, struct.pack('>ii', 250, 2))        # every 250ms, 2 particles
        else:
            memory.write(address + 4, struct.pack('>ii', 6, 0))          # exactly 6 particles
        memory.write(address + 0x28, struct.pack('>ii', 0, 0))           # spawnDelay
        memory.write(address + 0x30, struct.pack('>ii', 900, 200))       # life 900..1100ms
        for axis in range(3):
            put_f32(memory, address + 0x38 + axis * 8, 0.0, 4.0)         # origin jitter
        put_f32(memory, address + 0x50, 2.0, 1.0)                        # offset radius
        put_f32(memory, address + 0x58, 0.0, 0.0)
        put_f32(memory, address + 0x90, 0.0, 0.0)                        # initial rotation
        put_f32(memory, address + 0x98, 100.0, 0.0)                      # gravity
        memory.data[address + 0xB0] = elem_type
        memory.data[address + 0xB1] = 1                                  # visualCount
        memory.data[address + 0xB2] = 1                                  # velIntervalCount
        memory.data[address + 0xB3] = 1                                  # visIntervalCount
        vel = 0x42000 if looping else 0x42400
        memory.write(address + 0xB4, struct.pack('>I', vel))
        for sample in range(2):
            s = vel + sample * fx_simulation.VEL_SAMPLE_SIZE
            put_f32(memory, s + 0, 0.0, 0.0, 40.0)                       # local velocity base
            put_f32(memory, s + 12, 2.0, 2.0, 5.0)                       # amplitude
        vis = 0x42800 if looping else 0x42C00
        memory.write(address + 0xB8, struct.pack('>I', vis))
        for sample, color in ((0, 255), (1, 40)):
            s = vis + sample * fx_simulation.VIS_SAMPLE_SIZE
            memory.write(s, bytes((255, 128, 0, color)))                 # base RGBA
            put_f32(memory, s + 12, 6.0 + sample * 10.0, 6.0)            # size
        material_name = 0x43000
        memory.write(material_name, b'gfx_smoke\0')
        material_root = 0x43100
        memory.write(material_root, struct.pack('>I', material_name))
        memory.write(address + 0xBC, struct.pack('>I', material_root))

    build_elem(elems, looping=True)
    build_elem(elems + fx_simulation.FX_ELEM_SIZE, looping=False)
    return base


def test_simulation_shapes_and_determinism():
    memory = FakeMemory()
    effect = build_effect(memory)
    first = fx_simulation.simulate_effect(memory, cstr_reader(memory), effect,
                                          frames=12, frame_step=0.1)
    second = fx_simulation.simulate_effect(memory, cstr_reader(memory), effect,
                                           frames=12, frame_step=0.1)
    check(first == second, 'simulation must be deterministic')
    check(first['name'] == 'smoke/test_fx', first['name'])
    check(len(first['elements']) == 2, f"elements {len(first['elements'])}")
    looping, oneshot = first['elements']
    check(looping['category'] == 'looping' and oneshot['category'] == 'oneshot', 'categories')
    check(looping['material'] == 'gfx_smoke', looping['material'])
    check(looping['particles'] > 0 and oneshot['particles'] == 6,
          f"particles {looping['particles']}/{oneshot['particles']}")
    check(len(looping['frames']) == 13, 'frame count')
    check(not first['skipped_elements'], first['skipped_elements'])

    # physics sanity on a one-shot particle: rises with vz then gravity pulls
    frame_two = oneshot['frames'][2]
    frame_nine = oneshot['frames'][9]
    check(frame_two, 'live particles at t=0.2')
    z_two = frame_two[0][2]
    check(z_two > 0.0, f'particle should rise initially ({z_two})')
    # colors fade toward the second visual sample's alpha
    early_alpha = frame_two[0][5][3]
    late_rows = frame_nine or oneshot['frames'][8]
    check(late_rows, 'particles near end of life')
    late_alpha = late_rows[0][5][3]
    check(late_alpha < early_alpha, f'alpha fades ({early_alpha} -> {late_alpha})')
    # sizes grow toward second sample
    check(late_rows[0][3] > frame_two[0][3], 'size grows across samples')


def test_non_sprite_elements_are_reported():
    memory = FakeMemory()
    effect = build_effect(memory)
    elems = 0x41000
    memory.data[elems + 0xB0] = fx_simulation.ELEM_SOUND
    document = fx_simulation.simulate_effect(memory, cstr_reader(memory), effect,
                                             frames=4, frame_step=0.1)
    check(len(document['elements']) == 1, 'sound element excluded')
    check(document['skipped_elements'] and
          document['skipped_elements'][0]['elem_type'] == fx_simulation.ELEM_SOUND,
          document['skipped_elements'])


def main():
    tests = [value for key, value in sorted(globals().items()) if key.startswith('test_')]
    for test in tests:
        test()
    print(json.dumps({'passed': True, 'tests': len(tests)}))


if __name__ == '__main__':
    main()
