"""Human-readable notification formatting.

All functions in this module are pure: no DB, no I/O, no side effects.
They take structured data and return strings ready for ntfy.

Two rendering modes controlled by ``debug: bool``:

  * **Client mode** (``debug=False``, default): Portuguese text,
    emoji decision phrases, top-3 most relevant reasons, friendly
    asset names.  Designed for phone lock-screen readability.

  * **Debug mode** (``debug=True``): Client text followed by a
    ``--- debug ---`` separator and the full raw data block (all
    indicator values, score breakdown, regime label, raw pair name).
"""

from __future__ import annotations

from typing import Any, Mapping


# ---------- symbol display ----------

_FRIENDLY_NAMES: dict[str, str] = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
    "SOLUSDT": "SOL",
    "BNBUSDT": "BNB",
    "XRPUSDT": "XRP",
    "ADAUSDT": "ADA",
    "DOGEUSDT": "DOGE",
    "DOTUSDT": "DOT",
    "AVAXUSDT": "AVAX",
    "LINKUSDT": "LINK",
    "MATICUSDT": "MATIC",
    "LTCUSDT": "LTC",
    "UNIUSDT": "UNI",
    "ATOMUSDT": "ATOM",
    "NEARUSDT": "NEAR",
    "APTUSDT": "APT",
    "ARBUSDT": "ARB",
    "OPUSDT": "OP",
    "SUIUSDT": "SUI",
    "SEIUSDT": "SEI",
}


def friendly_name(symbol: str) -> str:
    """Return a short display name for a Binance pair.

    Known pairs map to their base asset (BTCUSDT → BTC).  Unknown
    pairs strip a trailing ``USDT`` if present, otherwise return the
    raw symbol.
    """
    if symbol in _FRIENDLY_NAMES:
        return _FRIENDLY_NAMES[symbol]
    if symbol.endswith("USDT") and len(symbol) > 4:
        return symbol[:-4]
    return symbol


# ---------- severity → decision phrase ----------

_DECISION_PHRASES: dict[str, tuple[str, str]] = {
    # severity → (emoji+phrase, interpretation)
    "very_strong": ("🟢 Bom momento de compra", "Vários indicadores apontam oportunidade clara."),
    "strong":      ("🟡 Vale observar", "Sinais moderados — acompanhe antes de agir."),
    "normal":      ("🟠 Melhor esperar", "Sinal fraco — observe com cautela."),
}

# Fallback for unknown severity (shouldn't happen in practice).
_DEFAULT_PHRASE = ("🔴 Evitar entrada agora", "Condições desfavoráveis.")


# ---------- timeframe translations ----------

_TF_PT: dict[str, str] = {
    "1h": "1 hora",
    "24h": "24 horas",
    "7d": "7 dias",
    "30d": "30 dias",
    "180d": "6 meses",
}


# ---------- pattern translations ----------

_PATTERN_PT: dict[str, str] = {
    "hammer": "martelo",
    "bullish_engulfing": "engolfo de alta",
    "piercing_line": "linha de perfuração",
    "morning_star": "estrela da manhã",
    "dragonfly_doji": "doji libélula",
}


def _translate_pattern(name: str | None) -> str | None:
    """Translate a reversal pattern name to Portuguese."""
    if name is None:
        return None
    return _PATTERN_PT.get(name, name)


# ---------- reason lines ----------

