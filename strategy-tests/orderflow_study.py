"""Strategy research on ATAS order flow exports (OrderFlowExporter CSVs).

Four analyses, each split in-sample (first --is-frac of the bars) / out-of-sample (rest):

  1. Features      - which order flow features (delta %, delta position, absorption, stacked imbalances,
                     unfinished auctions, delta/price divergence ...) predict the next N bars?
                     Rank correlation (IC) per horizon + quintile table.
  2. Bot defaults  - the four OrderFlowAutoBot modules with their default settings: forward ticks per horizon.
  3. Param grid    - each module over a grid of thresholds (volume/delta thresholds from IS quantiles).
                     'robust' = enough signals, IS t >= 1.5 and OOS still positive with t >= 1.
  4. Brackets      - stop/target grid (+ cooldown, session close) for the best IS setting of every module,
                     like the bot trades it; plus MFE/MAE of the signals to size stop and target.

Results go to output/orderflow_<file>_<timestamp>/ incl. summary.md with suggested ATAS parameter values.
Only fresh signals count in the event studies (signal differs from the previous bar's) to limit overlap.

Examples:
    python orderflow_study.py "C:/Users/MH/Documents/ATAS_OrderFlowExport/ESZ6_1m_20261024-101500_bars.csv"
    python orderflow_study.py <folder>                         # newest *_bars.csv in the folder
    python orderflow_study.py <file> --tz Europe/Berlin --entry-window 15:30-21:45 --cost-ticks 1.5
"""

from __future__ import annotations

import argparse
import itertools
import sys
from datetime import datetime, time
from pathlib import Path

import numpy as np
import pandas as pd

import orderflow as of

ROOT = Path(__file__).parent
OUTPUT_DIR = ROOT / "output"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def resolve_bars_path(arg: str) -> Path:
    p = Path(arg)
    if p.is_dir():
        files = sorted(p.glob("*_bars.csv"), key=lambda f: f.stat().st_mtime)
        if not files:
            raise SystemExit(f"no *_bars.csv in {p}")
        return files[-1]
    if not p.exists():
        raise SystemExit(f"file not found: {p}")
    return p


def window_mask(index: pd.DatetimeIndex, text: str | None) -> np.ndarray:
    if not text:
        return np.ones(len(index), bool)
    a, b = (time.fromisoformat(x.strip()) for x in text.split("-"))
    clock = index.time
    return (clock >= a) & (clock < b) if a < b else (clock >= a) | (clock < b)


def fresh(sig: np.ndarray) -> np.ndarray:
    """Keep only the first bar of a run of identical signals."""
    prev = np.concatenate([[0], sig[:-1]])
    return np.where(sig != prev, sig, 0)


def signed_stats(sig: np.ndarray, fwd: np.ndarray, drift: float, mask: np.ndarray) -> dict:
    """Mean signed forward ticks of the signals (drift-adjusted), hit rate and t-stat vs zero."""
    sel = (sig != 0) & mask & ~np.isnan(fwd)
    x = sig[sel] * (fwd[sel] - drift)
    raw = sig[sel] * fwd[sel]
    n = len(x)
    if n == 0:
        return {"n": 0, "mean": np.nan, "hit": np.nan, "t": np.nan}
    sd = x.std(ddof=1) if n > 1 else np.nan
    return {"n": n, "mean": raw.mean(), "hit": (raw > 0).mean(),
            "t": x.mean() / (sd / np.sqrt(n)) if n > 1 and sd > 0 else np.nan}


def spearman(a: pd.Series, b: pd.Series) -> float:
    ok = a.notna() & b.notna()
    if ok.sum() < 30 or a[ok].nunique() < 2:
        return np.nan
    return float(np.corrcoef(a[ok].rank(), b[ok].rank())[0, 1])


def nice_volume(x: float) -> int:
    return int(max(1, round(x)))


# ---------------------------------------------------------------------------
# Analyses
# ---------------------------------------------------------------------------

