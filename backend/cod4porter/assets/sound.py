from __future__ import annotations
from dataclasses import dataclass,field
import hashlib,struct
from ..graph import AssetType,GraphContext,nested_symbol
from ..zone_writer import RelocatingZoneWriter,PackedOffset,TEMP_BLOCK,VIRTUAL_BLOCK,FOLLOWING,INSERT,NULL
from ..sound import SoundFileType,SoundListPlan,SoundFilePlan,SoundCurve,SpeakerMap

LIST=0x0c;ALIAS=0x5c;SOUNDFILE=0x10;LOADED=0x1c;CURVE=0x48;SPEAKER=0x198

def u32(b,o,v):struct.pack_into('>I',b,o,int(v)&0xffffffff)
def f32(b,o,v):struct.pack_into('>f',b,o,float(v))

def curve_id(c):return f'pc-root-{c.root_offset:08X}'
def loaded_id(s:SoundFilePlan):
    if s.source.loaded_sound:return f'pc-root-{s.source.loaded_sound.root_offset:08X}'
    n=(s.staged_asset_name or s.loaded_name or 'unnamed').strip().lower();return 'staged-'+n+'-'+hashlib.sha256(s.payload).hexdigest().upper()

@dataclass
class SoundNode:
    plan:SoundListPlan;symbol:str;allow_null_source_soundfiles:bool=False
    type:AssetType=field(init=False,default=AssetType.SOUND);root_block:int=field(init=False,default=TEMP_BLOCK);dependencies:tuple=field(init=False,default=())
    def __post_init__(self):
        if not self.plan.owned_by_map:raise ValueError('only map-owned sound lists are serialized')
        if len(self.plan.aliases)!=len(self.plan.source.aliases):raise ValueError('sound plan cardinality mismatch')
        for a in self.plan.aliases:
            if a.source.sound_file is not None and a.sound_file is None and not self.allow_null_source_soundfiles:raise ValueError('owned list contains unresolved SoundFile')
    def _curve_alias(self,i):return nested_symbol(self.symbol,'sndcurve.root.insert.'+i)
    def _loaded_alias(self,i):return nested_symbol(self.symbol,'loadedsound.root.insert.'+i)
    def _data_alias(self,i):return nested_symbol(self.symbol,'loadedsound.data.insert.'+i)
    def write(self,w:RelocatingZoneWriter,c:GraphContext):
        if w.current_block_index!=TEMP_BLOCK:raise ValueError('Sound root must be TEMP')
        # Load_SoundAliasListArray aligns the TEMP allocation even though the FastFile
        # stream itself stays physically contiguous.  Defining the symbol before this
        # logical-only alignment would make a packed reuse address the wrong PS3 slot.
        w.align_logical_only(4)
        src=self.plan.source;rp=w.physical_position;root=w.define_symbol(self.symbol);c.register_root(self.symbol,rp)
        lr=bytearray(LIST);u32(lr,0,FOLLOWING);u32(lr,4,FOLLOWING if src.aliases else NULL);u32(lr,8,len(src.aliases));w.write_in_block(lr,'Sound AliasList root')
        w.push_block(VIRTUAL_BLOCK);member_block_start=w.current_location;w.write_latin1z(src.name,'Sound list name')
        loaded_count=curve_count=speaker_count=0;loaded_bytes=0;emitted_loaded=set();emitted_curve=set();loaded_rows=[]
        if self.plan.aliases:
            w.align_logical_only(4);heads_phys=w.physical_position;heads=self._heads();w.write_in_block(heads,'Sound alias heads')
            first_curve={}
            for idx,a in enumerate(self.plan.aliases):
                cv=a.source.volume_curve
                if not cv:continue
                ident=curve_id(cv)
                if ident in first_curve:w.register_pointer_relocation(heads_phys+idx*ALIAS+72,self._curve_alias(ident),description='reused SndCurve persistent INSERT alias')
                else:first_curve[ident]=idx
            for ai,ap in enumerate(self.plan.aliases):
                a=ap.source
                for s,desc in ((a.alias_name,'aliasName'),(a.subtitle,'subtitle'),(a.secondary_alias_name,'secondaryAlias'),(a.chain_alias_name,'chainAlias')):
                    if s is not None:w.write_latin1z(s,'SoundAlias '+desc)
                if ap.sound_file:
                    sf=ap.sound_file;w.align_logical_only(4);sfp=w.physical_position;sf_location=w.current_location;w.write_in_block(self._soundfile(sf),'SoundFile root')
                    if sf.output_type in (SoundFileType.STREAMED,SoundFileType.PRIMED):
                        if sf.source.stream_directory is not None:w.write_latin1z(sf.source.stream_directory,'StreamedSound.dir')
                        if sf.source.stream_name is not None:w.write_latin1z(sf.source.stream_name,'StreamedSound.name')
                    if sf.output_type==SoundFileType.LOADED:
                        # SoundFile is a normal B4 member, but LoadedSound is a nested XAsset:
                        # root TEMP/B0, name B4, audio data TEMP/B0.  Reuse goes through the
                        # persistent INSERT/asset-pointer alias established by the loader.
                        loaded_count+=1;loaded_bytes+=len(sf.payload);ident=loaded_id(sf);alias=self._loaded_alias(ident)
                        if ident in emitted_loaded:
                            w.register_pointer_relocation(sfp+4,alias,description='reused LoadedSound B4 pointer-field alias')
                        else:
                            emitted_loaded.add(ident)
                            # LoadedSound is itself an XAsset.  Its inline root is allocated in
                            # TEMP even though the containing SoundFile lives in default-normal
                            # B4; Load_LoadedSound then returns to B4 for the owned name.
                            # FOLLOWING asset roots are reused through the owning B4
                            # pointer field, never through their reusable TEMP address.
                            loaded_symbol=w.define_symbol_at(alias,PackedOffset(sf_location.block,sf_location.offset+4))
                            # Keep the child TEMP frame alive until all LoadedSound members are
                            # complete.  The nested audio-data TEMP push must inherit the cursor
                            # immediately after the 0x1c-byte child root, not the parent SoundList
                            # cursor at 0x0c.
                            w.push_block(TEMP_BLOCK);w.align_logical_only(4);loaded_phys=w.physical_position
                            loaded_location=w.write_in_block(self._loaded(sf),'LoadedSound TEMP asset root')
                            loaded_name_location=None
                            if sf.loaded_name is not None:
                                w.push_block(VIRTUAL_BLOCK)
                                loaded_name_location=w.write_latin1z(sf.loaded_name,'LoadedSound name')
                                w.pop_block()
                            payload_location=None;payload_physical=None
                            if sf.payload:
                                w.push_block(TEMP_BLOCK)
                                payload_location=w.current_location;payload_physical=w.physical_position
                                w.write_in_block(sf.payload,'LoadedSound ID3 payload')
                                w.pop_block()
                            w.pop_block()
                            loaded_rows.append({
                                'identity':ident,'symbol':alias,'symbol_location':loaded_symbol,
                                'root_location':loaded_location,'root_physical':loaded_phys,
                                'name_location':loaded_name_location,'payload_location':payload_location,
                                'payload_physical':payload_physical,'payload_bytes':len(sf.payload),
                            })
                if a.volume_curve:
                    curve_count+=1;cv=a.volume_curve;ident=curve_id(cv)
                    if ident not in emitted_curve:
                        # SndCurve is another nested TEMP XAsset.  INSERT retains its persistent
                        # B4 alias cell, while the root itself consumes TEMP and its filename B4.
                        emitted_curve.add(ident);w.define_insert_pointer_alias(self._curve_alias(ident),'sound:sndcurve-root:'+ident)
                        w.push_block(TEMP_BLOCK);w.align_logical_only(4);w.write_in_block(self._curve(cv),'SndCurve TEMP asset root');w.pop_block()
                        if cv.name is not None:w.write_latin1z(cv.name,'SndCurve filename')
                if a.speaker_map:
                    speaker_count+=1;w.align_logical_only(4);w.write_in_block(self._speaker(a.speaker_map),'SpeakerMap root')
                    if a.speaker_map.name is not None:w.write_latin1z(a.speaker_map.name,'SpeakerMap name')
        member_block_end=w.current_location;w.pop_block();c.register_result('sound:'+self.symbol,{'root_physical':rp,'root_location':root,'member_block_start':member_block_start,'member_block_end':member_block_end,'source_asset_index':src.source_asset_index,'aliases':len(src.aliases),'loaded_payloads':loaded_count,'unique_loaded_payloads':len(emitted_loaded),'loaded_bytes':loaded_bytes,'loaded_sound_rows':tuple(loaded_rows),'curves':curve_count,'speaker_maps':speaker_count})
    def _heads(self):
        out=bytearray(len(self.plan.aliases)*ALIAS)
        for i,ap in enumerate(self.plan.aliases):
            a=ap.source
            if len(a.raw_words)!=23:raise ValueError('SoundAlias must contain 23 words')
            r=i*ALIAS
            for j,x in enumerate(a.raw_words):u32(out,r+j*4,x)
            u32(out,r+0,FOLLOWING if a.alias_name is not None else NULL);u32(out,r+4,FOLLOWING if a.subtitle is not None else NULL);u32(out,r+8,FOLLOWING if a.secondary_alias_name is not None else NULL);u32(out,r+12,FOLLOWING if a.chain_alias_name is not None else NULL);u32(out,r+16,FOLLOWING if ap.sound_file else NULL);u32(out,r+72,INSERT if a.volume_curve else NULL);u32(out,r+88,FOLLOWING if a.speaker_map else NULL)
        return bytes(out)
    def _soundfile(self,s):
        d=bytearray(SOUNDFILE);d[0]=int(s.output_type);d[1]=1 if s.source.exists else 0
        if s.output_type==SoundFileType.LOADED:u32(d,4,FOLLOWING)
        elif s.output_type in (SoundFileType.STREAMED,SoundFileType.PRIMED):
            u32(d,4,FOLLOWING if s.source.stream_directory is not None else NULL)
            u32(d,8,FOLLOWING if s.source.stream_name is not None else NULL)
        return bytes(d)
    def _loaded(self,s):
        d=bytearray(LOADED);u32(d,0,FOLLOWING if s.loaded_name is not None else NULL);u32(d,4,len(s.payload));u32(d,8,s.format);u32(d,12,s.duration_ms if getattr(s,'duration_ms',0)>0 else int(round((s.sample_count*1000.0)/s.sample_rate)));u32(d,16,s.channels);u32(d,20,s.sample_rate);u32(d,24,FOLLOWING if s.payload else NULL);return bytes(d)
    def _curve(self,c):
        if len(c.knots)!=16 or c.knot_count>8:raise ValueError('invalid SndCurve')
        d=bytearray(CURVE);u32(d,0,FOLLOWING if c.name is not None else NULL);u32(d,4,c.knot_count)
        for i,x in enumerate(c.knots):f32(d,8+i*4,x)
        return bytes(d)
    def _speaker(self,s):
        if len(s.channel_map_words)!=(SPEAKER-8)//4:raise ValueError('SpeakerMap word count')
        d=bytearray(SPEAKER);d[0]=1 if s.is_default else 0;u32(d,4,FOLLOWING if s.name is not None else NULL)
        for i,x in enumerate(s.channel_map_words):u32(d,8+i*4,x)
        return bytes(d)

