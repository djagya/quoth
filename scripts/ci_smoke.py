import pathlib,runpy,subprocess,sqlite3,json,hashlib,asyncio,sys,tarfile,zipfile,argparse,tempfile
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
repo=pathlib.Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser(); p.add_argument('--output',required=True); p.add_argument('--require-wheel',action='store_true'); options=p.parse_args()
root=pathlib.Path(tempfile.mkdtemp(prefix='quoth-ci-'))
if options.require_wheel:
 import quoth
 assert '/site-packages/' in str(quoth.__file__), 'wheel smoke must not import editable source'

t=runpy.run_path(str(repo/'tests/test_bundle.py')); vault=t['_write_vault'](root/'vault'); source=root/'source.db'; t['build_source_db'](source,vault)
cli=pathlib.Path(sys.executable).parent/'quoth'; receipts=[]
def invoke(*args):
 p=subprocess.run([str(cli),*map(str,args)],capture_output=True,text=True,timeout=30)
 receipts.append({'args':list(map(str,args)),'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
 (root/'cli-receipts.json').write_text(json.dumps(receipts,ensure_ascii=False,indent=2))
 assert p.returncode==0,p.stderr
 return p.stdout
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def inventory(p):
 c=sqlite3.connect('file:'+str(p)+'?mode=ro',uri=True); tables=[x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]; result={}
 for n in tables:
  rows=c.execute('SELECT * FROM "'+n+'"').fetchall(); result[n]={'count':len(rows),'digest':hashlib.sha256(repr(sorted(rows,key=repr)).encode()).hexdigest()}
 assert c.execute('pragma integrity_check').fetchone()[0]=='ok'; assert c.execute('pragma foreign_key_check').fetchall()==[]; c.close(); return result
with sqlite3.connect(source) as conn: conn.execute('DELETE FROM embeddings')
invoke('embed','--db',source,'--model','hash-ngram')
initial=sha(source); output={}
for scope in (None,'shelest'):
 name=scope or 'full'; bundle=root/(name+'.db'); dest=root/('restored-'+name+'.db')
 args=['bundle','export','--source',source,'--target',bundle]
 if scope: args+=['--scope',scope]
 invoke(*args); invoke('bundle','validate','--db',bundle); invoke('bundle','import','--db',bundle,'--target',dest)
 a=inventory(bundle); b=inventory(dest); assert a==b
 output[name]={'inventory':a,'roundtrip_equal':True,'queries':{}}
 if not scope:
  src_inventory=inventory(source)
  for table in ('sources','source_versions','chunks','entities','embeddings'):
   assert src_inventory[table]==a[table],table
 else:
  assert t['SENTINEL'].encode() not in bundle.read_bytes() and t['SENTINEL'].encode() not in dest.read_bytes()
  assert t['SENTINEL'] not in bundle.with_suffix('.manifest.json').read_text()
 for query in ['медитация','kite']:
  result=json.loads(invoke('search',query,'--db',dest,'--json')); assert result
  output[name]['queries'][query]=len(result)
assert sha(source)==initial
async def mcpcheck():
 db=root/'restored-shelest.db'; before=sha(db); results=[]
 params=StdioServerParameters(command=str(cli),args=['mcp','--db',str(db)])
 async with stdio_client(params) as (read,write):
  async with ClientSession(read,write) as sess:
   init=await sess.initialize(); listing=await sess.list_tools(); assert [x.name for x in listing.tools]==['evidence_bundle']
   for query in ['медитация','kite']:
    r=await sess.call_tool('evidence_bundle',{'query':query,'k':5,'model':'hash-ngram'}); assert not r.is_error, str(r.content)
    assert r.content[0].type=='text'
    data=json.loads(r.content[0].text); assert data['hits'] and data.get('answer_use')
    for hit in data['hits']: assert 'path' in hit and 'line_start' in hit and 'date_basis' in hit
    results.append({'query':query,'hit_count':len(data['hits']),'provenance_checked':True})
   r=await sess.call_tool('remember',{'text':'must not write'}); assert r.is_error
 assert sha(db)==before
 return {'queries':results,'mutation_rejected':True,'db_unchanged':True}
output['mcp']=asyncio.run(mcpcheck()); output['source_unchanged']=sha(source)==initial
# Check built artifacts, not merely git tracked inventory.
for p in (repo/'dist').iterdir():
 if not (p.name.endswith('.tar.gz') or p.suffix=='.whl'): continue
 if p.suffix=='.whl': names=zipfile.ZipFile(p).namelist()
 else: names=tarfile.open(p).getnames()
 assert not any('/.git/' in x or '/.venv/' in x or x.endswith('.db') or 'rollback' in x or 'receipt' in x for x in names)
 output[p.name]={'entries':len(names),'private_state_excluded':True}
# The public eval fixture, built and scored from the installed package (eval set v1 harness).
from quoth.assets import eval_path
fx=root/'fixture'; invoke('eval','fixture','--out',fx)
eset=eval_path('public')/'set.jsonl'; invoke('eval','validate',eset)
run_out=root/'eval-run.json'; invoke('eval','run',eset,'--fixture-dir',fx,'--out',run_out,'--quiet')
report=json.loads(run_out.read_text()); rows=report['set']['rows']
assert report['status']=='ok' and report['scored']==rows
assert report['metrics']['overall']['stores']['vault']['hit@10']>=0.8, 'regressed from public fixture baseline'
output['golden']={'rows':rows,'report':report,'scope':'synthetic public fixture, not real corpus quality'}
# Box surface from the installed package: plugin skills, WebUI extension, health, nudge.
from quoth import hermes_install
from quoth.assets import webui_extension_dir
info=hermes_install.install(root/'hermes-home')
assert all((pathlib.Path(info['skills_dir'])/s/'SKILL.md').is_file() for s in hermes_install.SKILLS)
ext=webui_extension_dir(); assert (ext/'extensions.json').is_file() and (ext/'quoth'/'boot.js').is_file()
if options.require_wheel: assert '/site-packages/' in str(ext), 'WebUI extension must come from the wheel'
invoke('health','--help'); ledger_db=root/'ledger.db'
invoke('ledger','--ledger',ledger_db,'health'); assert invoke('ledger','--ledger',ledger_db,'nudge')==''
output['box_surface']={'skills':list(hermes_install.SKILLS),'webui_extension':True,'health_cli':True,'nudge_empty':True}
out=pathlib.Path(options.output); out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(output,ensure_ascii=False,indent=2))
print(json.dumps({'full_scoped_cli_roundtrip':True,'source_unchanged':True,'mcp':output['mcp'],'package_inventory_checked':True,'golden_rows':rows},ensure_ascii=False,indent=2))
