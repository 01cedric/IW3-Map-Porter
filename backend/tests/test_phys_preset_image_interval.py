"""An inline PhysPreset must not hide a later image pointer's exact owner."""
from types import SimpleNamespace as NS
import struct
from cod4porter.image_alias_replay import _replay_closed_xmodel_intervals, _relative_xmodel_layouts, _packed_requests
from cod4porter.xmodel import PC_XSURFACE_ROOT_SIZE
from cod4porter.backend.v4_ffio import FOLLOWING, encode_pc_packed
from test_image_alias_replay import mat, tex


def main():
    z=bytearray(0x1000)
    def word(p,n):struct.pack_into('<I',z,p,n)
    models=[]
    for i in range(3):
        root=0x100+i*0x100
        surfaces=(NS(pc_root=0x600,blend_element_count=0,rigid_vert_list_count=0),) if i==1 else ()
        model=NS(name=f'm{i}',source_asset_index=i,surfaces=surfaces,num_bones=0,
                 num_root_bones=0,num_collision_surfaces=0,inline_phys_preset=None)
        word(root+0x20,FOLLOWING);word(root+0x24,FOLLOWING)
        models.append(NS(root_offset=root,model=model,inline_material_offsets=(0x700,) if i==1 else (),
                         material_handle_pointers=(FOLLOWING,) if i==1 else (), inline_rigid_offsets=(None,)))
    sig=(0x1111,0,0,1,2)
    image=tex(name='leaf',raw=FOLLOWING,signature=sig);image.inline_image_root=0x900
    owner=mat(0x700,'a',(image,))
    word(0x700,FOLLOWING);word(0x744,FOLLOWING);word(0x920,FOLLOWING)
    word(0x2D4,FOLLOWING);word(0xA00,FOLLOWING);word(0xA1C,FOLLOWING)
    z[0xA2C:0xA30]=b'p\0s\0'
    preset=NS(root_offset=0xA00,physical_end=0xA30)
    models[1].model.inline_phys_preset=preset
    base=0x1000+PC_XSURFACE_ROOT_SIZE
    target=base+16
    request=mat(0xB00,'use',(tex(name=None,raw=encode_pc_packed(4,target),signature=sig),))
    layouts=_relative_xmodel_layouts(models,{0x700:owner},bytes(z))
    args=(bytes(z),models,layouts,{0x700:owner},{0:0x1000,2:base+32},_packed_requests((request,)))
    result=_replay_closed_xmodel_intervals(*args)
    assert result['aliases']=={target:'leaf'},result
    preset.physical_end+=1
    result=_replay_closed_xmodel_intervals(*args)
    assert not result['aliases'] and not result['bases'],result
    assert 'physical boundary' in result['intervals'][0]['reason']
    print('PASS: bounded PhysPreset tail closes image interval; incorrect boundary rejected')
if __name__=='__main__':main()