@dataclass
class SoundCurveAssetNode:
    curve:SoundCurve;symbol:str
    type:AssetType=field(init=False,default=AssetType.SNDCURVE);root_block:int=field(init=False,default=TEMP_BLOCK);dependencies:tuple=field(init=False,default=())
    def write(self,w:RelocatingZoneWriter,c:GraphContext):
        rp=w.physical_position;rl=w.define_symbol(self.symbol);c.register_root(self.symbol,rp)
        if len(self.curve.knots)!=16 or self.curve.knot_count>8:raise ValueError('invalid standalone SndCurve')
        d=bytearray(CURVE);u32(d,0,FOLLOWING if self.curve.name is not None else NULL);u32(d,4,self.curve.knot_count)
        for i,x in enumerate(self.curve.knots):f32(d,8+i*4,x)
        w.write_in_block(d,'standalone SndCurve root')
        if self.curve.name is not None:
            w.push_block(VIRTUAL_BLOCK);w.write_latin1z(self.curve.name,'standalone SndCurve name');w.pop_block()
        c.register_result('sndcurve:'+self.symbol,{'root_physical':rp,'root_location':rl,'source_asset_index':self.curve.source_asset_index,'name':self.curve.name})

@dataclass(frozen=True)
class StandaloneLoadedPlan:
    source:object
    name:str|None
    payload:bytes
    format:int
    sample_count:int
    channels:int
    sample_rate:int
    duration_ms:int=0

