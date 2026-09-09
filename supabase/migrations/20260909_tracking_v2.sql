-- Vlocal 1.3.0 (9 septembre 2026) : traçage précis, toujours déclaré.
--
-- Objectif : que la console d'administration soit interprétable sans deviner.
-- On ajoute, et rien de plus :
--   installs    : modèle de Mac, langue de l'interface, raccourci, moteur ;
--   usage_days  : durée de parole dictée (audio_seconds) ;
--   incidents   : par jour et par code, le NOMBRE d'incidents techniques.
--                 Jamais le contexte d'un incident (il peut contenir un nom de
--                 périphérique ou un message) : uniquement un compteur.
--
-- Rien d'identifiant en plus : mac_model est un modèle commercial, pas un
-- numéro de série. Voir README « Données » et la page Confidentialité.

alter table public.installs
  add column if not exists mac_model text not null default '',
  add column if not exists ui_lang   text not null default '',
  add column if not exists hotkey    text not null default '',
  add column if not exists engine    text not null default '';

alter table public.installs
  drop constraint if exists installs_mac_model_len,
  add  constraint installs_mac_model_len check (char_length(mac_model) <= 64),
  drop constraint if exists installs_ui_lang_len,
  add  constraint installs_ui_lang_len   check (char_length(ui_lang) <= 8),
  drop constraint if exists installs_hotkey_len,
  add  constraint installs_hotkey_len    check (char_length(hotkey) <= 24),
  drop constraint if exists installs_engine_len,
  add  constraint installs_engine_len    check (char_length(engine) <= 8);

alter table public.usage_days
  add column if not exists audio_seconds real not null default 0;

alter table public.usage_days
  drop constraint if exists usage_days_audio_positive,
  add  constraint usage_days_audio_positive check (audio_seconds >= 0);

create table if not exists public.incidents (
  install_id uuid not null references public.installs(install_id) on delete cascade,
  day        date not null,
  code       text not null,
  count      integer not null default 0,
  updated_at timestamptz not null default now(),
  primary key (install_id, day, code),
  constraint incidents_code_len check (char_length(code) <= 40),
  constraint incidents_count_positive check (count >= 0)
);
create index if not exists incidents_day_idx  on public.incidents(day);
create index if not exists incidents_code_idx on public.incidents(code);

alter table public.incidents enable row level security;
revoke all on public.incidents from anon, authenticated;

drop trigger if exists incidents_set_updated_at on public.incidents;
create trigger incidents_set_updated_at
  before update on public.incidents
  for each row execute function public.set_updated_at();

-- RPC v4. Les nouveaux paramètres ont une valeur par défaut : une version
-- 1.2.x, qui n'envoie que les six premiers, continue de fonctionner. L'ancienne
-- signature est supprimée pour éviter toute ambiguïté de résolution PostgREST.
drop function if exists public.vlocal_report_usage(uuid, text, text, text, text, jsonb);
drop function if exists public.vlocal_report_usage(uuid, text, text, text, text, jsonb, text, timestamptz);

create or replace function public.vlocal_report_usage(
  p_install_id   uuid,
  p_first_name   text    default '',
  p_last_name    text    default '',
  p_app_version  text    default '',
  p_os_version   text    default '',
  p_days         jsonb   default '[]'::jsonb,
  p_email        text    default '',
  p_last_used_at timestamptz default null,
  p_mac_model    text    default '',
  p_ui_lang      text    default '',
  p_hotkey       text    default '',
  p_engine       text    default '',
  p_incidents    jsonb   default '[]'::jsonb
) returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  d jsonb;
  n int := 0;
  fresh int;
  clean_first text;
  clean_last  text;
  clean_email text;
