-- Boltracker — Supabase schema
-- Voer dit één keer uit in de Supabase SQL Editor (Project > SQL Editor > New query).
-- De tracker schrijft met de service_role key (omzeilt RLS); het dashboard leest
-- met de anon key en heeft alleen SELECT nodig.

-- Huidige status per product (één rij per winkel+EAN).
create table if not exists public.tracker_state (
  id          bigint generated always as identity primary key,
  retailer    text        not null default 'bol',
  ean         text        not null,
  name        text        not null,
  product_id  text,
  url         text,
  price       numeric,
  in_stock    boolean     not null default false,
  listed      boolean     not null default false,
  last_check  timestamptz,
  unique (retailer, ean)
);

-- Gebeurtenissen-log (restock, prijsdaling, uitverkocht, drop-signaal).
create table if not exists public.tracker_events (
  id          bigint generated always as identity primary key,
  ts          timestamptz not null default now(),
  type        text        not null,
  retailer    text        not null default 'bol',
  ean         text,
  name        text,
  product_id  text,
  url         text,
  price       numeric,
  old_price   numeric
);

create index if not exists tracker_events_ts_idx on public.tracker_events (ts desc);

-- Row Level Security: alleen lezen met de publieke anon key.
alter table public.tracker_state  enable row level security;
alter table public.tracker_events enable row level security;

drop policy if exists "anon read state"  on public.tracker_state;
drop policy if exists "anon read events" on public.tracker_events;

create policy "anon read state"  on public.tracker_state  for select to anon using (true);
create policy "anon read events" on public.tracker_events for select to anon using (true);