def feature_analysis(df, fwd: dict[int, pd.Series], is_mask, main_h: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    feats = [f for f in of.FEATURES if f in df and df[f].notna().sum() > 50]
    rows = []
    for f in feats:
        row = {"feature": f}
        for h, y in fwd.items():
            row[f"ic_is_{h}"] = spearman(df[f][is_mask], y[is_mask])
            row[f"ic_oos_{h}"] = spearman(df[f][~is_mask], y[~is_mask])
        rows.append(row)
    ic = pd.DataFrame(rows)
    if len(ic):
        ic["same_sign_main"] = np.sign(ic[f"ic_is_{main_h}"]) == np.sign(ic[f"ic_oos_{main_h}"])
        ic = ic.sort_values(f"ic_is_{main_h}", key=lambda s: s.abs(), ascending=False)

    q_rows = []
    y = fwd[main_h]
    for f in feats:
        x = df[f]
        try:
            buckets = pd.qcut(x[is_mask].rank(method="first"), 5, labels=False)
        except ValueError:
            continue
        edges = x[is_mask].groupby(buckets).agg(["min", "max"])
        for part, m in (("is", is_mask), ("oos", ~is_mask)):
            b = np.searchsorted(edges["max"].to_numpy()[:-1], x[m].to_numpy(), side="left")
            g = pd.DataFrame({"q": b + 1, "y": y[m].to_numpy()}).dropna().groupby("q")["y"]
            for q, s in g:
                q_rows.append({"feature": f, "sample": part, "quintile": q, "from": edges["min"].iloc[q - 1],
                               "to": edges["max"].iloc[q - 1], "n": len(s), "mean_ticks": s.mean(),
                               "hit_long": (s > 0).mean()})
    return ic, pd.DataFrame(q_rows)


def module_signals(df, levels, tick, params: dict, poc_cache: dict) -> dict[str, np.ndarray]:
    out = {}
    p = params["delta_imbalance"]
    out["delta_imbalance"] = of.sig_delta_imbalance(df, p["ratio"], p["min_volume"], p["confirm_bars"])
    p = params["cum_delta_divergence"]
    out["cum_delta_divergence"] = of.sig_cum_delta_divergence(df, p["lookback"], p["min_delta"], p["max_move"])
    p = params["absorption"]
    out["absorption"] = of.sig_absorption(df, p["min_volume"], p["max_move"], p["min_delta_abs"])
    if levels is not None:
        p = params["poc_proximity"]
        if p["lookback"] not in poc_cache:
            poc_cache[p["lookback"]] = of.rolling_poc(df, levels, p["lookback"])
        out["poc_proximity"] = of.sig_poc_proximity(df, poc_cache[p["lookback"]], p["proximity"])
    return out


def param_grid(df, levels, tick, is_mask) -> dict[str, list[dict]]:
    """Grids per module; volume/delta thresholds come from IS quantiles so the test set stays unseen."""
    d_is = df[is_mask]
    q = lambda s, qs: sorted({nice_volume(s.quantile(x)) for x in qs})  # noqa: E731
    grids = {
        "delta_imbalance": [dict(ratio=r, min_volume=v, confirm_bars=c)
                            for r, v, c in itertools.product([1.5, 2, 3, 4], q(d_is["volume"], [0.25, 0.5, 0.75]),
                                                             [1, 2, 3])],
        "absorption": [dict(min_volume=v, max_move=m * tick, min_delta_abs=dd)
                       for v, m, dd in itertools.product(q(d_is["volume"], [0.5, 0.75, 0.9]), [0, 1, 2, 4],
                                                         q(d_is["delta"].abs(), [0.5, 0.75, 0.9]))],
        "cum_delta_divergence": [],
        "poc_proximity": [] if levels is None else
        [dict(lookback=lb, proximity=pr * tick) for lb, pr in itertools.product([20, 50, 100], [1, 2, 4, 8])],
    }
    for lb in (5, 10, 20, 40):
        rec = d_is["delta"].rolling(lb).sum()
        change = (rec - rec.shift(lb)).abs()
        for md, mm in itertools.product(q(change, [0.75, 0.9, 0.95]), [0, 2, 4]):
            grids["cum_delta_divergence"].append(dict(lookback=lb, min_delta=float(md), max_move=mm * tick))
    return grids


def run_module(name, p, df, levels, poc_cache):
    p = {k: int(v) if k in ("lookback", "confirm_bars") else float(v) for k, v in p.items()}
    if name == "delta_imbalance":
        return of.sig_delta_imbalance(df, p["ratio"], p["min_volume"], p["confirm_bars"])
    if name == "cum_delta_divergence":
        return of.sig_cum_delta_divergence(df, p["lookback"], p["min_delta"], p["max_move"])
    if name == "absorption":
        return of.sig_absorption(df, p["min_volume"], p["max_move"], p["min_delta_abs"])
    if p["lookback"] not in poc_cache:
        poc_cache[p["lookback"]] = of.rolling_poc(df, levels, p["lookback"])
    return of.sig_poc_proximity(df, poc_cache[p["lookback"]], p["proximity"])


def bracket_levels(median_range_ticks: float) -> tuple[list[int], list[int]]:
    r = max(median_range_ticks, 1)
    sl = sorted({max(1, round(r * k)) for k in (0.5, 1, 1.5, 2, 3, 4)})
    tp = sorted({max(1, round(r * k)) for k in (0.5, 1, 1.5, 2, 3, 4, 6)})
    return sl, tp


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

ATAS_NAMES = {
    "delta_imbalance": {"ratio": "Imbalance-Verhältnis", "min_volume": "Min. Volumen für Imbalance-Bar",
                        "confirm_bars": "Bestätigungs-Bars"},
    "cum_delta_divergence": {"lookback": "Lookback (Bars) je Fenster", "min_delta": "Min. Delta-Änderung",
                             "max_move": "Max. Preisänderung (Punkte)"},
    "absorption": {"min_volume": "Min. Volumen pro Bar", "max_move": "Max. Preisbewegung (Punkte)",
                   "min_delta_abs": "Min. absolutes Delta"},
    "poc_proximity": {"lookback": "POC Lookback (Bars)", "proximity": "Max. Abstand zum POC (Punkte)"},
}


def fmt(x, d=2):
    return "–" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:.{d}f}"


