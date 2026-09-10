from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import gc,hashlib,struct,tempfile
from typing import Sequence,Mapping,Any

from .graph import AssetNode,AssetType,OwnershipEdge,write_graph
from .zone_writer import SerializationOptions,ZONE_HEADER_SIZE
from .backend.v4_ffio import build_ps3_fastfile,read_ps3_fastfile,parse_xasset_list,parse_zone_header,require_retail_ps3_adler32,PS3_RAW_BLOCK

# Canonical FIX114 family order. Symbol ordering inside a family is deterministic.
_PRIORITY={
    AssetType.TECHSET:10,AssetType.IMAGE:20,AssetType.MATERIAL:30,AssetType.LIGHTDEF:40,
    AssetType.PHYSPRESET:50,AssetType.XMODEL:60,
    AssetType.COMWORLD:70,AssetType.GFXWORLD:71,AssetType.GAMEWORLD_MP:72,AssetType.CLIPMAP_MP:73,
    AssetType.RAWFILE:80,AssetType.STRINGTABLE:81,
    AssetType.FX:90,AssetType.IMPACTFX:91,AssetType.SOUND:100,AssetType.SNDCURVE:101,AssetType.LOADED_SOUND:102,
}

@dataclass(frozen=True)
class MainBuildResult:
    serialized_zone:bytes
    retail_zone:bytes
    fastfile:bytes
    asset_count:int
    script_string_count:int
    relocation_count:int
    block_sizes:tuple[int,...]
    deterministic:bool
    serialized_sha256:str
    retail_zone_sha256:str
    fastfile_sha256:str
    asset_counts:dict[str,int]
    relocation_rows:tuple[Mapping[str,Any],...]
    graph_results:Mapping[str,Any]


def sha(b:bytes):return hashlib.sha256(b).hexdigest().upper()

def canonical_assets(assets:Sequence[AssetNode])->tuple[AssetNode,...]:
    unknown=[a for a in assets if a.type not in _PRIORITY]
    if unknown:raise ValueError('canonical main writer has no family-order proof for: '+', '.join(f'{a.type.name}:{a.symbol}' for a in unknown))
    return tuple(sorted(assets,key=lambda a:(_PRIORITY[a.type],a.symbol)))

def prepare_retail_main_zone(serialized:bytes)->bytes:
    if len(serialized)<ZONE_HEADER_SIZE:raise ValueError('serialized PS3 zone shorter than 36-byte header')
    capacity=(len(serialized)+PS3_RAW_BLOCK-1)&~(PS3_RAW_BLOCK-1)
    out=bytearray(capacity);out[:len(serialized)]=serialized
    # Retail main semantics: word0 is source content bytes excluding zone header, not block high-water.
    struct.pack_into('>I',out,0,len(serialized)-ZONE_HEADER_SIZE)
    return bytes(out)

def build_main_fastfile(
    assets:Sequence[AssetNode],
    script_strings:Sequence[str|None]=(),
    options:SerializationOptions|None=None,
    *,
    preserve_order:bool=False,
    top_level_assets:Sequence[AssetNode]|None=None,
    ownership_edges:Sequence[OwnershipEdge]=(),
)->MainBuildResult:
    """Serialize an owner-aware graph while exposing only global roots in XAssetList.

    ``assets`` is the complete active symbol registry.  ``top_level_assets`` is the much smaller
    global XAssetList.  Support nodes are emitted recursively by their owning pointer sites and
    therefore must never be silently promoted to global XAssets.
    """
    # A generated MAIN zone raises blocks 0 and 2 to the measured Retail floor; see
    # zone_writer.declared_block_size_with_floor for the mp_shipment ground-truth
    # comparison this comes from.  The load zone builds its own options and is untouched.
    if options is None:
        options=SerializationOptions(apply_retail_block_floors=True)
    registry=tuple(assets)
    requested_top=tuple(top_level_assets) if top_level_assets is not None else registry
    ordered_top=requested_top if preserve_order else canonical_assets(requested_top)
    # A visual owner graph can carry hundreds of MiB of converted image payloads.  Retaining two
    # complete in-memory ZoneWriteResult objects just to prove determinism doubled the peak and
    # made a correct graph fail through host OOM.  Spool the first byte stream to an anonymous
    # temporary file, release it, then compare the second write byte-for-byte in bounded chunks.
    with tempfile.TemporaryFile() as first_stream:
        first=write_graph(
            registry,script_strings,options,
            top_level_assets=ordered_top,ownership_edges=ownership_edges,
        )
        first_blocks=tuple(first.zone.block_sizes)
        first_length=len(first.zone.zone_bytes)
        first_hash=sha(first.zone.zone_bytes)
        first_stream.write(first.zone.zone_bytes)
        del first
        gc.collect()

        second=write_graph(
            registry,script_strings,options,
            top_level_assets=ordered_top,ownership_edges=ownership_edges,
        )
        deterministic=(
            tuple(second.zone.block_sizes)==first_blocks
            and len(second.zone.zone_bytes)==first_length
            and sha(second.zone.zone_bytes)==first_hash
        )
        if deterministic:
            first_stream.seek(0)
            view=memoryview(second.zone.zone_bytes)
            for start in range(0,len(view),8*1024*1024):
                chunk=first_stream.read(min(8*1024*1024,len(view)-start))
                if chunk!=view[start:start+len(chunk)]:
                    deterministic=False;break
            del view
        if not deterministic:raise ValueError('main graph second write is not deterministic')

    serialized=second.zone.zone_bytes;retail=prepare_retail_main_zone(serialized);ff=build_ps3_fastfile(retail)
    al=parse_xasset_list(retail,'ps3');hdr=parse_zone_header(retail,'ps3')
    if len(al.assets)!=len(ordered_top) or len(al.script_strings)!=len(script_strings):raise ValueError('retail main readback XAssetList mismatch')
    if hdr.zone_memory_size!=len(serialized)-ZONE_HEADER_SIZE:raise ValueError('retail main word0 does not match serialized-content length')
    if len(retail)%PS3_RAW_BLOCK:raise ValueError('retail main decompressed source is not 64-KiB padded')
    counts={}
    for a in al.assets:counts[a.type_name]=counts.get(a.type_name,0)+1
    reloc_rows=tuple({'physical_field_offset':r.physical_field_offset,'target_symbol':r.target_symbol,'target_block':r.target.block,'target_offset':r.target.offset,'serialized_pointer':r.serialized_pointer,'kind':r.kind,'description':r.description} for r in second.zone.relocations)
    return MainBuildResult(serialized,retail,ff,len(ordered_top),len(script_strings),len(second.zone.relocations),second.zone.block_sizes,True,sha(serialized),sha(retail),sha(ff),counts,reloc_rows,dict(second.context.results))

def save_main_fastfile(path:str|Path,result:MainBuildResult,verify=True)->None:
    path=Path(path);path.write_bytes(result.fastfile)
    if verify:
        doc=read_ps3_fastfile(path)
        require_retail_ps3_adler32(doc)
        if doc.zone!=result.retail_zone:raise ValueError('saved main FastFile decompressed readback differs from writer output')
        al=parse_xasset_list(doc.zone,'ps3')
        if len(al.assets)!=result.asset_count:raise ValueError('saved main FastFile asset count mismatch')
