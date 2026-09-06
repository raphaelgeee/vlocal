-- Vlocal 1.1.0 : télémétrie minimale déclarée.
-- Deux tables alimentées par l'application (clé anon, upserts idempotents),
-- lues uniquement côté administrateur (service role, edge function `admin`).
--
-- Ce que l'app envoie, et rien d'autre : identifiant d'installation aléatoire,
-- prénom et nom saisis par l'utilisateur (peuvent être vides), version de
-- Vlocal et de macOS, puis par jour : nombre de dictées, mots, temps gagné.

create table if not exists public.installs (
  install_id   uuid primary key,
  first_name   text not null default '',
  last_name    text not null default '',
  app_version  text not null default '',
  os_version   text not null default '',
  created_at   timestamptz not null default now(),
  last_seen_at timestamptz not null default now()
);

create table if not exists public.usage_days (
  install_id    uuid not null references public.installs(install_id) on delete cascade,
  day           date not null,
  dictations    integer not null default 0,
  words         integer not null default 0,
  seconds_saved real not null default 0,
  updated_at    timestamptz not null default now(),
  primary key (install_id, day)
);

create index if not exists usage_days_day_idx on public.usage_days(day);

-- Garde-fous : longueurs bornées côté base (l'app tronque déjà à 80 / 32 / 64).
alter table public.installs
  drop constraint if exists installs_first_name_len,
  add constraint installs_first_name_len check (char_length(first_name) <= 80),
  drop constraint if exists installs_last_name_len,
  add constraint installs_last_name_len check (char_length(last_name) <= 80),
  drop constraint if exists installs_app_version_len,
  add constraint installs_app_version_len check (char_length(app_version) <= 32),
  drop constraint if exists installs_os_version_len,
  add constraint installs_os_version_len check (char_length(os_version) <= 64);

alter table public.usage_days
  drop constraint if exists usage_days_positive,
  add constraint usage_days_positive
    check (dictations >= 0 and words >= 0 and seconds_saved >= 0);

-- updated_at tenu à jour automatiquement sur usage_days.
create or replace function public.set_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end $$;

drop trigger if exists usage_days_set_updated_at on public.usage_days;
create trigger usage_days_set_updated_at
  before update on public.usage_days
  for each row execute function public.set_updated_at();

-- RLS : l'app (rôle anon) peut écrire sa propre ligne (insert + update via
-- upsert PostgREST) mais ne peut RIEN lire. L'identifiant d'installation est
-- un UUID aléatoire : il fait office de secret par installation.
alter table public.installs   enable row level security;
alter table public.usage_days enable row level security;

drop policy if exists "installs: app insert"  on public.installs;
drop policy if exists "installs: app update"  on public.installs;
drop policy if exists "usage: app insert"     on public.usage_days;
drop policy if exists "usage: app update"     on public.usage_days;

create policy "installs: app insert" on public.installs
  for insert to anon with check (true);
create policy "installs: app update" on public.installs
  for update to anon using (true) with check (true);
create policy "usage: app insert" on public.usage_days
  for insert to anon with check (true);
create policy "usage: app update" on public.usage_days
  for update to anon using (true) with check (true);

-- Aucune politique SELECT pour anon : la lecture passe par le service role.

-- Vue pratique pour l'administration : une ligne par installation avec les
-- totaux, le mois en cours et la dernière utilisation.
create or replace view public.installs_overview as
select
  i.install_id,
  i.first_name,
  i.last_name,
  i.app_version,
  i.os_version,
  i.created_at,
  i.last_seen_at,
  coalesce(sum(u.dictations), 0)::integer                                   as dictations_total,
  coalesce(sum(u.words), 0)::integer                                        as words_total,
  coalesce(sum(u.seconds_saved), 0)::real                                   as seconds_saved_total,
  coalesce(sum(u.seconds_saved) filter (where u.day >= date_trunc('month', current_date)), 0)::real
                                                                            as seconds_saved_month,
  max(u.day)                                                                as last_usage_day
from public.installs i
left join public.usage_days u on u.install_id = i.install_id
group by i.install_id;

-- La vue hérite des droits : on la réserve au service role.
revoke all on public.installs_overview from anon, authenticated;
