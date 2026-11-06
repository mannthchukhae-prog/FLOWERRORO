-- Схема базы данных для flower-price-bot
-- Выполнить в Supabase: SQL Editor → New query → вставить → Run

-- Настройки мастерской (одна строка на чат Telegram)
create table if not exists shops (
  chat_id          bigint primary key,
  tax_mode         text    not null default 'usn6'   check (tax_mode in ('usn6','usn15')),
  vat_rate         numeric not null default 5,
  acquiring_pct    numeric not null default 2,
  marketplace_pct  numeric not null default 0,
  waste_pct        numeric not null default 15,
  logistics_pct    numeric not null default 10,
  delivery_mode    text    not null default 'paid' check (delivery_mode in ('free','paid','driver')),
  delivery_trip    numeric not null default 1500,
  stems_per_trip   integer not null default 300,
  driver_salary    numeric not null default 45000,
  florist_mode     text    not null default 'salary' check (florist_mode in ('percent','fixed','salary')),
  florist_pct      numeric not null default 10,
  florist_fixed    numeric not null default 150,
  florist_salary   numeric not null default 60000,
  fixed_monthly    numeric not null default 150000,
  planned_bouquets integer not null default 500,
  target_margin_pct numeric not null default 25,
  created_at       timestamptz not null default now()
);

-- Миграция для уже созданной таблицы (выполнить один раз):
alter table shops add column if not exists delivery_mode text not null default 'paid';
alter table shops add column if not exists delivery_trip numeric not null default 1500;
alter table shops add column if not exists stems_per_trip integer not null default 300;
alter table shops add column if not exists driver_salary numeric not null default 45000;
alter table shops add column if not exists florist_salary numeric not null default 60000;

-- Справочник цветов мастерской
create table if not exists flowers (
  id             bigint generated always as identity primary key,
  chat_id        bigint not null references shops(chat_id) on delete cascade,
  name           text   not null,
  purchase_price numeric not null check (purchase_price >= 0),
  packaging      numeric not null default 0,
  created_at     timestamptz not null default now()
);

create index if not exists flowers_chat_idx on flowers(chat_id);

-- Бот работает на сервере с service_role-ключом, RLS не требуется.
-- Если хотите включить RLS, добавьте политики под свой ключ самостоятельно.
