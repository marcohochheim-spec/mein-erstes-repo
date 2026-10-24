using System;
using System.ComponentModel.DataAnnotations;

namespace OrderFlowAutoBot
{
    public enum TradeDirectionMode
    {
        Both,
        LongOnly,
        ShortOnly
    }

    public enum StopLossMode
    {
        FixedTicks,
        AtrMultiple
    }

    public enum TakeProfitMode
    {
        FixedTicks,
        RiskRewardRatio,
        AtrMultiple
    }

    public partial class OrderFlowSignalStrategy
    {
        #region General

        private TradeDirectionMode _tradeDirection = TradeDirectionMode.Both;

        [Display(Name = "Trade direction", GroupName = "General", Order = 10,
            Description = "Beschränkt den Bot auf Long, Short oder beide Richtungen.")]
        public TradeDirectionMode TradeDirection
        {
            get { return _tradeDirection; }
            set { _tradeDirection = value; RecalculateValues(); }
        }

        private int _orderVolume = 1;

        [Display(Name = "Order volume (contracts)", GroupName = "General", Order = 20)]
        [Range(1, 1000)]
        public int OrderVolume
        {
            get { return _orderVolume; }
            set { _orderVolume = value; RecalculateValues(); }
        }

        private int _minSignalsRequired = 1;

        [Display(Name = "Min. übereinstimmende Signale", GroupName = "General", Order = 30,
            Description = "Wie viele der aktivierten Signalmodule in dieselbe Richtung zeigen müssen, damit ein Einstieg ausgelöst wird.")]
        [Range(1, 4)]
        public int MinSignalsRequired
        {
            get { return _minSignalsRequired; }
            set { _minSignalsRequired = value; RecalculateValues(); }
        }

        private int _cooldownBarsAfterTrade = 5;

        [Display(Name = "Cooldown (Bars) nach Trade", GroupName = "General", Order = 40,
            Description = "Anzahl Bars nach Positionsschluss, in denen keine neuen Signale ausgewertet werden.")]
        [Range(0, 500)]
        public int CooldownBarsAfterTrade
        {
            get { return _cooldownBarsAfterTrade; }
            set { _cooldownBarsAfterTrade = value; RecalculateValues(); }
        }

        #endregion

        #region Session & Limits

        private bool _useSessionFilter;

        [Display(Name = "Handelszeitfenster aktiv", GroupName = "Session & Limits", Order = 10)]
        public bool UseSessionFilter
        {
            get { return _useSessionFilter; }
            set { _useSessionFilter = value; RecalculateValues(); }
        }

        private TimeSpan _sessionStart = new TimeSpan(9, 0, 0);

        [Display(Name = "Session Start (Plattformzeit)", GroupName = "Session & Limits", Order = 20)]
        public TimeSpan SessionStart
        {
            get { return _sessionStart; }
            set { _sessionStart = value; RecalculateValues(); }
        }

        private TimeSpan _sessionEnd = new TimeSpan(22, 0, 0);

        [Display(Name = "Session Ende (Plattformzeit)", GroupName = "Session & Limits", Order = 30)]
        public TimeSpan SessionEnd
        {
            get { return _sessionEnd; }
            set { _sessionEnd = value; RecalculateValues(); }
        }

        private int _maxTradesPerDay = 10;

        [Display(Name = "Max. Trades pro Tag", GroupName = "Session & Limits", Order = 40)]
        [Range(1, 1000)]
        public int MaxTradesPerDay
        {
            get { return _maxTradesPerDay; }
            set { _maxTradesPerDay = value; RecalculateValues(); }
        }

        private bool _useMaxDailyLoss;

        [Display(Name = "Tagesverlustlimit aktiv", GroupName = "Session & Limits", Order = 50)]
        public bool UseMaxDailyLoss
        {
            get { return _useMaxDailyLoss; }
            set { _useMaxDailyLoss = value; RecalculateValues(); }
        }

        private decimal _maxDailyLossMoney = 500m;

