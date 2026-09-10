from __future__ import annotations
from dataclasses import dataclass,asdict
from pathlib import Path
from typing import Mapping,Any,Sequence
import hashlib,struct

from .backend.v4_ffio import read_ps3_fastfile,parse_xasset_list,parse_zone_header,decode_pc_pointer,decode_ps3_pointer
from .main_ff import MainBuildResult
from .plan import PortPlan
from .graph import AssetType
from .zone_writer import NULL,FOLLOWING,INSERT,ZONE_HEADER_SIZE

@dataclass(frozen=True)
class MainReadbackReport:
    asset_count:int
    script_string_count:int
    type_counts:Mapping[str,int]
    zone_bytes:int
    zone_memory_size:int
    zero_tail_bytes:int
    zone_sha256:str
    passed:bool
    checks:Mapping[str,bool]|None=None
    family_checks:tuple[Mapping[str,Any],...]=()
    relocation_checks:Mapping[str,Any]|None=None
    pointer_leaks:int=0


def _u16(z:bytes,o:int)->int:return struct.unpack_from('>H',z,o)[0]
def _u32(z:bytes,o:int)->int:return struct.unpack_from('>I',z,o)[0]
def _i32(z:bytes,o:int)->int:return struct.unpack_from('>i',z,o)[0]
def _sha(b:bytes)->str:return hashlib.sha256(b).hexdigest().upper()

def _need(z:bytes,o:int,n:int,label:str)->None:
    if o<ZONE_HEADER_SIZE or n<0 or o+n>len(z):raise ValueError(f'{label} range 0x{o:X}+0x{n:X} outside PS3 zone')

def _packed_ok(raw:int,block_sizes:Sequence[int])->bool:
    if raw in (NULL,FOLLOWING,INSERT):return True
    try:
        p=decode_ps3_pointer(raw,block_sizes)
        return p.kind=='packed' and p.block is not None and p.offset is not None and 0<=p.block<len(block_sizes) and 0<=p.offset<block_sizes[p.block]
    except Exception:return False

def _result_key(node,results:Mapping[str,Any])->str|None:
    prefixes={
      AssetType.TECHSET:('techset.external:',),AssetType.IMAGE:('image:','image.external:'),
      AssetType.MATERIAL:('material:','material.external:'),AssetType.LIGHTDEF:('lightdef:',),
      AssetType.PHYSPRESET:('physpreset:',),AssetType.XMODEL:('xmodel:','xmodel.external:'),AssetType.COMWORLD:('comworld:',),
      AssetType.GFXWORLD:('gfxworld:',),AssetType.GAMEWORLD_MP:('gameworldmp:',),AssetType.CLIPMAP_MP:('clipmap:',),
      AssetType.RAWFILE:('rawfile:',),AssetType.STRINGTABLE:('stringtable:',),AssetType.FX:('fx.owned:','fx.external:'),
      AssetType.IMPACTFX:('impactfx:',),AssetType.SOUND:('sound:',),AssetType.SNDCURVE:('sndcurve:',),AssetType.LOADED_SOUND:('loadedsound:'),
    }
    hits=[]
    for p in prefixes.get(node.type,()):
        k=p+node.symbol
        if k in results:hits.append(k)
    if len(hits)!=1:return None
    return hits[0]

