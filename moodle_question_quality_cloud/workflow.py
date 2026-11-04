"""Stable export inventory, reviewable identity links and safe XML version publishing.
No network calls in this module. The Moodle XML comment `question: ID` is a
candidate bank identity within one Moodle site, NOT a quiz Q# or version ID.
"""
import csv, hashlib, html, io, json, re, sqlite3, xml.etree.ElementTree as ET
from pathlib import Path
from collections import defaultdict
from bank_import import inventory
from notion_bridge import normal, normalized_type, title, type_name

SITE='moodle.mishazujev.com'

def init(con):
    con.executescript('''
    CREATE TABLE IF NOT EXISTS bank_items (
        moodle_id TEXT PRIMARY KEY, site TEXT NOT NULL, name TEXT NOT NULL,
        question_type TEXT NOT NULL, category TEXT NOT NULL,
        tags_json TEXT NOT NULL DEFAULT '[]', source_path TEXT NOT NULL,
        content_hash TEXT NOT NULL, xml_available INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS publication_batches (
        id TEXT PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        file_name TEXT NOT NULL, payload BLOB NOT NULL, confirmed_at TEXT);
    CREATE TABLE IF NOT EXISTS publication_items (
        batch_id TEXT REFERENCES publication_batches(id),
        moodle_id TEXT NOT NULL, notion_page_id TEXT NOT NULL,
        content_hash TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'Awaiting import',
        PRIMARY KEY(batch_id,moodle_id));
    ''')

def get_xml_element(path, moodle_id):
    parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    root=ET.parse(path,parser=parser).getroot()
    current=''
    for element in root:
        if element.tag is ET.Comment:
            match=re.search(r'question:\s*(\d+)',element.text or '')
            if match:current=match.group(1)
        elif element.tag=='question' and element.get('type')!='category' and current==str(moodle_id):
            return element
    raise ValueError('Moodle ID not found in original XML: '+str(moodle_id))

def import_banks(con, paths):
    raw=inventory(paths)
    conflicts=[];loaded=0
    with con:
        for r in raw:
            ident=r['moodle_export_question_id']
            if not ident or not ident.isdigit(): continue
            # Use stable file identity and preserve existing XML when an HTML export is uploaded later.
            existing=con.execute('SELECT * FROM bank_items WHERE moodle_id=?',(ident,)).fetchone()
            if existing and (normal(existing['name'])!=normal(r['name']) or
                             (existing['xml_available'] and r['full_xml_available']=='yes' and existing['content_hash']!=r['content_sha256'])):
                conflicts.append({'id':ident,'old':existing['name'],'new':r['name'],'reason':'Name or XML changed: retained prior template; resolve manually'})
                continue
            if existing and existing['xml_available'] and r['full_xml_available']!='yes':continue
            source=str(Path(next(p for p in paths if Path(p).name==r['source_file'])).resolve().relative_to(Path(__file__).resolve().parent))
            tags=[]
            if r['full_xml_available']=='yes':
                element=get_xml_element(Path(__file__).resolve().parent/source,ident)
                tags=[t.text or '' for t in element.findall('./tags/tag/text')]
            con.execute('''INSERT INTO bank_items(moodle_id,site,name,question_type,category,tags_json,source_path,content_hash,xml_available)
              VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(moodle_id) DO UPDATE SET name=excluded.name,question_type=excluded.question_type,
              category=excluded.category,tags_json=excluded.tags_json,source_path=excluded.source_path,content_hash=excluded.content_hash,
              xml_available=excluded.xml_available''',
              (ident,SITE,r['name'],r['question_type'],r['category'],json.dumps(tags),source,r['content_sha256'],int(r['full_xml_available']=='yes')))
            loaded+=1
    return {'loaded':loaded,'conflicts':conflicts,'found':len(raw)}

