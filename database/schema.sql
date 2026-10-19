-- Этап 5, схема v1. Только новый выделенный проект/БД. Выполнить от postgres.
-- Не миграция существующих таблиц. Ошибка откатывает весь скрипт.
begin;

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
commit;
