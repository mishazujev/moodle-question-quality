"""Supabase REST storage. All keys stay in Streamlit server-side secrets."""
from __future__ import annotations
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from io import BytesIO
from pathlib import Path
import tempfile
from legacy.review_moodle_questions import read_items, parse_number


class CloudError(RuntimeError):
    pass


class Cloud:
    def __init__(self, url, service_role_key, bucket='moodle-question-xml'):
        self.url=url.rstrip('/')
        self.key=service_role_key
        self.bucket=bucket
        if not self.url.startswith('https://') or not self.key:
            raise CloudError('Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in Streamlit secrets')

    def request(self, method, path, data=None, binary=False, headers=None):
        h={'apikey':self.key}
        # New sb_secret keys are not JWTs and must not be sent as Bearer tokens.
        if not self.key.startswith('sb_secret_'):
            h['Authorization']='Bearer '+self.key
        if data is not None and not binary:
            data=json.dumps(data,ensure_ascii=False).encode('utf-8')
            h['Content-Type']='application/json'
        if headers:h.update(headers)
        req=urllib.request.Request(self.url+path, data=data, headers=h,method=method)
        try:
            with urllib.request.urlopen(req,timeout=90) as resp:
                payload=resp.read()
                return payload if binary else (json.loads(payload) if payload else None)
        except urllib.error.HTTPError as e:
            body=e.read().decode('utf-8','replace')[:350]
            raise CloudError(f'Supabase HTTP {e.code}: {body}') from e
        except urllib.error.URLError as e:
            raise CloudError('Cannot reach Supabase; check URL and connection') from e

    def select(self, table, query=''):
        # Supabase REST commonly caps responses at 1000 records; paginate via Range.
        output=[]
        step=500
        for offset in range(0, 100000, step):
            page=self.request('GET','/rest/v1/'+table+'?'+query,headers={
                'Range':f'{offset}-{offset+step-1}',
            })
            if not isinstance(page,list):return page
            output.extend(page)
            if len(page)<step:return output
        raise CloudError('Result set too large; apply a filter')

    def rpc(self, func, args):
        return self.request('POST','/rest/v1/rpc/'+func,args)

    def update(self, table, filters, values):
        return self.request('PATCH','/rest/v1/'+table+'?'+filters,values,headers={'Prefer':'return=representation'})

    def upsert(self, table, values, on_conflict):
        q=urllib.parse.quote(on_conflict,safe=',')
        return self.request('POST','/rest/v1/'+table+'?on_conflict='+q,values,
                            headers={'Prefer':'resolution=merge-duplicates,return=representation'})

    def upload(self, path, data, content_type='application/xml'):
        safe=urllib.parse.quote(path,safe='/')
        return self.request('POST',f'/storage/v1/object/{self.bucket}/{safe}',data,binary=True,
                            headers={'Content-Type':content_type, 'x-upsert':'false'})

    def download(self, path):
        safe=urllib.parse.quote(path,safe='/')
        return self.request('GET',f'/storage/v1/object/authenticated/{self.bucket}/{safe}',binary=True)

    def summaries(self):
        return self.select('question_quality_view','select=*&order=name.asc&limit=10000')

    def assessments(self):
        return self.select('assessments','select=assessment_key,quiz,cohort,academic_year,stats_hash,imported_at&order=imported_at.desc&limit=1000')

    def questions(self):
        return self.select('questions','select=*&limit=10000')

    def bank_items(self):
        return self.select('bank_items','select=*&order=name.asc&limit=10000')


def normalize(x):
    return re.sub(r'\s+',' ',str(x or '').strip()).casefold()


def parse_csv(raw):
    if len(raw)>8_000_000:raise ValueError('Statistics CSV exceeds 8 MB limit')
    with tempfile.TemporaryDirectory() as td:
        p=Path(td)/'statistics.csv';p.write_bytes(raw)
        items,kind=read_items(p)
    if not items:raise ValueError('No individual Moodle question statistics found')
    result=[]
    for v in items:
        attempts=parse_number(v['attempts'])
        f=parse_number(v['facility'],True)
        d=parse_number(v['discrimination'],True)
        if attempts is None or attempts<0 or int(attempts)!=attempts:raise ValueError('Invalid number of attempts')
        if f is not None and not -100<=f<=100:raise ValueError('Facility out of range')
        if d is not None and not -100<=d<=100:raise ValueError('Discrimination out of range')
        signature=normalize(v['question_name'])+'|'+normalize(v['question_type'])
        result.append({'item_key':str(v['question_id']),'position':str(v['question_id']),
                       'name':v['question_name'],'question_type':v['question_type'],
                       'signature':signature,'attempts':int(attempts), 'facility':f,'discrimination':d})
    if len(set(x['item_key'] for x in result))!=len(result):
        raise ValueError('Repeated Q# within this export; cannot distinguish rows safely')
    canonical=[(r['item_key'],r['signature'],r['attempts'],r['facility'],r['discrimination']) for r in result]
    canonical.sort()
    fingerprint=hashlib.sha256(json.dumps(canonical,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    return result,fingerprint,kind


def quality(attempts,facility,negative,low):
    if attempts is None or attempts<10 or facility is None:return 'More data'
    if negative:return 'At risk'
    if facility<30 or facility>90 or low:return 'Review'
    return 'Keep candidate'


def row_summary(row):
    q=dict(row)
    q['facility_percent']=float(q['facility_percent']) if q.get('facility_percent') is not None else None
    q['quality']=quality(q.get('attempts'),q.get('facility_percent'),q.get('negative_assessments'),q.get('low_discrimination_assessments'))
    return q


def report_csv(rows):
    import csv,io
    if not rows:return b''
    stream=io.StringIO();writer=csv.DictWriter(stream,fieldnames=list(rows[0]),extrasaction='ignore');writer.writeheader();writer.writerows(rows)
    return stream.getvalue().encode('utf-8-sig')
