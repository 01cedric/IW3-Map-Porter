"""Static script dependency inspection; this does not compile or execute GSC."""
import re

TOKEN = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"')
QUALIFIED = re.compile(r'([A-Za-z_][\w\\/]*)\s*::\s*([A-Za-z_]\w*)')
INCLUDE = re.compile(r'(?i)#include\s+([\w\\/]+)')
DECLARATION = re.compile(r'(?m)^\s*([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{')


def normalized(name):
    return name.replace('\\', '/').casefold()


def masked(text, strings=True):
    def replacement(match):
        if not strings and match.group().startswith('"'):
            return match.group()
        return ''.join('\n' if char == '\n' else ' ' for char in match.group())
    return TOKEN.sub(replacement, text)


def analyze_scripts(scripts, inventory, entry):
    scripts = {normalized(n): text for n, text in scripts.items()}
    code = {n: masked(text) for n, text in scripts.items()}
    definitions = {n: {v.casefold() for v in DECLARATION.findall(text)} for n, text in code.items()}
    assets = {(t, normalized(n)) for t, n in inventory}
    pending = [normalized(entry)]; seen = set(); missing = []; refs = []; literal = []
    if normalized(entry) in scripts and 'main' not in definitions[normalized(entry)]:
        missing.append(dict(target=normalized(entry), function='main',
                            reason='map entry function declaration not found by static scan'))
    patterns = (
        (27, re.compile(r'(?i)\bloadfx\s*\(\s*"([^"\n]+)"')),
        (3, re.compile(r'(?i)\bprecachemodel\s*\(\s*"([^"\n]+)"')),
        (9, re.compile(r'(?i)\b(?:ambientplay|playsound|playloopsound)\s*\(\s*"([^"\n]+)"')),
        (9, re.compile(r'(?i)\[\s*"soundalias"\s*\]\s*=\s*"([^"\n]+)"')),
    )
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        if name not in scripts:
            missing.append(dict(target=name, reason='script not in loaded database'))
            continue
        text = scripts[name]; clean = code[name]
        for match in QUALIFIED.finditer(clean):
            target = normalized(match[1]) + '.gsc'; function = match[2]
            row = dict(source=name, line=text.count('\n', 0, match.start()) + 1,
                       target=target, function=function)
            refs.append(row)
            if target not in scripts:
                missing.append({**row, 'reason': 'script not in loaded database'})
            else:
                pending.append(target)
                if function.casefold() not in definitions[target]:
                    missing.append({**row, 'reason': 'function declaration not found by static scan'})
        for match in INCLUDE.finditer(clean):
            pending.append(normalized(match[1]) + '.gsc')
        comments_removed = masked(text, strings=False)
        for asset_type, pattern in patterns:
            for match in pattern.finditer(comments_removed):
                if clean[match.start()].isspace():
                    continue  # expression was inside a quoted literal
                if match.group().startswith('[') and match[1] == 'nil':
                    continue  # stock _fx.gsc explicitly excludes this unset soundalias sentinel
                literal.append(dict(source=name, line=text.count('\n', 0, match.start()) + 1,
                    asset_type=asset_type, name=match[1],
                    present=(asset_type, normalized(match[1])) in assets))
    return dict(scope='static named scripts/functions and literal asset requests; all branches included',
        compiled=False, executed=False, dynamic_calls_checked=False,
        reachable_scripts=len(seen), qualified_references=len(refs),
        missing_script_references=missing,
        missing_literal_assets=[r for r in literal if not r['present']],
        note='Missing entries are diagnostic candidates, not proof of a runtime abort. '
             'Dynamic calls, builtins, branch reachability and the target filesystem are not evaluated.')


def inspect_linked_scripts(machine, inventory, mapname):
    scripts = {}; malformed = []
    for (typ, name), header in inventory.items():
        if typ != 33 or not name.casefold().endswith('.gsc'):
            continue
        length = machine.m.u32(header + 4); pointer = machine.m.u32(header + 8)
        if length > 4 * 1024 * 1024 or not pointer:
            malformed.append(dict(name=name, reason='invalid text pointer or implausible script length'))
            continue
        payload = machine.m.read(pointer, length)
        if b'\0' in payload or machine.m.u8(pointer + length) != 0:
            malformed.append(dict(name=name, reason='embedded NUL or missing terminator'))
        scripts[name] = payload.decode('latin1')
    report = analyze_scripts(scripts, inventory, 'maps/mp/' + mapname + '.gsc')
    report['malformed_rawfiles'] = malformed
    return report