def bank_matches(con,pages):
    by_id=defaultdict(list);by_name=defaultdict(list)
    for p in pages:
        idval=plain(p,'Moodle ID').strip()
        if idval:by_id[idval].append(p)
        by_name[normal(title(p))].append(p)
    results=[]
    for bank in con.execute('SELECT * FROM bank_items ORDER BY name'):
        r=dict(bank);id_matches=by_id[r['moodle_id']]
        match=None;method='';status='unmatched'
        if len(id_matches)>1:status='ambiguous'
        elif len(id_matches)==1:
            match=id_matches[0];method='existing Moodle ID';status='confirmed'
        else:
            candidates=by_name[normal(r['name'])]
            if len(candidates)==1:
                match=candidates[0];method='unique exact name (review first)';status='candidate'
            elif len(candidates)>1:status='ambiguous'
        if match and r['question_type'] and type_name(match):
            if normalized_type(r['question_type'])!=normalized_type(type_name(match)):
                status='conflict';method='Question type mismatch'
        results.append({**r,'page_id':match['id'] if match else '', 'page_name':title(match) if match else '',
                        'status':status,'method':method})
    # Don't allow two bank IDs to claim same Notion page, even for name matches.
    claims=defaultdict(list)
    for x in results:
        if x['page_id']:claims[x['page_id']].append(x)
    for items in claims.values():
        if len(items)>1:
            for x in items:x['status']='ambiguous';x['method']='Multiple bank IDs claim same Notion page'
    return results

def plain(page,prop):
    value=page.get('properties',{}).get(prop) or {}
    return ''.join(t.get('plain_text',t.get('text',{}).get('content','')) for t in value.get('rich_text',[]) or [])

def status(page):
    return ((page.get('properties',{}).get('Workflow',{}).get('select') or {}).get('name') or '')

def rich(value):
    return {'rich_text':[{'type':'text','text':{'content':value[:1900]}}]}

def confirm_bank_links(client,con,entries):
    count=0;errors=[]
    for e in entries:
        if e['status'] not in ('candidate','confirmed'):continue
        try:
            pageid=e['page_id']
            if e['status']=='candidate':client.patch(pageid,{'Moodle ID':rich(e['moodle_id'])})
            # Link local history by uniquely matching name/type. Do not force mismatched stats identity.
            qrows=con.execute('SELECT id,name,question_type FROM questions WHERE name IS NOT NULL').fetchall()
            qrows=[q for q in qrows if normal(q['name'])==normal(e['name']) and normalized_type(q['question_type'])==normalized_type(e['question_type'])]
            with con:
                if len(qrows)==1:
                    con.execute('UPDATE questions SET notion_page_id=?,moodle_bank_id=? WHERE id=?',(pageid,e['moodle_id'],qrows[0]['id']))
            count+=1
        except Exception as exc:errors.append({'name':e['name'],'error':str(exc)})
    return count,errors

def _as_html(text):
    return '<p>'+html.escape(text).replace('\n','<br>')+'</p>'

def _set_text(el,path,new):
    node=el.find(path)
    if node is None:raise ValueError('Original XML missing '+path)
    node.text=new

def _source_answer(el):
    kind=el.get('type')
    ans=el.findall('answer')
    if kind in ('numerical','shortanswer'):
        full=[a for a in ans if float(a.get('fraction','0'))==100.0]
        return full[0].findtext('text','') if len(full)==1 else None
    if kind=='truefalse':
        full=[a for a in ans if float(a.get('fraction','0'))==100.0]
        return full[0].findtext('text','') if len(full)==1 else None
    return None

