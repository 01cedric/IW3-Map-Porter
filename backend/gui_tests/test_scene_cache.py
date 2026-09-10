import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT/'tools/ps3_loader_emulator'))
import gui_cache
import gui_emulator

class SceneCache(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.base=Path(self.temp.name)
        self.env=patch.dict(os.environ, LOCALAPPDATA=str(self.base)); self.env.start()
        self.out=self.base/'run'; self.out.mkdir()
        (self.out/'world.scene.json').write_text(json.dumps({'mesh_file':'world.scene.bin','textures':{'0':'texture.png'}}))
        for name in ('world.scene.bin','texture.png','material-rsx-report.json','mp_test.zone.bin.link.json'):
            (self.out/name).write_bytes(b'fixture')
        self.report=self.out/'mp_test.zone.bin.link.json'
        self.report.write_text(json.dumps({'status':'ok', 'externals_missing':[]}))
        self.dest=self.base/'reopen'; self.dest.mkdir()
    def tearDown(self): self.env.stop(); self.temp.cleanup()
    def test_roundtrip_and_corruption_rejection(self):
        gui_cache.store('abc', self.out, self.report)
        self.assertIsNotNone(gui_cache.restore('abc',self.dest))
        self.assertEqual((self.dest/'texture.png').read_bytes(),b'fixture')
        (gui_cache.cache_root()/'abc/world.scene.bin').write_bytes(b'damaged')
        self.assertIsNone(gui_cache.restore('abc',self.dest))
    def test_input_changes_invalidate_scene(self):
        support=self.base/'support';support.mkdir();backend=self.base/'code';backend.mkdir()
        for name in ('code_post_gfx_mp','ui_mp','common_mp'): (support/(name+'.ff')).write_bytes(b'A')
        ff=self.base/'mp_test.ff';ff.write_bytes(b'A');elf=self.base/'game.elf';elf.write_bytes(b'A')
        s=dict(ps3_ff=str(ff),elf_path=str(elf),support_directory=str(support),map_name='mp_test')
        first=gui_cache.key_for(s,backend)
        ff.write_bytes(b'B'); self.assertNotEqual(first,gui_cache.key_for(s,backend))
        ff.write_bytes(b'A'); self.assertEqual(first,gui_cache.key_for(s,backend))
        (backend/'parser.py').write_text('new parser');self.assertNotEqual(first,gui_cache.key_for(s,backend))
    def test_partial_and_path_traversal_rejected(self):
        gui_cache.store('abc',self.out,self.report)
        p=gui_cache.cache_root()/'abc/manifest.json';d=json.loads(p.read_text());d['files']['../outside']='x';p.write_text(json.dumps(d))
        self.assertIsNone(gui_cache.restore('abc',self.dest))
        self.assertIsNone(gui_cache.restore('unfinished',self.dest))
    def test_cache_hit_skips_game_emulation(self):
        gui_cache.store('abc',self.out,self.report)
        with patch.object(gui_emulator,'validate_elf'),patch.object(gui_cache,'key_for',return_value='abc'),patch.object(gui_emulator,'zone_bytes',side_effect=AssertionError('Emulation path repeated')):
            status,message=gui_emulator.execute('link',{'elf_path':'fixture'},self.dest,lambda *a,**k:None,lambda *a:None)
        self.assertEqual(status,'unproven');self.assertIn('Cached',message)
        self.assertTrue((self.dest/'preview-timings.json').is_file())
    def test_unused_models_are_not_dereferenced(self):
        machine=SimpleNamespace(m=object(),inventory=lambda:{(3,'unused'):0x12345})
        self.assertEqual(gui_emulator.read_models(machine,set()),{})

if __name__=='__main__': unittest.main(verbosity=2)
