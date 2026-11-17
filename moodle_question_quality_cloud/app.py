"""Hosted Moodle assessment import and question-quality workbench."""
from __future__ import annotations
import hashlib
import hmac
import streamlit as st
from assessment_ui import render_assessment_import
from cloud_backend import Cloud,parse_csv,row_summary,report_csv
from cloud_workflow import bank_import,match_bank,confirm_links,build_batch,save_batch,confirm_published
from notion_bridge import Notion,plan as notion_plan,title
from workflow import plain,status

st.set_page_config(page_title='Moodle Question Quality',layout='wide')
st.title('Moodle Question Quality')
st.caption('Assessment import · Complete statistics and grades')


def config(name):
    try:return str(st.secrets.get(name,'')).strip()
    except (FileNotFoundError,KeyError):return ''

required=['APP_PASSWORD','SUPABASE_URL','SUPABASE_SECRET_KEY','NOTION_TOKEN']
missing=[name for name in required if not config(name)]
if missing:
    st.error('Server configuration incomplete: '+', '.join(missing))
    st.info('Configure these keys in Streamlit Cloud → App settings → Secrets. Do not paste keys into GitHub.')
    st.stop()

if not st.session_state.get('authenticated'):
    with st.form('login'):
        pw=st.text_input('Application password',type='password')
        login=st.form_submit_button('Sign in')
    if login:
        if hmac.compare_digest(pw,config('APP_PASSWORD')):
            st.session_state['authenticated']=True
            st.rerun()
        else:st.error('Invalid password')
    st.stop()

if st.sidebar.button('Sign out'):
    st.session_state.clear();st.rerun()

cloud=Cloud(config('SUPABASE_URL'),config('SUPABASE_SECRET_KEY'))
notion=Notion(config('NOTION_TOKEN'))

@st.cache_data(ttl=90,show_spinner=False)
def read_reports():
    return cloud.assessments(),[row_summary(r) for r in cloud.summaries()]


def load_notion():
    with st.spinner('Reading Küsimused from Notion…'):
        return notion.all_pages()


def fetch_pages():
    if 'notion_pages' not in st.session_state:
        st.session_state['notion_pages']=load_notion()
    return st.session_state['notion_pages']


def refresh_pages():
    st.session_state['notion_pages']=load_notion()


def render_statistics_only():
    uploader=st.file_uploader('Upload questionstats.csv or completestats.csv',type=['csv'])
    pasted=st.text_area('Or paste the complete CSV text',height=120)
    raw=pasted.encode('utf-8-sig') if pasted.strip() else (uploader.getvalue() if uploader else None)
    rows=[];stats_hash=None
    if raw:
        try:
            rows,stats_hash,report_type=parse_csv(raw)
            st.info(f'{len(rows)} individual question-statistics rows detected. Complete reports and questionstats exports deduplicate by statistical content.')
        except Exception as e:st.error('Invalid statistics export: '+str(e))
    assessments,_=read_reports()
    already=next((a for a in assessments if a['stats_hash']==stats_hash),None) if stats_hash else None
    if already:st.success('These statistics are already stored under '+already['assessment_key'])
    with st.expander('Assessment metadata / updated export',expanded=False):
        mode=st.radio('Mode',['New assessment','Replace snapshot of existing assessment'],horizontal=True)
        existing=st.selectbox('Existing assessment', [a['assessment_key'] for a in assessments]) if mode.startswith('Replace') and assessments else None
        label=st.text_input('Assessment label',value='Moodle quiz')
        cohort=st.text_input('Class (optional)',value='Unknown')
        year=st.text_input('Academic year (optional)',value='Unknown')
    if st.button('Import and evaluate',disabled=not rows or bool(already),type='primary'):
        if mode.startswith('Replace') and not existing:
            st.error('Select an existing assessment to replace')
        else:
            key=existing if mode.startswith('Replace') else 'auto:'+hashlib.sha256(raw).hexdigest()[:24]
            prior=next((a for a in assessments if a['assessment_key']==existing),None)
            try:
                outcome=cloud.rpc('import_assessment',{'p_assessment_key':key,'p_quiz':label,
                    'p_cohort':prior['cohort'] if prior else cohort or 'Unknown',
                    'p_academic_year':prior['academic_year'] if prior else year or 'Unknown',
                    'p_stats_hash':stats_hash,'p_rows':rows})
                st.success(f"Import result: {outcome}")
                read_reports.clear();st.rerun()
            except Exception as e:st.error(str(e))


