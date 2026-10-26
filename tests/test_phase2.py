import sys,json,hashlib,tempfile,socket
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lib.phase2 import validate,render_one,_parse_provider_sections
from lib import batchplan,transcripts

def plan():return {'schemaVersion':1,'meeting':{'id':'m','title':'<script>x</script>','date':'2026-10-19','timezone':'Asia/Kolkata'},'source':{},'editorialMode':'AGENT_AUTHORED','sharedNotes':{'purpose':'p','takeaways':['t'],'detailedSummary':[{'heading':'h','points':['d']}]},'recipients':[{'userId':'u','name':'A <x>','intendedEmail':'a@example.com','actions':[]}],'templateVersion':'kik-py-2','applicationUrl':'https://delivery-desk-frontend.vercel.app'}
def bundle():return {'meeting':{'id':'m'},'completeness':{'complete':True,'notes':[]},'identities':{'attendees':[{'user':{'userId':'u','email':'a@example.com','isActive':True}}]},'summaries':{'items':[]},'storedActionItems':{'items':[]},'transcript':{'id':'t','fullText':'full exact quotation','segments':[{'id':'s','sequence':1,'text':'segment exact quotation'}]}}
def test_html_escaping_and_empty_actions():
 p=plan();x=render_one(p,p['recipients'][0]);assert '<script>' not in x['html'] and '&lt;script&gt;' in x['html'];assert 'No action items were assigned to you in today’s meeting summary.' in x['html']
def test_duplicate_recipient():
 p=plan();p['recipients'].append(dict(p['recipients'][0]));assert not validate(p)['valid']
def test_invalid_url():
 p=plan();p['applicationUrl']='javascript:alert(1)';assert not validate(p)['valid']
def test_inactive_mapping_fails():
 p=plan();b=bundle();b['identities']['attendees'][0]['user']['isActive']=False;assert any('inactive' in e for e in validate(p,b)['errors'])
def test_segment_and_fulltext_citations_pass_and_bad_fail():
 p=plan();p['recipients'][0]['actions']=[{'ownerType':'OWNER','title':'a','body':'b','sourceReferences':[{'sourceType':'TRANSCRIPT_SEGMENT','sourceId':'s','quote':'segment exact'},{'sourceType':'TRANSCRIPT_FULL_TEXT','sourceId':'t','quote':'full exact'}]}];assert validate(p,bundle())['valid'];p['recipients'][0]['actions'][0]['sourceReferences'][0]['quote']='invented';assert not validate(p,bundle())['valid']
def test_mislabeled_summary_citation_fails():
 p=plan();p['recipients'][0]['actions']=[{'ownerType':'OWNER','sourceReferences':[{'sourceType':'TRANSCRIPT_SEGMENT','sourceId':'wrong','quote':'full exact'}]}];assert not validate(p,bundle())['valid']
def test_preserve_provider_sections_and_excludes_actions():
 raw='## Purpose\nPurpose <safe>.\n## Takeaways\n- First point\n- Second point\n## Detailed summary\n### Topic\n- Third point\n## Action items\n- Private action: Fakhrul\n- Private action: Satarupa'
 b=bundle();b['summaries']={'items':[{'id':'sum','source':'PROVIDER','text':raw}]};p=plan();p['editorialMode']='PRESERVE_PROVIDER';p['sharedNotes']={'providerText':raw,**_parse_provider_sections(raw)};p['recipients'][0]['actions']=[{'ownerType':'OWNER','title':'Only Fakhrul task','body':'task'}];assert validate(p,b)['valid'];out=render_one(p,p['recipients'][0]);assert '<pre' not in out['html'] and 'Purpose</h2>' in out['html'] and 'First point' in out['html'] and 'Second point' in out['html'] and 'Action items' in out['html'];assert 'Private action: Fakhrul' not in out['html'] and 'Private action: Satarupa' not in out['html'];assert 'Only Fakhrul task' in out['html'] and '&lt;safe&gt;' in out['html']
def test_preserve_detects_changed_shared_sections():
 raw='## Purpose\nP\n## Takeaways\n- A\n- B\n## Detailed summary\n### H\n- C\n## Action items\n- X';b=bundle();b['summaries']={'items':[{'id':'sum','source':'PROVIDER','text':raw}]};p=plan();p['editorialMode']='PRESERVE_PROVIDER';p['sharedNotes']={'providerText':raw,**_parse_provider_sections(raw)};assert validate(p,b)['valid'];p['sharedNotes']['takeaways'].reverse();assert not validate(p,b)['valid']
def test_mentioned_item_is_separate_and_owner_named():
 p=plan();p['recipients'][0]['actions']=[{'ownerType':'MENTIONED_RECIPIENT','ownerName':'Satarupa Roy','title':'Forward JD to Roja for vendor checks.'}];x=render_one(p,p['recipients'][0]);assert 'No action items were assigned to you in today’s meeting summary.' in x['html'];assert 'Related item — owned by Satarupa Roy' in x['html']
def test_stable_manifest_hash_and_changed_inputs():
 from lib.phase2 import _approval_hash
 p=plan();r=p['recipients'][0];o=render_one(p,r);row={'intendedRecipient':r['intendedEmail'],**o};h=_approval_hash(row);assert h==_approval_hash({'intendedRecipient':r['intendedEmail'],**render_one(p,r)});r['actions']=[{'ownerType':'OWNER','title':'new','body':'new'}];assert h!=_approval_hash({'intendedRecipient':r['intendedEmail'],**render_one(p,r)});r['actions']=[];r['intendedEmail']='b@example.com';assert h!=_approval_hash({'intendedRecipient':r['intendedEmail'],**render_one(p,r)})
def test_network_blocked_validate_and_render():
 from lib.phase2 import render_run
 root=Path(tempfile.mkdtemp())
 def blocked(*a,**k):raise AssertionError('network attempted')
 old=socket.socket.connect;socket.socket.connect=blocked
 try:
  p=plan();assert validate(p)['valid'];d,_=render_run(p,root);assert Path(d).is_dir()
 finally:socket.socket.connect=old
def test_transcript_batch_replay_and_changed_content():
 b=bundle();m=transcripts.plan_from_bundle(b,max_chars=500).manifest();assert batchplan.plan_from_manifest(b,m).batch_count==1;b['transcript']['segments'][0]['text']='changed'
 try:batchplan.plan_from_manifest(b,m)
 except transcripts.BatchError:return
 assert False
