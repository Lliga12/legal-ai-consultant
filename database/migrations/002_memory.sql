-- Этап 6. Применить после schema.sql. Идемпотентная добавочная миграция.
begin;

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
commit;
