"""
Расчётный движок ценообразования цветочной мастерской.

Логика: продажная цена решается как уравнение, в котором из цены «вынимаются»
НДС, налог УСН, эквайринг/комиссии, работа флориста, а остаток покрывает
полную себестоимость и оставляет целевую чистую маржу.

Поддерживаются оба объекта УСН:
  - usn6  — «Доходы», 6% с выручки без НДС;
  - usn15 — «Доходы минус расходы», 15% с прибыли.

НДС по умолчанию 5% (пониженная ставка для УСН, доход до ~272,5 млн ₽ в 2026 г.,
без права на вычет входного НДС).
"""

from dataclasses import dataclass, field


@dataclass
class Settings:
    tax_mode: str = "usn6"            # usn6 | usn15
    vat_rate: float = 5.0             # НДС, %
    acquiring_pct: float = 2.0        # эквайринг, % от цены
    marketplace_pct: float = 0.0      # комиссия маркетплейса/агрегатора, % от цены
    waste_pct: float = 15.0           # убыль/списания, % от закупки
    logistics_pct: float = 10.0       # доставка от поставщика, % к закупке
    florist_mode: str = "percent"     # percent | fixed
    florist_pct: float = 10.0         # работа флориста, % от цены
    florist_fixed: float = 150.0      # или фикс ₽ за букет
    fixed_monthly: float = 150000.0   # аренда + коммуналка + прочее, ₽/мес
    planned_bouquets: int = 500       # план продаж, букетов/мес
    target_margin_pct: float = 25.0   # целевая чистая маржа, % от цены

    @classmethod
    def from_row(cls, row: dict) -> "Settings":
        s = cls()
        for k in s.__dataclass_fields__:
            if k in row and row[k] is not None:
                setattr(s, k, row[k])
        return s


@dataclass
class BouquetItem:
    name: str
    qty: int
    purchase_price: float             # закупочная цена за стебель, ₽
    packaging: float = 0.0            # упаковка на единицу, ₽


@dataclass
class PriceResult:
    mode: str
    price: float                      # рекомендуемая цена, ₽ (округлённая)
    cost_total: float                 # полная себестоимость букета, ₽
    lines: list = field(default_factory=list)   # [(название, ₽)] разбивка цены
    profit: float = 0.0               # чистая прибыль, ₽
    margin_pct: float = 0.0           # фактическая маржа, %
    markup_on_cogs: float = 0.0       # наценка на «сырую» закупку, %
    ok: bool = True
    error: str = ""


def _item_cost(item: BouquetItem, s: Settings) -> float:
    """Полная себестоимость позиции: закупка + логистика, довес на убыль, упаковка."""
    stem = item.purchase_price * (1 + s.logistics_pct / 100)
    stem = stem / (1 - s.waste_pct / 100)
    return stem * item.qty + item.packaging * item.qty


def raw_cogs(items: list) -> float:
    """«Сырая» закупка без довесов — для сравнения с рыночной наценкой x2–x3,5."""
    return sum(i.purchase_price * i.qty for i in items)


def full_cost(items: list, extra_packaging: float, s: Settings) -> dict:
    cogs = sum(_item_cost(i, s) for i in items)
    fixed_per = s.fixed_monthly / max(int(s.planned_bouquets), 1)
    florist_fix = float(s.florist_fixed) if s.florist_mode == "fixed" else 0.0
    total = cogs + extra_packaging + fixed_per + florist_fix
    return {
        "cogs": cogs,
        "packaging": extra_packaging,
        "fixed_per": fixed_per,
        "florist_fixed": florist_fix,
        "total": total,
    }


def calc_price(items: list, extra_packaging: float, s: Settings, mode: str) -> PriceResult:
    fc = full_cost(items, extra_packaging, s)
    C = fc["total"]
    v = s.vat_rate / 100
    net_share = 1 / (1 + v)                      # доля выручки без НДС в цене
    var_pct = (s.acquiring_pct + s.marketplace_pct) / 100
    if s.florist_mode == "percent":
        var_pct += s.florist_pct / 100
    m = s.target_margin_pct / 100

    if mode == "usn6":
        denom = net_share * (1 - 0.06) - var_pct - m
    else:  # usn15
        denom = 0.85 * (net_share - var_pct) - m

    if denom <= 0.05:
        return PriceResult(
            mode=mode, price=0, cost_total=C, ok=False,
            error=(f"Нереалистичные параметры: знаменатель формулы {denom:.2f}. "
                   "Снизьте целевую маржу, процент флориста или комиссии."),
        )

    price_raw = C / denom
    # Психологическое округление до красивого ценника (…90 / …50)
    price = _pretty_price(price_raw)

    # Разбивка итоговой цены по статьям
    vat = price * v / (1 + v)
    revenue_net = price - vat
    acquiring = price * (s.acquiring_pct + s.marketplace_pct) / 100
    florist_var = price * s.florist_pct / 100 if s.florist_mode == "percent" else 0.0

    if mode == "usn6":
        tax = revenue_net * 0.06
        profit = revenue_net - tax - acquiring - florist_var - C
    else:
        base = max(revenue_net - acquiring - florist_var - C, 0)
        tax = base * 0.15
        profit = base - tax

    lines = [
        ("Закупка цветов + логистика + убыль", fc["cogs"]),
        ("Упаковка", fc["packaging"]),
        ("Аренда и постоянные (на 1 букет)", fc["fixed_per"]),
    ]
    if fc["florist_fixed"]:
        lines.append(("Работа флориста (фикс)", fc["florist_fixed"]))
    if florist_var:
        lines.append(("Работа флориста (% от цены)", florist_var))
    lines += [
        ("НДС", vat),
        (f"Налог УСН ({'6% с дохода' if mode == 'usn6' else '15% с прибыли'})", tax),
        ("Эквайринг/комиссии", acquiring),
        ("Чистая прибыль", profit),
    ]

    return PriceResult(
        mode=mode,
        price=price,
        cost_total=C,
        lines=lines,
        profit=profit,
        margin_pct=profit / price * 100 if price else 0,
        markup_on_cogs=(price / raw_cogs(items) - 1) * 100 if raw_cogs(items) else 0,
    )


def compare_modes(items: list, extra_packaging: float, s: Settings) -> dict:
    """Считает цену в обоих режимах УСН для сравнения."""
    return {
        "usn6": calc_price(items, extra_packaging, s, "usn6"),
        "usn15": calc_price(items, extra_packaging, s, "usn15"),
    }


def _pretty_price(p: float) -> float:
    """Округление вверх до «красивого» ценника."""
    if p < 500:
        return (int(p + 9) // 10) * 10          # до 10 ₽
    if p < 3000:
        base = int(p // 50) * 50
        for cand in (base + 50, base + 90, base + 100):
            if cand >= p:
                return cand
    base = int(p // 100) * 100
    for cand in (base + 90, base + 100, base + 150, base + 190, base + 200):
        if cand >= p:
            return cand
    return base + 290