@dataclass
class LoadedSoundAssetNode:
    plan:StandaloneLoadedPlan;symbol:str
    type:AssetType=field(init=False,default=AssetType.LOADED_SOUND);root_block:int=field(init=False,default=TEMP_BLOCK);dependencies:tuple=field(init=False,default=())
    def write(self,w:RelocatingZoneWriter,c:GraphContext):
        rp=w.physical_position;rl=w.define_symbol(self.symbol);c.register_root(self.symbol,rp)
        d=bytearray(LOADED);u32(d,0,FOLLOWING if self.plan.name is not None else NULL);u32(d,4,len(self.plan.payload));u32(d,8,self.plan.format);u32(d,12,self.plan.duration_ms if self.plan.duration_ms>0 else int(round((self.plan.sample_count*1000.0)/self.plan.sample_rate)));u32(d,16,self.plan.channels);u32(d,20,self.plan.sample_rate);u32(d,24,INSERT if self.plan.payload else NULL)
        w.write_in_block(d,'standalone LoadedSound root')
        if self.plan.name is not None:
            w.push_block(VIRTUAL_BLOCK);w.write_latin1z(self.plan.name,'standalone LoadedSound name');w.pop_block()
        if self.plan.payload:
            w.push_block(TEMP_BLOCK);w.define_insert_pointer_alias(nested_symbol(self.symbol,'data.insert'),'loadedsound:standalone-data');w.write_in_block(self.plan.payload,'standalone LoadedSound ID3 payload');w.pop_block()
        c.register_result('loadedsound:'+self.symbol,{'root_physical':rp,'root_location':rl,'source_asset_index':getattr(self.plan.source,'source_asset_index',-1),'name':self.plan.name,'payload_bytes':len(self.plan.payload)})
