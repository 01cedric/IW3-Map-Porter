import json
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
import zipfile
import numpy as np
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tools/ps3_loader_emulator'))
from gui_emulator import read_models
from gui_materials import export_materials
from gui_scene import export_scene
from gui_texture_sources import IwdTextures
from gui_cache import key_for


class Memory:
    def __init__(self):self.data=bytearray(0x30000)
    def read(self,a,n):
        if a<0 or a+n>len(self.data):raise ValueError('outside fixture')
        return bytes(self.data[a:a+n])
    def u8(self,a):return self.data[a]
    def u16(self,a):return struct.unpack_from('>H',self.data,a)[0]
    def u32(self,a):return struct.unpack_from('>I',self.data,a)[0]
    def put(self,a,fmt,*v):struct.pack_into('>'+fmt,self.data,a,*v)
    def string(self,a,value):self.data[a:a+len(value)+1]=value.encode()+b'\0'


def iwi(pixels):
    size=28+len(pixels)
    return b'IWi\x06\x01\x02'+struct.pack('<HHH4I',2,1,1,*([size]*4))+pixels


class ModelTextures(unittest.TestCase):
    def test_linked_model_uv_material_iwd_and_scene_roundtrip(self):
        m=Memory();hdr=0x10000;surf=0x11000;vp=0x12000;secondary=0x13000;tp=0x14000;handles=0x15000
        material=0x18000;table=0x19000;image_hdr=0x1a000
        m.data[hdr+6]=1;m.put(hdr+0x20,'II',surf,handles);m.put(hdr+0x28,'fHH',0,1,0)
        m.put(surf+2,'HH',3,1);m.put(surf+8,'I',tp);m.put(surf+0x18,'I',vp);m.put(surf+0x24,'I',secondary)
        expected_uv=np.array([[0.25,0.75],[1.25,-0.25],[0.5,1.5]])
        for i,coords in enumerate(expected_uv):
            m.put(vp+i*16,'4f',i,0,0,1);m.put(secondary+i*16+4,'2e',*coords)
        m.put(tp,'3H',0,1,2);m.put(handles,'I',material);m.put(material,'I',0x1c000);m.string(0x1c000,'foliage/leaf')
        m.data[material+0x32]=1;m.put(material+0x74,'I',table);m.data[table+7]=2;m.put(table+8,'I',image_hdr)
        m.put(image_hdr+0x30,'I',0x1b000);m.put(image_hdr+8,'I',0x1aae4);m.string(0x1b000,'leaf')
        machine=SimpleNamespace(m=m,inventory=lambda:{(3,'palm'):hdr})
        models=read_models(machine,{hdr});surface=models['palm']['surfaces'][0]
        np.testing.assert_array_equal(surface['uv'],expected_uv)
        self.assertEqual(surface['material'],'foliage/leaf')
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp);archive=out/'map.iwd';ff=out/'map.ff';ff.write_bytes(b'fixture')
            # B,G,R,A: retain transparent leaf pixels in the exported PNG.
            with zipfile.ZipFile(archive,'w') as z:z.writestr('images/leaf.iwi',iwi(bytes([10,20,30,0,40,50,60,255])))
            textures=export_materials(machine,{material:'foliage/leaf'},dict(ps3_ff=str(ff),iwd_paths=[str(archive)]),b'',out)
            self.assertIn('foliage/leaf',textures)
            with Image.open(out/textures['foliage/leaf']) as png:
                self.assertEqual(png.getpixel((0,0)),(30,20,10,0));self.assertEqual(png.getpixel((1,0)),(60,50,40,255))
            report=json.loads((out/'material-rsx-report.json').read_text())
            self.assertEqual(report['materials'][0]['textures'][0]['source']['kind'],'pc-iwd-preview')
            d=dict(pos=np.zeros((0,3)),indices=np.zeros(0,dtype=int),surfaces=np.zeros((0,11),dtype=int),
                   smodels=np.array([[0,10,20,30,1023,1023<<11,511<<22,1,hdr,0,0]],dtype=float))
            scene=export_scene(d,{},out,models,textures);doc=json.loads(scene.read_text())
            self.assertEqual(doc['triangles'],1);self.assertEqual(doc['textures'],{'0':textures['foliage/leaf']})
            raw=(out/doc['mesh_file']).read_bytes()
            np.testing.assert_array_equal(np.frombuffer(raw,dtype='<f4',offset=24+36,count=6).reshape(3,2),expected_uv)

    def test_default_image_recovers_exact_verified_binding_from_stock_library(self):
        import hashlib
        m=Memory();material=0x18000;table=0x19000;image_hdr=0x1a000
        m.data[material+0x32]=1;m.put(material+0x74,'I',table)
        m.data[table+7]=2;m.put(table,'I',123);m.put(table+8,'I',image_hdr)
        m.put(image_hdr+0x30,'I',0x1b000);m.put(image_hdr+8,'I',0x1aae4);m.string(0x1b000,'$white')
        machine=SimpleNamespace(m=m,inventory=lambda:{},default_clones=[(6,'leaf','map')],default_replaced=[])
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp);library=out/'stock';library.mkdir();ff=out/'map.ff';ff.write_bytes(b'fixture')
            with zipfile.ZipFile(library/'iw_00.iwd','w') as z:
                z.writestr('images/leaf.iwi',iwi(bytes([10,20,30,0,40,50,60,255])))
            report={'main':{'fastfile_sha256':hashlib.sha256(b'fixture').hexdigest()},
                    'material_texture_bindings':[{'name':'foliage/leaf','textures':[{'index':0,'semantic':2,'name_hash':123,'image':'leaf'}]}]}
            (out/'map.python-port-report.json').write_text(json.dumps(report))
            settings={'ps3_ff':str(ff),'texture_library_directory':str(library)}
            textures=export_materials(machine,{material:'foliage/leaf'},settings,b'',out)
            with Image.open(out/textures['foliage/leaf']) as png:self.assertEqual(png.getpixel((0,0)),(30,20,10,0))
            doc=json.loads((out/'material-rsx-report.json').read_text());texture=doc['materials'][0]['textures'][0]
            self.assertTrue(texture['native_default_image']);self.assertEqual(texture['runtime_image'],'$white')
            self.assertEqual(texture['image'],'leaf')
            # A stale report must not supply material identity for a different FF.
            ff.write_bytes(b'changed')
            self.assertEqual(export_materials(machine,{material:'foliage/leaf'},settings,b'',out),{})

    def test_conflicting_iwd_sources_are_not_silently_chosen(self):
        with tempfile.TemporaryDirectory() as temp:
            paths=[]
            for index in (1,2):
                p=Path(temp)/f'{index}.iwd';paths.append(p)
                with zipfile.ZipFile(p,'w') as z:z.writestr('images/leaf.iwi',iwi(bytes([index])*8))
            source=IwdTextures(paths)
            try:
                with self.assertRaisesRegex(ValueError,'Conflicting'):source.read('leaf')
            finally:source.close()

    def test_scene_cache_tracks_iwd_pixel_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            for name in ('map.ff','game.elf','code_post_gfx_mp.ff','ui_mp.ff','common_mp.ff','map.iwd'):(folder/name).write_bytes(b'fixture')
            settings=dict(map_name='mp_test',ps3_ff=str(folder/'map.ff'),elf_path=str(folder/'game.elf'),support_directory=temp,iwd_paths=[str(folder/'map.iwd')])
            before=key_for(settings,folder);(folder/'map.iwd').write_bytes(b'changed')
            self.assertNotEqual(before,key_for(settings,folder))


if __name__=='__main__':unittest.main()
