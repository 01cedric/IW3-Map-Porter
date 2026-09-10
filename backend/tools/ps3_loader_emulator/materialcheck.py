"""Check named constants and samplers required by the selected PS3 graph."""


def validate_material_constants(memory, material, cstr):
    name = cstr(memory.u32(material))
    techset = memory.u32(material + 0x70)
    if not techset:
        raise RuntimeError(f"Material '{name}' has no TechniqueSet")
    count = memory.u8(material + 0x33)
    table = memory.u32(material + 0x78)
    if count and not table:
        raise RuntimeError(f"Material '{name}' has a null constant table")
    present = {memory.u32(table + i * 32) for i in range(count)}
    texture_count = memory.u8(material + 0x32)
    textures = memory.u32(material + 0x74)
    if texture_count and not textures:
        raise RuntimeError(f"Material '{name}' has a null texture table")
    texture_hashes = {memory.u32(textures + i * 12) for i in range(texture_count)}
    for slot in range(26):
        technique = memory.u32(techset + 8 + slot * 4)
        if not technique:
            continue
        passes = memory.u16(technique + 6)
        if not 0 < passes <= 32:
            raise RuntimeError(f"Material '{name}' has invalid technique pass count {passes}")
        for index in range(passes):
            p = technique + 8 + index * 24
            n = sum(memory.u8(p + offset) for offset in (12, 13, 14))
            args = memory.u32(p + 20)
            if n and not args:
                raise RuntimeError(f"Material '{name}' has null shader arguments")
            for i in range(n):
                arg = args + 8 * i
                argument_type = memory.u16(arg)
                if argument_type not in (0, 2, 6):
                    continue
                required = memory.u32(arg + 4)
                available = texture_hashes if argument_type == 2 else present
                if required not in available:
                    target = cstr(memory.u32(techset))
                    label = 'texture sampler' if argument_type == 2 else 'shader constant'
                    raise RuntimeError(
                        f"Material '{name}', PS3 TechniqueSet '{target}', slot {slot}, "
                        f"pass {index}: missing {label} 0x{required:08x}; "
                        'the material cannot satisfy the selected shader graph')
