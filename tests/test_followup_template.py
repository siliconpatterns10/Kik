from lib.followup_template import render

def sample():
 return {'meeting':{'title':'Daily <Leadership>'},'applicationUrl':'https://example.invalid','followUp':{'date':'26 Oct 2026','introduction':'Please update today.'}}, {'name':'Test <Owner>','actions':[{'title':'Original <task>','body':'Original scope & caveat','updatePrompt':'What changed today?'}]}
def test_followup_escapes_and_allows_freeform_reply():
 plan,r=sample();out=render(plan,r)
 assert 'Original &lt;task&gt;' in out['html'] and 'Original <task>' in out['text']
 assert 'Test &lt;Owner&gt;' in out['html']
 assert 'What changed today?' in out['html'] and 'What changed today?' in out['text']
 for word in ('Please reply with','Status:','Latest update or outcome:','Expected completion:','Blocker or support needed:'):
  assert word not in out['html'] and word not in out['text']
 assert '<!--[if mso]>' in out['html'] and 'if email_type' not in out['html']
def test_followup_requires_tasks():
 plan,r=sample();r['actions']=[]
 try:render(plan,r)
 except ValueError:pass
 else:raise AssertionError('empty tasks accepted')
