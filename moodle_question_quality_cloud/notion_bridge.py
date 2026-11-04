"""Conservative Notion synchronization for Moodle Question Quality.

Never creates question pages, changes question content, or sets editorial Workflow.
Matches by unique normalized Name+Type or prior confirmed page ID.
Only writes after an explicit preview and approval in the UI.
"""
import json
import re
import time
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import date

DS_ID = '2179e758-9a23-80cc-b87e-000b7b3fed2f'
API = 'https://api.notion.com/v1'
VERSION = '2025-09-03'
REQUIRED = {
    'Quality': {'select': {'options': [
        {'name': 'More data', 'color': 'gray'},
        {'name': 'Keep candidate', 'color': 'green'},
        {'name': 'Review', 'color': 'yellow'},
        {'name': 'At risk', 'color': 'red'}]}},
    'Workflow': {'select': {'options': [
        {'name': 'Active', 'color': 'gray'},
        {'name': 'Fix', 'color': 'red'},
        {'name': 'In review', 'color': 'yellow'},
        {'name': 'Update', 'color': 'yellow'},
        {'name': 'Awaiting import', 'color': 'purple'},
        {'name': 'Ready', 'color': 'blue'},
        {'name': 'Published', 'color': 'green'},
        {'name': 'Retired', 'color': 'brown'}]}},
    'Moodle attempts': {'number': {'format':'number'}},
    'Moodle facility (%)': {'number': {'format':'number'}},
    'Moodle negative discrimination assessments': {'number': {'format':'number'}},
    'Moodle assessment count': {'number': {'format':'number'}},
    'Moodle checked': {'date': {}},
    'Moodle ID': {'rich_text': {}},
    'Moodle tags': {'rich_text': {}},
    'Published version': {'number': {'format': 'number'}},
}


def normal(s):
    return re.sub(r'\s+', ' ', (s or '').strip().strip('*').strip()).casefold()

TYPE_MAP = {
    'multiple choice': 'Multichoice', 'calculated multichoice': 'Multichoice',
    'numerical': 'Numerical', 'short answer': 'Short answer', 'matching': 'Matching',
    'embedded answers (cloze)': 'Cloze', 'cloze': 'Cloze',
    'true/false': 'True-False', 'true-false': 'True-False',
    'drag and drop onto image': 'Drag onto image',
    'drag markers': 'Drag markers', 'drag and drop into text': 'Drag and drop text',
    'ordering': 'Ordering', 'calculated': 'Calculated',
}

def normalized_type(moodle):
    s=normal(moodle)
    return normal(TYPE_MAP.get(s, moodle))


def title(page):
    return ''.join(z.get('plain_text', z.get('text',{}).get('content','')) for z in page.get('properties',{}).get('Name',{}).get('title',[]))


def type_name(page):
    return ((page.get('properties',{}).get('Type',{}).get('select') or {}).get('name') or '')


class Notion:
    def __init__(self, token):
        if not token.strip(): raise ValueError('Notion token is required')
        self.token=token.strip()

    def request(self, method, route, payload=None):
        data=json.dumps(payload).encode('utf-8') if payload is not None else None
        headers={'Authorization': 'Bearer '+self.token,'Notion-Version':VERSION,'Content-Type':'application/json'}
        for attempt in range(5):
            req=urllib.request.Request(API+route,data=data,headers=headers,method=method)
            try:
                with urllib.request.urlopen(req, timeout=40) as response:
                    out=response.read()
                    return json.loads(out) if out else {}
            except urllib.error.HTTPError as err:
                body=err.read().decode('utf-8','replace')[:700]
                if err.code==429 and attempt<4:
                    time.sleep(min(8,float(err.headers.get('Retry-After','1'))))
                    continue
                raise RuntimeError(f'Notion HTTP {err.code}: {body}') from err
            except urllib.error.URLError as err:
                raise RuntimeError(f'Notion network error: {err.reason}') from err

    def schema(self):
        return self.request('GET','/data_sources/'+DS_ID)

    def ensure_fields(self):
        schema=self.schema()
        existing=schema.get('properties',{})
        to_add={k:v for k,v in REQUIRED.items() if k not in existing}
        if to_add:
            self.request('PATCH','/data_sources/'+DS_ID, {'properties':to_add})
        refreshed=self.schema().get('properties',{})
        for k in REQUIRED:
            if k not in refreshed: raise RuntimeError('Missing property after setup: '+k)
        for k,v in REQUIRED.items():
            expected=next(iter(v))
            if refreshed[k].get('type')!=expected:
                raise RuntimeError(f'Property {k} exists with wrong type: expected {expected}')
        return list(to_add)

    def all_pages(self):
        pages=[];cursor=None
        while True:
            payload={'page_size':100}
            if cursor:payload['start_cursor']=cursor
            result=self.request('POST','/data_sources/'+DS_ID+'/query',payload)
            pages.extend(p for p in result.get('results',[]) if not p.get('in_trash',False))
            if not result.get('has_more'):break
            cursor=result.get('next_cursor')
            if not cursor:raise RuntimeError('Pagination cursor absent')
        return pages

    def patch(self, page_id, props):
        return self.request('PATCH','/pages/'+page_id, {'properties':props})


