-- Vlocal 1.1.1 : l'app n'écrit plus directement dans installs / usage_days.
-- Un upsert PostgREST sous RLS exige de VOIR la ligne existante (ON CONFLICT),
-- donc une politique SELECT pour anon, donc la lecture des noms par quiconque
-- détient la clé publique. Inacceptable. À la place : une fonction RPC
-- SECURITY DEFINER, seule surface accessible au rôle anon, qui valide et écrit.

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
begin
  if p_install_id is null then
    raise exception 'install_id required';
  end if;
  insert into installs (install_id, first_name, last_name, app_version, os_version, last_seen_at)
  values (p_install_id,
          left(coalesce(p_first_name, ''), 80),
          left(coalesce(p_last_name, ''), 80),
          left(coalesce(p_app_version, ''), 32),
          left(coalesce(p_os_version, ''), 64),
          now())
  on conflict (install_id) do update
    set first_name   = excluded.first_name,
        last_name    = excluded.last_name,
        app_version  = excluded.app_version,
        os_version   = excluded.os_version,
        last_seen_at = now();

  for d in select * from jsonb_array_elements(coalesce(p_days, '[]'::jsonb)) loop
    n := n + 1;
    if n > 31 then exit; end if;              -- au plus un mois de rattrapage
    insert into usage_days (install_id, day, dictations, words, seconds_saved)
    values (p_install_id,
            (d->>'day')::date,
            greatest(0, coalesce((d->>'dictations')::int, 0)),
            greatest(0, coalesce((d->>'words')::int, 0)),
            greatest(0, coalesce((d->>'seconds_saved')::real, 0)))
    on conflict (install_id, day) do update
      set dictations    = excluded.dictations,
          words         = excluded.words,
          seconds_saved = excluded.seconds_saved;
  end loop;
end
$$;

revoke all on function public.vlocal_report_usage(uuid, text, text, text, text, jsonb) from public;
grant execute on function public.vlocal_report_usage(uuid, text, text, text, text, jsonb) to anon;

-- Les tables ne sont plus accessibles au rôle anon : ni lecture ni écriture directe.
drop policy if exists "installs: app insert" on public.installs;
drop policy if exists "installs: app update" on public.installs;
drop policy if exists "usage: app insert"    on public.usage_days;
drop policy if exists "usage: app update"    on public.usage_days;
revoke all on public.installs   from anon, authenticated;
revoke all on public.usage_days from anon, authenticated;
