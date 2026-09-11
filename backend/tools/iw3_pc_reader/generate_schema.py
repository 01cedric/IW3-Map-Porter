"""Regenerate the IW3 layout schema from bundled OpenAssetTools definitions. GPL-3.0.
Requires pycparser (development only).
"""
from pathlib import Path
import re,pickle
from pycparser import c_parser
HERE=Path(__file__).resolve().parent
s=(HERE/'vendor/IW3_Assets.h').read_text()
s=re.sub(r'/\*.*?\*/','',s,flags=re.S);s=re.sub(r'//[^\n]*','',s)
s=re.sub(r'^\s*#.*$','',s,flags=re.M)
s=re.sub(r'\bnamespace IW3\s*\{','',s);s=s[:s.rfind('}')]+s[s.rfind('}')+1:]
s=re.sub(r'static_assert\([^;]*;','',s)
underlying=dict(re.findall(r'enum\s+(\w+)\s*:\s*([^\{]+)',s))
s=re.sub(r'(enum\s+\w+)\s*:\s*[^\{]+',r'\1',s)
s=re.sub(r'(?:type_align(?:32|64)?|tdef_align(?:32|64)?|gcc_align(?:32|64)?)\(\d+\)','',s)
s=s.replace('std::','')
names=list(dict.fromkeys(re.findall(r'\b(struct|union|enum)\s+(\w+)',s)))
pre='typedef unsigned char bool; typedef unsigned char uint8_t; typedef signed char int8_t; typedef unsigned short uint16_t; typedef short int16_t; typedef unsigned int uint32_t; typedef int int32_t; typedef unsigned long long uint64_t; typedef long long int64_t; typedef unsigned long DWORD; typedef int BOOL; typedef unsigned char BYTE; typedef unsigned short WORD; typedef float FLOAT; typedef void IDirect3DVertexDeclaration9; typedef void IDirect3DPixelShader9; typedef void IDirect3DVertexShader9;\n'
pre+='\n'.join(f'typedef {k} {n} {n};' for k,n in names)+'\n'
a=c_parser.CParser().parse(pre+s)