def render_migration():
    st.subheader('Import old Windows history')
    st.caption('Upload data/quality.sqlite from v3.1 once. Only aggregate question statistics and confirmed identity links are migrated; student responses are not used.')
    old_db=st.file_uploader('Existing quality.sqlite',type=['sqlite','db'],key='old_sqlite')
    migration_ok=st.checkbox('I confirm this is my previous Moodle Question Quality SQLite database')
    if st.button('Migrate old local history to Supabase',disabled=not old_db or not migration_ok):
        try:
            import tempfile
            from pathlib import Path
            from migrate_sqlite import migrate
            if len(old_db.getvalue())>40_000_000:raise ValueError('SQLite file is too large')
            with tempfile.TemporaryDirectory() as temp:
                filename=Path(temp)/'quality.sqlite'
                filename.write_bytes(old_db.getvalue())
                outcomes=migrate(str(filename),cloud)
            st.success(f'Processed {len(outcomes)} historical assessments. Existing duplicate statistics were skipped.')
            read_reports.clear();st.rerun()
        except Exception as e:st.error('Migration failed: '+str(e))

def render_history():
    assessments,quality_rows=read_reports()
    st.subheader('Historical question quality')
    st.caption('Quality is a screening signal. Questions with fewer than 10 attempts remain More data.')
    if quality_rows:
        statuses=sorted(set(r['quality'] for r in quality_rows))
        chosen=st.multiselect('Show',statuses,default=statuses)
        cols=['quality','name','question_type','attempts','facility_percent','assessment_count','negative_assessments','years','cohorts','moodle_bank_id']
        shown=[{k:r.get(k) for k in cols} for r in quality_rows if r['quality'] in chosen]
        st.dataframe(shown,hide_index=True,use_container_width=True)
        st.download_button('Download review CSV',report_csv(shown),'question_review.csv','text/csv')
    else:st.info('No imported statistics yet.')
    with st.expander('Assessment history'):
        st.dataframe(assessments,hide_index=True,use_container_width=True)

def render_bank():
    st.subheader('Register Moodle question-bank exports')
    st.caption('Moodle XML preferred. HTML contains IDs but cannot serve as a safe revision template. Original XML is kept in private Supabase Storage.')
    exports=st.file_uploader('Select one or more XML/HTML files',type=['xml','html','htm'],accept_multiple_files=True)
    site=st.text_input('Moodle site identifier',value='moodle.mishazujev.com')
    if st.button('Save question-bank exports',disabled=not exports):
        try:
            findings=bank_import(cloud,[(f.name,f.getvalue()) for f in exports],site)
            st.dataframe(findings,hide_index=True,use_container_width=True)
        except Exception as e:st.error(str(e))
    if st.button('Refresh Notion records'):
        try:refresh_pages();st.success(f"Read {len(fetch_pages())} Notion pages")
        except Exception as e:st.error(str(e))

    st.subheader('Link stable Moodle IDs to Notion')
    st.caption('First register XML/HTML exports. Only uniquely matched IDs and reviewed exact-name candidates can be confirmed.')
    bank=cloud.bank_items()
    st.write(f'{len(bank)} bank entries registered.')
    if st.button('Preview identity links',disabled=not bank):
        try:
            links=match_bank(bank,fetch_pages())
            st.session_state['links']=links
        except Exception as e:st.error(str(e))
    if 'links' in st.session_state:
        links=st.session_state['links']
        st.dataframe([{k:r.get(k) for k in ('moodle_id','name','page_name','status')} for r in links],hide_index=True,use_container_width=True)
        st.download_button('Download identity review',report_csv(links),'identity_links.csv','text/csv')
        okay=st.checkbox('I verified all proposed identity matches before writing IDs')
        if st.button('Confirm verified identity links',disabled=not okay):
            total,errors=confirm_links(cloud,notion,links)
            st.success(f'{total} verified links processed')
            if errors:st.dataframe(errors)
            st.session_state.pop('links',None)
            read_reports.clear();st.session_state.pop('notion_pages',None)
    with st.expander('Manually link a renamed question'):
        if bank:
            chosen=st.selectbox('Moodle ID',bank,format_func=lambda b:f"{b['moodle_id']} — {b['name']}")
            search=st.text_input('Search Notion question name')
            if search and len(search)>2:
                try:
                    matched=[p for p in fetch_pages() if search.casefold() in title(p).casefold()][:30]
                    if matched:
                        target=st.selectbox('Matching Notion page',matched,format_func=lambda p:title(p)+' ('+p['id'][:8]+')')
                        if st.button('Confirm this mapping'):
                            existing=plain(target,'Moodle ID')
                            if existing and existing!=chosen['moodle_id']:raise ValueError('Notion page already has a different Moodle ID')
                            other=cloud.select('bank_items','select=moodle_id&notion_page_id=eq.'+target['id'])
                            if any(b['moodle_id']!=chosen['moodle_id'] for b in other):raise ValueError('Another Moodle ID already linked to this page')
                            notion.patch(target['id'],{'Moodle ID':{'rich_text':[{'type':'text','text':{'content':chosen['moodle_id']}}]}})
                            cloud.update('bank_items','moodle_id=eq.'+chosen['moodle_id'],{'notion_page_id':target['id']})
                            st.success('Verified mapping saved. Statistics with changed names still require explicit history reconciliation.')
                            st.session_state.pop('notion_pages',None)
                    else:st.info('No matching Notion titles')
                except Exception as e:st.error(str(e))

