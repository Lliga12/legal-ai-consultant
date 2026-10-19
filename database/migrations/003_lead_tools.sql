-- Этап 7. После schema.sql и 002_memory.sql. Повторное применение допустимо.
begin;

create table if not exists public.lead_requests (
    session_id uuid primary key references public.chat_sessions(id) on delete cascade,
    request_id uuid not null unique default gen_random_uuid(),
    name text not null check (char_length(btrim(name)) between 1 and 100),
    phone text not null check (phone ~ '^\+7[0-9]{10}$'),
    legal_topic text not null check (char_length(btrim(legal_topic)) between 1 and 200),
    description text not null check (char_length(btrim(description)) between 1 and 2000),
    consent_version text not null check (char_length(btrim(consent_version)) between 1 and 100),
    consent_at timestamptz,
    lead_id uuid references public.leads(id) on delete set null,
    created_at timestamptz not null default now()
);
alter table public.lead_requests enable row level security;
revoke all on public.lead_requests from public, anon, authenticated;
grant select, insert, update, delete on public.lead_requests to service_role;

create or replace function public.legal_ai_lead_request(
    p_session_id uuid, p_token_hash text, p_request_id uuid)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare d public.lead_requests;
declare s public.chat_sessions;
begin
    s := public.legal_ai_lock_session(p_session_id, p_token_hash);
    select * into d from public.lead_requests
        where session_id = p_session_id and request_id = p_request_id;
    if not found then raise exception 'request unavailable' using errcode = 'P0002'; end if;
    return to_jsonb(d) || jsonb_build_object('session_version', s.version);
end;
$$;

