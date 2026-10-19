-- Этап 8. После миграций 002 и 003. Повторное применение допустимо.
begin;
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
commit;
