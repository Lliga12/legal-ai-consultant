-- ONLY for the new empty legal-ai-consultant Supabase project.
-- All four existing migrations run in one transaction.
begin;

-- SOURCE: database/schema.sql
-- Этап 5, схема v1. Только новый выделенный проект/БД. Выполнить от postgres.
-- Не миграция существующих таблиц. Ошибка откатывает весь скрипт.

create table public.chat_sessions (
    id uuid primary key,
    token_hash text not null check (token_hash ~ '^[0-9a-f]{64}$'),
    state jsonb not null default '{}'::jsonb
        check (jsonb_typeof(state) = 'object' and octet_length(state::text) <= 8000),
    version bigint not null default 0 check (version >= 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table public.chat_messages (
    id uuid primary key,
    session_id uuid not null references public.chat_sessions(id) on delete cascade,
    sequence bigint generated always as identity unique,
    role text not null check (role in ('user', 'assistant')),
    content text not null check (char_length(btrim(content)) between 1 and 16000),
    created_at timestamptz not null default now()
);
create index chat_messages_session_order on public.chat_messages(session_id, sequence desc);

create table public.leads (
    id uuid primary key default gen_random_uuid(),
    name text not null check (char_length(btrim(name)) between 1 and 100),
    phone text not null check (phone ~ '^\+7[0-9]{10}$'),
    legal_topic text not null check (char_length(btrim(legal_topic)) between 1 and 200),
    description text not null check (char_length(btrim(description)) between 1 and 2000),
    status text not null default 'new' check (status in ('new', 'contacted', 'closed')),
    consent_at timestamptz not null default now(),
    consent_version text not null check (char_length(btrim(consent_version)) between 1 and 100),
    session_id uuid not null references public.chat_sessions(id) on delete cascade,
    idempotency_key uuid not null,
    created_at timestamptz not null default now(),
    unique (session_id, idempotency_key)
);

create table public.notification_logs (
    id uuid primary key default gen_random_uuid(),
    lead_id uuid not null unique references public.leads(id) on delete cascade,
    status text not null default 'pending' check (status in ('pending', 'sending', 'sent', 'failed')),
    error_message text check (char_length(error_message) <= 500),
    attempts integer not null default 0 check (attempts >= 0),
    next_attempt_at timestamptz not null default now(),
    locked_until timestamptz,
    sent_at timestamptz,
    created_at timestamptz not null default now()
);
create index notification_logs_pending on public.notification_logs(next_attempt_at)
    where status in ('pending', 'failed');

alter table public.chat_sessions enable row level security;
alter table public.chat_messages enable row level security;
alter table public.leads enable row level security;
alter table public.notification_logs enable row level security;
-- Нет разрешающих политик для браузерных ролей: доступ только с сервера.
revoke all on public.chat_sessions, public.chat_messages, public.leads,
    public.notification_logs from public, anon, authenticated;
revoke all on sequence public.chat_messages_sequence_seq from public, anon, authenticated;
grant usage on schema public to service_role;
grant select, insert, update, delete on public.chat_sessions, public.chat_messages,
    public.leads, public.notification_logs to service_role;
grant usage, select on sequence public.chat_messages_sequence_seq to service_role;

-- SECURITY INVOKER: функции не повышают права вызывающего.
-- Функции с префиксом legal_ai_ доступны только service_role.
create function public.legal_ai_lock_session(p_session_id uuid, p_token_hash text)
returns public.chat_sessions language plpgsql security invoker set search_path = '' as $$
declare s public.chat_sessions;
begin
    select * into s from public.chat_sessions
        where id = p_session_id and token_hash = p_token_hash for update;
    if not found then
        raise exception 'session unavailable' using errcode = 'P0002';
    end if;
    return s;
end;
$$;

create function public.legal_ai_get_session(p_session_id uuid, p_token_hash text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare s public.chat_sessions;
begin
    s := public.legal_ai_lock_session(p_session_id, p_token_hash);
    return to_jsonb(s) - 'token_hash';
end;
$$;

create function public.legal_ai_create_session(p_session_id uuid, p_token_hash text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
begin
    insert into public.chat_sessions(id, token_hash) values (p_session_id, p_token_hash)
        on conflict (id) do nothing;
    return public.legal_ai_get_session(p_session_id, p_token_hash);
end;
$$;

create function public.legal_ai_append_message(
    p_session_id uuid, p_token_hash text, p_message_id uuid, p_role text, p_content text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare m public.chat_messages;
begin
    perform public.legal_ai_lock_session(p_session_id, p_token_hash);
    insert into public.chat_messages(id, session_id, role, content)
        values (p_message_id, p_session_id, p_role, p_content)
        on conflict (id) do nothing returning * into m;
    if not found then
        select * into m from public.chat_messages where id = p_message_id;
        if m.session_id is distinct from p_session_id or m.role is distinct from p_role
            or m.content is distinct from p_content then
            raise exception 'idempotency conflict' using errcode = '23505';
        end if;
    else
        update public.chat_sessions set updated_at = now(), version = version + 1
            where id = p_session_id;
    end if;
    return to_jsonb(m);
end;
$$;

create function public.legal_ai_messages(p_session_id uuid, p_token_hash text, p_limit integer default 40)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare result jsonb;
begin
    perform public.legal_ai_lock_session(p_session_id, p_token_hash);
    if p_limit is null or p_limit not between 1 and 100 then
        raise exception 'invalid history limit' using errcode = '22023';
    end if;
    select coalesce(jsonb_agg(to_jsonb(m) order by m.sequence), '[]'::jsonb) into result
        from (select * from public.chat_messages where session_id = p_session_id
              order by sequence desc limit p_limit) m;
    return result;
end;
$$;

create function public.legal_ai_update_state(
    p_session_id uuid, p_token_hash text, p_expected_version bigint, p_state jsonb)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare s public.chat_sessions;
begin
    s := public.legal_ai_lock_session(p_session_id, p_token_hash);
    if s.version is distinct from p_expected_version then
        raise exception 'state conflict' using errcode = '40001';
    end if;
    update public.chat_sessions set state = p_state, version = version + 1, updated_at = now()
        where id = p_session_id returning * into s;
    return to_jsonb(s) - 'token_hash';
end;
$$;

create function public.legal_ai_create_lead(
    p_session_id uuid, p_token_hash text, p_idempotency_key uuid,
    p_name text, p_phone text, p_legal_topic text, p_description text,
    p_consent boolean, p_consent_version text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare l public.leads;
declare duplicate boolean := false;
begin
    perform public.legal_ai_lock_session(p_session_id, p_token_hash);
    if p_consent is distinct from true then
        raise exception 'explicit consent required' using errcode = '22023';
    end if;
    select * into l from public.leads
        where session_id = p_session_id and idempotency_key = p_idempotency_key;
    if found then
        duplicate := true;
        if l.name is distinct from p_name or l.phone is distinct from p_phone
            or l.legal_topic is distinct from p_legal_topic or l.description is distinct from p_description
            or l.consent_version is distinct from p_consent_version then
            raise exception 'idempotency conflict' using errcode = '23505';
        end if;
    else
        insert into public.leads(session_id, idempotency_key, name, phone, legal_topic,
            description, consent_version)
            values (p_session_id, p_idempotency_key, p_name, p_phone, p_legal_topic,
                    p_description, p_consent_version) returning * into l;
        -- Заявка и задача уведомления фиксируются одной транзакцией RPC.
        insert into public.notification_logs(lead_id) values (l.id);
    end if;
    return jsonb_build_object('id', l.id, 'created_at', l.created_at, 'duplicate', duplicate);
end;
$$;

create function public.legal_ai_clear_history(p_session_id uuid, p_token_hash text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare n integer;
begin
    perform public.legal_ai_lock_session(p_session_id, p_token_hash);
    delete from public.chat_messages where session_id = p_session_id;
    get diagnostics n = row_count;
    update public.chat_sessions set state = '{}'::jsonb, version = version + 1, updated_at = now()
        where id = p_session_id;
    return jsonb_build_object('deleted_messages', n);
end;
$$;

create function public.legal_ai_delete_leads(p_session_id uuid, p_token_hash text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare n integer;
begin
    perform public.legal_ai_lock_session(p_session_id, p_token_hash);
    delete from public.leads where session_id = p_session_id;
    get diagnostics n = row_count;
    return jsonb_build_object('deleted_leads', n);
end;
$$;

create function public.legal_ai_delete_session(p_session_id uuid, p_token_hash text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
begin
    perform public.legal_ai_lock_session(p_session_id, p_token_hash);
    delete from public.chat_sessions where id = p_session_id;
    return jsonb_build_object('deleted', true);
end;
$$;

create function public.legal_ai_health()
returns jsonb language sql security invoker set search_path = '' as $$
    select jsonb_build_object(
        'schema_version', 1,
        'rls_enabled', (select count(*) = 4 and bool_and(c.relrowsecurity)
            from pg_catalog.pg_class c join pg_catalog.pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'public' and c.relname in
                ('chat_sessions', 'chat_messages', 'leads', 'notification_logs')),
        'public_access', (
            exists (select 1 from (values ('chat_sessions'), ('chat_messages'),
                ('leads'), ('notification_logs')) t(name)
                cross join (values ('anon'), ('authenticated')) r(name)
                cross join (values ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE')) p(name)
                where pg_catalog.has_table_privilege(r.name, 'public.' || t.name, p.name))
            or exists (select 1 from pg_catalog.pg_proc p
                join pg_catalog.pg_namespace n on n.oid = p.pronamespace
                where n.nspname = 'public' and p.proname like 'legal_ai_%'
                and (pg_catalog.has_function_privilege('anon', p.oid, 'EXECUTE')
                     or pg_catalog.has_function_privilege('authenticated', p.oid, 'EXECUTE')))
        )
    );
$$;

-- Default EXECUTE PUBLIC необходимо отозвать у каждой новой функции.
do $$
declare f record;
begin
    for f in select p.oid::regprocedure as signature from pg_catalog.pg_proc p
        join pg_catalog.pg_namespace n on n.oid = p.pronamespace
        where n.nspname = 'public' and p.proname like 'legal_ai_%'
    loop
        execute format('revoke all on function %s from public, anon, authenticated', f.signature);
        execute format('grant execute on function %s to service_role', f.signature);
    end loop;
end;
$$;

notify pgrst, 'reload schema';


-- SOURCE: database/migrations/002_memory.sql
-- Этап 6. Применить после schema.sql. Идемпотентная добавочная миграция.

create or replace function public.legal_ai_memory_snapshot(
    p_session_id uuid, p_token_hash text, p_limit integer default 40)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare s jsonb;
begin
    -- Все существующие операции также берут блокировку этой строки.
    s := public.legal_ai_get_session(p_session_id, p_token_hash);
    return jsonb_build_object('session', s,
        'messages', public.legal_ai_messages(p_session_id, p_token_hash, p_limit));
end;
$$;

create or replace function public.legal_ai_memory_append(
    p_session_id uuid, p_token_hash text, p_message_id uuid,
    p_role text, p_content text, p_expected_version bigint, p_limit integer default 40)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare s public.chat_sessions;
declare m public.chat_messages;
begin
    s := public.legal_ai_lock_session(p_session_id, p_token_hash);
    -- Согласие задаёт сервер после явного действия пользователя, не модель.
    if nullif(btrim(s.state->>'history_notice_version'), '') is null
        or nullif(btrim(s.state->>'history_consent_at'), '') is null then
        raise exception 'history consent required' using errcode = '22023';
    end if;
    select * into m from public.chat_messages where id = p_message_id;
    if found then
        -- Повтор того же сообщения допустим даже после повышения версии.
        if m.session_id is distinct from p_session_id or m.role is distinct from p_role
            or m.content is distinct from p_content then
            raise exception 'message idempotency conflict' using errcode = '23505';
        end if;
    else
        if s.version is distinct from p_expected_version then
            raise exception 'stale memory version' using errcode = '40001';
        end if;
        perform public.legal_ai_append_message(
            p_session_id, p_token_hash, p_message_id, p_role, p_content);
    end if;
    return public.legal_ai_memory_snapshot(p_session_id, p_token_hash, p_limit);
end;
$$;

create or replace function public.legal_ai_memory_health()
returns jsonb language sql security invoker set search_path = '' as $$
    select jsonb_build_object('memory_version', 1, 'rpc_ready',
        (select count(*) = 3 and not bool_or(p.prosecdef)
            and not bool_or(pg_catalog.has_function_privilege('anon', p.oid, 'EXECUTE'))
            and not bool_or(pg_catalog.has_function_privilege('authenticated', p.oid, 'EXECUTE'))
         from pg_catalog.pg_proc p join pg_catalog.pg_namespace n on n.oid = p.pronamespace
         where n.nspname = 'public' and p.proname in
            ('legal_ai_memory_snapshot', 'legal_ai_memory_append', 'legal_ai_memory_health')));
$$;

revoke all on function public.legal_ai_memory_snapshot(uuid,text,integer),
    public.legal_ai_memory_append(uuid,text,uuid,text,text,bigint,integer),
    public.legal_ai_memory_health() from public, anon, authenticated;
grant execute on function public.legal_ai_memory_snapshot(uuid,text,integer),
    public.legal_ai_memory_append(uuid,text,uuid,text,text,bigint,integer),
    public.legal_ai_memory_health() to service_role;
notify pgrst, 'reload schema';


-- SOURCE: database/migrations/003_lead_tools.sql
-- Этап 7. После schema.sql и 002_memory.sql. Повторное применение допустимо.

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


-- SOURCE: database/migrations/004_telegram.sql
-- Этап 8. После миграций 002 и 003. Повторное применение допустимо.
alter table public.notification_logs add column if not exists claim_token uuid;
alter table public.notification_logs add column if not exists telegram_message_id bigint;
alter table public.notification_logs alter column next_attempt_at drop not null;

-- Общий cooldown для единственного настроенного бота/чата, переживает перезапуск.
create table if not exists public.telegram_delivery_state (
    id text primary key check (id='lawyer'),
    retry_after timestamptz
);
insert into public.telegram_delivery_state(id) values ('lawyer') on conflict do nothing;
alter table public.telegram_delivery_state enable row level security;
revoke all on public.telegram_delivery_state from public,anon,authenticated;
grant select,insert,update,delete on public.telegram_delivery_state to service_role;

create or replace function public.legal_ai_claim_notification(
    p_lease_seconds integer default 120, p_max_attempts integer default 5)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare n public.notification_logs;
begin
    if p_lease_seconds not between 120 and 600 or p_lease_seconds is null
        or p_max_attempts not between 1 and 10 or p_max_attempts is null then
        raise exception 'invalid notification limits' using errcode='22023';
    end if;
    -- Истёкшая аренда означает неизвестный результат, а не разрешение отправить снова.
    update public.notification_logs set status='failed',error_message='lease_expired_unknown',
        next_attempt_at=null,locked_until=null,claim_token=null
        where status='sending' and (locked_until <= clock_timestamp() or locked_until is null);
    if exists(select 1 from public.telegram_delivery_state
        where id='lawyer' and retry_after>clock_timestamp()) then return null; end if;
    select * into n from public.notification_logs
        where status in ('pending','failed') and next_attempt_at <= clock_timestamp()
            and attempts < p_max_attempts
        order by next_attempt_at,created_at,id for update skip locked limit 1;
    if not found then return null; end if;
    update public.notification_logs set status='sending',attempts=attempts+1,
        claim_token=gen_random_uuid(),locked_until=clock_timestamp()+make_interval(secs=>p_lease_seconds),
        error_message=null where id=n.id returning * into n;
    return jsonb_build_object('id',n.id,'claim_token',n.claim_token,'attempts',n.attempts);
end;
$$;

create or replace function public.legal_ai_notification_current(p_notification_id uuid,p_claim_token uuid)
returns jsonb language sql security invoker set search_path = '' as $$
    -- Без описания дела, истории, токена сессии и других лишних данных.
    select jsonb_build_object('id',l.id,'name',l.name,'phone',l.phone,
        'legal_topic',l.legal_topic,'created_at',l.created_at)
    from public.notification_logs n join public.leads l on l.id=n.lead_id
    where n.id=p_notification_id and n.claim_token=p_claim_token
        and n.status='sending' and n.locked_until > clock_timestamp();
$$;

create or replace function public.legal_ai_finish_notification(
    p_notification_id uuid,p_claim_token uuid,p_sent boolean,
    p_message_id bigint default null,p_error_code text default null,p_retry_delay integer default null,
    p_cooldown_delay integer default null)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare changed integer;
begin
    if p_sent is null or (p_sent and (p_message_id is null or p_message_id<=0
        or p_error_code is not null or p_retry_delay is not null or p_cooldown_delay is not null))
        or (not p_sent and (p_message_id is not null or p_error_code is null
            or p_error_code not in ('connection_failed','delivery_unknown','response_unknown','api_400',
                'api_401','api_403','api_404','api_429','api_500','api_502','api_503','api_504',
                'api_rejected','invalid_retry_after','unexpected_recipient','invalid_payload')))
        or (p_retry_delay is not null and p_retry_delay not between 1 and 604800)
        or (p_cooldown_delay is not null and (p_cooldown_delay not between 1 and 604800
            or p_error_code is distinct from 'api_429')) then
        raise exception 'invalid delivery result' using errcode='22023';
    end if;
    update public.notification_logs set status=case when p_sent then 'sent' else 'failed' end,
        telegram_message_id=p_message_id,sent_at=case when p_sent then now() else null end,
        error_message=p_error_code,next_attempt_at=case when not p_sent and p_retry_delay is not null
            then clock_timestamp()+make_interval(secs=>p_retry_delay) else null end,
        locked_until=null,claim_token=null
        where id=p_notification_id and claim_token=p_claim_token and status='sending';
    get diagnostics changed=row_count;
    if changed=1 and p_cooldown_delay is not null then
        update public.telegram_delivery_state set retry_after=greatest(retry_after,
            clock_timestamp()+make_interval(secs=>p_cooldown_delay)) where id='lawyer';
    end if;
    return jsonb_build_object('recorded',changed=1);
end;
$$;

create or replace function public.legal_ai_notifications_health()
returns jsonb language sql security invoker set search_path = '' as $$
    select jsonb_build_object('notifications_version',1,'rpc_ready',
        (select count(*)=4 and not bool_or(p.prosecdef)
            and not bool_or(pg_catalog.has_function_privilege('anon',p.oid,'EXECUTE'))
            and not bool_or(pg_catalog.has_function_privilege('authenticated',p.oid,'EXECUTE'))
         from pg_catalog.pg_proc p join pg_catalog.pg_namespace n on n.oid=p.pronamespace
         where n.nspname='public' and p.proname in ('legal_ai_claim_notification',
            'legal_ai_notification_current','legal_ai_finish_notification','legal_ai_notifications_health'))
        and exists(select 1 from pg_catalog.pg_attribute where attrelid='public.notification_logs'::regclass
            and attname='claim_token' and not attisdropped)
        and exists(select 1 from pg_catalog.pg_attribute where attrelid='public.notification_logs'::regclass
            and attname='telegram_message_id' and not attisdropped)
        and (select relrowsecurity from pg_catalog.pg_class where oid='public.telegram_delivery_state'::regclass)
        and not pg_catalog.has_table_privilege('anon','public.telegram_delivery_state','SELECT,INSERT,UPDATE,DELETE')
        and not pg_catalog.has_table_privilege('authenticated','public.telegram_delivery_state','SELECT,INSERT,UPDATE,DELETE'));
$$;

revoke all on function public.legal_ai_claim_notification(integer,integer),
    public.legal_ai_notification_current(uuid,uuid),
    public.legal_ai_finish_notification(uuid,uuid,boolean,bigint,text,integer,integer),
    public.legal_ai_notifications_health() from public,anon,authenticated;
grant execute on function public.legal_ai_claim_notification(integer,integer),
    public.legal_ai_notification_current(uuid,uuid),
    public.legal_ai_finish_notification(uuid,uuid,boolean,bigint,text,integer,integer),
    public.legal_ai_notifications_health() to service_role;
notify pgrst,'reload schema';


notify pgrst, 'reload schema';
commit;
select 'LEGAL_AI_SETUP_OK' as result;