create or replace function public.legal_ai_prepare_lead(
    p_session_id uuid, p_token_hash text, p_expected_version bigint,
    p_name text, p_phone text, p_legal_topic text, p_description text, p_consent_version text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare s public.chat_sessions;
declare d public.lead_requests;
begin
    s := public.legal_ai_lock_session(p_session_id, p_token_hash);
    if nullif(btrim(s.state->>'history_notice_version'), '') is null
        or nullif(btrim(s.state->>'history_consent_at'), '') is null then
        raise exception 'history consent required' using errcode = '22023';
    end if;
    select * into d from public.lead_requests where session_id = p_session_id;
    if found and d.name = p_name and d.phone = p_phone and d.legal_topic = p_legal_topic
        and d.description = p_description and d.consent_version = p_consent_version then
        return public.legal_ai_lead_request(p_session_id, p_token_hash, d.request_id);
    end if;
    if s.version is distinct from p_expected_version then
        raise exception 'stale draft version' using errcode = '40001';
    end if;
    -- Новый/изменённый черновик имеет новый ключ и требует нового согласия.
    delete from public.lead_requests where session_id = p_session_id;
    insert into public.lead_requests(session_id,name,phone,legal_topic,description,consent_version)
        values (p_session_id,p_name,p_phone,p_legal_topic,p_description,p_consent_version)
        returning * into d;
    update public.chat_sessions set version = version + 1, updated_at = now() where id = p_session_id;
    return public.legal_ai_lead_request(p_session_id, p_token_hash, d.request_id);
end;
$$;

create or replace function public.legal_ai_lead_consent(
    p_session_id uuid, p_token_hash text, p_request_id uuid,
    p_expected_version bigint, p_consent boolean)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare s public.chat_sessions;
begin
    s := public.legal_ai_lock_session(p_session_id, p_token_hash);
    if s.version is distinct from p_expected_version then
        raise exception 'stale consent event' using errcode = '40001';
    end if;
    if p_consent is null then raise exception 'explicit consent required' using errcode = '22023'; end if;
    update public.lead_requests set consent_at = case when p_consent then now() else null end
        where session_id = p_session_id and request_id = p_request_id;
    if not found then raise exception 'request unavailable' using errcode = 'P0002'; end if;
    update public.chat_sessions set version = version + 1, updated_at = now() where id = p_session_id;
    return public.legal_ai_lead_request(p_session_id, p_token_hash, p_request_id);
end;
$$;

create or replace function public.legal_ai_submit_lead(
    p_session_id uuid, p_token_hash text, p_request_id uuid, p_name text, p_phone text,
    p_legal_topic text, p_description text, p_consent boolean, p_consent_version text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare d public.lead_requests;
declare result jsonb;
declare notification_status text;
begin
    perform public.legal_ai_lock_session(p_session_id, p_token_hash);
    select * into d from public.lead_requests
        where session_id = p_session_id and request_id = p_request_id;
    if not found then raise exception 'request unavailable' using errcode = 'P0002'; end if;
    if p_consent is distinct from true or d.consent_at is null then
        raise exception 'confirmed consent required' using errcode = '22023';
    end if;
    if d.name is distinct from p_name or d.phone is distinct from p_phone
        or d.legal_topic is distinct from p_legal_topic or d.description is distinct from p_description
        or d.consent_version is distinct from p_consent_version then
        raise exception 'draft payload conflict' using errcode = '23505';
    end if;
    result := public.legal_ai_create_lead(p_session_id,p_token_hash,p_request_id,
        p_name,p_phone,p_legal_topic,p_description,true,p_consent_version);
    update public.lead_requests set lead_id = (result->>'id')::uuid where session_id = p_session_id;
    if not (result->>'duplicate')::boolean then
        update public.leads set consent_at = d.consent_at where id = (result->>'id')::uuid;
        update public.chat_sessions set version = version + 1, updated_at = now() where id = p_session_id;
    end if;
    select status into notification_status from public.notification_logs where lead_id = (result->>'id')::uuid;
    if not found then raise exception 'missing outbox' using errcode = 'P0001'; end if;
    return result || jsonb_build_object('notification_status',notification_status);
end;
$$;

-- Очистка контекста инвалидирует черновик, его согласие и старые попытки отправки.
create or replace function public.legal_ai_clear_history(p_session_id uuid, p_token_hash text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare n integer;
begin
    perform public.legal_ai_lock_session(p_session_id, p_token_hash);
    delete from public.lead_requests where session_id = p_session_id;
    delete from public.chat_messages where session_id = p_session_id;
    get diagnostics n = row_count;
    update public.chat_sessions set state = '{}'::jsonb, version = version + 1, updated_at = now()
        where id = p_session_id;
    return jsonb_build_object('deleted_messages', n);
end;
$$;

create or replace function public.legal_ai_delete_leads(p_session_id uuid, p_token_hash text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare n integer;
begin
    perform public.legal_ai_lock_session(p_session_id, p_token_hash);
    delete from public.lead_requests where session_id = p_session_id;
    delete from public.leads where session_id = p_session_id;
    get diagnostics n = row_count;
    update public.chat_sessions set version = version + 1, updated_at = now() where id = p_session_id;
    return jsonb_build_object('deleted_leads', n);
end;
$$;

create or replace function public.legal_ai_tools_health()
returns jsonb language sql security invoker set search_path = '' as $$
    select jsonb_build_object('tools_version',1,'rpc_ready',
        (select count(*) = 5 and not bool_or(p.prosecdef)
            and not bool_or(pg_catalog.has_function_privilege('anon',p.oid,'EXECUTE'))
            and not bool_or(pg_catalog.has_function_privilege('authenticated',p.oid,'EXECUTE'))
         from pg_catalog.pg_proc p join pg_catalog.pg_namespace n on n.oid = p.pronamespace
         where n.nspname='public' and p.proname in ('legal_ai_lead_request','legal_ai_prepare_lead',
            'legal_ai_lead_consent','legal_ai_submit_lead','legal_ai_tools_health'))
        and (select c.relrowsecurity from pg_catalog.pg_class c
            where c.oid='public.lead_requests'::regclass)
        and not pg_catalog.has_table_privilege('anon','public.lead_requests','SELECT,INSERT,UPDATE,DELETE')
        and not pg_catalog.has_table_privilege('authenticated','public.lead_requests','SELECT,INSERT,UPDATE,DELETE'));
$$;

revoke all on function public.legal_ai_lead_request(uuid,text,uuid),
    public.legal_ai_prepare_lead(uuid,text,bigint,text,text,text,text,text),
    public.legal_ai_lead_consent(uuid,text,uuid,bigint,boolean),
    public.legal_ai_submit_lead(uuid,text,uuid,text,text,text,text,boolean,text),
    public.legal_ai_tools_health() from public, anon, authenticated;
grant execute on function public.legal_ai_lead_request(uuid,text,uuid),
    public.legal_ai_prepare_lead(uuid,text,bigint,text,text,text,text,text),
    public.legal_ai_lead_consent(uuid,text,uuid,bigint,boolean),
    public.legal_ai_submit_lead(uuid,text,uuid,text,text,text,text,boolean,text),
    public.legal_ai_tools_health() to service_role;
notify pgrst,'reload schema';
commit;