        [Display(Name = "Max. Tagesverlust (realisiert, Kontowährung)", GroupName = "Session & Limits", Order = 60,
            Description = "Sinkt der realisierte Tages-PnL (TradingManager.Position.RealizedPnL) unter -Wert, werden für den Rest des Tages keine neuen Einstiege mehr ausgelöst.")]
        public decimal MaxDailyLossMoney
        {
            get { return _maxDailyLossMoney; }
            set { _maxDailyLossMoney = value; RecalculateValues(); }
        }

        #endregion

        #region Signal: Delta Imbalance

        private bool _useDeltaImbalance = true;

        [Display(Name = "Delta-Imbalance aktiv", GroupName = "Signal: Delta Imbalance", Order = 10)]
        public bool UseDeltaImbalance
        {
            get { return _useDeltaImbalance; }
            set { _useDeltaImbalance = value; RecalculateValues(); }
        }

        private decimal _deltaImbalanceRatio = 3m;

        [Display(Name = "Imbalance-Verhältnis (Ask/Bid bzw. Bid/Ask)", GroupName = "Signal: Delta Imbalance", Order = 20,
            Description = "Ab welchem Verhältnis Ask- zu Bid-Volumen (oder umgekehrt) ein Bar als Imbalance gilt.")]
        [Range(1.0, 50.0)]
        public decimal DeltaImbalanceRatio
        {
            get { return _deltaImbalanceRatio; }
            set { _deltaImbalanceRatio = value; RecalculateValues(); }
        }

        private int _deltaImbalanceMinVolume = 50;

        [Display(Name = "Min. Volumen für Imbalance-Bar", GroupName = "Signal: Delta Imbalance", Order = 30,
            Description = "Filtert Imbalances auf zu dünnen Bars heraus.")]
        [Range(1, 1000000)]
        public int DeltaImbalanceMinVolume
        {
            get { return _deltaImbalanceMinVolume; }
            set { _deltaImbalanceMinVolume = value; RecalculateValues(); }
        }

        private int _deltaImbalanceConfirmBars = 1;

        [Display(Name = "Bestätigungs-Bars (gleiche Richtung)", GroupName = "Signal: Delta Imbalance", Order = 40,
            Description = "Wie viele aufeinanderfolgende Bars die Imbalance in dieselbe Richtung zeigen müssen.")]
        [Range(1, 20)]
        public int DeltaImbalanceConfirmBars
        {
            get { return _deltaImbalanceConfirmBars; }
            set { _deltaImbalanceConfirmBars = value; RecalculateValues(); }
        }

        #endregion

        #region Signal: Cumulative Delta Divergence

        private bool _useCumDeltaDivergence = true;

        [Display(Name = "Cum.-Delta-Divergenz aktiv", GroupName = "Signal: Cumulative Delta Divergence", Order = 10)]
        public bool UseCumDeltaDivergence
        {
            get { return _useCumDeltaDivergence; }
            set { _useCumDeltaDivergence = value; RecalculateValues(); }
        }

        private int _cumDeltaLookbackBars = 20;

        [Display(Name = "Lookback (Bars) je Fenster", GroupName = "Signal: Cumulative Delta Divergence", Order = 20,
            Description = "Größe der beiden Vergleichsfenster für Preis- vs. Delta-Trend.")]
        [Range(2, 2000)]
        public int CumDeltaLookbackBars
        {
            get { return _cumDeltaLookbackBars; }
            set { _cumDeltaLookbackBars = value; RecalculateValues(); }
        }

        private decimal _cumDeltaDivergenceMinDelta = 200m;

        [Display(Name = "Min. Delta-Änderung für Divergenz", GroupName = "Signal: Cumulative Delta Divergence", Order = 30,
            Description = "Mindestunterschied des kumulierten Delta zwischen den beiden Fenstern, damit eine Divergenz gewertet wird.")]
        public decimal CumDeltaDivergenceMinDelta
        {
            get { return _cumDeltaDivergenceMinDelta; }
            set { _cumDeltaDivergenceMinDelta = value; RecalculateValues(); }
        }

        private decimal _cumDeltaDivergenceMaxPriceMovePoints = 0m;

