"""Offline plan validation and faithful email-safe renderer port (kik-py-2)."""
from __future__ import annotations
import hashlib, html, json, re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse
from . import runstore
TEMPLATE_VERSION='kik-py-2'; TEST_RECIPIENT='yaswanth.ampolu@siliconpatterns.com'
class PlanError(ValueError): pass
def _esc(x): return html.escape(str(x),quote=True)
def _url(x):
 try:
  u=urlparse(x);return u.scheme in ('http','https') and bool(u.netloc) and not u.username and not u.password
 except Exception:return False
def _filehash(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def _approval_hash(row):
 return hashlib.sha256(json.dumps({'intendedRecipient':row['intendedRecipient'],'subject':row['subject'],'text':row['text'],'html':row['html']},ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def _date(value):
 try:
  if isinstance(value,str):d=date.fromisoformat(value[:10])
  else:return ''
  return f'{d.day:02d} {("Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec")[d.month-1]} {d.year}'
 except (ValueError,TypeError):return ''
def _parse_provider_sections(raw):
 result={'purpose':None,'takeaways':[],'detailedSummary':[]};current=None;heading=None;points=[]
 def flush():
  nonlocal heading,points
  if heading is not None or points:result['detailedSummary'].append({'heading':heading or '','points':points})
  heading=None;points=[]
 for line in raw.splitlines():
  h=re.match(r'^#{1,6}\s+(.+?)\s*$',line)
  if h:
   title=h.group(1).strip();key=title.casefold()
   if key=='purpose':flush();current='purpose';continue
   if key in ('takeaways','key takeaways'):flush();current='takeaways';continue
   if key in ('detailed summary','detailed-summary'):flush();current='detail';continue
   if key in ('action items','actions','action items and owners'):flush();current='skip';continue
   if current=='detail':flush();heading=title
   continue
  if current in (None,'skip'):continue
  bullet=re.match(r'^\s*(?:[-*+]\s+|\d+[.)]\s+)(.*)$',line);v=(bullet.group(1) if bullet else line).strip()
  if not v:continue
  if current=='purpose':result['purpose']=((result['purpose']+' ') if result['purpose'] else '')+v
  elif current=='takeaways':result['takeaways'].append(v)
  elif current=='detail':points.append(v)
 flush();return result

def _source_matches(b,ref):
 typ,sid,q=ref.get('sourceType'),ref.get('sourceId'),ref.get('quote')
 if not isinstance(q,str) or not q:return False
 if typ=='TRANSCRIPT_FULL_TEXT':
  t=b.get('transcript',{});return t.get('id')==sid and q in str(t.get('fullText') or '')
 data={'PROVIDER_SUMMARY':b.get('summaries',{}).get('items',[]),'TRANSCRIPT_SEGMENT':b.get('transcript',{}).get('segments',[]),'STORED_ACTION_ITEM':b.get('storedActionItems',{}).get('items',[])}
 rows=[x for x in data.get(typ,[]) if x.get('id')==sid and (typ!='PROVIDER_SUMMARY' or x.get('source')=='PROVIDER')]
 return bool(rows and q in str(rows[0].get('title' if typ=='STORED_ACTION_ITEM' else 'text') or ''))
def validate(plan,bundle=None):
 errors=[];warnings=[]
 if plan.get('emailType','COMBINED') not in ('SUMMARY','ACTIONS','COMBINED','FOLLOW_UP'):errors.append('unsupported emailType')
 for k in ('schemaVersion','meeting','source','editorialMode','sharedNotes','recipients','templateVersion','applicationUrl'):
  if k not in plan:errors.append('missing required field: '+k)
 if plan.get('schemaVersion')!=1:errors.append('unsupported schemaVersion')
 m=plan.get('meeting',{})
 for k in ('id','title','date','timezone'):
  if not isinstance(m.get(k),str) or not m.get(k):errors.append('meeting.'+k+' is required')
 if plan.get('editorialMode') not in ('PRESERVE_PROVIDER','AGENT_AUTHORED'):errors.append('unsupported editorialMode')
 if plan.get('templateVersion')!=TEMPLATE_VERSION:errors.append('unsupported templateVersion')
 if not _url(plan.get('applicationUrl','')):errors.append('applicationUrl must be a valid HTTP(S) URL')
 sh=plan.get('sharedNotes',{})
 for k in ('purpose','takeaways','detailedSummary'):
  if k not in sh:errors.append('missing sharedNotes.'+k)
 for key in ('decisionsAndPriorities','blockersAndHelp'):
  if key in sh and (not isinstance(sh[key],list) or any(not isinstance(v,str) or not v.strip() for v in sh[key])):errors.append('sharedNotes.'+key+' must be a list of non-empty strings')
 users={}
 if bundle:
  for sec in ('attendees','speakers','summary'):
   rows=bundle.get('identities',{}).get(sec,[]) or []
   if isinstance(rows,dict):rows=list(rows.values())
   for row in rows:
    if isinstance(row,dict):
     u=row.get('user') or {}
     if u.get('userId'):users[u['userId']]=u
 seenid=set();seenmail=set()
 for i,r in enumerate(plan.get('recipients',[])):
  tag=f'recipients[{i}]';uid=r.get('userId');em=(r.get('intendedEmail') or '').strip().casefold()
  if not uid or not r.get('name') or not em:errors.append(tag+': userId, name and intendedEmail are required')
  if uid in seenid:errors.append(tag+': duplicate userId')
  if em in seenmail:errors.append(tag+': duplicate intendedEmail')
  seenid.add(uid);seenmail.add(em)
  if bundle:
   u=users.get(uid)
   if not u:errors.append(tag+': unresolved user in identity evidence')
   elif not u.get('isActive'):errors.append(tag+': inactive user')
   elif (u.get('email') or '').casefold()!=em:errors.append(tag+': intended email conflicts with identity evidence')
  for j,a in enumerate(r.get('actions',[]) or []):
   if a.get('ownerType') not in ('OWNER','MENTIONED_RECIPIENT'):errors.append(f'{tag}.actions[{j}]: ownerType required')
   if a.get('ownerType')=='MENTIONED_RECIPIENT':
    if not (a.get('ownerName') or a.get('assignedTo')):errors.append(f'{tag}.actions[{j}]: mentioned item requires explicit ownerName')
    warnings.append(f'{tag}.actions[{j}]: referenced assignment belongs to another named person; not recipient-owned')
   for ref in a.get('sourceReferences',[]) or []:
    if ref.get('sourceType') not in ('PROVIDER_SUMMARY','TRANSCRIPT_SEGMENT','TRANSCRIPT_FULL_TEXT','STORED_ACTION_ITEM') or not ref.get('sourceId'):errors.append(f'{tag}.actions[{j}]: invalid source type/ID')
    elif bundle and not _source_matches(bundle,ref):errors.append(f'{tag}.actions[{j}]: invalid source ID or quote does not exactly match source')
 if bundle:
  comp=bundle.get('completeness',{})
  if not comp.get('complete',False):warnings.append('source bundle is incomplete')
  warnings.extend('bundle limitation: '+str(x) for x in comp.get('notes',[]) or [])
  src=plan.get('source',{})
  if src.get('bundlePath') and src.get('bundleSha256') and _filehash(src['bundlePath'])!=src['bundleSha256']:errors.append('source bundle hash mismatch')
  if plan.get('editorialMode')=='PRESERVE_PROVIDER':
   provider=next((x.get('text') for x in bundle.get('summaries',{}).get('items',[]) if x.get('source')=='PROVIDER'),None)
   if provider is None:errors.append('PRESERVE_PROVIDER requires provider summary')
   elif sh.get('providerText')!=provider:errors.append('PRESERVE_PROVIDER providerText must exactly match raw provider Markdown')
   else:
    extracted=_parse_provider_sections(provider)
    for key in ('purpose','takeaways','detailedSummary'):
     if sh.get(key)!=extracted[key]:errors.append('PRESERVE_PROVIDER sharedNotes.'+key+' differs from deterministically extracted provider section')
 return {'valid':not errors,'errors':errors,'warnings':warnings,'ownershipAndQualityCertified':False}
def render_one(plan,r):
 if plan.get('emailType')=='FOLLOW_UP':
  from .followup_template import render
  return render(plan,r)
 sh=plan['sharedNotes'];title=plan['meeting']['title'];actions=r.get('actions',[]) or [];owned=[a for a in actions if a.get('ownerType')=='OWNER'];mentioned=[a for a in actions if a.get('ownerType')=='MENTIONED_RECIPIENT']
 email_type=plan.get('emailType','COMBINED')
 if email_type not in ('COMBINED','SUMMARY','ACTIONS'): raise PlanError('unsupported emailType')
 subject=('Meeting summary: ' if email_type=='SUMMARY' else 'Meeting follow-ups: ' if email_type=='ACTIONS' else 'Meeting notes: ')+re.sub(r'[\r\n\x00-\x1f]+',' ',title)[:170];pretty_date=_date(plan['meeting'].get('date'))
 if plan.get('editorialMode')=='PRESERVE_PROVIDER':
  parsed=_parse_provider_sections(sh.get('providerText',''));purpose=parsed['purpose'] or 'A purpose was not included in the saved meeting notes.';takes=parsed['takeaways'];det=parsed['detailedSummary']
 else:
  purpose=sh.get('purpose') or 'A purpose was not included in the saved meeting notes.';takes=sh.get('takeaways',[]);det=sh.get('detailedSummary',[])
 summary_text='Purpose\n'+str(purpose)+'\n\nTakeaways\n'+'\n'.join('• '+str(x) for x in takes)+'\n\nDetailed summary\n'+'\n'.join((x.get('heading','')+'\n' if x.get('heading') else '')+'\n'.join('• '+str(y) for y in x.get('points',[])) for x in det)
 def list_html(items):return '<ul style="margin:0;padding:0 0 0 20px;color:#333744;font:15px/24px Arial,sans-serif">'+''.join('<li style="padding:0 0 5px 2px">'+_esc(x)+'</li>' for x in items)+'</ul>' if items else '<p style="margin:0;color:#5b6070;font:15px/23px Arial,sans-serif">No takeaways were included in the saved meeting notes.</p>'
 sections='<tr><td class="dd-email-pad" style="padding:0 30px 23px"><h2 style="margin:0 0 9px;color:#14151a;font:700 20px/27px Arial,sans-serif">Purpose</h2><p style="margin:0;color:#333744;font:15px/24px Arial,sans-serif">'+_esc(purpose)+'</p></td></tr><tr><td class="dd-email-pad" style="padding:0 30px 23px"><h2 style="margin:0 0 9px;color:#14151a;font:700 20px/27px Arial,sans-serif">Takeaways</h2>'+list_html(takes)+'</td></tr><tr><td class="dd-email-pad" style="padding:0 30px 23px"><h2 style="margin:0 0 9px;color:#14151a;font:700 20px/27px Arial,sans-serif">Detailed summary</h2>'
 if det:sections+=''.join('<div style="margin:0 0 16px">'+('<h3 style="margin:0 0 6px;color:#14151a;font:700 16px/23px Arial,sans-serif">'+_esc(x.get('heading',''))+'</h3>' if x.get('heading') else '')+('<ul style="margin:0;padding:0 0 0 20px;color:#333744;font:15px/24px Arial,sans-serif">'+''.join('<li style="padding:0 0 5px 2px">'+_esc(y)+'</li>' for y in x.get('points',[]))+'</ul>')+'</div>' for x in det)
 else:sections+='<p style="margin:0;color:#5b6070;font:15px/23px Arial,sans-serif">No detailed summary was included in the saved meeting notes.</p>'
 sections+='</td></tr>'
 # Optional agent-authored sections; absent fields preserve existing output.
 extra_text='';extra_html=''
 if plan.get('editorialMode')=='AGENT_AUTHORED':
  for key,label in (('decisionsAndPriorities','Decisions and priorities'),('blockersAndHelp','Blockers / help needed')):
   items=sh.get(key,[])
   if items:
    extra_text+='\n\n'+label+'\n'+'\n'.join('• '+str(v) for v in items)
    extra_html+='<tr><td class="dd-email-pad" style="padding:0 30px 23px"><h2 style="margin:0 0 9px;color:#14151a;font:700 20px/27px Arial,sans-serif">'+_esc(label)+'</h2>'+list_html(items)+'</td></tr>'
 if extra_text:
  summary_text=summary_text.replace('\n\nDetailed summary\n',extra_text+'\n\nDetailed summary\n',1)
  marker='<tr><td class="dd-email-pad" style="padding:0 30px 23px"><h2 style="margin:0 0 9px;color:#14151a;font:700 20px/27px Arial,sans-serif">Detailed summary</h2>'
  sections=sections.replace(marker,extra_html+marker,1)
 task_rows=''
 if owned:
  for i,a in enumerate(owned):
   cites=''
   for ref in a.get('sourceReferences',[]) or []:
    if ref.get('quote'):
     label='From the meeting summary' if ref.get('sourceType') in ('PROVIDER_SUMMARY','STORED_ACTION_ITEM') else 'From the transcript'
     cites+='<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-top:16px;border-left:3px solid #c6f24e"><tr><td style="padding:2px 0 2px 12px;color:#5b6070;font:13px/20px Arial,sans-serif"><strong style="color:#14151a">'+label+'</strong><br>“'+_esc(str(ref['quote'])[:420])+'”</td></tr></table>'
   task_rows+='<tr><td style="padding:'+('0' if i==0 else '14px')+' 0 0"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border:1px solid #e4e4de;border-radius:12px"><tr><td style="padding:18px 20px 20px"><p style="margin:0 0 7px;color:#5b6070;font:12px/18px Arial,sans-serif;letter-spacing:.04em;text-transform:uppercase">Action '+str(i+1)+'</p><h3 style="margin:0;color:#14151a;font:700 18px/25px Arial,sans-serif">'+_esc(a.get('title',''))+'</h3><p style="margin:9px 0 0;color:#333744;font:15px/23px Arial,sans-serif">'+_esc(a.get('body','')).replace('\n','<br>')+'</p>'+cites+'</td></tr></table></td></tr>'
 else:task_rows='<tr><td style="padding:0;color:#5b6070;font:15px/23px Arial,sans-serif">No action items were assigned to you in today’s meeting summary.</td></tr>'
 for a in mentioned:
  task_rows+='<tr><td style="padding:14px 0 0"><p style="margin:0;padding:12px 14px;border-left:3px solid #c6f24e;background:#f1f7df;color:#333744;font:14px/21px Arial,sans-serif"><strong>Related item — owned by '+_esc(a.get('ownerName') or a.get('assignedTo') or 'another meeting participant')+':</strong> '+_esc(a.get('title',''))+'</p></td></tr>'
 if email_type=='SUMMARY':
  task_rows=''
 elif email_type=='ACTIONS':
  sections=''
 dynamic_title=('Meeting summary' if email_type=='SUMMARY' else 'To-do list and related updates' if email_type=='ACTIONS' else 'Meeting notes')
 html_doc=f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><style type="text/css">@media only screen and (max-width:480px){{.dd-email-pad{{padding-left:20px!important;padding-right:20px!important}}.dd-brand-cell,.dd-follow-up-cell{{display:block!important;width:100%!important;text-align:left!important}}.dd-follow-up-cell{{padding-top:10px!important}}}}</style></head><body bgcolor="#f1f1ec" style="margin:0;padding:0;background:#f1f1ec;color:#14151a"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" bgcolor="#f1f1ec" style="background:#f1f1ec"><tr><td align="center" style="padding:28px 12px"><!--[if mso]><table role="presentation" width="620" cellspacing="0" cellpadding="0" border="0"><tr><td><![endif]--><table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" bgcolor="#fafaf7" style="max-width:620px;background:#fafaf7;border:1px solid #e4e4de;border-radius:16px"><tr><td class="dd-email-pad" style="padding:22px 30px;border-bottom:1px solid #e4e4de"><strong class="dd-brand-cell" style="font:700 17px/24px Arial,sans-serif">Delivery Desk</strong><p style="margin:6px 0 0;color:#5b6070;font:11px/16px Arial,sans-serif;letter-spacing:.08em">MEETING NOTES</p></td></tr><tr><td class="dd-email-pad" style="padding:27px 30px 18px"><p style="margin:0 0 10px;color:#5b6070;font:14px/21px Arial,sans-serif">Hello {_esc(r.get('name','there'))},</p><h1 style="margin:0;color:#14151a;font:700 28px/34px Arial,sans-serif">{dynamic_title}</h1><p style="margin:8px 0 0;color:#5b6070;font:15px/23px Arial,sans-serif"><strong style="color:#14151a">{_esc(title)}</strong> · {pretty_date}</p></td></tr>{sections}{'' if email_type=='SUMMARY' else '<tr><td class="dd-email-pad" style="padding:0 30px 24px"><h2 style="margin:0 0 13px;color:#14151a;font:700 20px/27px Arial,sans-serif">Action items</h2><table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">'+task_rows+'</table></td></tr>'}<tr><td class="dd-email-pad" style="padding:0 30px 28px"><a href="{_esc(plan['applicationUrl'])}" style="display:inline-block;padding:12px 20px;border-radius:999px;background:#c6f24e;color:#14151a;text-decoration:none;font:600 14px/20px Arial,sans-serif">Open Delivery Desk</a><p style="margin:18px 0 0;color:#5b6070;font:13px/20px Arial,sans-serif">This copy is addressed only to you.</p></td></tr></table><!--[if mso]></td></tr></table><![endif]--><p style="margin:14px 0 0;color:#5b6070;font:12px/18px Arial,sans-serif">Delivery Desk &middot; Silicon Patterns</p></td></tr></table></body></html>'''

 if email_type=='SUMMARY':
  text='Hello '+r.get('name','there')+',\n\nMeeting summary\n'+title+(' · '+pretty_date if pretty_date else '')+'\n\n'+summary_text+'\n\nOpen Delivery Desk: '+plan['applicationUrl']
 elif email_type=='ACTIONS':
  text='Hello '+r.get('name','there')+',\n\nTo-do list and related updates\n'+title+(' · '+pretty_date if pretty_date else '')+'\n\nAction items\n'+('\n'.join(f"{i+1}. {a.get('title')}\n{a.get('body')}" for i,a in enumerate(owned)) if owned else 'No action items were identified for you in the transcript.')+'\n\nRelated items\n'+('\n'.join('Related item — owned by '+str(a.get('ownerName') or a.get('assignedTo') or 'another meeting participant')+': '+str(a.get('title','')) for a in mentioned) if mentioned else 'None')+'\n\nOpen Delivery Desk: '+plan['applicationUrl']
 else:
  action_text='\n'.join(f"{i+1}. {a.get('title')}\n{a.get('body')}" for i,a in enumerate(owned)) if owned else 'No action items were assigned to you in today’s meeting summary.'
  related_text='\n'.join('Related item — owned by '+str(a.get('ownerName') or a.get('assignedTo') or 'another meeting participant')+': '+str(a.get('title','')) for a in mentioned) if mentioned else 'None'
  text='Hello '+r.get('name','there')+',\n\nMeeting notes\n'+title+(' · '+pretty_date if pretty_date else '')+'\n\n'+summary_text+'\n\nAction items\n'+action_text+'\n\nRelated items\n'+related_text+'\n\nOpen Delivery Desk: '+plan['applicationUrl']
 return {'subject':subject,'text':text,'html':html_doc}
def render_run(plan,root,bundle=None,plan_path_hint=None):
 result=validate(plan,bundle)
 if not result['valid']:raise PlanError('; '.join(result['errors']))
 run=runstore.new_run(Path(root),command='render',local_date=plan['meeting']['date']);rows=[]
 for i,r in enumerate(plan['recipients']):
  out=render_one(plan,r);stem=f'recipient-{i+1:03d}';row={'name':r['name'],'userId':r['userId'],'intendedRecipient':r['intendedEmail'],'permittedActualTestRecipient':TEST_RECIPIENT,'subject':out['subject'],'htmlFile':stem+'.html','textFile':stem+'.txt'}
  run.write_text(stem+'.html',out['html'],note='offline local preview');run.write_text(stem+'.txt',out['text'],note='offline plain text');row['htmlSha256']=_filehash(run.directory/row['htmlFile']);row['textSha256']=_filehash(run.directory/row['textFile']);rows.append(row)
 entries=[dict(r) for r in rows];binding={'result':result,'source':plan.get('source',{}),'meeting':plan.get('meeting',{}),'editorialMode':plan.get('editorialMode')};vh=hashlib.sha256(json.dumps(binding,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 run.write_text('index.html','<!doctype html><meta charset="utf-8"><title>Local previews</title><h1>Local previews only — no email sent</h1>'+''.join(f'<p><a href="{x["htmlFile"]}">{_esc(x["name"])} HTML</a> | <a href="{x["textFile"]}">Plain text</a></p>' for x in entries),note='local index')
 run.write_json('preview-manifest.json',{'schemaVersion':1,'runId':run.run_id,'templateVersion':TEMPLATE_VERSION,'meeting':plan['meeting'],'editorialMode':plan.get('editorialMode'),'deliveryMode':'TEST_REVIEW','validationBinding':binding,'validationSha256':vh,'recipients':entries,'validation':result,'sendPerformed':False},note='recipient/content manifest');run.manifest.sources=dict(plan.get('source',{}),planPath=str(Path(plan_path_hint).expanduser()) if plan_path_hint else None,emailType=plan.get('emailType','COMBINED'))
 run.manifest.sources={k:v for k,v in run.manifest.sources.items() if v};run.manifest.completeness={'complete':True};run.seal();return str(run.directory),result
