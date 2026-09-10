"""Static GSC findings must distinguish source calls, comments and case."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools/ps3_loader_emulator'))
from scriptcheck import analyze_scripts
scripts={'maps/mp/mp_test.gsc':r'''
#include common_scripts\utility;
main() {
    maps\mp\helper::DoWork();
    loadfx("fx/fire");
    ambientPlay("absent_amb");
    ent.v["soundalias"] = "missing_loop";
    ent.v["soundalias"] = "nil";
    // maps\mp\absent::main(); loadfx("bogus");
    /* maps\mp\absent::main(); */
    s="maps\\mp\\absent::main()";
}
''', 'maps/mp/helper.gsc':'dowork() { }', 'common_scripts/utility.gsc':'utility() { }'}
r=analyze_scripts(scripts,{(27,'FX/FIRE'):1},'maps/mp/mp_test.gsc')
assert not r['missing_script_references'],r
assert {row['name'] for row in r['missing_literal_assets']}=={'absent_amb','missing_loop'}
assert r['compiled'] is False and r['executed'] is False
scripts['maps/mp/helper.gsc']='other() {}'
r=analyze_scripts(scripts,{},'maps/mp/mp_test.gsc')
assert r['missing_script_references'][0]['function']=='DoWork'
del scripts['maps/mp/helper.gsc']
r=analyze_scripts(scripts,{},'maps/mp/mp_test.gsc')
assert 'script not in loaded database' in r['missing_script_references'][0]['reason']
r=analyze_scripts({'maps/mp/mp_test.gsc':'other() {}'}, {}, 'maps/mp/mp_test.gsc')
assert r['missing_script_references'][0]['function']=='main'
print('PASS: named GSC dependencies, case folding, comment/literal isolation and missing assets')