begin
  if p_install_id is null then
    raise exception 'install_id required';
  end if;
  clean_first := left(regexp_replace(coalesce(p_first_name, ''), '[[:cntrl:]]+', ' ', 'g'), 80);
  clean_last  := left(regexp_replace(coalesce(p_last_name,  ''), '[[:cntrl:]]+', ' ', 'g'), 80);
  clean_email := left(lower(regexp_replace(coalesce(p_email, ''), '[[:space:][:cntrl:]]+', '', 'g')), 200);
  if clean_email !~ '^[^@]+@[^@.]+\.[^@]+$' then
    clean_email := '';
  end if;

  -- Plafond anti-noyade : une clé publique dans un dépôt public ne doit pas
  -- permettre de créer des milliers d'installations fictives.
  if not exists (select 1 from installs where install_id = p_install_id) then
    select count(*) into fresh from installs where created_at > now() - interval '1 hour';
    if fresh >= 300 then
      raise exception 'too many new installs, retry later' using errcode = '53400';
    end if;
  end if;

  insert into installs (install_id, first_name, last_name, email, app_version,
                        os_version, last_used_at, mac_model, ui_lang, hotkey,
                        engine, last_seen_at)
  values (p_install_id, clean_first, clean_last, clean_email,
          left(coalesce(p_app_version, ''), 32),
          left(coalesce(p_os_version, ''), 64),
          p_last_used_at,
          left(regexp_replace(coalesce(p_mac_model, ''), '[[:cntrl:]]+', ' ', 'g'), 64),
          left(coalesce(p_ui_lang, ''), 8),
          left(coalesce(p_hotkey, ''), 24),
          left(coalesce(p_engine, ''), 8),
          now())
  on conflict (install_id) do update
    set first_name   = excluded.first_name,
        last_name    = excluded.last_name,
        email        = case when excluded.email <> '' then excluded.email else installs.email end,
        app_version  = excluded.app_version,
        os_version   = excluded.os_version,
        last_used_at = coalesce(excluded.last_used_at, installs.last_used_at),
        mac_model    = case when excluded.mac_model <> '' then excluded.mac_model else installs.mac_model end,
        ui_lang      = case when excluded.ui_lang   <> '' then excluded.ui_lang   else installs.ui_lang end,
        hotkey       = case when excluded.hotkey    <> '' then excluded.hotkey    else installs.hotkey end,
        engine       = case when excluded.engine    <> '' then excluded.engine    else installs.engine end,
        last_seen_at = now();

  for d in select * from jsonb_array_elements(coalesce(p_days, '[]'::jsonb)) loop
    n := n + 1;
    if n > 31 then exit; end if;              -- au plus un mois de rattrapage
    insert into usage_days (install_id, day, dictations, words, seconds_saved,
                            audio_seconds, meetings, meeting_words)
    values (p_install_id,
            (d->>'day')::date,
            least(100000,   greatest(0, coalesce((d->>'dictations')::int, 0))),
            least(10000000, greatest(0, coalesce((d->>'words')::int, 0))),
            least(1000000,  greatest(0, coalesce((d->>'seconds_saved')::real, 0))),
            least(1000000,  greatest(0, coalesce((d->>'audio_seconds')::real, 0))),
            least(1000,     greatest(0, coalesce((d->>'meetings')::int, 0))),
            least(10000000, greatest(0, coalesce((d->>'meeting_words')::int, 0))))
    on conflict (install_id, day) do update
      set dictations    = excluded.dictations,
          words         = excluded.words,
          seconds_saved = excluded.seconds_saved,
          audio_seconds = greatest(excluded.audio_seconds, usage_days.audio_seconds),
          meetings      = excluded.meetings,
          meeting_words = excluded.meeting_words;
  end loop;

  n := 0;
  for d in select * from jsonb_array_elements(coalesce(p_incidents, '[]'::jsonb)) loop
    n := n + 1;
    if n > 200 then exit; end if;
    continue when coalesce(d->>'code', '') = '';
    insert into incidents (install_id, day, code, count)
    values (p_install_id,
            (d->>'day')::date,
            left(regexp_replace(d->>'code', '[^a-zA-Z0-9_.:-]', '', 'g'), 40),
            least(100000, greatest(0, coalesce((d->>'count')::int, 0))))
    on conflict (install_id, day, code) do update
      set count = greatest(excluded.count, incidents.count);
  end loop;
end
$$;

revoke all on function public.vlocal_report_usage(uuid, text, text, text, text, jsonb,
  text, timestamptz, text, text, text, text, jsonb) from public, authenticated;
grant execute on function public.vlocal_report_usage(uuid, text, text, text, text, jsonb,
  text, timestamptz, text, text, text, text, jsonb) to anon;

-- Vue d'administration : une ligne par installation, tous les totaux utiles.
drop view if exists public.installs_overview;
create view public.installs_overview as
select
  i.install_id, i.first_name, i.last_name, i.email,
  i.app_version, i.os_version, i.mac_model, i.ui_lang, i.hotkey, i.engine,
  i.created_at, i.last_seen_at, i.last_used_at,
  coalesce(sum(u.dictations), 0)::integer     as dictations_total,
  coalesce(sum(u.words), 0)::integer          as words_total,
  coalesce(sum(u.seconds_saved), 0)::real     as seconds_saved_total,
  coalesce(sum(u.audio_seconds), 0)::real     as audio_seconds_total,
  coalesce(sum(u.meetings), 0)::integer       as meetings_total,
  coalesce(sum(u.meeting_words), 0)::integer  as meeting_words_total,
  coalesce(sum(u.seconds_saved) filter (where u.day >= date_trunc('month', current_date)), 0)::real
                                              as seconds_saved_month,
  coalesce(sum(u.words) filter (where u.day >= date_trunc('month', current_date)), 0)::integer
                                              as words_month,
  count(distinct u.day) filter (where u.dictations > 0 or u.meetings > 0)::integer
                                              as active_days,
  min(u.day)                                  as first_usage_day,
  max(u.day)                                  as last_usage_day
from public.installs i
left join public.usage_days u on u.install_id = i.install_id
group by i.install_id;

revoke all on public.installs_overview from anon, authenticated;