def _family_check(z:bytes,block_sizes:Sequence[int],node,row:Mapping[str,Any])->dict:
    rp=int(row.get('root_physical',-1)); typ=node.type; errs=[]; proof={}
    def chk(cond,msg):
        if not cond:errs.append(msg)
    try:
        sizes={AssetType.TECHSET:0x70,AssetType.IMAGE:0x34,AssetType.MATERIAL:0x80,AssetType.LIGHTDEF:0x10,
               AssetType.PHYSPRESET:0x2c,AssetType.XMODEL:0xcc,AssetType.COMWORLD:0x10,AssetType.GFXWORLD:0x2e4,
               AssetType.GAMEWORLD_MP:4,AssetType.CLIPMAP_MP:0x11c,AssetType.RAWFILE:0x0c,AssetType.STRINGTABLE:0x10,
               AssetType.FX:0x20,AssetType.IMPACTFX:0x08,AssetType.SOUND:0x0c,AssetType.SNDCURVE:0x48,AssetType.LOADED_SOUND:0x1c}
        _need(z,rp,sizes.get(typ,4),f'{typ.name} {node.symbol}')
        proof['root_physical']=rp
        if typ==AssetType.TECHSET:
            chk(_u32(z,rp)==FOLLOWING,'TechniqueSet.name marker');proof['name_marker']=_u32(z,rp)
        elif typ==AssetType.IMAGE:
            raw_name=_u32(z,rp+0x30);proof.update({'map_type':_i32(z,rp),'format':z[rp+4],'levels':z[rp+5],'width':_u16(z,rp+0xc),'height':_u16(z,rp+0xe),'resource_bytes':_u32(z,rp+0x20),'name_marker':raw_name})
            if node.__class__.__name__=='ExternalImageNode':
                chk(raw_name==FOLLOWING,'external Image.name must FOLLOWING')
                chk(z[rp:rp+0x30]==bytes(0x30),'external Image body must match retail zero-shell ABI')
            else:
                chk(raw_name==FOLLOWING,'owned Image.name must FOLLOWING')
                img=node.image;chk(z[rp+4]==img.ps3_format,'Image format');chk(_u16(z,rp+0xc)==img.width and _u16(z,rp+0xe)==img.height,'Image dimensions');chk(_u32(z,rp+0x20)==len(img.resource),'Image resource length')
                res=int(row.get('resource_physical',-1));delay=int(z[rp+0x2b]);data_marker=_u32(z,rp+0x2c)
                policy=getattr(getattr(node,'resource_policy',None),'value',str(getattr(node,'resource_policy','self-contained')))
                proof.update({'delay_load_pixels':delay,'data_marker':data_marker,'resource_policy':policy,'resource_physical':res})
                if img.resource:
                    chk(data_marker==FOLLOWING,'owned Image.data marker must FOLLOWING')
                    if policy=='retail-delayed':
                        # A retail-delayed Image shell deliberately declares the source resource but
                        # omits it from the main zone.  CP19 uses this as a low-memory A/B candidate
                        # until an external PS3 stream container is generated.  Treat the audited
                        # shell policy as structurally valid instead of trying to hash bytes at -1.
                        chk(delay==1,'retail-delayed Image.delayLoadPixels must be 1')
                        chk(res<0,'retail-delayed Image must not expose an embedded resource offset')
                        chk(not bool(row.get('embedded',False)),'retail-delayed Image result must be non-embedded')
                        chk(int(row.get('omitted_resource_bytes',-1))==len(img.resource),'retail-delayed Image omitted byte count')
                    elif policy=='fastfile-delayed':
                        chk(delay==1,'fastfile-delayed Image.delayLoadPixels must be 1')
                        chk(res>=0,'fastfile-delayed Image must expose its global FIFO offset')
                        chk(not bool(row.get('embedded',False)),'fastfile-delayed Image must not be inlined')
                        chk(bool(row.get('deferred',False)),'fastfile-delayed Image result must be deferred')
                        chk(int(row.get('resource_logical_block',-1))==3,'fastfile-delayed Image must target block 3')
                        _need(z,res,len(img.resource),'delayed Image resource');chk(_sha(z[res:res+len(img.resource)])==_sha(img.resource),'delayed Image resource SHA256')
                    else:
                        chk(delay==0,'self-contained Image.delayLoadPixels must be 0')
                        _need(z,res,len(img.resource),'Image resource');chk(_sha(z[res:res+len(img.resource)])==_sha(img.resource),'Image resource SHA256')
                else:
                    chk(data_marker==NULL,'resource-less Image.data marker must be NULL')
                    chk(delay==0,'resource-less Image.delayLoadPixels must be 0')
        elif typ==AssetType.MATERIAL:
            name=_u32(z,rp);proof['name_marker']=name
            if node.__class__.__name__=='ExternalMaterialNode':chk(name==FOLLOWING,'external Material.name must FOLLOWING')
            else:
                chk(name==FOLLOWING,'Material.name marker');tc=z[rp+0x32];cc=z[rp+0x33];sc=z[rp+0x34];proof.update({'textures':tc,'constants':cc,'states':sc,'water_count':len(node.source.water_by_texture)})
                chk(tc==len(node.source.textures) and cc==len(node.source.constants) and sc==len(node.state.state_bits),'Material table counts')
                chk(z[rp+0x38:rp+0x6C]==node.platform_state[:0x34],'Material PlatformState exact bytes')
                chk(_u32(z,rp+0x6C)!=0,'Material technique runtime table marker must be non-zero')
                chk(_packed_ok(_u32(z,rp+0x70),block_sizes) and _u32(z,rp+0x70) not in (NULL,FOLLOWING,INSERT),'Material TechniqueSet packed relocation')
                entries=z[rp+0x18:rp+0x32];chk(all(x==0xff or x<sc for x in entries),'Material stateBitsEntry bounds')
                for off,count in ((0x74,tc),(0x78,cc),(0x7c,sc)):chk(_u32(z,rp+off)==(FOLLOWING if count else NULL),f'Material table pointer +0x{off:X}')
                # Independently walk owned 0x48 water roots following the texture table physical roots.
                owned_sites=set(row.get('owned_image_sites',()))
                if tc:
                    tp=int(row.get('texture_table_physical',-1));_need(z,tp,tc*0x0c,'Material texture table')
                    for ti in range(tc):
                        if ti in node.source.water_by_texture:
                            chk(_u32(z,tp+ti*0x0c+8)==FOLLOWING,f'water texture[{ti}] root marker')
                            continue
                        raw_image=_u32(z,tp+ti*0x0c+8);site=f'texture[{ti}]'
                        if site in owned_sites:
                            chk(raw_image in (FOLLOWING,INSERT),f'{site} owned image marker')
                        else:
                            chk(_packed_ok(raw_image,block_sizes) and raw_image not in (NULL,FOLLOWING,INSERT),f'{site} packed image relocation')
                waters=sorted(node.source.water_by_texture.items())
                if waters:
                    tp=int(row.get('texture_table_physical',-1));cur=tp+tc*0x0c
                    for ti,wsrc in waters:
                        _need(z,cur,0x48,f'water[{ti}] root');chk(_u32(z,cur+0x0c)==FOLLOWING,'water +0x0C PS3 pointer evidence');chk(_i32(z,cur+0x10)==wsrc.m and _i32(z,cur+0x14)==wsrc.n,'water dimensions')
                        raw_image=_u32(z,cur+0x44);site=f'water[{ti}]'
                        if site in owned_sites:
                            chk(raw_image in (FOLLOWING,INSERT),f'{site} owned image marker')
                        else:
                            chk(_packed_ok(raw_image,block_sizes) and raw_image not in (NULL,FOLLOWING,INSERT),'water image packed relocation')
                        n=wsrc.m*wsrc.n;cur+=0x48+n*8+n*4
        elif typ==AssetType.LIGHTDEF:
            chk(_u32(z,rp)==FOLLOWING,'LightDef.name marker');img=_u32(z,rp+4);owned=bool(row.get('attenuation_owned',False));external=bool(row.get('attenuation_external',False));proof.update({'attenuation_pointer':img,'attenuation_owned':owned,'attenuation_external':external})
            if external:
                expected=INSERT if node.attenuation_external_pointer_kind=='insert' else FOLLOWING
                chk(img==expected,'LightDef external attenuation ownership marker')
                er=int(row.get('attenuation_external_root_physical',-1));en=int(row.get('attenuation_external_name_physical',-1))
                _need(z,er,0x34,'LightDef external attenuation shell')
                chk(z[er:er+0x30]==b'\0'*0x30,'LightDef external attenuation shell reserved bytes')
                chk(_u32(z,er+0x30)==FOLLOWING,'LightDef external attenuation image name marker')
                expected_name=(','+node.attenuation_external_image_name.lstrip(',')).encode('latin-1')+b'\0'
                _need(z,en,len(expected_name),'LightDef external attenuation name')
                chk(z[en:en+len(expected_name)]==expected_name,'LightDef external attenuation name identity')
                chk(en==er+0x34,'LightDef external attenuation physical order')
            elif node.attenuation_image_symbol:
                if owned:chk(img in (FOLLOWING,INSERT),'LightDef owned image marker')
                else:chk(_packed_ok(img,block_sizes) and img not in (NULL,FOLLOWING,INSERT),'LightDef image packed relocation')
            else:chk(img==NULL,'LightDef null image')
        elif typ==AssetType.PHYSPRESET:
            chk(_u32(z,rp)==FOLLOWING,'PhysPreset.name marker');sp=_u32(z,rp+0x1c);chk(sp==FOLLOWING,'PhysPreset sound prefix must be a non-NULL XString')
            prefix=(node.preset.sound_alias_prefix or '').encode('latin-1')+b'\0'
            prefix_start=rp+0x2c+len(node.preset.name.encode('latin-1'))+1
            chk(z[prefix_start:prefix_start+len(prefix)]==prefix,'PhysPreset sound prefix bytes and terminator')
        elif typ==AssetType.XMODEL:
            if node.__class__.__name__=='ExternalXModelNode':
                chk(_u32(z,rp)==FOLLOWING,'external XModel.name marker')
                chk(z[rp+4:rp+0xcc]==bytes(0xc8),'external XModel zero-shell body')
                expected=node.serialized_name.encode('latin-1')+b'\0'
                np=int(row.get('name_physical',-1));_need(z,np,len(expected),'external XModel name')
                chk(np==rp+0xcc and z[np:np+len(expected)]==expected,'external XModel name identity and order')
                proof['external_name']=node.serialized_name
            else:
                chk(_u32(z,rp)==FOLLOWING,'XModel.name marker');proof.update({'bones':z[rp+4],'surfaces':z[rp+6],'collision_surfaces':_i32(z,rp+0x8c)})
                chk(z[rp+6]==node.model.surface_count,'XModel surface count');chk(z[rp+4]==node.model.num_bones,'XModel bone count')
                phys_word=_u32(z,rp+0xc4); nested_owner=getattr(node.model,'resolved_nested_phys_preset_owner_asset_index',None)
                if node.model.resolved_phys_preset_asset_index is not None and not node.suppress_resolved_phys_preset_reference:
                    chk(_packed_ok(phys_word,block_sizes) and phys_word not in (NULL,FOLLOWING,INSERT),'XModel PhysPreset top-level packed relocation')
                elif nested_owner is not None:
                    chk(_packed_ok(phys_word,block_sizes) and phys_word not in (NULL,FOLLOWING,INSERT),'XModel PhysPreset nested INSERT packed relocation')
                elif node.model.inline_phys_preset is not None:
                    expected=INSERT if getattr(node.model,'inline_phys_preset_pointer_kind','insert')=='insert' else FOLLOWING
                    chk(phys_word==expected,'XModel inline PhysPreset ownership marker')
                else:
                    chk(phys_word==NULL,'XModel null PhysPreset marker')
        elif typ==AssetType.COMWORLD:
            chk(_u32(z,rp)==FOLLOWING,'ComWorld.name marker');chk(_i32(z,rp+8)==node.source.primary_light_count,'ComWorld primary-light count');proof['primary_lights']=_i32(z,rp+8)
            namep=int(row.get('name_physical',-1));name_len=len(node.source.name.encode('latin-1'))+1;light_base=namep+name_len
            packed=0;nonnull=0
            for light in node.source.primary_lights:
                raw=_u32(z,light_base+light.index*0x44+0x40);kind=decode_ps3_pointer(raw,block_sizes).kind
                source_kind=decode_pc_pointer(light.def_name_pointer).kind
                if source_kind=='null':chk(raw==NULL,f'ComWorld light {light.index} null defName')
                elif source_kind in ('following','insert'):chk(raw==FOLLOWING,f'ComWorld light {light.index} inline defName')
                else:
                    chk(kind=='packed',f'ComWorld light {light.index} packed defName')
                    if kind=='packed':
                        p=decode_ps3_pointer(raw,block_sizes);chk(p.block==4,f'ComWorld light {light.index} defName block4');packed+=1
                if raw!=NULL:nonnull+=1
            proof.update({'packed_def_names':packed,'nonnull_def_names':nonnull})
        elif typ==AssetType.GFXWORLD:
            proof.update({'indices':_i32(z,rp+0x10),'surfaces':_i32(z,rp+0x1c),'vertices':_i32(z,rp+0x34),'vertex_layer_bytes':_i32(z,rp+0x44),'cells':_i32(z,rp+0xfc)})
            chk(proof['indices']==node.report.index_count,'GfxWorld index count');chk(proof['surfaces']==node.report.surface_count,'GfxWorld surface count');chk(proof['vertices']==node.report.vertex_count,'GfxWorld vertex count');chk(proof['vertex_layer_bytes']==len(node._layers),'GfxWorld PS3 vertex-layer byte count');chk(_packed_ok(_u32(z,rp),block_sizes),'GfxWorld name relocation')
            surface_physical=int(row.get('surface_physical',-1));expected_offsets=tuple(int(value) for value in row.get('surface_vertex_layer_offsets',()))
            _need(z,surface_physical,len(expected_offsets)*0x34,'GfxWorld surfaces')
            actual_offsets=tuple(_u32(z,surface_physical+i*0x34) for i in range(len(expected_offsets)))
            chk(len(expected_offsets)==node.report.surface_count and actual_offsets==expected_offsets,'GfxWorld PS3 surface vertex-layer offsets')
            proof.update({'vertex_layer_offset_unique':len(set(actual_offsets)),'vertex_layer_offset_max':max(actual_offsets,default=0)})
        elif typ==AssetType.GAMEWORLD_MP:
            chk(_packed_ok(_u32(z,rp),block_sizes) and _u32(z,rp) not in (NULL,FOLLOWING,INSERT),'GameWorldMP name packed relocation')
        elif typ==AssetType.CLIPMAP_MP:
            proof.update({'planes':_i32(z,rp+8),'static_models':_i32(z,rp+0x10),'triangles':_i32(z,rp+0x60),'brushes':_u16(z,rp+0x8c)})
            chk(proof['planes']==len(node.clip.planes),'ClipMap plane count');chk(proof['static_models']==len(node.clip.static_models),'ClipMap static model count');chk(proof['triangles']==node.clip.triangle_count,'ClipMap triangle count');chk(proof['brushes']==len(node.clip.brushes),'ClipMap brush count');chk(_packed_ok(_u32(z,rp),block_sizes),'ClipMap name relocation')
        elif typ==AssetType.RAWFILE:
            name_word=_u32(z,rp)
            if node.name_symbol:
                p=decode_ps3_pointer(name_word,block_sizes);chk(p.kind=='packed' and p.block==4,'RawFile shared name B4 relocation')
            else:chk(name_word==FOLLOWING,'RawFile.name marker')
            ln=_u32(z,rp+4);chk(ln==len(node.data),'RawFile payload length');bp=int(row.get('buffer_physical',-1));_need(z,bp,ln+1,'RawFile buffer');chk(z[bp:bp+ln]==node.data and z[bp+ln]==0,'RawFile exact payload+terminator');proof.update({'bytes':ln,'shared_name':bool(node.name_symbol),'name_pointer':name_word})
        elif typ==AssetType.STRINGTABLE:
            chk(_u32(z,rp)==FOLLOWING,'StringTable.name marker');cols=_u32(z,rp+4);rows=_u32(z,rp+8);chk(cols==node.columns and rows==node.rows,'StringTable shape');chk(_u32(z,rp+0xc)==(FOLLOWING if node.values else NULL),'StringTable values marker');proof.update({'columns':cols,'rows':rows,'values':len(node.values)})
        elif typ==AssetType.FX:
            chk(_u32(z,rp)==FOLLOWING,'FX.name marker');
            if node.__class__.__name__=='OwnedFxNode':chk(_u32(z,rp+0x1c)==(FOLLOWING if node.source.elements else NULL),'owned FX elem marker');proof['elements']=len(node.source.elements)
        elif typ==AssetType.IMPACTFX:
            chk(_u32(z,rp)==FOLLOWING,'ImpactFx.name marker');proof['nonnull']=int(row.get('nonnull',0))
        elif typ==AssetType.SOUND:
            chk(_u32(z,rp)==FOLLOWING,'Sound list name marker');chk(_u32(z,rp+8)==len(node.plan.source.aliases),'Sound alias count');proof['aliases']=_u32(z,rp+8)
        elif typ==AssetType.SNDCURVE:
            chk(_u32(z,rp)==(FOLLOWING if node.curve.name is not None else NULL),'SndCurve filename marker');chk(_u32(z,rp+4)==node.curve.knot_count,'SndCurve knot count')
        elif typ==AssetType.LOADED_SOUND:
            chk(_u32(z,rp)==(FOLLOWING if node.plan.name is not None else NULL),'LoadedSound name marker');chk(_u32(z,rp+4)==len(node.plan.payload),'LoadedSound payload length');chk(_u32(z,rp+0x18)==(INSERT if node.plan.payload else NULL),'LoadedSound data marker')
    except Exception as exc:errs.append(str(exc))
    return {'symbol':node.symbol,'type':typ.name,'passed':not errs,'errors':errs,'proof':proof}


