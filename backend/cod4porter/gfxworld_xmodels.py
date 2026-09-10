"""Exact shared XModel shells owned by a GfxWorld draw-instance array."""
import struct
from .backend.v4_ffio import decode_pc_pointer

PC_DRAW = 0x4C


def read_shared_shell(zone, cursor):
    end = cursor + 0xDC
    if cursor < 0 or end >= len(zone):
        raise ValueError('Inline GfxWorld XModel root is outside the source zone')
    marker = struct.unpack_from('<I', zone, cursor)[0]
    if marker not in (0xFFFFFFFF, 0xFFFFFFFE) or any(zone[cursor+4:end]):
        raise ValueError('GfxWorld inline XModel has owned geometry; only a verified empty shared-reference shell is supported')
    stop = zone.find(b'\0', end, min(len(zone), end+256))
    if stop < 0 or zone[end:end+1] != b',' or stop == end+1:
        raise ValueError('Inline GfxWorld XModel must have a terminated comma-prefixed name')
    name = zone[end+1:stop]
    if any(c < 33 or c > 126 for c in name):
        raise ValueError('Invalid shared XModel name')
    return name.decode('ascii'), stop+1


def resolve_draw_aliases(raws, inline_symbols, known, array_base=None):
    """Recover the unique draw-array base from backward pointer-cell edges.

    Packed IW3 asset references address pointer cells. An unresolved draw-model
    edge must land on an earlier draw's +0x38 field and ultimately reach a typed
    inline shell or a previously resolved top-level model. Ambiguous layouts fail.
    """
    unresolved = [(i, raw) for i, raw in enumerate(raws)
                  if decode_pc_pointer(raw).kind == 'packed' and raw not in known]
    if not unresolved:
        return {}, None
    first, raw = unresolved[0]
    pointer = decode_pc_pointer(raw)
    if pointer.block != 4:
        raise ValueError('Unresolved draw XModel reference is outside block 4')
    anchors = [i for i in range(first) if i in inline_symbols or raws[i] in known]
    candidates = []
    bases = [array_base] if array_base is not None else [pointer.offset-index*PC_DRAW-0x38 for index in anchors]
    for base in bases:
        if base < 0 or base % 4:
            continue
        resolved = dict(known); cells = {}; valid = True
        for i, value in enumerate(raws):
            if i in inline_symbols: symbol = inline_symbols[i]
            elif not value: symbol = None
            elif value in resolved: symbol = resolved[value]
            else:
                p = decode_pc_pointer(value)
                symbol = cells.get(p.offset) if p.kind == 'packed' and p.block == 4 else None
                if symbol is None: valid = False; break
                resolved[value] = symbol
            cells[base+i*PC_DRAW+0x38] = symbol
        if valid:
            candidates.append((base, {value: resolved[value] for _, value in unresolved}))
    if len(candidates) != 1:
        raise ValueError(f'GfxWorld draw-model alias replay requires one exact array base; found {len(candidates)}')
    base, aliases = candidates[0]
    return aliases, base


def draw_array_base(zone, report, material_replay):
    """Continue the proven material-memory B4 cursor through the world tail.

    PC world vertices are 44-byte, 4-aligned records. This differs from the
    16-byte alignment used when writing PS3 world vertices. Source layer bytes
    also advance B4; PS3 moves the expanded layer stream to another block.
    """
    if not material_replay.get('topology_target_count', 0):
        raise ValueError('Shared draw-model replay needs the exact GfxWorld material B4 anchor')
    u32=lambda p:struct.unpack_from('<I',zone,p)[0]
    u16=lambda p:struct.unpack_from('<H',zone,p)[0]
    align=lambda c,a:(c+a-1)&~(a-1)
    cursor=material_replay['graph_base']+material_replay['graph_size']
    def allocation(c,raw,count,stride,alignment):
        if not count:return c
        p=decode_pc_pointer(raw)
        if p.kind not in ('following','insert'):
            raise ValueError('World-tail allocation is not inline')
        if p.kind=='insert':c=align(c,4)+4
        return align(c,alignment)+count*stride
    r=report.root_offset
    cursor=allocation(cursor,u32(r+0x34),report.vertex_count,44,4)
    cursor=allocation(cursor,u32(r+0x40),report.vertex_layer_data_size,1,1)
    from .pc_material import parse_material_at
    from .pc_fx import _replay_inline_material_b4, FxVisual, VisualKind
    physical=next(b.start for b in report.full.branches if b.name=='sun/outdoor aliases')
    for offset in (0x180,0x184):
        raw=u32(r+offset)
        if decode_pc_pointer(raw).kind in ('following','insert'):
            if raw==0xfffffffe:cursor=align(cursor,4)+4
            material,end=parse_material_at(zone,physical)
            cursor=_replay_inline_material_b4(zone,cursor,FxVisual(raw,VisualKind.MATERIAL,physical,material.name),'GfxWorld sun material')
            physical=end
    raw=u32(r+0x21c)
    if decode_pc_pointer(raw).kind in ('following','insert'):
        if raw==0xfffffffe:cursor=align(cursor,4)+4
        image=next(i for i in report.full.images if i.label=='outdoorImage')
        name_raw=u32(image.root_offset+0x20)
        if decode_pc_pointer(name_raw).kind not in ('following','insert'):
            raise ValueError('Outdoor image name is not inline')
        if name_raw==0xfffffffe:cursor=align(cursor,4)+4
        cursor+=len(image.name.encode('latin-1'))+1
        if u32(image.root_offset+4)==0xfffffffe:cursor=align(cursor,4)+4
    p=next(b.start for b in report.full.branches if b.name=='shadow/light-region graph')
    cursor=allocation(cursor,u32(r+0x23c),report.primary_light_count,12,4)
    for i in range(report.primary_light_count):
        for count_offset,pointer_offset in ((0,4),(2,8)):
            cursor=allocation(cursor,u32(p+i*12+pointer_offset),u16(p+i*12+count_offset),2,2)
    p+=report.primary_light_count*12+(report.full.shadow_surface_values+report.full.shadow_model_values)*2
    cursor=allocation(cursor,u32(r+0x240),report.primary_light_count,8,4)
    roots=p;p+=report.primary_light_count*8
    for i in range(report.primary_light_count):
        count=u32(roots+i*8)
        cursor=allocation(cursor,u32(roots+i*8+4),count,80,4)
        hulls=p;p+=count*80
        for j in range(count):
            axes=u32(hulls+j*80+72)
            cursor=allocation(cursor,u32(hulls+j*80+76),axes,20,4);p+=axes*20
    for offset,count,stride,alignment in ((0x28c,report.static_surface_count+report.static_surface_count_no_decal,2,2),
                                         (0x290,report.static_model_count,28,4),
                                         (0x294,report.surface_count,48,4),
                                         (0x298,report.cull_group_count,32,4)):
        cursor=allocation(cursor,u32(r+offset),count,stride,alignment)
    raw=u32(r+0x29c)
    if raw==0xfffffffe:cursor=align(cursor,4)+4
    elif raw!=0xffffffff:raise ValueError('Draw array is not inline')
    return align(cursor,4)
