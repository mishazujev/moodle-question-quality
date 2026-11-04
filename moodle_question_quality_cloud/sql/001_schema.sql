-- Run once in Supabase Dashboard -> SQL Editor.
-- All tables have RLS enabled; access is only through the SERVER-SIDE service role.
-- NEVER expose your service-role key to a web browser, repository or student.
create extension if not exists pgcrypto;

create table if not exists public.questions (
 id uuid primary key default gen_random_uuid(),
 signature text not null unique,
 name text not null,
 question_type text not null,
 moodle_bank_id text,
 notion_page_id text,
 created_at timestamptz not null default now()
);
create unique index if not exists question_moodle_id_unique on public.questions(moodle_bank_id) where moodle_bank_id is not null;

create table if not exists public.assessments (
 assessment_key text primary key,
 quiz text not null,
 cohort text not null default 'Unknown',
 academic_year text not null default 'Unknown',
 stats_hash text not null unique,
 imported_at timestamptz not null default now()
);
create table if not exists public.observations (
 assessment_key text not null references public.assessments(assessment_key) on delete cascade,
 item_key text not null,
 question_id uuid references public.questions(id),
 q_position text not null,
 version_label text not null default 'unverified',
 name text not null,
 question_type text not null,
 attempts integer not null check(attempts>=0),
 facility double precision check(facility between -100 and 100),
 discrimination double precision check(discrimination between -100 and 100),
 match_status text not null,
 primary key(assessment_key,item_key)
);
create index if not exists observations_question_idx on public.observations(question_id);

create table if not exists public.bank_items (
 moodle_id text primary key,
 site text not null default 'moodle.mishazujev.com',
 name text not null,
 question_type text not null,
 category text not null default '',
 tags jsonb not null default '[]'::jsonb,
 xml_storage_path text,
 element_hash text,
 notion_page_id text,
 updated_at timestamptz not null default now()
);
create table if not exists public.publication_batches (
 batch_id text primary key,
 xml_storage_path text not null,
 created_at timestamptz not null default now(),
 confirmed_at timestamptz
);
create table if not exists public.publication_items (
 batch_id text not null references public.publication_batches(batch_id),
 moodle_id text not null,
 notion_page_id text not null,
 content_hash text not null,
 primary key(batch_id,moodle_id)
);

-- Atomic import. Subsequent snapshots of one assessment replace old observations,
-- while identical statistics from the complete and questionstats exports are deduplicated.
create or replace function public.import_assessment(
 p_assessment_key text, p_quiz text, p_cohort text, p_academic_year text,
 p_stats_hash text, p_rows jsonb
) returns jsonb language plpgsql set search_path=public as $$
declare row_value jsonb; qid uuid; sig text; duplicate_count integer; inserted integer := 0;
begin
 if coalesce(jsonb_typeof(p_rows),'') <> 'array' then raise exception 'rows must be an array';end if;
 if exists(select 1 from assessments where stats_hash=p_stats_hash) then
   return jsonb_build_object('result','unchanged_equivalent_statistics','rows',jsonb_array_length(p_rows));
 end if;
 if exists(select 1 from assessments where assessment_key=p_assessment_key and
          (cohort<>p_cohort or academic_year<>p_academic_year)) then
   raise exception 'Existing assessment has different cohort or year';
 end if;
 insert into assessments(assessment_key,quiz,cohort,academic_year,stats_hash)
 values(p_assessment_key,p_quiz,p_cohort,p_academic_year,p_stats_hash)
 on conflict(assessment_key) do update set quiz=excluded.quiz,stats_hash=excluded.stats_hash,imported_at=now();
 delete from observations where assessment_key=p_assessment_key;
 for row_value in select value from jsonb_array_elements(p_rows) loop
   sig := row_value->>'signature';
   select count(*) into duplicate_count from jsonb_array_elements(p_rows) v
   where v->>'signature'=sig;
   qid := null;
   if duplicate_count=1 then
     insert into questions(signature,name,question_type)
     values(sig,row_value->>'name',row_value->>'question_type')
     on conflict(signature) do nothing;
     select id into qid from questions where signature=sig;
   end if;
   insert into observations(assessment_key,item_key,question_id,q_position,version_label,
      name,question_type,attempts,facility,discrimination,match_status)
   values(p_assessment_key,row_value->>'item_key',qid,row_value->>'position','unverified',
      row_value->>'name',row_value->>'question_type',(row_value->>'attempts')::integer,
      (row_value->>'facility')::double precision,(row_value->>'discrimination')::double precision,
      case when duplicate_count>1 then 'ambiguous_duplicate_name' else 'provisional_name_type' end);
   inserted := inserted+1;
 end loop;
 return jsonb_build_object('result','imported','rows',inserted);
end $$;

create or replace view public.question_quality_view as
select q.id::text as id, q.name, q.question_type, q.signature,
  q.moodle_bank_id,q.notion_page_id,o.version_label,
  count(o.assessment_key)::integer as assessment_count,
  coalesce(sum(o.attempts),0)::integer as attempts,
  round((sum(o.attempts*o.facility) / nullif(sum(o.attempts) filter(where o.facility is not null),0))::numeric,2)::double precision as facility_percent,
  count(*) filter (where o.discrimination<0 and o.attempts>=10)::integer as negative_assessments,
  count(*) filter (where o.discrimination>=0 and o.discrimination<20 and o.attempts>=10)::integer as low_discrimination_assessments,
  string_agg(distinct a.academic_year,', ') as years,
  string_agg(distinct a.cohort,', ') as cohorts
from questions q left join observations o on o.question_id=q.id
 left join assessments a on a.assessment_key=o.assessment_key
 group by q.id,o.version_label;

alter table public.questions enable row level security;
alter table public.assessments enable row level security;
alter table public.observations enable row level security;
alter table public.bank_items enable row level security;
alter table public.publication_batches enable row level security;
alter table public.publication_items enable row level security;
revoke all on function public.import_assessment(text,text,text,text,text,jsonb) from public,anon,authenticated;
grant execute on function public.import_assessment(text,text,text,text,text,jsonb) to service_role;

-- Private bucket (for the original question-bank XML and exported XML files).
insert into storage.buckets(id,name,public)
values('moodle-question-xml','moodle-question-xml',false)
on conflict (id) do update set public=false;

-- Prevent views from being queried by low-privilege API roles.
revoke all on public.question_quality_view from public, anon, authenticated;
grant select on public.question_quality_view to service_role;