def property_values(r):
    return {
        'Quality': {'select': {'name':r['quality']}},
        'Moodle attempts': {'number':r['attempts'] or 0},
        'Moodle facility (%)': {'number':r['facility_percent']},
        'Moodle negative discrimination assessments': {'number':r['negative_assessments'] or 0},
        'Moodle assessment count': {'number':r['assessment_count'] or 0},
        'Moodle checked': {'date': {'start':date.today().isoformat()}},
    }


def field_equal(page, field, desired):
    p=page.get('properties',{}).get(field,{})
    if 'select' in desired:return (p.get('select') or {}).get('name')==desired['select']['name']
    if 'number' in desired:return p.get('number')==desired['number']
    # Checked date is deliberately excluded from change detection.
    return True


def plan(rows, pages):
    """Generate unique, safe matches. One Notion page cannot be claimed by multiple local rows."""
    by_name=defaultdict(list);by_id={};by_moodle_id=defaultdict(list)
    for p in pages:
        by_id[p['id'].replace('-','').lower()]=p
        n=normal(title(p))
        if n:by_name[n].append(p)
        mid=''.join(x.get('plain_text','') for x in p.get('properties',{}).get('Moodle ID',{}).get('rich_text',[]) or []).strip()
        if mid:by_moodle_id[mid].append(p)
    tentative=[]
    for r in rows:
        entry={'id':r['id'],'name':r['name'],'quality':r['quality'], 'attempts':r['attempts'],
               'facility_percent':r['facility_percent'],'target_page_id':'','status':'','reason':'','updates':{}}
        # Ambiguous question identity in local stats is never synced.
        if r.get('version_label')!='unverified':
            entry.update(status='skip',reason='Unverified version grouping; choose current version first')
            tentative.append(entry);continue
        target=None;method=''
        if r.get('notion_page_id'):
            target=by_id.get(r['notion_page_id'].replace('-','').lower())
            if target:method='confirmed mapping'
            else:
                entry.update(status='skip',reason='Saved Notion page no longer accessible')
                tentative.append(entry);continue
        elif r.get('moodle_bank_id'):
            candidates=by_moodle_id[r['moodle_bank_id']]
            if len(candidates)==1:
                target=candidates[0];method='unique verified Moodle ID'
            else:
                entry.update(status='skip',reason='Missing or duplicate Notion Moodle ID')
                tentative.append(entry);continue
        else:
            candidates=by_name[normal(r['name'])]
            if len(candidates)==1:
                target=candidates[0];method='unique exact name'
            elif len(candidates)>1:
                entry.update(status='skip',reason='Duplicate Notion name')
                tentative.append(entry);continue
            else:
                entry.update(status='skip',reason='No exact Notion name match')
                tentative.append(entry);continue
        existing_mid=''.join(x.get('plain_text','') for x in target.get('properties',{}).get('Moodle ID',{}).get('rich_text',[]) or []).strip()
        if existing_mid and not r.get('moodle_bank_id'):
            entry.update(status='skip',reason='Notion page already has Moodle ID but local statistics identity is not verified')
            tentative.append(entry);continue
        if existing_mid and r.get('moodle_bank_id') and existing_mid != r['moodle_bank_id']:
            entry.update(status='skip',reason='Moodle ID conflict')
            tentative.append(entry);continue
        typ=normal(type_name(target))
        if typ and typ!=normalized_type(r['question_type']):
            entry.update(status='skip',reason=f'Type mismatch: Moodle {r["question_type"]}, Notion {type_name(target)}')
        else:
            entry['target_page_id']=target['id']
            desired=property_values(r)
            # Promote only sufficiently evidenced at-risk items; never overwrite an ongoing revision or teacher decision.
            existing_workflow=(target.get('properties',{}).get('Workflow',{}).get('select') or {}).get('name')
            if r['quality']=='At risk' and (not existing_workflow or existing_workflow=='Active'):
                desired['Workflow']={'select':{'name':'Fix'}}
            changed={k:v for k,v in desired.items() if k!='Moodle checked' and not field_equal(target,k,v)}
            if changed:changed['Moodle checked']=desired['Moodle checked']
            entry['updates']=changed
            entry['status']='update' if changed else 'unchanged'
            entry['reason']=method
        tentative.append(entry)
    # Multiple versions or same-name rows must not overwrite one page with contradictory metrics.
    claim=defaultdict(list)
    for e in tentative:
        if e['target_page_id']:claim[e['target_page_id']].append(e)
    for entries in claim.values():
        if len(entries)>1:
            for e in entries:e.update(status='skip',reason='Multiple local question/version rows target same Notion page',updates={})
    return tentative


def apply(client, entries, con):
    count=0;errors=[]
    for e in entries:
        if e['status']!='update':continue
        try:
            client.patch(e['target_page_id'],e['updates'])
            with con:
                con.execute('UPDATE questions SET notion_page_id=? WHERE id=?',(e['target_page_id'],e['id']))
            count+=1
        except Exception as err:
            errors.append({'name':e['name'],'error':str(err)})
    return count,errors
