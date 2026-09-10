import sys
import struct
import unittest
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from cod4porter.technique_binding import canonical_stock_name,bind_technique
from cod4porter.pc_technique import TechniqueSet
from cod4porter.pc_technique_replay import replay_technique_members
from cod4porter.pc_fx import resolve_fx_runner_bindings
from cod4porter.pc_xmodel import _best_two_monotonic_assignments
from cod4porter.physics import top_level_phys_presets
from cod4porter.backend.v4_ffio import XAsset,XAssetList,FOLLOWING
from test_fx_runner_replay import _reference,_element
from test_pc_load_converter import _pc_load_fastfile,_iwi
from cod4porter.pc_load_converter import convert_pc_load_to_ps3


class ConversionRegressionTests(unittest.TestCase):
    def test_owned_defeat_pixels_and_reported_missing_pixel_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp=Path(directory);pc=tmp/'source.ff';iwd=tmp/'source.iwd';out=tmp/'mp_fixture_load.ff'
            pc.write_bytes(_pc_load_fastfile('mp_fixture',shared_victory=True,owned_defeat=True))
            with zipfile.ZipFile(iwd,'w') as archive:
                archive.writestr('images/loadscreen_mp_fixture.iwi',_iwi('loadscreen_mp_fixture'))
            report=convert_pc_load_to_ps3(pc,iwd,out)
            self.assertTrue(report['defeat_material_shared'])
            self.assertFalse(report['load_full_fidelity'])
            self.assertIn('native_shared_defeatbackdrop_missing_source_pixels',report['compatibility_fallbacks'])
            with zipfile.ZipFile(iwd,'a') as archive:archive.writestr('images/defeat.iwi',_iwi('defeat'))
            report=convert_pc_load_to_ps3(pc,iwd,out,overwrite=True)
            self.assertFalse(report['defeat_material_shared'])
            self.assertTrue(report['defeat_resource_sha256'])
            self.assertTrue(report['load_full_fidelity'])

    def test_consecutive_physics_roots_exclude_later_shader_false_positive(self):
        zone=bytearray(0x500)
        def put(offset,name):
            struct.pack_into('<Ii',zone,offset,FOLLOWING,1)
            zone[offset+44:offset+45+len(name)]=name.encode()+b'\0'
            return offset+45+len(name)
        second=put(0x100,'wood_chunk');put(second,'cardboardbox_empty')
        put(0x280,'false_shader_candidate')
        lower=SimpleNamespace(model=SimpleNamespace(source_asset_index=0),root_offset=0x80,physical_end=0x100)
        upper=SimpleNamespace(model=SimpleNamespace(source_asset_index=5),root_offset=0x400,physical_end=0x440)
        assets=XAssetList('pc',0,0,(),tuple(XAsset(i,0,1,'physpreset',FOLLOWING) for i in (1,2)),0,0x40)
        result=top_level_phys_presets(bytes(zone),assets,(lower,upper))
        self.assertEqual([p.name for p in result],['wood_chunk','cardboardbox_empty'])

    def test_a0_family_is_recognized_but_reported_as_substitution(self):
        for name in ('mc_l_sm_a0c0','mc_l_hsm_a0c0'):
            self.assertTrue(canonical_stock_name(name))
            b=bind_technique(TechniqueSet(0,'sm2/'+name,0,False,0,(0,)*34))
            self.assertTrue(b.can_bind_native)
            self.assertTrue(b.substitution)
            self.assertNotEqual(b.candidate_name,name)
        self.assertFalse(canonical_stock_name('mc_l_sm_q0c0'))

    def test_one_model_cannot_have_two_material_array_bases(self):
        a=dict(cell=(0,0),model_index=0,base=0x1000,pair_score=30,surface=0)
        b=dict(cell=(0,1),model_index=0,base=0x2000,pair_score=40,surface=1)
        paths,count,_=_best_two_monotonic_assignments((0x1000,0x2004),((a,),(b,)))
        self.assertEqual((paths,count),([],0))

    def bridge_fixture(self):
        # Two FX targets separated by an owned PC technique and shader, with
        # independent packed name anchors. No copyrighted fixture payloads.
        root=bytearray(148);struct.pack_into('<I',root,0,0xffffffff)
        struct.pack_into('<I',root,28,0xffffffff)
        techroot=struct.pack('<IHH',0xffffffff,0,1)
        shader=struct.pack('<IIIHH',0xffffffff,0,0xffffffff,2,0)
        program=struct.pack('<II',0xfffe0200,0xffff)
        entry=struct.pack('<III4BI',0,0xffffffff,0,0,0,0,0,0)
        data=bytes(root)+b'bridge\0'+techroot+entry+shader+b'shader\0'+program+b'pass\0'
        start=0x1000;zone=bytes(start)+data
        tech=TechniqueSet(start,'bridge',0,False,0,(0,0,0,0,0xffffffff)+(0,)*29,11)
        end=replay_technique_members(zone,tech,0x202,len(zone))
        target_a=_reference(10,start-34,start,'a')
        target_b=_reference(12,len(zone),len(zone)+34,'b')
        owner=_reference(13,len(zone)+34,len(zone)+300,'owner',
                         _element(0,0x40000201,0x40000001+end))
        return zone,tech,(target_a,target_b,owner),end

    def test_owned_shader_bridge_requires_exact_anchor(self):
        zone,tech,refs,end=self.bridge_fixture()
        result=resolve_fx_runner_bindings('any',refs,zone=zone,technique_sets=(tech,))
        self.assertEqual([b.target_source_asset_index for b in result.bindings],[10,12])
        bad_owner=_reference(13,refs[2].root_offset,refs[2].physical_end_offset,'owner',
                             _element(0,0x40000201,0x40000005+end))
        with self.assertRaisesRegex(ValueError,'anchor'):
            resolve_fx_runner_bindings('any',(*refs[:2],bad_owner),zone=zone,technique_sets=(tech,))

    def test_shader_bridge_rejects_corrupt_program(self):
        zone,tech,refs,end=self.bridge_fixture()
        broken=bytearray(zone)
        pos=broken.index(struct.pack('<II',0xfffe0200,0xffff),tech.root_offset)
        broken[pos:pos+4]=bytes(4)
        with self.assertRaisesRegex(ValueError,'header/terminator'):
            replay_technique_members(bytes(broken),tech,0x202,len(broken))

    def test_later_runner_reuses_verified_targets(self):
        zone,tech,refs,end=self.bridge_fixture()
        later=_reference(50,len(zone)+1000,len(zone)+2000,'later',
                         _element(0,0x40000201,0x40000001+end))
        result=resolve_fx_runner_bindings('any',(*refs,later),zone=zone,technique_sets=(tech,))
        self.assertEqual([b.target_source_asset_index for b in result.bindings],[10,12,10,12])