from pathlib import Path
import pickle,re,json
from pycparser import c_ast,c_generator
ast=a;gen=c_generator.CGenerator()
raw=(HERE/'vendor/IW3_Assets.h').read_text()
record_align={n:int(a) for a,n in re.findall(r'(?:struct|union)\s+(?:type_align(?:32)?|gcc_align32)\((\d+)\)\s+(\w+)',raw)}
alias_align={n:int(a) for a,n in re.findall(r'typedef\s+tdef_align32\((\d+)\)[^;]+?\b(\w+)\s*(?:\[[^;]+\])?\s*;',raw)}
const={};records={};aliases={};enums={};layouts={}
def expr(n):
 if isinstance(n,c_ast.Constant):
  v=re.sub(r'[uUlL]+$','',n.value)
  if n.type=='char':return ord(__import__('ast').literal_eval(v))
  return int(v,0) if not (v.startswith('0') and v.isdigit()) else int(v,8)
 if isinstance(n,c_ast.ID):return const[n.name]
 if isinstance(n,c_ast.UnaryOp):
  a=expr(n.expr);return {'-':lambda:-a,'+':lambda:a,'~':lambda:~a}[n.op]()
 if isinstance(n,c_ast.BinaryOp):
  a,b=expr(n.left),expr(n.right);return {'+':lambda:a+b,'-':lambda:a-b,'*':lambda:a*b,'/':lambda:a//b,'<<':lambda:a<<b,'>>':lambda:a>>b,'|':lambda:a|b,'&':lambda:a&b}[n.op]()
 raise ValueError(gen.visit(n))
class Collect(c_ast.NodeVisitor):
 def visit_Enum(self,n):
  if n.values:
   value=-1
   for e in n.values.enumerators:
    value=expr(e.value) if e.value else value+1;const[e.name]=value
  if n.name:enums[n.name]=underlying.get(n.name,'int').strip()
 def visit_Struct(self,n):
  if n.decls is not None:
   if not n.name:n.name='Anonymous_'+str(n.coord.line)+'_'+str(n.coord.column)
   records[n.name]=n
   for d in n.decls:self.visit(d)
 def visit_Union(self,n):self.visit_Struct(n)
 def visit_Typedef(self,n):
  aliases[n.name]=n.type;self.generic_visit(n)
Collect().visit(ast)
def typ(n):
 if isinstance(n,(c_ast.TypeDecl,c_ast.Typename)):return typ(n.type)
 if isinstance(n,c_ast.IdentifierType):return ' '.join(n.names)
 if isinstance(n,c_ast.PtrDecl):return {'ptr':typ(n.type)}
 if isinstance(n,c_ast.ArrayDecl):return {'array':typ(n.type),'count':expr(n.dim) if n.dim else 0}
 if isinstance(n,(c_ast.Struct,c_ast.Union,c_ast.Enum)):return n.name
 raise ValueError(str(n))
primitive={'void':(0,1),'char':(1,1),'signed char':(1,1),'unsigned char':(1,1),'short':(2,2),'short int':(2,2),'unsigned short':(2,2),'int':(4,4),'unsigned int':(4,4),'long':(4,4),'unsigned long':(4,4),'long long':(8,8),'unsigned long long':(8,8),'float':(4,4),'double':(8,8)}
def align(n,a):return (n+a-1)//a*a
def layout(t):
 if isinstance(t,dict):
  if 'ptr'in t:return 4,4
  s,a=layout(t['array']);return s*t['count'],a
 if t in primitive:return primitive[t]
 if t in layouts:return layouts[t]['size'],layouts[t]['align']
 if t in enums:return layout(enums[t])
 if t in records:
  n=records[t];union=isinstance(n,c_ast.Union);off=0;ma=record_align.get(t,1);fields=[];used=0;unit=0;lastsize=0
  for d in n.decls:
   dt=typ(d.type);s,a=layout(dt);ma=max(ma,a)
   bits=expr(d.bitsize) if d.bitsize else None
   if bits is not None:
    if union:pos=0;shift=0;off=max(off,s)
    else:
     if used==0 or s!=lastsize or used+bits>s*8:
      unit=align(off,a);off=unit+s;used=0;lastsize=s
     pos=unit;shift=used;used+=bits
    fields.append({'name':d.name,'offset':pos,'type':dt,'bits':bits,'shift':shift});continue
   used=0
   if union:pos=0;off=max(off,s)
   else:pos=align(off,a);off=pos+s
   fields.append({'name':d.name,'offset':pos,'type':dt})
  layouts[t]={'kind':'union' if union else 'struct','size':align(off,ma),'align':ma,'fields':fields}
  return layouts[t]['size'],ma
 if t in aliases:
  dest=typ(aliases[t])
  if dest==t:raise ValueError('Undefined '+t)
  s,a=layout(dest);return s,max(a,alias_align.get(t,1))
 raise ValueError('Unknown '+str(t))
for t in records:layout(t)
for t,(s,a) in primitive.items():layouts[t]={'kind':'scalar','size':s,'align':a}
for t,d in aliases.items():
 if t not in layouts and t not in enums:
  s,a=layout(t);layouts[t]={'kind':'alias','target':typ(d),'size':s,'align':a}
for t in enums:
 s,a=layout(t);layouts[t]={'kind':'alias','target':enums[t],'size':s,'align':a}
rules={};assets={};current=None
for path in sorted((HERE/'vendor/XAssets').glob('*.txt')):
 s=re.sub(r'//[^\n]*','',path.read_text());s=re.sub(r'/\*.*?\*/','',s,flags=re.S)
 for stmt in s.split(';'):
  stmt=' '.join(stmt.split())
  if not stmt:continue
  if stmt.startswith('use '):current=stmt[4:];continue
  if stmt.startswith('reorder '):
   owner,order=stmt[8:].split(':',1);rules.setdefault(owner.strip(),{}).setdefault('$',{})['reorder']=order.split();continue
  if stmt.startswith('reorder:'):
   rules.setdefault(current,{}).setdefault('$',{})['reorder']=stmt[8:].split();continue
  if not stmt.startswith('set '):raise ValueError(stmt)
  _,op,rest=stmt.split(' ',2)
  if op=='action':rules.setdefault(current,{}).setdefault('$',{})['action']=rest;continue
  target,_,value=rest.partition(' ')
  if target.startswith('XFILE_BLOCK_'):owner=current;target='$';value=const[target] if target in const else rest
  elif target in records:owner=target;target='$'
  elif '::' in target and target.split('::')[0] in records:owner,target=target.split('::',1)
  else:owner=current
  rules.setdefault(owner,{}).setdefault(target,{})[op]=value if value else True
for match in re.finditer(r'asset (\w+) (\w+);',(HERE/'vendor/IW3_Commands.txt').read_text()):assets[match[1]]=match[2]
result={'types':layouts,'constants':const,'rules':rules,'assets':assets}
(HERE/'schema.json').write_text(json.dumps(result,indent=2))
print('Records',len(records),'types',len(layouts),'rules',len(rules),'assets',len(assets))
