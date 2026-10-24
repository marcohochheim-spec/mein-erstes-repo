using ATAS.Indicators;
using ATAS.Indicators.Technical;
using ATAS.Strategies.Chart;

namespace OrderFlowAutoBot
{
    // Vollstaendig parametrisierte Orderflow-Strategie fuer ATAS.
    // Kombiniert vier unabhaengig zu-/abschaltbare Signalmodule
    // (Delta-Imbalance, Cumulative-Delta-Divergenz, Absorption, POC-Naehe)
    // zu einem Einstiegssignal und verwaltet automatisiert Einstieg,
    // Stop-Loss, Take-Profit, Breakeven und Trailing-Stop.
    //
    // ACHTUNG: Vor jedem Live-Einsatz zwingend im ATAS Strategy Tester
    // auf Marktrepliken und danach im Sim-/Demo-Konto testen. Automatisierter
    // Handel kann zu Verlusten fuehren, die das eingesetzte Kapital uebersteigen.
    public partial class OrderFlowSignalStrategy : ChartStrategy
    {
        private readonly ATR _atr = new ATR { Period = 14 };

        private int _lastSeenBar = -1;

        public OrderFlowSignalStrategy()
            : base(true)
        {
            Add(_atr);
        }

        protected override void OnCalculate(int bar, decimal value)
        {
            if (bar == _lastSeenBar)
                return;

            _lastSeenBar = bar;

            var signalBar = bar - 1;

            if (signalBar < 1)
                return;

            _atr.Period = AtrPeriod;
            _atr.Calculate(signalBar, GetCandle(signalBar).Close);

            var candle = GetCandle(signalBar);
            RollTradingDayIfNeeded(candle.Time);

            var direction = EvaluateSignals(signalBar);
            this[signalBar] = direction;

            if (!CanProcess(signalBar))
                return;

            ManageOpenPosition(signalBar);

            if (CurrentPosition != 0)
                return;

            if (!IsWithinSession(candle.Time))
                return;

            TryEnter(signalBar, direction);
        }

        private int EvaluateSignals(int bar)
        {
            var signals = new[]
            {
                GetDeltaImbalanceSignal(bar),
                GetCumulativeDeltaDivergenceSignal(bar),
                GetAbsorptionSignal(bar),
                GetPocProximitySignal(bar)
            };

            var longVotes = 0;
            var shortVotes = 0;

            foreach (var signal in signals)
            {
                if (signal > 0) longVotes++;
                else if (signal < 0) shortVotes++;
            }

            if (longVotes >= MinSignalsRequired && longVotes > shortVotes)
                return 1;

            if (shortVotes >= MinSignalsRequired && shortVotes > longVotes)
                return -1;

            return 0;
        }

        private decimal GetAtrValue(int bar)
        {
            return _atr[bar];
        }
    }
}