def build_one(element,page):
    """Only make changes when known safe. Unknown fields cannot silently override XML.
    For multichoice and other complex types, answer edits require an external dedicated editor;
    original grading is preserved and revisions with a changed nonempty Vastus are rejected.
    """
    import copy
    el=copy.deepcopy(element)
    body=plain(page,'Sisu').strip()
    answer=plain(page,'Vastus').strip()
    if not body:raise ValueError('Missing Sisu in Notion')
    oldtext=(el.findtext('./questiontext/text') or '')
    # Existing image references would be lost by replacing question text; do not attempt.
    if '<img' in oldtext.lower() or '@@PLUGINFILE@@' in oldtext or el.find('./questiontext/file') is not None:
        raise ValueError('Question text contains media; requires manual XML review')
    if el.get('type') in ('ddimageortext','ddmarker','ddwtos','gapselect','matching','cloze','calculated','calculatedmulti','calculatedsimple','ordering'):
        raise ValueError('Complex question type: export safely requires a type-specific editor')
    if el.get('type') not in ('numerical','shortanswer','truefalse','multichoice'):
        raise ValueError('Unsupported question type '+str(el.get('type')))
    question_node=el.find('questiontext')
    if question_node is None:raise ValueError('Missing question text')
    question_node.set('format','html')
    _set_text(el,'./questiontext/text',_as_html(body))
    if answer:
        orig=_source_answer(el)
        if el.get('type') in ('numerical','shortanswer'):
            if orig is None:raise ValueError('Multiple or no fully correct answers: cannot safely rewrite')
            full=[a for a in el.findall('answer') if float(a.get('fraction','0'))==100.0]
            if el.get('type')=='numerical':
                try:float(answer.replace(',','.'))
                except ValueError:raise ValueError('Numerical Vastus is not a number')
                if len(el.findall('answer'))!=1:raise ValueError('Multiple graded alternatives: manual export needed')
            _set_text(full[0],'text',answer)
        elif el.get('type')=='truefalse':
            normalized=normal(answer)
            if normalized not in ('true','false','tõene','väär','jah','ei'):
                raise ValueError('True/false Vastus must be true or false')
            value='true' if normalized in ('true','tõene','jah') else 'false'
            for a in el.findall('answer'):
                if normal(a.findtext('text')) not in ('true','false'):raise ValueError('Unrecognized true/false format')
                a.set('fraction','100' if normal(a.findtext('text'))==value else '0')
        else:
            # Do not risk changing partial credits or multiselect semantics.
            raise ValueError('Multichoice Vastus is nonempty: answer mapping requires manual review')
    return el

def make_xml(con,pages):
    """Prepare XML for existing ID-linked pages with Workflow=Ready; return successes/errors.
    Never changes Notion or database; the calling UI makes the explicit state transitions.
    """
    by_id={plain(p,'Moodle ID').strip():p for p in pages if plain(p,'Moodle ID').strip()}
    dup=defaultdict(int)
    for p in pages:
        key=plain(p,'Moodle ID').strip()
        if key:dup[key]+=1
    result=ET.Element('quiz');errors=[];success=[]
    for bank in con.execute('SELECT * FROM bank_items WHERE xml_available=1 ORDER BY category,name'):
        mid=bank['moodle_id'];page=by_id.get(mid)
        if not page or status(page)!='Ready':continue
        if dup[mid]>1:
            errors.append({'id':mid,'name':bank['name'],'error':'Duplicate Moodle ID in Notion'});continue
        try:
            source=Path(bank['source_path'])
            if not source.is_absolute():source=Path(__file__).resolve().parent/source
            if not source.exists():raise ValueError('Original XML missing; reupload question bank XML')
            el=build_one(get_xml_element(source,mid),page)
            # Preserve full category path from the original Moodle XML export.
            cat=ET.SubElement(result,'question',{'type':'category'})
            ET.SubElement(ET.SubElement(cat,'category'),'text').text=bank['category']
            result.append(el)
            success.append({'id':mid,'name':bank['name'],'page_id':page['id'],
                            'hash':hashlib.sha256(ET.tostring(el,encoding='utf-8')).hexdigest()})
        except Exception as exc:errors.append({'id':mid,'name':bank['name'],'error':str(exc)})
    if not success:return None,[],errors
    buffer=io.BytesIO();ET.ElementTree(result).write(buffer,encoding='utf-8',xml_declaration=True)
    return buffer.getvalue(),success,errors

def save_batch(con,payload,entries):
    h=hashlib.sha256(payload).hexdigest();file=f'approved_questions_{h[:12]}.xml'
    with con:
        con.execute('INSERT OR IGNORE INTO publication_batches(id,file_name,payload) VALUES(?,?,?)',(h,file,payload))
        for e in entries:
            con.execute('''INSERT OR IGNORE INTO publication_items(batch_id,moodle_id,notion_page_id,content_hash) VALUES(?,?,?,?)''',
                        (h,e['id'],e['page_id'],e['hash']))
    return h,file

def pending_batches(con):
    return [dict(row) for row in con.execute('''SELECT b.id,b.file_name,b.created_at,b.confirmed_at,COUNT(i.moodle_id) AS questions
       FROM publication_batches b JOIN publication_items i ON i.batch_id=b.id
       GROUP BY b.id ORDER BY b.created_at DESC''')]
