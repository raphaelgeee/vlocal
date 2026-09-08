-- Durcissement (8 septembre 2026), suite à l'audit avant annonce publique.
-- Appliqué en production le 8/09 via la console (MCP).

-- 1. search_path figé sur le trigger (lint Supabase 0011).
create or replace function public.set_updated_at() returns trigger
language plpgsql
set search_path = public
as $$
begin
  new.updated_at := now();
  return new;
end $$;

-- 2. La RPC de télémétrie : réservée au rôle anon (l'app), pas aux comptes connectés,
--    entrées assainies, plafonds de valeurs, et plafond de 300 installations
--    nouvelles par heure : une clé publique dans un dépôt public ne doit pas
--    permettre de noyer la console d'administration.
create or replace function public.vlocal_report_usage(
  p_install_id  uuid,
  p_first_name  text,
  p_last_name   text,
  p_app_version text,
  p_os_version  text,
  p_days        jsonb
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
begin
  if p_install_id is null then
    raise exception 'install_id required';
  end if;
  clean_first := left(regexp_replace(coalesce(p_first_name, ''), '[[:cntrl:]]+', ' ', 'g'), 80);
  clean_last  := left(regexp_replace(coalesce(p_last_name,  ''), '[[:cntrl:]]+', ' ', 'g'), 80);

  if not exists (select 1 from installs where install_id = p_install_id) then
    select count(*) into fresh from installs where created_at > now() - interval '1 hour';
    if fresh >= 300 then
      raise exception 'too many new installs, retry later' using errcode = '53400';
    end if;
  end if;

  insert into installs (install_id, first_name, last_name, app_version, os_version, last_seen_at)
  values (p_install_id, clean_first, clean_last,
          left(coalesce(p_app_version, ''), 32), left(coalesce(p_os_version, ''), 64), now())
  on conflict (install_id) do update
    set first_name   = excluded.first_name,
        last_name    = excluded.last_name,
        app_version  = excluded.app_version,
        os_version   = excluded.os_version,
        last_seen_at = now();

  for d in select * from jsonb_array_elements(coalesce(p_days, '[]'::jsonb)) loop
    n := n + 1;
    if n > 31 then exit; end if;
    insert into usage_days (install_id, day, dictations, words, seconds_saved)
    values (p_install_id,
            (d->>'day')::date,
            least(100000, greatest(0, coalesce((d->>'dictations')::int, 0))),
            least(10000000, greatest(0, coalesce((d->>'words')::int, 0))),
            least(1000000, greatest(0, coalesce((d->>'seconds_saved')::real, 0))))
    on conflict (install_id, day) do update
      set dictations    = excluded.dictations,
          words         = excluded.words,
          seconds_saved = excluded.seconds_saved;
  end loop;
end
$$;

revoke all on function public.vlocal_report_usage(uuid, text, text, text, text, jsonb) from public, authenticated;
grant execute on function public.vlocal_report_usage(uuid, text, text, text, text, jsonb) to anon;

-- 3. Secrets de l'ancien modèle payant, désormais sans usage : supprimés.
delete from app_config where key in ('license_priv_seed', 'stripe_webhook_secret');
