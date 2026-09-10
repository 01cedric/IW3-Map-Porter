"""Strict, serializable provenance ledger for converted assets.

A production resolution is valid only when it is backed by an exact source
pointer/alias replay, deterministic field conversion, or a byte-exact retail
signature. Name, nearest-address, sort-key-family, neutral/default and
"best-effort" resolutions are rejected in strict mode.
"""
from __future__ import annotations
from dataclasses import dataclass,field,asdict
from enum import Enum
from typing import Any,Iterable,Mapping,MutableMapping,Sequence
import hashlib,json

class ProvenanceError(ValueError): pass
class EvidenceKind(str,Enum):
 SOURCE_POINTER='source-pointer'
 INSERT_ALIAS='insert-alias'
 FOLLOWING_OWNERSHIP='following-ownership'
 DETERMINISTIC_CONVERSION='deterministic-conversion'
 RETAIL_SIGNATURE='retail-signature'
 INDEPENDENT_READBACK='independent-readback'
 HEURISTIC='heuristic'
 FALLBACK='fallback'
 DEFAULT='default'
 NEUTRAL='neutral'

STRICT_ALLOWED=frozenset({EvidenceKind.SOURCE_POINTER,EvidenceKind.INSERT_ALIAS,EvidenceKind.FOLLOWING_OWNERSHIP,EvidenceKind.DETERMINISTIC_CONVERSION,EvidenceKind.RETAIL_SIGNATURE,EvidenceKind.INDEPENDENT_READBACK})

@dataclass(frozen=True)
class Evidence:
 kind:EvidenceKind
 source:str
 detail:str
 digest:str|None=None
 def validate(self,strict:bool=True)->None:
  if not self.source or not self.detail: raise ProvenanceError('evidence source/detail must be non-empty')
  if strict and self.kind not in STRICT_ALLOWED: raise ProvenanceError(f'non-production evidence kind: {self.kind.value}')
  if self.digest is not None and (len(self.digest)!=64 or any(c not in '0123456789abcdefABCDEF' for c in self.digest)): raise ProvenanceError(f'invalid SHA-256 digest: {self.digest!r}')

@dataclass
class Resolution:
 asset_type:str
 asset_key:str
 owner_key:str|None
 output_identity:str
 evidence:list[Evidence]=field(default_factory=list)
 source_address:str|None=None
 output_address:str|None=None
 metadata:dict[str,Any]=field(default_factory=dict)
 def validate(self,strict:bool=True)->None:
  if not self.asset_type or not self.asset_key or not self.output_identity: raise ProvenanceError('resolution identity fields must be non-empty')
  if not self.evidence: raise ProvenanceError(f'{self.asset_type}:{self.asset_key} has no evidence')
  for e in self.evidence:e.validate(strict)
  if strict and not any(e.kind in STRICT_ALLOWED for e in self.evidence): raise ProvenanceError(f'{self.asset_type}:{self.asset_key} lacks production evidence')

@dataclass
class Ledger:
 strict:bool=True
 resolutions:MutableMapping[tuple[str,str],Resolution]=field(default_factory=dict)
 def add(self,r:Resolution)->None:
  r.validate(self.strict); k=(r.asset_type,r.asset_key)
  old=self.resolutions.get(k)
  if old is not None:
   if old.output_identity!=r.output_identity or old.owner_key!=r.owner_key: raise ProvenanceError(f'conflicting resolution for {k}: {old.output_identity!r}/{r.output_identity!r}')
   # merge only unique exact evidence
   seen={(x.kind,x.source,x.detail,x.digest) for x in old.evidence}
   old.evidence.extend(x for x in r.evidence if (x.kind,x.source,x.detail,x.digest) not in seen)
   return
  self.resolutions[k]=r
 def require(self,asset_type:str,asset_key:str)->Resolution:
  try:r=self.resolutions[(asset_type,asset_key)]
  except KeyError:raise ProvenanceError(f'missing provenance: {asset_type}:{asset_key}')
  r.validate(self.strict); return r
 def validate_complete(self,expected:Iterable[tuple[str,str]])->None:
  missing=sorted(set(expected)-set(self.resolutions))
  extra=sorted(set(self.resolutions)-set(expected))
  if missing: raise ProvenanceError(f'missing provenance entries ({len(missing)}): {missing[:20]}')
  if extra: raise ProvenanceError(f'unexpected provenance entries ({len(extra)}): {extra[:20]}')
  for r in self.resolutions.values():r.validate(self.strict)
 def report(self)->dict[str,Any]:
  vals=sorted(self.resolutions.values(),key=lambda x:(x.asset_type,x.asset_key))
  by_kind={k.value:0 for k in EvidenceKind}
  for r in vals:
   for e in r.evidence:by_kind[e.kind.value]+=1
  return {'strict':self.strict,'resolution_count':len(vals),'evidence_by_kind':by_kind,'invalid_production_evidence':sum(by_kind[k.value] for k in (EvidenceKind.HEURISTIC,EvidenceKind.FALLBACK,EvidenceKind.DEFAULT,EvidenceKind.NEUTRAL)),'resolutions':[asdict(x) for x in vals]}
 def write(self,path:str)->None:
  data=self.report(); raw=json.dumps(data,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode(); data['canonical_sha256']=hashlib.sha256(raw).hexdigest().upper()
  from pathlib import Path
  Path(path).write_text(json.dumps(data,indent=2,ensure_ascii=False),encoding='utf-8')
