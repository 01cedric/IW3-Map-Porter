"""Deterministic FxEffectDef particle simulation for the 3D preview.

Walks linked FxEffectDef assets in emulated PS3 memory (the exact structures
the console runs, big-endian, FxElemDef 0xFC) and produces a bounded,
reproducible particle timeline per effect: spawn cadence from the looping /
one-shot spawn definitions, per-particle lifetime, origin ranges, first-frame
velocity integration with gravity, and visual state (color, size, rotation)
interpolated across the authored visual samples.

This is a preview simulation with documented approximations - velocity uses
the first sample interval, world/local frames are not separated, and physics
collision is ignored.  It is deterministic per (effect, element, particle)
so exported timelines are stable across runs.
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

FX_ELEM_SIZE = 0xFC
VEL_SAMPLE_SIZE = 96
VIS_SAMPLE_SIZE = 48

# FxElemType (IW3)
ELEM_SPRITE_BILLBOARD = 0
ELEM_SPRITE_ORIENTED = 1
ELEM_TAIL = 2
ELEM_TRAIL = 3
ELEM_CLOUD = 4
ELEM_MODEL = 5
ELEM_OMNI_LIGHT = 6
ELEM_SPOT_LIGHT = 7
ELEM_SOUND = 8
ELEM_DECAL = 9
ELEM_RUNNER = 10

SPRITE_TYPES = (ELEM_SPRITE_BILLBOARD, ELEM_SPRITE_ORIENTED, ELEM_TAIL, ELEM_CLOUD)

DEFAULT_FRAMES = 24
DEFAULT_FRAME_STEP = 1.0 / 12.0
MAX_PARTICLES_PER_ELEMENT = 192
MAX_ELEMENTS_PER_EFFECT = 64      # element counts are raw i32s from linked memory
MAX_PARTICLES_PER_EFFECT = 1024   # bounds the JSON the viewer parses on the UI thread


def _rand(*seeds) -> float:
    """Deterministic [0,1) stream from a hash of the seed tuple."""
    digest = hashlib.sha256(('/'.join(str(s) for s in seeds)).encode()).digest()
    return int.from_bytes(digest[:8], 'big') / 2.0 ** 64


def _srand(*seeds) -> float:
    """Deterministic [-1,1)."""
    return _rand(*seeds) * 2.0 - 1.0


class _R:
    def __init__(self, memory):
        self.memory = memory

    def u8(self, address):
        return self.memory.u8(address)

    def u16(self, address):
        return self.memory.u16(address)

    def u32(self, address):
        return self.memory.u32(address)

    def i32(self, address):
        value = self.memory.u32(address)
        return value - 0x100000000 if value & 0x80000000 else value

    def f32(self, address):
        return struct.unpack('>f', struct.pack('>I', self.memory.u32(address)))[0]

    def frange(self, address):
        return (self.f32(address), self.f32(address + 4))

    def irange(self, address):
        return (self.i32(address), self.i32(address + 4))


def _sample(range_pair, r):
    base, amplitude = range_pair
    return base + amplitude * r


class ElementDef:
    def __init__(self, reader: _R, cstr, address: int, index: int, category: str):
        self.index = index
        self.category = category            # 'looping' | 'oneshot' | 'emission'
        self.flags = reader.u32(address)
        self.spawn = (reader.i32(address + 4), reader.i32(address + 8))
        self.spawn_delay = reader.irange(address + 0x28)
        self.life_span = reader.irange(address + 0x30)
        self.spawn_origin = tuple(reader.frange(address + 0x38 + axis * 8) for axis in range(3))
        self.spawn_offset_radius = reader.frange(address + 0x50)
        self.spawn_offset_height = reader.frange(address + 0x58)
        self.initial_rotation = reader.frange(address + 0x90)
        self.gravity = reader.frange(address + 0x98)
        self.elem_type = reader.u8(address + 0xB0)
        self.visual_count = reader.u8(address + 0xB1)
        self.vel_interval_count = reader.u8(address + 0xB2)
        self.vis_interval_count = reader.u8(address + 0xB3)
        self.vel_samples = reader.u32(address + 0xB4)
        self.vis_samples = reader.u32(address + 0xB8)
        self.visuals_raw = reader.u32(address + 0xBC)
        self.material = None
        if self.elem_type in SPRITE_TYPES and self.visual_count and self.visuals_raw:
            visual_pointer = self.visuals_raw
            if self.visual_count > 1:
                visual_pointer = reader.u32(self.visuals_raw)  # first array entry
            if visual_pointer:
                try:
                    self.material = cstr(reader.u32(visual_pointer))
                except Exception:  # noqa: BLE001 - preview only
                    self.material = None
        self._reader = reader

    def velocity(self, interval: int, r0: float, r1: float, r2: float):
        if not self.vel_samples or self.vel_interval_count == 0:
            return (0.0, 0.0, 0.0)
        interval = max(0, min(interval, self.vel_interval_count))
        base = self.vel_samples + interval * VEL_SAMPLE_SIZE
        out = []
        for axis, r in enumerate((r0, r1, r2)):
            lo = self._reader.f32(base + axis * 4)
            amplitude = self._reader.f32(base + 12 + axis * 4)
            out.append(lo + amplitude * r)
        return tuple(out)

    def visual_state(self, life_fraction: float, r: float):
        count = self.vis_interval_count + 1 if self.vis_samples else 0
        if count <= 0:
            return {'color': (1.0, 1.0, 1.0, 1.0), 'size': (8.0, 8.0), 'rotation_delta': 0.0}
        life_fraction = min(max(life_fraction, 0.0), 1.0)
        position = life_fraction * self.vis_interval_count if self.vis_interval_count else 0.0
        first = int(position)
        second = min(first + 1, count - 1)
        blend = position - first

        def state(sample_index):
            base = self.vis_samples + sample_index * VIS_SAMPLE_SIZE
            color = tuple(self._reader.u8(base + component) / 255.0 for component in range(4))
            amp_color = tuple(self._reader.u8(base + 24 + component) / 255.0 for component in range(4))
            size = (self._reader.f32(base + 12), self._reader.f32(base + 16))
            amp_size = (self._reader.f32(base + 24 + 12), self._reader.f32(base + 24 + 16))
            rotation = self._reader.f32(base + 4)
            return color, amp_color, size, amp_size, rotation

        c0, a0, s0, as0, rot0 = state(first)
        c1, a1, s1, as1, rot1 = state(second)

        def lerp(x, y):
            return x + (y - x) * blend

        color = tuple(min(1.0, max(0.0, lerp(c0[i] + a0[i] * r, c1[i] + a1[i] * r)))
                      for i in range(4))
        size = tuple(max(0.0, lerp(s0[i] + as0[i] * r, s1[i] + as1[i] * r)) for i in range(2))
        return {'color': color, 'size': size, 'rotation_delta': lerp(rot0, rot1)}


def _spawn_times(element: ElementDef, effect_seed: str, duration: float):
    times = []
    if element.category == 'looping':
        interval_msec, count = element.spawn
        if interval_msec <= 0 or count <= 0:
            return times
        cycle = 0
        time = 0.0
        while time <= duration and len(times) < MAX_PARTICLES_PER_ELEMENT:
            # ``count`` is a raw i32 from linked memory: bound the inner burst
            # loop too, or a corrupt/extreme spawn count stalls the export
            # unrecoverably before the outer guard is ever re-checked.
            for burst in range(min(count, MAX_PARTICLES_PER_ELEMENT - len(times))):
                delay = _sample(element.spawn_delay,
                                _rand(effect_seed, element.index, cycle, burst, 'delay')) / 1000.0
                times.append(time + max(0.0, delay))
            cycle += 1
            time = cycle * interval_msec / 1000.0
    else:
        base, amplitude = element.spawn
        count = int(round(_sample((base, amplitude),
                                  _rand(effect_seed, element.index, 'count'))))
        for burst in range(max(0, min(count, MAX_PARTICLES_PER_ELEMENT))):
            delay = _sample(element.spawn_delay,
                            _rand(effect_seed, element.index, burst, 'delay')) / 1000.0
            times.append(max(0.0, delay))
    return times[:MAX_PARTICLES_PER_ELEMENT]


def simulate_effect(memory, cstr, effect_address: int, *, frames: int = DEFAULT_FRAMES,
                    frame_step: float = DEFAULT_FRAME_STEP):
    """Simulate one linked FxEffectDef.  Returns the timeline document."""
    reader = _R(memory)
    name = cstr(reader.u32(effect_address))
    counts = (reader.i32(effect_address + 16), reader.i32(effect_address + 20),
              reader.i32(effect_address + 24))
    counts = tuple(max(0, count) for count in counts)
    elem_base = reader.u32(effect_address + 28)
    total = sum(counts)
    truncated_elements = max(0, total - MAX_ELEMENTS_PER_EFFECT)
    elements = []
    skipped = []
    for index in range(min(total, MAX_ELEMENTS_PER_EFFECT)):
        category = ('looping' if index < counts[0]
                    else 'oneshot' if index < counts[0] + counts[1] else 'emission')
        element = ElementDef(reader, cstr, elem_base + index * FX_ELEM_SIZE, index, category)
        if element.elem_type not in SPRITE_TYPES:
            skipped.append({'index': index, 'elem_type': element.elem_type,
                            'reason': 'non-sprite element (model/light/sound/decal/runner)'})
            continue
        if element.category == 'emission':
            skipped.append({'index': index, 'elem_type': element.elem_type,
                            'reason': 'emission elements need a moving emitter'})
            continue
        elements.append(element)

    duration = frames * frame_step
    element_rows = []
    total_particles = 0
    for element in elements:
        if total_particles >= MAX_PARTICLES_PER_EFFECT:
            skipped.append({'index': element.index, 'elem_type': element.elem_type,
                            'reason': 'per-effect particle budget reached'})
            continue
        spawn_times = _spawn_times(element, name, duration)
        spawn_times = spawn_times[:MAX_PARTICLES_PER_EFFECT - total_particles]
        particles = []
        for particle_index, spawn_time in enumerate(spawn_times):
            seed = (name, element.index, particle_index)
            life = max(_sample(element.life_span, _rand(*seed, 'life')) / 1000.0, 0.05)
            origin = tuple(_sample(element.spawn_origin[axis], _srand(*seed, 'org', axis))
                           for axis in range(3))
            radius = _sample(element.spawn_offset_radius, _rand(*seed, 'radius'))
            height = _sample(element.spawn_offset_height, _srand(*seed, 'height'))
            angle = _rand(*seed, 'angle') * 6.2831853
            import math
            origin = (origin[0] + radius * math.cos(angle),
                      origin[1] + radius * math.sin(angle),
                      origin[2] + height)
            velocity = element.velocity(0, _srand(*seed, 'vel', 0),
                                        _srand(*seed, 'vel', 1), _srand(*seed, 'vel', 2))
            gravity = _sample(element.gravity, _rand(*seed, 'grav'))
            rotation0 = _sample(element.initial_rotation, _srand(*seed, 'rot'))
            visual_r = _rand(*seed, 'vis')
            particles.append({'spawn': spawn_time, 'life': life, 'origin': origin,
                              'velocity': velocity, 'gravity': gravity,
                              'rotation0': rotation0, 'visual_r': visual_r})
        total_particles += len(particles)
        frames_out = []
        for frame in range(frames + 1):
            time = frame * frame_step
            rows = []
            for particle in particles:
                age = time - particle['spawn']
                if age < 0.0 or age > particle['life']:
                    continue
                life_fraction = age / particle['life']
                x = particle['origin'][0] + particle['velocity'][0] * age
                y = particle['origin'][1] + particle['velocity'][1] * age
                z = (particle['origin'][2] + particle['velocity'][2] * age
                     - 0.5 * particle['gravity'] * age * age)
                visual = element.visual_state(life_fraction, particle['visual_r'])
                rows.append([round(x, 3), round(y, 3), round(z, 3),
                             round(visual['size'][0], 3), round(visual['size'][1], 3),
                             [round(component, 4) for component in visual['color']],
                             round(particle['rotation0'] + visual['rotation_delta'] * age, 4)])
            frames_out.append(rows)
        element_rows.append({
            'index': element.index, 'category': element.category,
            'elem_type': element.elem_type, 'material': element.material,
            'particles': len(particles), 'frames': frames_out,
        })
    return {'name': name, 'frame_step': frame_step, 'frames': frames + 1,
            'elements': element_rows, 'skipped_elements': skipped,
            'element_counts': {'looping': counts[0], 'oneshot': counts[1],
                               'emission': counts[2]},
            'total_particles': total_particles,
            'truncated_elements': truncated_elements,
            'approximations': [
                'first velocity interval integrated linearly; local/world frames merged',
                'gravity applied as authored units per second squared',
                'no collision, no emitted child effects, sprites only',
            ]}


def export_fx_simulations(machine, out_dir, *, textures_by_material=None, limit=64):
    """Simulate every linked FX asset and write fx-simulation.json."""
    inventory = machine.inventory()
    effects = sorted((name, header) for (asset_type, name), header in inventory.items()
                     if asset_type == 0x1B)
    rows = []
    errors = []
    for name, header in effects[:limit]:
        try:
            document = simulate_effect(machine.m, machine.cstr, header)
            for element in document['elements']:
                material = element.get('material')
                if material and textures_by_material:
                    element['texture'] = textures_by_material.get(material)
            rows.append(document)
        except Exception as error:  # noqa: BLE001 - preview only, report and continue
            errors.append({'name': name, 'error': str(error)})
    document = {'schema': 'iw3-fx-simulation/v1', 'effects': rows, 'errors': errors,
                'effect_count': len(rows), 'skipped_beyond_limit': max(0, len(effects) - limit),
                'deterministic': True,
                'contract': 'preview particle simulation; not engine playback'}
    path = Path(out_dir) / 'fx-simulation.json'
    path.write_text(json.dumps(document, separators=(',', ':')), encoding='utf-8')
    return path, document