def plot_ic(ic: pd.DataFrame, horizons: list[int], path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    if ic.empty:
        return
    m = ic.set_index("feature")[[f"ic_is_{h}" for h in horizons] + [f"ic_oos_{h}" for h in horizons]]
    fig, ax = plt.subplots(figsize=(1.1 * m.shape[1] + 3, 0.45 * len(m) + 1.5))
    lim = np.nanmax(np.abs(m.to_numpy())) or 0.05
    im = ax.imshow(m.to_numpy(), cmap="RdBu", vmin=-lim, vmax=lim, aspect="auto")
    ax.set_xticks(range(m.shape[1]), [c.replace("ic_", "").replace("_", " ") for c in m.columns], rotation=45)
    ax.set_yticks(range(len(m)), m.index)
    for (i, j), v in np.ndenumerate(m.to_numpy()):
        if not np.isnan(v):
            ax.text(j, i, f"{v:.3f}", ha="center", va="center", fontsize=7)
    ax.set_title("Rank-IC feature vs. forward ticks (IS | OOS, horizon in bars)")
    fig.colorbar(im, ax=ax, shrink=0.8)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bars", help="*_bars.csv from OrderFlowExporter, or a folder (newest file is used)")
    ap.add_argument("--levels", help="*_levels.csv (default: next to the bars file)")
    ap.add_argument("--tz", help="convert ATAS UTC times, e.g. Europe/Berlin or America/Chicago")
    ap.add_argument("--start"), ap.add_argument("--end")
    ap.add_argument("--session-gap", type=float, default=60, help="minutes of pause that start a new session")
    ap.add_argument("--entry-window", help="only signals whose bar starts in HH:MM-HH:MM (after --tz)")
    ap.add_argument("--horizons", default="1,3,5,10,20")
    ap.add_argument("--main-horizon", type=int, default=10)
    ap.add_argument("--is-frac", type=float, default=0.7, help="share of bars used in-sample")
    ap.add_argument("--min-n", type=int, default=30, help="min. signals per sample for a 'robust' setting")
    ap.add_argument("--cost-ticks", type=float, default=1.0, help="round-trip costs + slippage in ticks")
    ap.add_argument("--tick-value", type=float, help="money per tick and contract, e.g. 12.5 for ES")
    ap.add_argument("--cooldown", type=int, default=of.BOT_RISK_DEFAULTS["cooldown"])
    ap.add_argument("--imb-ratio", type=float, default=3.0, help="diagonal imbalance ratio for footprint features")
    ap.add_argument("--out", help="output folder")
    a = ap.parse_args(argv)

    bars_path = resolve_bars_path(a.bars)
    bars, levels = of.load_export(bars_path, Path(a.levels) if a.levels else None, a.tz, a.start, a.end)
    if len(bars) < 200:
        print(f"only {len(bars)} bars - export more history in ATAS (Chart -> 'Tage laden').", file=sys.stderr)
        return 1
    tick = of.tick_size(bars)
    sessions = of.session_ids(bars.index, a.session_gap)
    horizons = [int(h) for h in a.horizons.split(",")]
    if a.main_horizon not in horizons:
        horizons.append(a.main_horizon)

    df = of.add_features(bars, levels, tick, sessions, imb_ratio=a.imb_ratio)
    fwd = {h: of.forward_ticks(df, h, tick, sessions) for h in horizons}
    n = len(df)
    split = int(n * a.is_frac)
    is_mask = np.arange(n) < split
    allowed = window_mask(df.index, a.entry_window)
    main = fwd[a.main_horizon].to_numpy()
    drift = np.nanmean(main[is_mask])

    out = Path(a.out) if a.out else OUTPUT_DIR / f"orderflow_{bars_path.stem.removesuffix('_bars')}_{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"{bars_path.name}: {n} bars {df.index[0]} .. {df.index[-1]}, tick {tick:g}, "
          f"{len(np.unique(sessions))} sessions, levels {'yes' if levels is not None else 'NO'}")
    print(f"in-sample until {df.index[split - 1]}  |  output: {out}")

    # 1. Features --------------------------------------------------------
    ic, quint = feature_analysis(df, fwd, is_mask, a.main_horizon)
    ic.to_csv(out / "feature_ic.csv", index=False)
    quint.to_csv(out / "feature_quintiles.csv", index=False)
    plot_ic(ic, horizons, out / "feature_ic.png")

    # 2. Bot defaults ----------------------------------------------------
    poc_cache: dict[int, np.ndarray] = {}
    sigs = module_signals(df, levels, tick, of.BOT_DEFAULTS, poc_cache)
    sigs["combined_min1"] = of.combine_votes(list(sigs.values()), 1)
    sigs["combined_min2"] = of.combine_votes([sigs[k] for k in of.BOT_DEFAULTS if k in sigs], 2)
    rows = []
    for name, s in sigs.items():
        s = fresh(s)
        for part, m in (("is", is_mask), ("oos", ~is_mask)):
            row = {"signal": name, "sample": part}
            for h in horizons:
                st = signed_stats(s, fwd[h].to_numpy(), np.nanmean(fwd[h].to_numpy()[is_mask]), m & allowed)
                row.update({"n": st["n"], f"mean_{h}": st["mean"], f"hit_{h}": st["hit"], f"t_{h}": st["t"]})
            rows.append(row)
    defaults = pd.DataFrame(rows)
    defaults.to_csv(out / "signals_bot_defaults.csv", index=False)

    # 3. Parameter grid ---------------------------------------------------
    grid_rows = []
    for name, combos in param_grid(df, levels, tick, is_mask).items():
        for p in combos:
            s = fresh(run_module(name, p, df, levels, poc_cache))
            si = signed_stats(s, main, drift, is_mask & allowed)
            so = signed_stats(s, main, drift, ~is_mask & allowed)
            grid_rows.append({"module": name, **{f"p_{k}": v for k, v in p.items()},
                              "is_n": si["n"], "is_mean": si["mean"], "is_hit": si["hit"], "is_t": si["t"],
                              "oos_n": so["n"], "oos_mean": so["mean"], "oos_hit": so["hit"], "oos_t": so["t"]})
    grid = pd.DataFrame(grid_rows)
    grid["robust"] = ((grid["is_n"] >= a.min_n) & (grid["oos_n"] >= a.min_n // 2)
                      & (grid["is_t"] >= 1.5) & (grid["oos_mean"] > 0) & (grid["oos_t"] >= 1.0))
    grid = grid.sort_values(["module", "is_t"], ascending=[True, False])
    grid.to_csv(out / "param_grid.csv", index=False)

    best: dict[str, dict] = {}
    for name, g in grid[grid["is_n"] >= a.min_n].groupby("module"):
        r = g.iloc[0]
        best[name] = {k[2:]: r[k] for k in g.columns if k.startswith("p_") and pd.notna(r[k])}
        best[name]["_row"] = r

    # 4. Brackets + excursions --------------------------------------------
    sl_grid, tp_grid = bracket_levels(float(df["range_ticks"][is_mask].median()))
    mfe, mae = of.excursions(df, a.main_horizon, tick, sessions)
    br_rows, ex_rows = [], []
    candidates = {f"{k} (best IS)": v for k, v in best.items()}
    candidates["bot defaults combined"] = None
    for label, p in candidates.items():
        if p is None:
            s = sigs["combined_min1"]
        else:
            module = label.split(" ")[0]
            s = run_module(module, {k: v for k, v in p.items() if k != "_row"}, df, levels, poc_cache)
        f = fresh(s)
        sel = (f != 0) & allowed
        fav = np.where(f > 0, mfe, mae)[sel]
        adv = np.where(f > 0, mae, mfe)[sel]
        ok = ~np.isnan(fav)
        if ok.sum():
            ex_rows.append({"signal": label, "n": int(ok.sum()),
                            **{f"mfe_p{q}": np.percentile(fav[ok], q) for q in (25, 50, 75, 90)},
                            **{f"mae_p{q}": np.percentile(adv[ok], q) for q in (25, 50, 75, 90)}})
        for sl, tp in itertools.product(sl_grid, tp_grid):
            for part, m in (("is", is_mask), ("oos", ~is_mask)):
                tr = of.simulate(df, np.where(m, s, 0), tick, sessions, sl_ticks=sl, tp_ticks=tp,
                                 cost_ticks=a.cost_ticks, cooldown=a.cooldown, allowed=allowed)
                br_rows.append({"signal": label, "sl_ticks": sl, "tp_ticks": tp, "sample": part,
                                **of.trade_stats(tr)})
    brackets = pd.DataFrame(br_rows)
    brackets.to_csv(out / "bracket_grid.csv", index=False)
    pd.DataFrame(ex_rows).to_csv(out / "excursions.csv", index=False)

    # Bot exactly as configured by default (SL 20, TP = 1.5 x SL, breakeven 20/+2)
    rd = of.BOT_RISK_DEFAULTS
    bot_default = {part: of.trade_stats(of.simulate(
        df, np.where(m, sigs["combined_min1"], 0), tick, sessions, sl_ticks=rd["sl_ticks"], tp_ticks=rd["tp_ticks"],
        cost_ticks=a.cost_ticks, cooldown=rd["cooldown"], be_trigger=rd["be_trigger"], be_offset=rd["be_offset"],
        allowed=allowed)) for part, m in (("is", is_mask), ("oos", ~is_mask))}

    write_summary(out / "summary.md", a, bars_path, df, tick, split, ic, defaults, grid, best, brackets,
                  pd.DataFrame(ex_rows), bot_default, horizons, levels is not None)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # Windows console (cp1252)
    print((out / "summary.md").read_text(encoding="utf-8"))
    return 0


def write_summary(path, a, bars_path, df, tick, split, ic, defaults, grid, best, brackets, ex, bot_default,
                  horizons, has_levels):
    h = a.main_horizon
    money = (lambda t: f" ({t * a.tick_value:,.0f})") if a.tick_value else (lambda t: "")
    L = [f"# Orderflow-Studie: {bars_path.name}", "",
         f"- {len(df)} Bars, {df.index[0]} bis {df.index[-1]}, Tick {tick:g}, Kosten {a.cost_ticks:g} Ticks/Trade",
         f"- In-Sample bis {df.index[split - 1]} ({a.is_frac:.0%}), danach Out-of-Sample",
         f"- Haupthorizont {h} Bars; Footprint-Daten: {'ja' if has_levels else 'NEIN (POC/Imbalance-Features fehlen)'}",
         "", "## 1. Welche Orderflow-Merkmale haben Vorhersagekraft?", "",
         f"Rang-Korrelation (IC) Merkmal -> Ticks der nächsten {h} Bars. |IC| ≥ 0,03 ist bei Intraday-Daten "
         "schon bemerkenswert, aber nur, wenn IS und OOS dasselbe Vorzeichen haben. "
         "Positiv = Momentum (Merkmal hoch -> Preis steigt), negativ = Umkehr.", "",
         "| Merkmal | IC IS | IC OOS | stabil |", "|---|---|---|---|"]
    for _, r in ic.head(8).iterrows():
        L.append(f"| {r['feature']} | {fmt(r[f'ic_is_{h}'], 3)} | {fmt(r[f'ic_oos_{h}'], 3)} | "
                 f"{'ja' if r['same_sign_main'] else 'nein'} |")

    L += ["", "## 2. Bot-Module mit Standard-Einstellungen", "",
          f"Durchschnittliche Ticks in Signalrichtung nach {h} Bars (vor Kosten), t-Wert gegen Drift.", "",
          "| Signal | n IS | Ø IS | t IS | n OOS | Ø OOS | t OOS |", "|---|---|---|---|---|---|---|"]
    for name, g in defaults.groupby("signal", sort=False):
        i, o = g[g["sample"] == "is"].iloc[0], g[g["sample"] == "oos"].iloc[0]
        L.append(f"| {name} | {i['n']} | {fmt(i[f'mean_{h}'])} | {fmt(i[f't_{h}'])} | {o['n']} | "
                 f"{fmt(o[f'mean_{h}'])} | {fmt(o[f't_{h}'])} |")
    for part in ("is", "oos"):
        st = bot_default[part]
        if st.get("trades"):
            L.append(f"\nBot komplett (alle Module, SL 20 / TP 30 Ticks, Breakeven 20+2, Cooldown 5) {part.upper()}: "
                     f"{st['trades']} Trades, Trefferquote {st['win_rate']:.0%}, Ø {st['avg_ticks']:.2f} Ticks, "
                     f"Summe {st['total_ticks']:.0f} Ticks{money(st['total_ticks'])}, PF {st['profit_factor']:.2f}")

    L += ["", "## 3. Beste Parameter je Modul (nach IS-t-Wert, mind. "
          f"{a.min_n} Signale)", "",
          "In ATAS-Einheiten (Punkte = Preis-Einheiten, Volumen in Kontrakten). "
          "Nur Zeilen mit **robust = ja** haben im OOS-Zeitraum bestätigt.", ""]
    for name, p in best.items():
        r = p["_row"]
        settings = ", ".join(f"{ATAS_NAMES[name].get(k, k)} = {v:g}" for k, v in p.items() if k != "_row")
        L.append(f"- **{name}**: {settings}  \n  IS n={r['is_n']}, Ø {fmt(r['is_mean'])} Ticks, t={fmt(r['is_t'])} · "
                 f"OOS n={r['oos_n']}, Ø {fmt(r['oos_mean'])} Ticks, t={fmt(r['oos_t'])} · "
                 f"robust: {'ja' if r['robust'] else 'nein'}")
    n_rob = int(grid["robust"].sum())
    L.append(f"\n{n_rob} von {len(grid)} getesteten Einstellungen sind robust (Details: param_grid.csv).")

    L += ["", "## 4. Stop/Target (Bracket wie im Bot, ein Trade gleichzeitig, Cooldown "
          f"{a.cooldown}, Schluss zum Sessionende)", "",
          "Beste Kombination je Signal nach IS-Summe, mit OOS-Kontrolle (nach Kosten):", "",
          "| Signal | SL | TP | Trades IS | Ø IS | PF IS | Trades OOS | Ø OOS | PF OOS |",
          "|---|---|---|---|---|---|---|---|---|"]
    if len(brackets) and "avg_ticks" in brackets:
        for name, g in brackets.groupby("signal", sort=False):
            gi = g[(g["sample"] == "is") & (g["trades"] >= max(10, a.min_n // 2))]
            if gi.empty:
                continue
            b = gi.sort_values("total_ticks", ascending=False).iloc[0]
            o = g[(g["sample"] == "oos") & (g["sl_ticks"] == b["sl_ticks"]) & (g["tp_ticks"] == b["tp_ticks"])].iloc[0]
            L.append(f"| {name} | {b['sl_ticks']} | {b['tp_ticks']} | {b['trades']} | {fmt(b['avg_ticks'])} | "
                     f"{fmt(b['profit_factor'])} | {o.get('trades', 0)} | {fmt(o.get('avg_ticks', np.nan))} | "
                     f"{fmt(o.get('profit_factor', np.nan))} |")
    if len(ex):
        L += ["", f"Kursausschläge nach Signalen ({h} Bars, Ticks): Median MFE / MAE — damit Stop jenseits der "
              "typischen MAE und Ziel innerhalb der typischen MFE setzen.", ""]
        for _, r in ex.iterrows():
            L.append(f"- {r['signal']}: MFE p50 {r['mfe_p50']:.1f} / p75 {r['mfe_p75']:.1f}, "
                     f"MAE p50 {r['mae_p50']:.1f} / p75 {r['mae_p75']:.1f}")

    L += ["", "## Hinweise", "",
          "- Viele Parameter getestet => der beste IS-Wert ist optimistisch. Maßgeblich ist die OOS-Spalte.",
          "- Bar-Simulation: liegen Stop und Ziel im selben Bar, zählt der Stop. Kein Slippage-Modell über "
          "--cost-ticks hinaus.",
          "- Vor dem Live-Einsatz Einstellungen im ATAS Strategy Tester (Marktreplay) und im Sim-Konto prüfen.",
          "- Keine Anlageberatung."]
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
