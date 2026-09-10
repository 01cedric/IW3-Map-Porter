"""Material** is four-byte aligned; constant arrays align to absolute 16 bytes."""
from types import SimpleNamespace as NS
from cod4porter.image_alias_replay import _relative_xmodel_layouts, _recover_unanchored_bases, _packed_requests, _signature_names
from cod4porter.pc_xmodel import apply_replayed_material_handles
from cod4porter.backend.v4_ffio import encode_pc_packed, FOLLOWING
from test_image_alias_replay import mat, tex


def main():
    sig_a, sig_b = (0x1111, 0, 0, 1, 2), (0x2222, 0, 0, 1, 2)
    a = mat(0x100, 'a', (tex(name='bark', raw=FOLLOWING, signature=sig_a),))
    a.constants = (bytes(32),)
    b = mat(0x200, 'b', (tex(name='leaf', raw=FOLLOWING, signature=sig_b),))
    x = NS(model=NS(name='tree', surfaces=(NS(material_name='a'), NS(material_name='b'))),
           inline_material_offsets=(a.root_offset, b.root_offset),
           material_handle_pointers=(FOLLOWING, FOLLOWING))
    layouts = _relative_xmodel_layouts((x,), {a.root_offset:a, b.root_offset:b})
    for residue in (0, 4, 8, 12):
        base = 0x300 + residue
        # handles 8, name 2, pad to 4, image table 12, "bark" 5,
        # then ABSOLUTE alignment to 16 for constants, followed by Material b.
        first = base + 20
        const = (base + 29 + 15) & ~15
        second = const + 32 + 4 + 8
        request = mat(0x400, 'reuse', (
            tex(name=None, raw=encode_pc_packed(4,first), signature=sig_a),
            tex(name=None, raw=encode_pc_packed(4,second), signature=sig_b)))
        materials = (a,b,request)
        result = _recover_unanchored_bases(layouts, {}, _packed_requests(materials),
                                          _signature_names(materials), set())
        assert result['bases'] == {0:base}, (residue,result)
        assert result['aliases'] == {first:'bark',second:'leaf'}

    reuse = NS(model=NS(name='tree_lod',surfaces=(NS(material_name=',default'),)),
               material_handle_pointers=(encode_pc_packed(4,0x304),))
    r = {'aliases':{}, 'exact_unresolved':({'model':'tree_lod','surface':0},),
         'unresolved':[], 'runtime_fallbacks':1}
    apply_replayed_material_handles((x,reuse),r,{0:0x300})
    assert reuse.model.surfaces[0].material_name == 'b'
    assert r['runtime_fallbacks'] == 0 and not r['exact_unresolved']
    # A forward pointer into a later owner must never become an identity proof.
    early = NS(model=NS(name='early',surfaces=(NS(material_name=',default'),)),
               material_handle_pointers=(encode_pc_packed(4,0x304),))
    r = {'aliases':{},'exact_unresolved':(),'unresolved':[],'runtime_fallbacks':0}
    apply_replayed_material_handles((early,x),r,{1:0x300})
    assert early.model.surfaces[0].material_name == ',default'
    print('PASS: all four base alignments, exact LOD reuse and forward-reference rejection')

if __name__ == '__main__':main()