        [Display(Name = "Max. Preisänderung im 1. Fenster (Punkte)", GroupName = "Signal: Cumulative Delta Divergence", Order = 40,
            Description = "Preis darf sich im jüngeren Fenster höchstens um diesen Wert in die 'falsche' Richtung bewegt haben (0 = Preis muss flach/gegenläufig sein).")]
        [Range(0, 100000)]
        public decimal CumDeltaDivergenceMaxPriceMovePoints
        {
            get { return _cumDeltaDivergenceMaxPriceMovePoints; }
            set { _cumDeltaDivergenceMaxPriceMovePoints = value; RecalculateValues(); }
        }

        #endregion

        #region Signal: Absorption

        private bool _useAbsorption = true;

        [Display(Name = "Absorption aktiv", GroupName = "Signal: Absorption", Order = 10,
            Description = "Hohes Volumen bei geringer Preisbewegung = mögliche Absorption durch große Marktteilnehmer.")]
        public bool UseAbsorption
        {
            get { return _useAbsorption; }
            set { _useAbsorption = value; RecalculateValues(); }
        }

        private int _absorptionMinVolume = 500;

        [Display(Name = "Min. Volumen pro Bar", GroupName = "Signal: Absorption", Order = 20)]
        [Range(1, 10000000)]
        public int AbsorptionMinVolume
        {
            get { return _absorptionMinVolume; }
            set { _absorptionMinVolume = value; RecalculateValues(); }
        }

        private decimal _absorptionMaxPriceMovePoints = 1m;

        [Display(Name = "Max. Preisbewegung (Punkte) trotz Volumen", GroupName = "Signal: Absorption", Order = 30)]
        [Range(0, 100000)]
        public decimal AbsorptionMaxPriceMovePoints
        {
            get { return _absorptionMaxPriceMovePoints; }
            set { _absorptionMaxPriceMovePoints = value; RecalculateValues(); }
        }

        private int _absorptionMinDeltaAbs = 100;

        [Display(Name = "Min. absolutes Delta im Bar", GroupName = "Signal: Absorption", Order = 40,
            Description = "Stellt sicher, dass trotz Absorption weiterhin eine klare Kauf- oder Verkaufsseite erkennbar ist (bestimmt Richtung des Signals).")]
        [Range(0, 10000000)]
        public int AbsorptionMinDeltaAbs
        {
            get { return _absorptionMinDeltaAbs; }
            set { _absorptionMinDeltaAbs = value; RecalculateValues(); }
        }

        #endregion

        #region Signal: POC Proximity

        private bool _usePocProximity = true;

        [Display(Name = "POC-Nähe aktiv", GroupName = "Signal: POC Proximity", Order = 10,
            Description = "Erkennt Preisnähe zum Volumenschwerpunkt (Point of Control) der letzten N Bars als Unterstützung/Widerstand.")]
        public bool UsePocProximity
        {
            get { return _usePocProximity; }
            set { _usePocProximity = value; RecalculateValues(); }
        }

        private int _pocLookbackBars = 50;

        [Display(Name = "POC Lookback (Bars)", GroupName = "Signal: POC Proximity", Order = 20)]
        [Range(2, 5000)]
        public int PocLookbackBars
        {
            get { return _pocLookbackBars; }
            set { _pocLookbackBars = value; RecalculateValues(); }
        }

        private decimal _pocProximityPoints = 2m;

        [Display(Name = "Max. Abstand zum POC (Punkte)", GroupName = "Signal: POC Proximity", Order = 30)]
        [Range(0, 100000)]
        public decimal PocProximityPoints
        {
            get { return _pocProximityPoints; }
            set { _pocProximityPoints = value; RecalculateValues(); }
        }

        #endregion

        #region Risk Management: Stop Loss

        private StopLossMode _stopLossMode = StopLossMode.FixedTicks;

        [Display(Name = "Stop-Loss Modus", GroupName = "Risk Management: Stop", Order = 10)]
        public StopLossMode StopLossMode
        {
            get { return _stopLossMode; }
            set { _stopLossMode = value; RecalculateValues(); }
        }

        private int _stopLossTicks = 20;

        [Display(Name = "Stop-Loss (Ticks, fix)", GroupName = "Risk Management: Stop", Order = 20)]
        [Range(1, 100000)]
        public int StopLossTicks
        {
            get { return _stopLossTicks; }
            set { _stopLossTicks = value; RecalculateValues(); }
        }

