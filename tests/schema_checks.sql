-- Выполняется только во временной локальной БД через check_schema_local.py.
set role service_role;
do $$
declare a uuid := '00000000-0000-0000-0000-000000000001';
declare b uuid := '00000000-0000-0000-0000-000000000002';
declare h text := repeat('a',64);
declare j jsonb;
declare first_id text;
begin
    j := public.legal_ai_health();
    assert j = '{"schema_version":1,"rls_enabled":true,"public_access":false}'::jsonb;
    j := public.legal_ai_create_session(a,h);
    assert j->>'id' = a::text;
    assert not j ? 'token_hash';
    perform public.legal_ai_create_session(a,h);
    perform public.legal_ai_create_session(b,repeat('b',64));
    begin
        perform public.legal_ai_get_session(a,repeat('b',64));
        raise exception 'wrong session secret was accepted';
    exception when no_data_found then null;
    end;
    perform public.legal_ai_append_message(a,h,
        '00000000-0000-0000-0000-000000000010','user','Тестовый вопрос');
    perform public.legal_ai_append_message(a,h,
        '00000000-0000-0000-0000-000000000010','user','Тестовый вопрос');
    perform public.legal_ai_append_message(a,h,
        '00000000-0000-0000-0000-000000000011','assistant','Тестовый ответ');
    j := public.legal_ai_messages(a,h,40);
    assert jsonb_array_length(j) = 2;
    assert j->0->>'role' = 'user';
    assert j->1->>'role' = 'assistant';
    assert jsonb_array_length(public.legal_ai_messages(a,h,1)) = 1;
    assert jsonb_array_length(public.legal_ai_messages(b,repeat('b',64),40)) = 0;
    begin
        perform public.legal_ai_append_message(a,h,
            '00000000-0000-0000-0000-000000000010','user','Другой текст');
        raise exception 'message id conflict was accepted';
    exception when unique_violation then null;
    end;
    begin
        perform public.legal_ai_append_message(b,repeat('b',64),
            '00000000-0000-0000-0000-000000000010','user','Тестовый вопрос');
        raise exception 'cross-session message reuse was accepted';
    exception when unique_violation then null;
    end;
    j := public.legal_ai_get_session(a,h);
    j := public.legal_ai_update_state(a,h,(j->>'version')::bigint,'{"booking_stage":"name"}');
    assert j->'state'->>'booking_stage' = 'name';
    begin
        perform public.legal_ai_update_state(a,h,0,'{}');
        raise exception 'stale state version was accepted';
    exception when serialization_failure then null;
    end;
    begin
        perform public.legal_ai_create_lead(a,h,'00000000-0000-0000-0000-000000000020',
            'Тест','+70000000000','Тестовая тема','Синтетическое описание',false,'test-v1');
        raise exception 'missing consent was accepted';
    exception when invalid_parameter_value then null;
    end;
    j := public.legal_ai_create_lead(a,h,'00000000-0000-0000-0000-000000000020',
        'Тест','+70000000000','Тестовая тема','Синтетическое описание',true,'test-v1');
    assert (j->>'duplicate')::boolean = false;
    first_id := j->>'id';
    j := public.legal_ai_create_lead(a,h,'00000000-0000-0000-0000-000000000020',
        'Тест','+70000000000','Тестовая тема','Синтетическое описание',true,'test-v1');
    assert (j->>'duplicate')::boolean = true;
    assert j->>'id' = first_id;
    assert (select count(*) from public.notification_logs where lead_id = first_id::uuid) = 1;
    begin
        perform public.legal_ai_create_lead(a,h,'00000000-0000-0000-0000-000000000020',
            'Другой','+70000000000','Тестовая тема','Синтетическое описание',true,'test-v1');
        raise exception 'lead id conflict was accepted';
    exception when unique_violation then null;
    end;
    j := public.legal_ai_clear_history(a,h);
    assert (j->>'deleted_messages')::int = 2;
    assert jsonb_array_length(public.legal_ai_messages(a,h,40)) = 0;
    assert (public.legal_ai_get_session(a,h))->'state' = '{}'::jsonb;
    assert (select count(*) from public.leads where session_id = a) = 1;
    j := public.legal_ai_delete_leads(a,h);
    assert (j->>'deleted_leads')::int = 1;
    assert (select count(*) from public.notification_logs) = 0;
end;
$$;
reset role;

-- Проверка атомарности: ошибка outbox должна откатить и вставку заявки.
create function public.reject_test_outbox() returns trigger language plpgsql as $$
begin raise exception 'simulated outbox failure'; end;
$$;
create trigger reject_outbox before insert on public.notification_logs
    for each row execute function public.reject_test_outbox();
set role service_role;
do $$
begin
    begin
        perform public.legal_ai_create_lead('00000000-0000-0000-0000-000000000001',repeat('a',64),
            '00000000-0000-0000-0000-000000000030',
            'Тест','+70000000000','Тестовая тема','Синтетическое описание',true,'test-v1');
        raise exception 'outbox failure not propagated';
    exception when raise_exception then
        if sqlerrm <> 'simulated outbox failure' then raise; end if;
    end;
    assert not exists (select 1 from public.leads
        where idempotency_key = '00000000-0000-0000-0000-000000000030');
end;
$$;
reset role;
drop trigger reject_outbox on public.notification_logs;
drop function public.reject_test_outbox();

-- Прямое чтение и вызов RPC запрещены обеим браузерным ролям.
set role anon;
do $$
begin
    begin
        perform * from public.leads;
        raise exception 'anon could read leads';
    exception when insufficient_privilege then null;
    end;
    begin
        perform public.legal_ai_health();
        raise exception 'anon could call RPC';
    exception when insufficient_privilege then null;
    end;
end;
$$;
reset role;
set role authenticated;
do $$
begin
    begin
        perform * from public.chat_messages;
        raise exception 'authenticated could read history';
    exception when insufficient_privilege then null;
    end;
    begin
        perform public.legal_ai_get_session('00000000-0000-0000-0000-000000000001',repeat('a',64));
        raise exception 'authenticated could call RPC';
    exception when insufficient_privilege then null;
    end;
end;
$$;
reset role;
