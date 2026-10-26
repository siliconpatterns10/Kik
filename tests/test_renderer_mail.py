import base64, email, hashlib, json, tempfile
from email import policy
from email.parser import BytesParser
from pathlib import Path
from lib import phase2 as p, send_review as s

def test_restored_template_structure_and_date():
 fixture=Path('/root/Kik/tests/fixtures/phase2-oct19-plan.json');plan=json.loads(fixture.read_text());r=p.render_one(plan,plan['recipients'][0]);h=r['html']
 for term in ('role="presentation"','bgcolor="#f1f1ec"','bgcolor="#fafaf7"','<!--[if mso]>','MEETING NOTES','border-bottom:1px solid #e4e4de','dd-brand-cell','@media only screen','border:1px solid #e4e4de','Open Delivery Desk','Delivery Desk &middot; Silicon Patterns','19 Oct 2026'):
  assert term in h,term
 assert h.count('<table')>=8 and 'Hello Fakhrul' in h

def test_recipient_action_counts_and_related_label():
 plan=json.loads(Path('/root/Kik/tests/fixtures/phase2-oct19-plan.json').read_text())
 rendered=[p.render_one(plan,r)['html'] for r in plan['recipients']]
 assert rendered[0].count('Action 1')==1 and rendered[0].count('Action 2')==1
 assert 'No action items were assigned to you in today’s meeting summary.' in rendered[3]
 assert 'Related item — owned by Satarupa Roy:' in rendered[4]
 assert 'Forward JD to Roja for vendor checks.' in rendered[4]
 assert rendered[4].count('Action 1')==0

def test_mime_outer_base64_round_trip_utf8_long_lines_and_equals():
 text=('ASCII literal = & < > · — curly ’ “quotes”\n'+'x'*3000+'\nline with = signs near boundary '+('='*80))
 html='<html><body>'+('long'+('='*200)+' · — ’ “&<>')*80+'</body></html>'
 outer=s._mime({'subject':'Review','text':text,'html':html})
 raw=base64.b64decode(outer,validate=True)
 assert b'\r\n' in raw and b'\n' not in raw.replace(b'\r\n',b'')
 assert max(map(len,raw.split(b'\r\n')))<998
 msg=BytesParser(policy=policy.default).parsebytes(raw)
 assert msg.get_content_type()=='multipart/alternative'
 parts=list(msg.iter_parts());assert [x.get_content_type() for x in parts]==['text/plain','text/html']
 assert all(x.get_content_charset().lower()=='utf-8' and x['Content-Transfer-Encoding']=='base64' for x in parts)
 assert parts[0].get_content().replace('\r\n','\n')==text.replace('\r\n','\n')+'\n'
 assert parts[1].get_content().replace('\r\n','\n')==html.replace('\r\n','\n')+'\n'
 assert '=' in parts[0].get_content()

def test_header_injection_rejected_by_mime():
 try:s._mime({'subject':'ok\r\nBcc: colleague@example.com','text':'x','html':'<p>x</p>'})
 except Exception:pass
 else:raise AssertionError('header injection accepted')


def test_summary_and_actions_are_separate_and_no_literal_code():
 plan=json.loads(Path('/root/Kik/tests/fixtures/phase2-oct19-plan.json').read_text())
 for kind,title in [('SUMMARY','Meeting summary'),('ACTIONS','To-do list and related updates')]:
  plan['emailType']=kind;out=p.render_one(plan,plan['recipients'][0])
  assert title in out['html'] and title in out['text']
  assert "if email_type" not in out['html'] and "'+(" not in out['html']
  assert '<!--[if mso]>' in out['html']
  if kind=='SUMMARY':assert 'Action 1' not in out['html'] and '\nAction items\n' not in out['text']
  else:assert '>Purpose<' not in out['html'] and '\nPurpose\n' not in out['text']