def render_quality_sync():
    _,quality_rows=read_reports()
    st.subheader('Update Notion Quality / Fix statuses')
    st.caption('Only stable Moodle-ID-linked questions. Does not edit Sisu, Vastus, Points, tags, or an active teacher editorial workflow.')
    if st.button('Preview Notion quality changes'):
        try:
            pages=fetch_pages()
            eligible=[r for r in quality_rows if r.get('moodle_bank_id') and r.get('notion_page_id') and r.get('version_label')=='unverified']
            proposed=notion_plan(eligible,pages)
            st.session_state['notion_changes']=[x for x in proposed if x.get('reason')!='unique exact name']
        except Exception as e:st.error(str(e))
    if 'notion_changes' in st.session_state:
        changes=st.session_state['notion_changes']
        st.write(f"{sum(x['status']=='update' for x in changes)} changes · {sum(x['status']=='skip' for x in changes)} skipped")
        st.dataframe([{k:e.get(k) for k in ('name','quality','attempts','facility_percent','status','reason')} for e in changes],hide_index=True,use_container_width=True)
        approved=st.checkbox('Approve these existing-page quality/status changes')
        if st.button('Sync approved changes to Notion',disabled=not approved):
            errors=[];done=0
            for ch in changes:
                if ch['status']!='update':continue
                try:notion.patch(ch['target_page_id'],ch['updates']);done+=1
                except Exception as e:errors.append({'name':ch['name'],'error':str(e)})
            st.success(f'{done} Notion records updated')
            if errors:st.dataframe(errors)
            st.session_state.pop('notion_changes',None)
            st.session_state.pop('notion_pages',None)

def render_publication():
    st.subheader('Export approved revisions to Moodle XML')
    st.caption('Edit questions in Notion, set Workflow = Ready, then validate the XML. Supported: numerical, shortanswer, truefalse and limited multichoice. Complex/media types are skipped with a reason.')
    if st.button('Prepare XML from Ready questions'):
        try:
            refresh_pages()
            xml,entries,errors=build_batch(cloud,fetch_pages())
            st.session_state['export_errors']=errors
            if xml:
                batch=save_batch(cloud,xml,entries)
                st.session_state['prepared_batch']=batch
                st.success(f'{len(entries)} questions prepared, batch {batch[:12]}')
            else:st.info('No XML could be generated from currently Ready questions.')
        except Exception as e:st.error(str(e))
    if st.session_state.get('export_errors'):
        st.dataframe(st.session_state['export_errors'],hide_index=True,use_container_width=True)
    batches=cloud.select('publication_batches','select=*&order=created_at.desc&limit=100')
    if batches:
        bid=st.selectbox('Publication batch',batches,format_func=lambda b:b['batch_id'][:12]+' · '+str(b.get('created_at')))
        try:payload=cloud.download(bid['xml_storage_path'])
        except Exception as e:st.error(str(e));payload=None
        if payload:
            st.download_button('Download approved Moodle XML',payload,'approved_questions_'+bid['batch_id'][:12]+'.xml','application/xml')
        if st.button('Mark this batch Awaiting import'):
            try:
                items=cloud.select('publication_items','select=*&batch_id=eq.'+bid['batch_id'])
                pages={p['id']:p for p in load_notion()}
                for item in items:
                    p=pages.get(item['notion_page_id'])
                    if not p or status(p) not in ('Ready','Awaiting import'):
                        raise ValueError('Question no longer Ready; revalidate XML before proceeding')
                for item in items:
                    notion.patch(item['notion_page_id'],{'Workflow':{'select':{'name':'Awaiting import'}}})
                st.success('Marked Awaiting import. Import downloaded XML in Moodle, then confirm below.')
                st.session_state.pop('notion_pages',None)
            except Exception as e:st.error(str(e))
        confirmed=st.checkbox('I manually imported this exact XML batch into Moodle and verified the questions')
        if st.button('Confirm Published',disabled=not confirmed or bool(bid.get('confirmed_at'))):
            try:
                n=confirm_published(cloud,notion,bid['batch_id'],load_notion())
                st.success(f'{n} Notion pages marked Published')
                st.session_state.pop('notion_pages',None)
            except Exception as e:st.error(str(e))
    else:st.info('No XML batches prepared yet.')

tools = {
    'Assessment history': render_history,
    'Question bank and ID links': render_bank,
    'Sync question quality only': render_quality_sync,
    'Publish revisions to Moodle': render_publication,
    'Statistics only': render_statistics_only,
    'Import old Windows history': render_migration,
}

with st.sidebar.expander('Other tools', expanded=False):
    tool = st.radio('Page', ['Import assessment', *tools])

if tool == 'Import assessment':
    st.subheader('Import Moodle assessment')
    render_assessment_import(cloud, notion, read_reports.clear)
else:
    tools[tool]()
