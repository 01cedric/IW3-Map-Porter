"""Post-link PhysPreset contracts used by physics sound initialization."""


def validate_physics_sound_prefixes(memory, inventory):
    checked = 0; empty = []
    for (typ, name), header in inventory.items():
        if typ != 1:
            continue
        pointer = memory.u32(header + 0x1c)
        if not pointer:
            raise RuntimeError("PhysPreset %r has NULL sndAliasPrefix (+0x1C); physics sound initialization dereferences this string" % name)
        if pointer < 0x10000 or pointer in (0xfffffffe, 0xffffffff):
            raise RuntimeError("PhysPreset %r has invalid linked sndAliasPrefix pointer 0x%08X" % (name, pointer))
        checked += 1
        # 4096 is a diagnostic read bound, not a claimed game string limit.
        for offset in range(4096):
            if memory.u8(pointer + offset) == 0:
                if offset == 0: empty.append(name)
                break
        else:
            raise RuntimeError("PhysPreset %r sndAliasPrefix has no terminator within the diagnostic read bound" % name)
    return dict(passed=True, checked=checked, empty_prefixes=empty,
                scope='linked PhysPreset sound-prefix pointer and termination; no audio playback')
