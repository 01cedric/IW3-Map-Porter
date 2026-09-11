"""Text GSC edits for omitted effects; comments and string contents are not code."""
from dataclasses import replace
import re
from .fx_omission import norm

TOKEN=re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"')
LOAD=re.compile(r'\bloadfx\s*\(',re.I)
PLAY=re.compile(r'\b(playfx|playfxontag|playloopedfx)\s*\(',re.I)
LITERAL=re.compile(r'\s*"((?:\\.|[^"\\])*)"\s*\Z')

def mask(text,strings=True):
    return TOKEN.sub(lambda m:(''.join('\n' if c=='\n' else ' ' for c in m[0]) if strings or not m[0].startswith('"') else m[0]),text)

def call_end(code,start):
    depth=0
    for i in range(start,len(code)):
        if code[i]=='(':depth+=1
        elif code[i]==')':
            depth-=1
            if depth==0:return i+1
    raise ValueError('Unbalanced GSC function call during FX omission')

def apply(text,edits):
    last=len(text)
    for start,end,value in sorted(edits,reverse=True):
        if end>last:raise ValueError('Overlapping FX script edits')
        text=text[:start]+value+text[end:];last=start
    return text

def rewrite_scripts(rawfiles,omitted_names):
    removed={norm(n) for n in omitted_names};sources={r.name:r.payload.decode('latin1') for r in rawfiles if r.name.lower().endswith('.gsc')}
    aliases=set();loads={};changes=[]
    for name,text in sources.items():
        code=mask(text);comments=mask(text,False);edits=[]
        for m in LOAD.finditer(code):
            opening=code.index('(',m.start());end=call_end(code,opening)
            literal=LITERAL.fullmatch(comments[opening+1:end-1])
            if literal is None:
                raise ValueError(f"Cannot omit FX safely: dynamic loadfx argument in {name}")
            if norm(literal[1]) not in removed:continue
            prefix=comments[:m.start()]
            registration=re.search(r'level\s*\.\s*_effect\s*\[\s*"([^"\n]+)"\s*\]\s*=\s*$',prefix,re.I)
            if registration:
                aliases.add(norm(registration[1]))
                tail=re.match(r'\s*;',comments[end:])
                if tail is None:raise ValueError(f'Complex effect registration in {name}')
                edits.append((registration.start(),end+tail.end(),''))
            else:edits.append((m.start(),end,'undefined'))
            changes.append({'script':name,'operation':'remove_loadfx','effect':literal[1],'line':text.count('\n',0,m.start())+1})
        loads[name]=edits
    output=[]
    for raw in rawfiles:
        if raw.name not in sources:output.append(raw);continue
        text=apply(sources[raw.name],loads[raw.name]);comments=mask(text,False);code=mask(text);edits=[]
        # Generated createfx entries are complete assignment blocks. Removing
        # their creation and properties prevents stock _utility from looking up
        # the removed level._effect key.
        pattern=re.compile(r'\b(\w+)\s*=\s*maps[\\/]mp[\\/]_utility\s*::\s*createOneshotEffect\s*\(\s*"([^"\n]+)"\s*\)\s*;',re.I)
        for m in pattern.finditer(comments):
            if code[m.start()].isspace() or norm(m[2]) not in aliases|removed:continue
            cursor=m.end();properties=0
            prop=re.compile(r'\s*'+re.escape(m[1])+r'\s*\.\s*v\s*\[\s*"[^"\n]+"\s*\]\s*=\s*[^;{}]*;',re.I)
            while True:
                p=prop.match(comments,cursor)
                if p is None:break
                cursor=p.end();properties+=1
            if not properties:raise ValueError(f'Unrecognized omitted createfx block in {raw.name}')
            edits.append((m.start(),cursor,''));changes.append({'script':raw.name,'operation':'remove_createfx_block','effect_key':m[2]})
        text=apply(text,edits);code=mask(text);edits=[];helpers={}
        for m in PLAY.finditer(code):
            if code[max(0,m.start()-2):m.start()]=='::':continue
            opening=code.index('(',m.start());end=call_end(code,opening)
            args=code[opening+1:end-1];depth=0;count=1 if args.strip() else 0
            for c in args:
                if c in '([{':depth+=1
                elif c in ')]}':depth-=1
                elif c==',' and depth==0:count+=1
            if not 1<=count<=8:raise ValueError(f'Unsupported FX call argument list in {raw.name}')
            previous=re.search(r'(\w+)\s*$',code[:m.start()])
            method=bool(previous and previous[1].lower() not in ('return','thread','else'))
            if method:raise ValueError(f'Method-form {m[1]} needs explicit omission support in {raw.name}')
            builtin=m[1].lower();helper=f'iw3porter_guard_{builtin}_{count}'
            if re.search(r'\b'+helper+r'\b',code):raise ValueError(f'FX helper name collision in {raw.name}')
            helpers[helper]=(builtin,count);edits.append((m.start(),m.start()+len(m[1]),helper))
        text=apply(text,edits)
        for helper,(builtin,count) in sorted(helpers.items()):
            params=', '.join('a'+str(i) for i in range(count))
            text+=f'\n{helper}({params})\n{{\n    if (!isdefined(a0)) return;\n    {builtin}({params});\n}}\n'
        if helpers:changes.append({'script':raw.name,'operation':'guard_fx_playback','calls':len(edits)})
        # Unknown remaining literal consumers could call stock scripts with a
        # removed effect. Stop instead of emitting a known dangling request.
        executable=mask(text,False)
        for m in TOKEN.finditer(executable):
            if not m[0].startswith('"'):continue
            if norm(m[0][1:-1]) in removed|aliases:
                raise ValueError(f'Unresolved script reference to omitted FX in {raw.name}: {m[0]}')
        output.append(replace(raw,payload=text.encode('latin1')))
    return tuple(output),changes
