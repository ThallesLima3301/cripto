"""Human-readable output for the simulated portfolio."""

from __future__ import annotations

from crypto_monitor.paper.types import SimulationReport


def format_simulation_report(report: SimulationReport) -> str:
    config = report.config
    source = "envios registrados como bem-sucedidos" if config.signal_source == "delivered" else "sinais detectados (pesquisa)"
    lines = [
        "CARTEIRA SIMULADA — sem ordens reais",
        f"Período UTC: {report.start} até {report.end} (fim exclusivo)",
        f"Entrada: {source}; score mínimo {config.min_score:g}",
        f"Capital: {config.initial_capital:,.2f} USDT; por compra: {config.position_pct:g}%; "
        f"máximo: {config.max_positions} posições; saída após {config.holding_hours}h",
        f"Custos por lado: taxa {config.fee_bps:g} bps + slippage {config.slippage_bps:g} bps",
        "",
        f"Patrimônio líquido final: {report.ending_equity:,.2f} USDT",
        f"Lucro/prejuízo líquido: {report.net_profit:+,.2f} USDT ({report.net_return_pct:+.2f}%)",
        f"Maior queda do patrimônio: {report.max_drawdown_pct:.2f}%",
        f"Caixa final: {report.ending_cash:,.2f} USDT; taxas já debitadas: {report.fees_paid:,.2f} USDT",
        f"Operações encerradas: {report.closed_trades}; posições abertas: {report.open_positions}",
    ]
    if report.win_rate_pct is not None:
        lines.append(f"Acerto nas operações encerradas: {report.win_rate_pct:.2f}%")
    if report.average_trade_return_pct is not None:
        lines.append(f"Retorno médio por operação encerrada: {report.average_trade_return_pct:+.2f}%")
    if report.profit_factor is not None:
        lines.append(f"Fator de lucro nas operações encerradas: {report.profit_factor:.2f}")
    lines.extend([
        f"Comprar e manter {report.benchmark.symbol}: {report.benchmark.net_return_pct:+.2f}% "
        f"({report.benchmark.net_profit:+,.2f} USDT)",
        f"Diferença para comprar e manter: {report.excess_return_pct:+.2f} pontos percentuais",
        f"Sinais considerados: {report.signals_considered}",
    ])
    if report.skipped:
        lines.append("Sinais sem compra: " + ", ".join(f"{reason}={count}" for reason, count in sorted(report.skipped.items()) if count))
    if report.open_positions:
        lines.append("Posições abertas avaliadas pelo último fechamento, descontando custos hipotéticos de venda.")
    if report.trades:
        lines.extend(["", "Operações simuladas (até 20; --json contém todas):"])
        for trade in report.trades[:20]:
            outcome = "aberta" if trade.net_pnl is None else f"{trade.net_pnl:+.2f} USDT"
            lines.append(
                f"  sinal {trade.signal_id} {trade.symbol}: {trade.entry_at} "
                f"-> {trade.exit_at or 'em aberto'}; {outcome}"
            )
    lines.extend(f"Observação: {warning}" for warning in report.warnings)
    return "\n".join(lines)
