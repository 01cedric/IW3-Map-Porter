"""Strict, schema-driven IW3 PC load-stream traversal.

Schema definitions and load ordering adapted from OpenAssetTools (GPL-3.0).
This independent command-line reader does not search for asset signatures.
"""
from __future__ import annotations
import ast,bisect,contextlib,json,re,struct,zlib
from functools import lru_cache
from pathlib import Path

class StreamError(ValueError):pass

def align(n,a):return (n+a-1)&-a

class View:
    def __init__(self,reader,typ,pos):self.r=reader;self.typ=typ;self.pos=pos
    def field(self,name):
        meta=self.r.meta(self.typ)
        f=next((f for f in meta.get('fields',[]) if f['name']==name),None)
        if f is None:raise KeyError(name)
        value=self.r.value(f['type'],self.pos+f['offset'])
        if 'bits'in f:value=(value>>f['shift'])&((1<<f['bits'])-1)
        return value
    def __getitem__(self,index):
        typ=self.r.resolve(self.typ)
        if not isinstance(typ,dict) or 'array'not in typ:raise StreamError('Index on non-array')
        if not 0<=index<typ['count']:raise StreamError('Array index outside declaration')
        return self.r.value(typ['array'],self.pos+index*self.r.size(typ['array']))

class Reader:
    def __init__(self,zone,schema=None):
        if len(zone)<44:raise StreamError('Truncated PC zone header: expected 44 bytes')
        self.z=zone;self.s=schema or json.loads(Path(__file__).with_name('schema.json').read_text())
        self.types=self.s['types'];self.rules=self.s['rules'];self.const=self.s['constants']
        self.pos=44;self.block=4;self.cursors=[0]*9;self.limits=list(struct.unpack_from('<9I',zone,8))
        self.ranges=[[]for _ in range(9)];self.starts=[[]for _ in range(9)]
        self.stack=[];self.ptrs={};self.aliases={};self.object_types={};self.owners=[];self.asset_edges=[];self.named_asset_refs=[];self.assets=[];self.objects=[];self.references=0;self.events=[]
        self.type_ids={1:'PhysPreset',2:'XAnimParts',3:'XModel',4:'Material',5:'MaterialTechniqueSet',6:'GfxImage',7:'snd_alias_list_t',8:'SndCurve',9:'LoadedSound',10:'clipMap_t',11:'clipMap_t',12:'ComWorld',13:'GameWorldSp',14:'GameWorldMp',15:'MapEnts',16:'GfxWorld',17:'GfxLightDef',19:'Font_s',20:'MenuList',21:'menuDef_t',22:'LocalizeEntry',23:'WeaponDef',25:'FxEffectDef',26:'FxImpactTable',31:'RawFile',32:'StringTable'}
    @lru_cache(maxsize=None)
    def plain(self,t):
        rt=self.resolve(t)
        if isinstance(rt,dict):return False
        m=self.types[rt]
        if m['kind']=='scalar':return True
        if self.rules.get(rt):return False
        return all(isinstance(f['type'],str) and self.plain(f['type']) for f in m.get('fields',[]))
    def resolve(self,t):
        while isinstance(t,str) and self.types[t]['kind']=='alias':t=self.types[t]['target']
        return t
    def meta(self,t):
        t=self.resolve(t)
        return self.types[t] if isinstance(t,str) else {'kind':'pointer'if'ptr'in t else'array'}
    def size(self,t):
        if isinstance(t,str):return self.types[t]['size']
        return 4 if'ptr'in t else self.size(t['array'])*t['count']
    def alignment(self,t):
        if isinstance(t,str):return self.types[t]['align']
        return 4 if'ptr'in t else self.alignment(t['array'])
    def value(self,t,p):
        rt=self.resolve(t)
        if isinstance(rt,dict):
            if'ptr'in rt:return self.u32(p)
            return View(self,t,p)
        m=self.types[rt]
        if m['kind']!='scalar':return View(self,t,p)
        n=m['size'];self.bounds(p,n)
        if rt in ('float','double'):return struct.unpack_from('<f'if n==4 else'<d',self.z,p)[0]
        return int.from_bytes(self.z[p:p+n],'little',signed=not(rt.startswith('unsigned') or rt=='void'))
    def bounds(self,p,n):
        if p<0 or n<0 or p+n>len(self.z):raise StreamError(f'Serialized read outside zone at 0x{p:X}, size {n}')
    def u32(self,p):self.bounds(p,4);return struct.unpack_from('<I',self.z,p)[0]
    @contextlib.contextmanager
    def push(self,b):
        old=self.block;save=self.cursors[b];self.block=b
        try:yield
        finally:
            self.block=old
            if b==0:self.cursors[b]=save
    def alloc(self,n,a=1,label=''):
        if not isinstance(n,int) or n<0 or n>len(self.z)*4:raise StreamError(f'Invalid allocation size {n}: {label}')
        b=self.block;start=align(self.cursors[b],a);end=start+n
        if end>self.limits[b]:raise StreamError(f'Block {b} overflow at {label}: 0x{end:X} > 0x{self.limits[b]:X}')
        self.cursors[b]=end;p=self.pos
        if b not in (1,2,3):
            self.bounds(p,n);self.pos+=n
            if n:
                self.starts[b].append(start);self.ranges[b].append((start,end,p))
        return p
    def resolve_pointer(self,raw,asset=False):
        b=(raw-1)>>28;o=(raw-1)&0xfffffff
        if b>=9:raise StreamError(f'Invalid packed pointer 0x{raw:08X}')
        self.references+=1
        if (b,o)in self.aliases:return self.aliases[b,o]
        # TEMP addresses may repeat after nested asset scopes; use the latest scope.
        entries=self.ranges[b]
        if b==0:candidates=reversed(entries)
        else:
            i=bisect.bisect_right(self.starts[b],o)-1;candidates=entries[max(0,i):i+1]
        for a,e,p in candidates:
            if a<=o<e:
                target=p+o-a
                if asset:
                    if target not in self.ptrs:raise StreamError(f'Unresolved asset alias 0x{raw:08X} at 0x{target:X}')
                    return self.ptrs[target]
                return target
        raise StreamError(f'Packed pointer 0x{raw:08X} has no preceding allocation')
    def evaluate(self,expression):
        if expression is True:return 1
        if expression in ('never','false'):return 0
        if expression in ('always','true'):return 1
        expression=expression.replace('::','.').replace('&&',' and ').replace('||',' or ')
        expression=re.sub(r'!(?!=)',' not ',expression)
        node=ast.parse(expression.strip(),mode='eval').body
        def ev(n):
            if isinstance(n,ast.Constant):return n.value
            if isinstance(n,ast.Name):
                if n.id in self.const:return self.const[n.id]
                for v in reversed(self.stack):
                    if v.typ==n.id:return v
                    try:return v.field(n.id)
                    except KeyError:pass
                raise StreamError(f'Unknown count/condition name {n.id}')
            if isinstance(n,ast.Attribute):return ev(n.value).field(n.attr)
            if isinstance(n,ast.Subscript):return ev(n.value)[ev(n.slice)]
            if isinstance(n,ast.UnaryOp):
                a=ev(n.operand)
                if isinstance(n.op,ast.Not):return not a
                if isinstance(n.op,ast.USub):return -a
                if isinstance(n.op,ast.Invert):return ~a
                if isinstance(n.op,ast.UAdd):return a
            if isinstance(n,ast.BoolOp):return all(ev(x) for x in n.values)if isinstance(n.op,ast.And)else any(ev(x)for x in n.values)
            if isinstance(n,ast.BinOp):
                a,b=ev(n.left),ev(n.right)
                ops={ast.Add:lambda:a+b,ast.Sub:lambda:a-b,ast.Mult:lambda:a*b,ast.Div:lambda:a//b,ast.FloorDiv:lambda:a//b,ast.Mod:lambda:a%b,ast.BitAnd:lambda:a&b,ast.BitOr:lambda:a|b,ast.LShift:lambda:a<<b,ast.RShift:lambda:a>>b}
                if type(n.op)in ops:return ops[type(n.op)]()
            if isinstance(n,ast.Compare):
                a=ev(n.left)
                for op,right in zip(n.ops,n.comparators):
                    b=ev(right);ops={ast.Eq:a==b,ast.NotEq:a!=b,ast.Lt:a<b,ast.LtE:a<=b,ast.Gt:a>b,ast.GtE:a>=b}
                    if not ops[type(op)]:return False
                    a=b
                return True
            raise StreamError(f'Unsupported count/condition expression: {expression}')
        return ev(node)
    def string(self,p):
        raw=self.u32(p)
        if not raw:self.ptrs[p]=None;return None
        if raw!=0xffffffff:
            q=self.resolve_pointer(raw)
            if not isinstance(q,int):raise StreamError('Invalid string alias target')
        else:
            q=self.pos;end=self.z.find(b'\0',q)
            if end<0:raise StreamError('Unterminated string')
            self.alloc(end-q+1,label='string')
        end=self.z.find(b'\0',q)
        if end<0:raise StreamError('Unterminated shared string')
        value=self.z[q:end].decode('latin-1');self.ptrs[p]=value;return value
    def dynamic_size(self,t,pos):
        rt=self.resolve(t)
        if not isinstance(rt,str):return self.size(t)
        meta=self.types[rt];rules=self.rules.get(rt,{})
        dynamic=[f for f in meta.get('fields',[]) if 'arraysize'in rules.get(f['name'],{})]
        if not dynamic:return self.size(t)
        self.stack.append(View(self,rt,pos))
        try:
            f=dynamic[0];array=self.resolve(f['type']);count=self.evaluate(rules[f['name']]['arraysize'])
            if not isinstance(array,dict)or'array'not in array:raise StreamError('Dynamic field is not an array')
            return f['offset']+self.size(array['array'])*count
        finally:self.stack.pop()
    def pointer(self,t,p,options):
        raw=self.u32(p)
        if not raw:self.ptrs[p]=None;return
        if options.get('string') and not isinstance(self.resolve(t['ptr']),dict):
            value=self.string(p)
            if options.get('assetref') and self.owners:self.named_asset_refs.append({'owner':self.owners[-1],'field':p,'name':value,'type':options['assetref']})
            return
        child=t['ptr'];rt=self.resolve(child);isasset=isinstance(rt,str)and rt in self.s['assets']
        temporary_alias=(isasset and self.rules.get(rt,{}).get('$',{}).get('block')=='XFILE_BLOCK_TEMP') or options.get('block')=='XFILE_BLOCK_TEMP'
        reusable=isasset or options.get('reusable') or temporary_alias
        if reusable and raw not in (0xffffffff,0xfffffffe):
            target=self.resolve_pointer(raw,asset=temporary_alias)
            if isasset and self.object_types.get(target)!=rt:raise StreamError(f'Asset alias type mismatch: expected {rt}, got {self.object_types.get(target)}')
            self.ptrs[p]=target
            if isasset and self.owners:self.asset_edges.append({'owner':self.owners[-1],'field':p,'target':target,'type':rt})
            return
        if raw==0xfffffffe and not temporary_alias:raise StreamError('INSERT pointer outside TEMP object')
        if not reusable and raw not in (0xffffffff,0xfffffffe):
            # Non-reusable members are serialized when non-null, regardless of the stored address.
            pass
        count=self.evaluate(options.get('count','1'))
        if count<0 or count>len(self.z):raise StreamError(f'Invalid pointer count {count}')
        b=self.block
        if 'block'in options:b=self.const[options['block']]
        elif isasset and self.rules.get(rt,{}).get('$',{}).get('block')=='XFILE_BLOCK_TEMP':b=0
        with self.push(b):
            if raw==0xfffffffe:
                with self.push(4):
                    alias=align(self.cursors[4],4);self.cursors[4]=alias+4
                    if alias+4>self.limits[4]:raise StreamError('Alias cell exceeds block')
            else:alias=None
            a=self.evaluate(options['allocalign'])if'allocalign'in options else self.alignment(child)
            if isasset and'allocalign'in self.rules.get(rt,{}).get('$',{}):a=self.evaluate(self.rules[rt]['$']['allocalign'])
            n=self.dynamic_size(child,self.pos) if count==1 else count*self.size(child)
            q=self.alloc(n,a,label=str(rt));self.ptrs[p]=q
            if alias is not None:self.aliases[4,alias]=q
            if b in (1,2,3):return
            if isasset:
                self.object_types[q]=rt
                if self.owners:self.asset_edges.append({'owner':self.owners[-1],'field':p,'target':q,'type':rt})
                self.owners.append(q)
                try:
                    with self.push(4):self.walk(child,q)
                finally:self.owners.pop()
                self.objects.append({'type':rt,'root':q,'end':self.pos,'name':self.asset_name(rt,q)})
            else:
                for i in range(0 if isinstance(child,str) and self.plain(child) else count):self.walk(child,q+i*self.size(child),options={k:v for k,v in options.items()if k=='string'})
    def asset_name(self,t,p):
        fields=self.meta(t).get('fields',[])
        if t in ('Material','menuDef_t'):return self.ptrs.get(p)
        name=next((f for f in fields if f['name']in ('name','aliasName','filename','fontName','szInternalName')),None)
        return self.ptrs.get(p+name['offset'])if name else None
    def walk(self,t,p,options=None,overrides=None):
        options=options or {};overrides=overrides or {}
        if isinstance(t,str) and self.plain(t):return
        rt=self.resolve(t)
        if isinstance(rt,dict):
            if'ptr'in rt:self.pointer(rt,p,options);return
            count=self.evaluate(options['arraysize'])if'arraysize'in options else rt['count']
            if count<0 or count>len(self.z):raise StreamError('Invalid inline array count')
            for i in range(0 if isinstance(rt['array'],str) and self.plain(rt['array']) else count):
                key=f'[{i}]';opts=dict(options);opts.update(overrides.get(key,{}))
                nested={k[len(key):]:v for k,v in overrides.items()if k.startswith(key+'[')}
                self.walk(rt['array'],p+i*self.size(rt['array']),opts,nested)
            return
        meta=self.types[rt]
        if meta['kind']=='scalar':return
        rules={k:dict(v)for k,v in self.rules.get(rt,{}).items()};rules.update(overrides)
        fields=meta.get('fields',[]);order=rules.get('$',{}).get('reorder')
        if order:
            if '...'in order:
                tail=order[order.index('...')+1:];pivot=next((i for i,f in enumerate(fields)if f['name']==tail[0]),len(fields))
                names=[f['name']for f in fields[:pivot]if f['name']not in tail]+tail+[f['name']for f in fields[pivot:]if f['name']not in tail]
            else:names=order+[f['name']for f in fields if f['name']not in order]
            fields=sorted(fields,key=lambda f:names.index(f['name']))
        if meta['kind']=='union':fields=sorted(fields,key=lambda f:'condition'not in rules.get(f['name'],{}))
        self.stack.append(View(self,rt,p))
        try:
            for f in fields:
                name=f['name'];opts=rules.get(name,{})
                if 'condition'in opts and not self.evaluate(opts['condition']):continue
                nested={k[len(name)+2:]:v for k,v in rules.items()if k.startswith(name+'::')}
                nested.update({k[len(name):]:v for k,v in rules.items()if k.startswith(name+'[')})
                try:self.walk(f['type'],p+f['offset'],opts,nested)
                except StreamError as e:raise StreamError(f'{rt}.{name} @0x{p:X}: {e}')from e
                if meta['kind']=='union':break
        except (KeyError,IndexError,struct.error,RecursionError) as e:raise StreamError(f'{rt} at 0x{p:X}: {e}')from e
        finally:self.stack.pop()
    def run(self):
        self.bounds(44,16);sc,sp,ac,ap=struct.unpack_from('<4I',self.z,44);self.pos=60
        if (sc and sp!=0xffffffff)or(ac and ap!=0xffffffff):raise StreamError('XAssetList arrays must be FOLLOWING')
        if sc:
            q=self.alloc(sc*4,4,'script string pointers')
            for i in range(sc):self.string(q+i*4)
        pool=self.alloc(ac*8,4,'XAssetList')
        for i in range(ac):
            typeid=self.u32(pool+i*8);ptr=pool+i*8+4;start=self.pos
            if typeid not in self.type_ids:raise StreamError(f'Unsupported XAsset type {typeid}, asset #{i}, offset 0x{start:X}')
            t=self.type_ids[typeid]
            try:self.pointer({'ptr':t},ptr,{})
            except (StreamError,ValueError,KeyError)as e:raise StreamError(f'Asset #{i} {t}, root 0x{start:X}, cursor 0x{self.pos:X}: {e}')from e
            q=self.ptrs[ptr];self.assets.append({'index':i,'type_id':typeid,'type':t,'root':q,'start':start,'end':self.pos,'name':self.asset_name(t,q)if q is not None else None})
            if i%25==0:print(f'asset {i}/{ac}: {t}, stream 0x{self.pos:X}',flush=True)
        expected_end=44+self.u32(0)
        if self.pos!=expected_end:raise StreamError(f'Asset stream ends at 0x{self.pos:X}, header declares 0x{expected_end:X}')
        if any(self.z[self.pos:]):raise StreamError(f'Nonzero unread bytes after last asset at 0x{self.pos:X}')
        return {'passed':True,'asset_count':ac,'stream_end':self.pos,'zone_bytes':len(self.z),'packed_references':self.references,'block_cursors':self.cursors,'assets':self.assets,'objects':self.objects,'asset_edges':self.asset_edges,'named_asset_refs':self.named_asset_refs,'pointers':self.ptrs,'block_limits':self.limits,'persistent_blocks_exact':self.cursors[1:]==self.limits[1:]}

def main():
    import argparse,traceback
    p=argparse.ArgumentParser();p.add_argument('fastfile',help="path, or '-' to read the bytes from stdin");p.add_argument('--out',required=True);p.add_argument('--zone',action='store_true');args=p.parse_args()
    r=None
    try:
        if args.fastfile=='-':
            import sys as _sys
            raw=_sys.stdin.buffer.read()
        else:
            raw=Path(args.fastfile).read_bytes()
        if not args.zone and raw[:12]!=b'IWffu100\x05\0\0\0':raise StreamError('Expected IW3 PC v5 FastFile')
        r=Reader(raw if args.zone else zlib.decompress(raw[12:]))
        report=r.run();rc=0
    except Exception as e:
        report={'passed':False,'error':str(e),'stream_offset':r.pos if r else None,
                'completed_assets':r.assets if r else [],'block_cursors':r.cursors if r else [],
                'trace':traceback.format_exc()};rc=2;print(report['error'])
    Path(args.out).write_text(json.dumps(report,indent=2)+'\n');return rc
if __name__=='__main__':raise SystemExit(main())