def _relocation_readback(z:bytes,block_sizes:Sequence[int],rows:Sequence[Mapping[str,Any]])->dict:
    bad=[]
    for r in rows:
        off=int(r['physical_field_offset']);raw=int(r['serialized_pointer'])
        try:
            _need(z,off,4,'relocation field');actual=_u32(z,off)
            if actual!=raw:bad.append({'field':off,'reason':'serialized pointer drift','actual':actual,'expected':raw});continue
            p=decode_ps3_pointer(actual,block_sizes)
            if p.kind!='packed' or p.block!=int(r['target_block']) or p.offset!=int(r['target_offset']):bad.append({'field':off,'reason':'packed target mismatch','decoded':{'kind':p.kind,'block':p.block,'offset':p.offset},'row':dict(r)})
        except Exception as exc:bad.append({'field':off,'reason':str(exc)})
    return {'passed':not bad,'checked':len(rows),'failures':bad,'source_pointer_leaks':len(bad)}


def inspect_main_fastfile(path:str|Path,expected_plan:PortPlan|None=None,expected_build:MainBuildResult|None=None)->MainReadbackReport:
    doc=read_ps3_fastfile(path);al=parse_xasset_list(doc.zone,'ps3');hdr=parse_zone_header(doc.zone,'ps3')
    counts={}
    for a in al.assets:counts[a.type_name]=counts.get(a.type_name,0)+1
    tail=0;i=len(doc.zone)
    while i and doc.zone[i-1]==0:i-=1
    tail=len(doc.zone)-i
    checks={'xasset_list':True,'script_strings':True,'asset_type_counts':True,'zone_memory_size':True,'family_roots':True,'relocations':True}
    family=();reloc={'passed':True,'checked':0,'failures':[],'source_pointer_leaks':0}
    if expected_plan is not None:
        val=expected_plan.validate();checks['xasset_list']=len(al.assets)==val['destination_assets'];checks['script_strings']=len(al.script_strings)==len(expected_plan.script_strings)
        exp={}
        for n in expected_plan.top_level_assets():exp[int(n.type)]=exp.get(int(n.type),0)+1
        got={}
        for a in al.assets:got[a.type_id]=got.get(a.type_id,0)+1
        checks['asset_type_counts']=got==exp
    if expected_build is not None:
        checks['zone_memory_size']=hdr.zone_memory_size==len(expected_build.serialized_zone)-ZONE_HEADER_SIZE
        if doc.zone!=expected_build.retail_zone:checks['zone_bytes_exact']=False
        else:checks['zone_bytes_exact']=True
        if expected_plan is not None:
            rows=[]
            top_symbols={node.symbol for node in expected_plan.top_level_assets()}
            for node in expected_plan.serialized_assets():
                k=_result_key(node,expected_build.graph_results)
                if k is None:rows.append({'symbol':node.symbol,'type':node.type.name,'placement':'global' if node.symbol in top_symbols else 'owned-support','passed':False,'errors':['writer result missing/ambiguous'],'proof':{}});continue
                row=_family_check(doc.zone,expected_build.block_sizes,node,expected_build.graph_results[k])
                row['placement']='global' if node.symbol in top_symbols else 'owned-support'
                rows.append(row)
            family=tuple(rows);checks['family_roots']=all(x['passed'] for x in family)
        reloc=_relocation_readback(doc.zone,expected_build.block_sizes,expected_build.relocation_rows);checks['relocations']=reloc['passed']
    passed=all(checks.values())
    return MainReadbackReport(len(al.assets),len(al.script_strings),counts,len(doc.zone),hdr.zone_memory_size,tail,doc.zone_sha256,passed,checks,family,reloc,int(reloc.get('source_pointer_leaks',0)))


def verify_build_roundtrip(result:MainBuildResult,plan:PortPlan)->dict:
    al=parse_xasset_list(result.retail_zone,'ps3');hdr=parse_zone_header(result.retail_zone,'ps3')
    got={}
    for a in al.assets:got[a.type_id]=got.get(a.type_id,0)+1
    exp={}
    for n in plan.top_level_assets():exp[int(n.type)]=exp.get(int(n.type),0)+1
    reloc=_relocation_readback(result.retail_zone,result.block_sizes,result.relocation_rows)
    checks={
      'asset_count':len(al.assets)==len(plan.top_level_assets()),'script_strings':len(al.script_strings)==len(plan.script_strings),
      'asset_type_counts':got==exp,'zone_memory_size':hdr.zone_memory_size==len(result.serialized_zone)-ZONE_HEADER_SIZE,
      'deterministic':result.deterministic,'relocations':reloc['passed'],
    }
    return {'passed':all(checks.values()),'checks':checks,'asset_types':got,'relocations':reloc,'serialized_sha256':result.serialized_sha256,'retail_zone_sha256':result.retail_zone_sha256,'fastfile_sha256':result.fastfile_sha256}
