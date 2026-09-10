import io
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from gui_menu_picture import prepare_picture
from gui_materials import decode_texture
import gui_bridge


def document(width,height,fmt=0xa5,pixels=None):
    desc=bytearray(40);desc[:4]=bytes((fmt,1,2,0))
    struct.pack_into('>I',desc,4,0x1aae4)
    struct.pack_into('>HHH',desc,8,width,height,1)
    pixels=pixels if pixels is not None else bytes((77,23,45,67))*(width*height)
    struct.pack_into('>I',desc,0x10,width*4 if fmt==0xa5 else 0)
    struct.pack_into('>I',desc,0x1c,len(pixels))
    image=SimpleNamespace(map_type=3,descriptor=bytes(desc),resource=pixels,name='source_picture')
    return SimpleNamespace(rawfile=SimpleNamespace(name='mp_picture_load'),
                           materials=[SimpleNamespace(name='$levelbriefing',textures=[SimpleNamespace(image=image)])])


class PictureImport(unittest.TestCase):
    def test_resize_preserves_aspect_and_bounds_and_keeps_input(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'ported_load.ff';path.write_bytes(b'unchanged input')
            with patch('gui_menu_picture.read_load_document',return_value=document(1024,512)):
                root,pixels,rgba,report=prepare_picture(path,'mp_picture')
            self.assertEqual(rgba.size,(512,256))
            self.assertTrue(report['resized'])
            self.assertEqual(len(pixels),512*256*4)
            self.assertEqual(root[4],0xa5)
            self.assertEqual(struct.unpack_from('>I',root,0x14)[0],512*4)
            self.assertEqual(struct.unpack_from('>I',root,0x20)[0],len(pixels))
            restored=decode_texture(pixels,512,256,0xa5)
            self.assertEqual(restored.tobytes(),rgba.tobytes())
            self.assertEqual(path.read_bytes(),b'unchanged input')

    def test_small_bc_picture_decodes_without_upscaling(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'ported_load.ff';path.write_bytes(b'fixture')
            red=struct.pack('<HHI',0xf800,0,0)
            with patch('gui_menu_picture.read_load_document',return_value=document(4,4,0x86,red)):
                _,_,rgba,report=prepare_picture(path)
            self.assertEqual(rgba.size,(4,4));self.assertEqual(rgba.getpixel((1,1)),(255,0,0,255))
            self.assertFalse(report['resized']);self.assertEqual(report['map_id'],'mp_picture')

    def test_wrong_map_and_truncated_resource_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'ported_load.ff';path.write_bytes(b'fixture')
            with patch('gui_menu_picture.read_load_document',return_value=document(4,4)):
                with self.assertRaisesRegex(ValueError,'belongs to'):prepare_picture(path,'mp_other')
            with patch('gui_menu_picture.read_load_document',return_value=document(4,4,0x86,b'\0')):
                with self.assertRaises(ValueError):prepare_picture(path)

    def test_picture_command_needs_no_ui_file_and_produces_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'ported_load.ff';path.write_bytes(b'fixture')
            out=Path(folder)/'output';wire=io.StringIO()
            with patch('gui_menu_picture.read_load_document',return_value=document(4,4)),patch.object(gui_bridge,'WIRE',wire):
                status,_=gui_bridge.run(dict(schema_version=1,command='mapmenu-picture',output_dir=str(out),
                                             settings=dict(ps3_load_ff=str(path),map_name='mp_picture')))
            self.assertEqual(status,'passed')
            report=json.loads((out/'map-menu-picture.json').read_text())
            self.assertTrue(Path(report['preview_png']).is_file())
            self.assertEqual(report['dimensions'],[4,4])
            self.assertFalse((out/'ui_mp.ff').exists())


if __name__=='__main__':unittest.main()