def _build_reason_lines(row: dict[str, Any]) -> list[str]:
    """Build ALL possible reason lines, ranked by relevance.

    Each line is a (priority, text) tuple sorted by priority (lower
    number = more relevant).  The caller takes the top N.
    """
    ranked: list[tuple[int, str]] = []

    # Drop is almost always the dominant reason.
    drop_pct = row.get("drop_trigger_pct")
    dom_tf = row.get("dominant_trigger_timeframe")
    if drop_pct is not None and drop_pct > 0 and dom_tf:
        tf_label = _TF_PT.get(dom_tf, dom_tf)
        ranked.append((1, f"Queda de {drop_pct:.1f}% em {tf_label}"))

    # RSI oversold is a strong confirming signal.
    rsi_1h = row.get("rsi_1h")
    if rsi_1h is not None and rsi_1h <= 30:
        if rsi_1h <= 20:
            ranked.append((2, f"RSI em sobrevenda extrema ({rsi_1h:.0f})"))
        else:
            ranked.append((3, f"RSI em sobrevenda ({rsi_1h:.0f})"))

    # Elevated volume confirms conviction.
    rel_vol = row.get("rel_volume")
    if rel_vol is not None and rel_vol >= 1.5:
        ranked.append((4, f"Volume {rel_vol:.1f}x acima do normal"))

    # Reversal pattern is actionable.
    pattern = row.get("reversal_pattern")
    if pattern:
        pt_name = _translate_pattern(pattern)
        ranked.append((5, f"Padrão de reversão: {pt_name}"))

    # Support proximity.
    dist_sup = row.get("dist_support_pct")
    sup_price = row.get("support_level_price")
    if dist_sup is not None and sup_price is not None and dist_sup < 5.0:
        ranked.append((6, f"Próximo do suporte em ${sup_price:g}"))

    # Discount from high.
    disc_30d = row.get("distance_from_30d_high_pct")
    disc_180d = row.get("distance_from_180d_high_pct")
    if disc_180d is not None and disc_180d > 20:
        ranked.append((7, f"{disc_180d:.0f}% abaixo da máxima de 6 meses"))
    elif disc_30d is not None and disc_30d > 20:
        ranked.append((7, f"{disc_30d:.0f}% abaixo da máxima de 30 dias"))

    ranked.sort(key=lambda t: t[0])
    return [text for _, text in ranked]


# ---------- regime line ----------

_REGIME_PT: dict[str, str] = {
    "risk_on": "Mercado favorável (risk-on)",
    "risk_off": "Mercado adverso (risk-off)",
}


def _regime_line(regime: str | None) -> str | None:
    """Translate regime label to Portuguese, or None if neutral/absent."""
    if regime is None:
        return None
    return _REGIME_PT.get(regime)


# ---------- alert formatting ----------

def format_alert_title(
    symbol: str,
    severity: str,
    *,
    debug: bool = False,
) -> str:
    """Render the notification title — a decision-oriented phrase.

    Client mode uses the friendly asset name; debug mode appends
    the raw pair.
    """
    phrase, _ = _DECISION_PHRASES.get(severity, _DEFAULT_PHRASE)
    name = friendly_name(symbol)
    title = f"{phrase} — {name}"
    if debug:
        title += f" ({symbol})"
    return title


def format_alert_body(
    row: dict[str, Any],
    *,
    debug: bool = False,
) -> str:
    """Render the notification body.

    ``row`` is a dict with signal column values (from the expanded
    SELECT in ``process_pending_signals``).

    Client mode: price line, 24h variation, top-3 reasons, regime
    line, one-line interpretation.

    Debug mode: client text + separator + raw data block.
    """
    symbol = row["symbol"]
    severity = row["severity"]
    price = row["price_at_signal"]

    # Price line.
    lines: list[str] = []
    lines.append(f"{friendly_name(symbol)} @ ${price:g}")

    # 24h variation (if available).
    drop_24h = row.get("drop_24h_pct")
    if drop_24h is not None and drop_24h > 0:
        lines.append(f"Variação 24h: -{drop_24h:.1f}%")
    elif drop_24h is not None:
        lines.append("Variação 24h: estável")

    # Top 3 reasons.
    reasons = _build_reason_lines(row)
    for reason in reasons[:3]:
        lines.append(f"• {reason}")

    # Regime line (if not neutral).
    regime = _regime_line(row.get("regime_at_signal"))
    if regime:
        lines.append(regime)

    # Interpretation line.
    _, interpretation = _DECISION_PHRASES.get(severity, _DEFAULT_PHRASE)
    lines.append("")
    lines.append(interpretation)

    body = "\n".join(lines)

    if debug:
        body += "\n\n--- debug ---\n"
        body += f"pair={symbol} score={row.get('score')} severity={severity}\n"
        body += f"trigger={row.get('trigger_reason')}\n"
        body += f"dom_tf={row.get('dominant_trigger_timeframe')} "
        body += f"drop_trigger={row.get('drop_trigger_pct')}\n"
        body += f"rsi_1h={row.get('rsi_1h')} rsi_4h={row.get('rsi_4h')}\n"
        body += f"rel_vol={row.get('rel_volume')}\n"
        body += f"drop_24h={drop_24h} drop_7d={row.get('drop_7d_pct')} "
        body += f"drop_30d={row.get('drop_30d_pct')}\n"
        body += f"disc_30d={row.get('distance_from_30d_high_pct')} "
        body += f"disc_180d={row.get('distance_from_180d_high_pct')}\n"
        body += f"regime={row.get('regime_at_signal')}"

    return body


# ---------- weekly formatting ----------