        private int _atrPeriod = 14;

        [Display(Name = "ATR Periode", GroupName = "Risk Management: Stop", Order = 30)]
        [Range(1, 500)]
        public int AtrPeriod
        {
            get { return _atrPeriod; }
            set { _atrPeriod = value; RecalculateValues(); }
        }

        private decimal _atrMultiplierSl = 1.5m;

        [Display(Name = "ATR Multiplikator (Stop)", GroupName = "Risk Management: Stop", Order = 40)]
        [Range(0.1, 50.0)]
        public decimal AtrMultiplierSl
        {
            get { return _atrMultiplierSl; }
            set { _atrMultiplierSl = value; RecalculateValues(); }
        }

        #endregion

        #region Risk Management: Take Profit

        private TakeProfitMode _takeProfitMode = TakeProfitMode.RiskRewardRatio;

        [Display(Name = "Take-Profit Modus", GroupName = "Risk Management: Target", Order = 10)]
        public TakeProfitMode TakeProfitMode
        {
            get { return _takeProfitMode; }
            set { _takeProfitMode = value; RecalculateValues(); }
        }

        private int _takeProfitTicks = 40;

        [Display(Name = "Take-Profit (Ticks, fix)", GroupName = "Risk Management: Target", Order = 20)]
        [Range(1, 100000)]
        public int TakeProfitTicks
        {
            get { return _takeProfitTicks; }
            set { _takeProfitTicks = value; RecalculateValues(); }
        }

        private decimal _riskRewardRatio = 1.5m;

        [Display(Name = "Chance/Risiko-Verhältnis (TP = SL * X)", GroupName = "Risk Management: Target", Order = 30)]
        [Range(0.1, 50.0)]
        public decimal RiskRewardRatio
        {
            get { return _riskRewardRatio; }
            set { _riskRewardRatio = value; RecalculateValues(); }
        }

        private decimal _atrMultiplierTp = 3m;

        [Display(Name = "ATR Multiplikator (Target)", GroupName = "Risk Management: Target", Order = 40)]
        [Range(0.1, 50.0)]
        public decimal AtrMultiplierTp
        {
            get { return _atrMultiplierTp; }
            set { _atrMultiplierTp = value; RecalculateValues(); }
        }

        #endregion

        #region Risk Management: Breakeven & Trailing

        private bool _useBreakeven = true;

        [Display(Name = "Breakeven aktiv", GroupName = "Risk Management: Breakeven & Trailing", Order = 10)]
        public bool UseBreakeven
        {
            get { return _useBreakeven; }
            set { _useBreakeven = value; RecalculateValues(); }
        }

        private int _breakevenTriggerTicks = 20;

        [Display(Name = "Breakeven-Trigger (Ticks im Profit)", GroupName = "Risk Management: Breakeven & Trailing", Order = 20)]
        [Range(1, 100000)]
        public int BreakevenTriggerTicks
        {
            get { return _breakevenTriggerTicks; }
            set { _breakevenTriggerTicks = value; RecalculateValues(); }
        }

        private int _breakevenOffsetTicks = 2;

        [Display(Name = "Breakeven-Offset (Ticks über Einstieg)", GroupName = "Risk Management: Breakeven & Trailing", Order = 30)]
        [Range(0, 1000)]
        public int BreakevenOffsetTicks
        {
            get { return _breakevenOffsetTicks; }
            set { _breakevenOffsetTicks = value; RecalculateValues(); }
        }

        private bool _useTrailingStop;

        [Display(Name = "Trailing-Stop aktiv", GroupName = "Risk Management: Breakeven & Trailing", Order = 40)]
        public bool UseTrailingStop
        {
            get { return _useTrailingStop; }
            set { _useTrailingStop = value; RecalculateValues(); }
        }

        private int _trailingStopTicks = 30;

        [Display(Name = "Trailing-Stop Abstand (Ticks)", GroupName = "Risk Management: Breakeven & Trailing", Order = 50)]
        [Range(1, 100000)]
        public int TrailingStopTicks
        {
            get { return _trailingStopTicks; }
            set { _trailingStopTicks = value; RecalculateValues(); }
        }

        #endregion
    }
}
