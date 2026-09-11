from pathlib import Path
import sys
import unittest
from types import SimpleNamespace as Obj
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cod4porter.fx_omission import plan_omissions
from cod4porter.fx_script_omission import rewrite_scripts
from cod4porter.source_assets import SourceRawFile

def graph(extra=()):
    objects=[(100,'MaterialTechniqueSet','effect_spot'),(110,'MaterialTechniqueSet','effect'),
      (200,'Material','bad'),(210,'Material','shared'),(300,'GfxImage','shared_image'),
      (400,'FxEffectDef','bad_fx'),(410,'FxEffectDef','parent_fx'),(420,'FxEffectDef','good_fx'),(500,'GfxWorld','world')]
    edges=[(200,100),(210,110),(210,300),(400,200),(400,210),(420,210),*extra]
    report={'objects':[dict(root=r,type=t,name=n)for r,t,n in objects],
      'assets':[dict(root=r,index=i)for i,(r,t,n)in enumerate(objects)],
      'asset_edges':[dict(owner=a,target=b)for a,b in edges],
      'named_asset_refs':[dict(owner=410,type='AssetFx',name='bad_fx')]}
    return Obj(report=report),[Obj(root_offset=100),Obj(root_offset=110)],[Obj(can_bind_native=False,candidate_name='effect_spot'),Obj(can_bind_native=True,candidate_name='effect')]

def raw(name,text):return SourceRawFile(0,0,name,text.encode(),len(text))

class FxOmissionTests(unittest.TestCase):
    def test_transitive_fx_and_exclusive_dependency_removal(self):
        p=plan_omissions(*graph())
        self.assertEqual(p.effect_names,{'bad_fx','parent_fx'})
        self.assertEqual(p.roots,{100,200,400,410})
        self.assertNotIn(210,p.roots)
        self.assertEqual(p.image_names,set())

    def test_impact_table_can_keep_other_entries(self):
        index,tech,bind=graph()
        index.report['objects'].append(dict(root=700,type='FxImpactTable',name='impacts'))
        index.report['assets'].append(dict(root=700,index=10))
        index.report['asset_edges'].append(dict(owner=700,target=400))
        p=plan_omissions(index,tech,bind)
        self.assertIn(400,p.roots)
        self.assertNotIn(700,p.roots)

    def test_world_using_bad_shader_still_blocks(self):
        with self.assertRaisesRegex(ValueError,'outside effects'):
            plan_omissions(*graph([(500,200)]))

    def test_strict_policy_stops(self):
        with self.assertRaisesRegex(ValueError,'Unsupported PS3 shaders'):
            plan_omissions(*graph(),policy='error')

    def test_unresolvable_fx_seeds_the_same_transitive_omission(self):
        # mp_wmd_night class: an FX whose packed runner references cannot be
        # proven is omitted like an unsupported-shader FX - together with its
        # named parent - instead of aborting the conversion.
        index,tech,_bind=graph()
        bind=[Obj(can_bind_native=True,candidate_name='effect_spot'),
              Obj(can_bind_native=True,candidate_name='effect')]
        p=plan_omissions(index,tech,bind,unresolvable_fx={400:'target corridor is not physically contiguous'})
        self.assertEqual(p.effect_names,{'bad_fx','parent_fx'})
        self.assertIn(400,p.roots)
        self.assertIn(410,p.roots)
        self.assertNotIn(420,p.roots)
        self.assertEqual(p.report['unresolvable_fx'],
                         [{'root':400,'name':'bad_fx','reason':'target corridor is not physically contiguous'}])

    def test_unresolvable_fx_strict_policy_stops(self):
        index,tech,_bind=graph()
        bind=[Obj(can_bind_native=True,candidate_name='effect_spot'),
              Obj(can_bind_native=True,candidate_name='effect')]
        with self.assertRaisesRegex(ValueError,'Unprovable FX references'):
            plan_omissions(index,tech,bind,policy='error',
                           unresolvable_fx={400:'broken corridor'})

    def test_unresolvable_root_must_be_a_typed_fx(self):
        index,tech,_bind=graph()
        bind=[Obj(can_bind_native=True,candidate_name='effect_spot'),
              Obj(can_bind_native=True,candidate_name='effect')]
        with self.assertRaisesRegex(ValueError,'not a typed FxEffectDef'):
            plan_omissions(index,tech,bind,unresolvable_fx={210:'reason'})

    def test_kept_child_fx_keeps_its_materials(self):
        p=plan_omissions(*graph([(400,420)]))
        self.assertNotIn(420,p.roots)
        self.assertNotIn(210,p.roots)
        self.assertNotIn(300,p.roots)

    def test_script_load_removed_and_playback_guarded(self):
        source='main(){fx=loadfx("bad_fx"); if(x) PlayFX(fx,self.origin); else y();}'
        rows,changes=rewrite_scripts([raw('a.gsc',source)],{'bad_fx'})
        text=rows[0].payload.decode()
        self.assertIn('fx=undefined',text)
        self.assertIn('if(x) iw3porter_guard_playfx_2(fx,self.origin); else y();',text)
        self.assertIn('if (!isdefined(a0)) return;',text)
        self.assertNotIn('"bad_fx"',text)

    def test_comments_and_string_contents_untouched(self):
        source='main(){// loadfx("bad_fx");\nx="PlayFX(a,b)"; fx=loadfx("good_fx");}'
        rows,_=rewrite_scripts([raw('a.gsc',source)],{'bad_fx'})
        self.assertEqual(rows[0].payload.decode(),source)

    def test_createfx_registration_and_only_matching_block_removed(self):
        scripts=[raw('register.gsc','main(){level._effect["bad_key"]=loadfx("bad_fx");}'),
          raw('create.gsc','main(){ent=maps\\mp\\_utility::createOneshotEffect("bad_key");ent.v["fxid"]="bad_key";ent.v["delay"]=-15;ent=maps\\mp\\_utility::createOneshotEffect("good_key");ent.v["fxid"]="good_key";}')]
        rows,_=rewrite_scripts(scripts,{'bad_fx'})
        text=''.join(r.payload.decode()for r in rows)
        self.assertNotIn('bad_key',text)
        self.assertIn('createOneshotEffect("good_key")',text)

    def test_dynamic_lookup_stops_instead_of_dangling_reference(self):
        with self.assertRaisesRegex(ValueError,'dynamic loadfx'):
            rewrite_scripts([raw('a.gsc','main(){fx=loadfx(name);}')],{'bad_fx'})

    def test_unknown_consumer_of_removed_key_stops(self):
        source='main(){level._effect["key"]=loadfx("bad_fx");unknown("key");}'
        with self.assertRaisesRegex(ValueError,'Unresolved script reference'):
            rewrite_scripts([raw('a.gsc',source)],{'bad_fx'})

if __name__=='__main__':unittest.main()