# Severity order and emoji for weekly breakdown.
_WEEKLY_SEVERITY: tuple[tuple[str, str], ...] = (
    ("very_strong", "🟢"),
    ("strong", "🟡"),
    ("normal", "🟠"),
)


def format_weekly_title(
    week_start_iso: str,
    week_end_iso: str,
    *,
    debug: bool = False,
) -> str:
    """Render the weekly summary title."""
    start_dd_mm = _dd_mm(week_start_iso)
    end_dd_mm = _dd_mm(week_end_iso)
    title = f"Resumo semanal — {start_dd_mm} a {end_dd_mm}"
    if debug:
        title += f" ({week_start_iso[:10]} → {week_end_iso[:10]})"
    return title


def format_weekly_body(
    *,
    week_start_iso: str,
    week_end_iso: str,
    signal_count: int,
    signal_by_severity: Mapping[str, int],
    top_drop_symbol: str | None,
    top_drop_pct: float | None,
    buy_count: int,
    matured_count: int,
    verdict_counts: Mapping[str, int],
    debug: bool = False,
) -> str:
    """Render the weekly summary body in Portuguese."""
    lines: list[str] = []
    lines.append("📊 Resumo da semana")
    lines.append("")

    # Signals section.
    if signal_count == 0:
        lines.append("Sinais emitidos: 0 (semana tranquila)")
    else:
        lines.append(f"Sinais emitidos: {signal_count}")
        for sev, emoji in _WEEKLY_SEVERITY:
            cnt = signal_by_severity.get(sev, 0)
            if cnt:
                sev_label = _severity_label_pt(sev)
                lines.append(f"  {emoji} {sev_label}: {cnt}")

    # Top drop.
    if top_drop_symbol is not None and top_drop_pct is not None:
        name = friendly_name(top_drop_symbol)
        lines.append(f"Maior queda: {name} (-{top_drop_pct:.1f}%)")

    # Buys section (only if > 0).
    if buy_count > 0:
        lines.append("")
        lines.append(f"Compras registradas: {buy_count}")

    # Verdicts section (only if matured > 0).
    if matured_count > 0:
        lines.append("")
        lines.append(f"Avaliações vencidas: {matured_count}")
        good_count = verdict_counts.get("great", 0) + verdict_counts.get("good", 0)
        neutral_count = verdict_counts.get("neutral", 0)
        bad_count = verdict_counts.get("poor", 0) + verdict_counts.get("bad", 0)
        if good_count:
            lines.append(f"  ✅ Boas: {good_count}")
        if neutral_count:
            lines.append(f"  ⚠️ Neutras: {neutral_count}")
        if bad_count:
            lines.append(f"  ❌ Ruins: {bad_count}")

    # Conclusion line — always present.
    lines.append("")
    lines.append(_weekly_conclusion(signal_count, signal_by_severity))

    if debug:
        lines.append("")
        lines.append("--- debug ---")
        lines.append(f"window={week_start_iso[:10]} → {week_end_iso[:10]}")
        lines.append(f"signal_count={signal_count}")
        lines.append(f"severity_breakdown={dict(signal_by_severity)}")
        if top_drop_symbol:
            lines.append(f"top_drop={top_drop_symbol} {top_drop_pct}")
        lines.append(f"buy_count={buy_count}")
        lines.append(f"matured={matured_count} verdicts={dict(verdict_counts)}")

    return "\n".join(lines)


# ---------- helpers ----------

def _dd_mm(iso: str) -> str:
    """Extract dd/MM from an ISO timestamp like '2026-04-11T...'."""
    return f"{iso[8:10]}/{iso[5:7]}"


def _severity_label_pt(severity: str) -> str:
    """Map severity to a short Portuguese label."""
    return {
        "very_strong": "Críticos",
        "strong": "Fortes",
        "normal": "Normais",
    }.get(severity, severity)


def _weekly_conclusion(
    signal_count: int,
    signal_by_severity: Mapping[str, int],
) -> str:
    """Generate the final one-line conclusion for the weekly summary."""
    if signal_count == 0:
        return "Leitura rápida: sem oportunidades nesta semana."

    vs = signal_by_severity.get("very_strong", 0)
    strong = signal_by_severity.get("strong", 0)

    if vs > 0:
        return "Leitura rápida: houve sinal forte de compra nesta semana."
    if strong > 0:
        return "Leitura rápida: sinais moderados — vale acompanhar."
    return "Leitura rápida: apenas sinais fracos, sem urgência."
