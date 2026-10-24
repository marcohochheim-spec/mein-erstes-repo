using ATAS.Indicators;

namespace OrderFlowAutoBot
{
    // Jede Signal-Methode gibt +1 (long), -1 (short) oder 0 (kein Signal) zurueck.
    // Alle Schwellenwerte kommen aus OrderFlowSignalStrategy.Parameters.cs und sind
    // in den ATAS-Strategie-Einstellungen frei veraenderbar.
    public partial class OrderFlowSignalStrategy
    {
        private int GetDeltaImbalanceSignal(int bar)
        {
            if (!UseDeltaImbalance)
                return 0;

            if (bar - DeltaImbalanceConfirmBars + 1 < 0)
                return 0;

            var direction = 0;

            for (var i = 0; i < DeltaImbalanceConfirmBars; i++)
            {
                var candle = GetCandle(bar - i);
                var barDirection = ClassifyDeltaImbalance(candle);

                if (barDirection == 0)
                    return 0;

                if (direction == 0)
                    direction = barDirection;
                else if (direction != barDirection)
                    return 0;
            }

            return direction;
        }

        private int ClassifyDeltaImbalance(IndicatorCandle candle)
        {
            if (candle.Volume < DeltaImbalanceMinVolume)
                return 0;

            if (candle.Bid <= 0 && candle.Ask <= 0)
                return 0;

            if (candle.Bid > 0 && candle.Ask / candle.Bid >= DeltaImbalanceRatio)
                return 1;

            if (candle.Ask > 0 && candle.Bid / candle.Ask >= DeltaImbalanceRatio)
                return -1;

            return 0;
        }

        private int GetCumulativeDeltaDivergenceSignal(int bar)
        {
            if (!UseCumDeltaDivergence)
                return 0;

            var lookback = CumDeltaLookbackBars;

            if (bar - 2 * lookback < 0)
                return 0;

            var recentDelta = SumDelta(bar - lookback + 1, bar);
            var priorDelta = SumDelta(bar - 2 * lookback + 1, bar - lookback);

            var priceNow = GetCandle(bar).Close;
            var priceStartOfRecentWindow = GetCandle(bar - lookback).Close;
            var priceMove = priceNow - priceStartOfRecentWindow;

            var deltaChange = recentDelta - priorDelta;

            // Bullische Divergenz: Preis bewegt sich nicht (oder faellt), waehrend
            // das kumulierte Delta deutlich zunimmt (verdeckte Kaufaktivitaet).
            if (priceMove <= CumDeltaDivergenceMaxPriceMovePoints &&
                deltaChange >= CumDeltaDivergenceMinDelta)
                return 1;

            // Baerische Divergenz: Preis bewegt sich nicht (oder steigt), waehrend
            // das kumulierte Delta deutlich abnimmt (verdeckte Verkaufsaktivitaet).
            if (priceMove >= -CumDeltaDivergenceMaxPriceMovePoints &&
                deltaChange <= -CumDeltaDivergenceMinDelta)
                return -1;

            return 0;
        }

        private decimal SumDelta(int fromBar, int toBar)
        {
            if (fromBar < 0)
                fromBar = 0;

            decimal sum = 0;

            for (var i = fromBar; i <= toBar; i++)
                sum += GetCandle(i).Delta;

            return sum;
        }

        private int GetAbsorptionSignal(int bar)
        {
            if (!UseAbsorption)
                return 0;

            var candle = GetCandle(bar);

            if (candle.Volume < AbsorptionMinVolume)
                return 0;

            var priceMove = System.Math.Abs(candle.Close - candle.Open);

            if (priceMove > AbsorptionMaxPriceMovePoints)
                return 0;

            if (candle.Delta >= AbsorptionMinDeltaAbs)
                return 1;

            if (candle.Delta <= -AbsorptionMinDeltaAbs)
                return -1;

            return 0;
        }

        private int GetPocProximitySignal(int bar)
        {
            if (!UsePocProximity)
                return 0;

            var lookback = PocLookbackBars;

            if (bar - lookback < 0)
                return 0;

            var pocPrice = FindPointOfControl(bar - lookback + 1, bar);

            if (pocPrice == null)
                return 0;

            var candle = GetCandle(bar);
            var distance = System.Math.Abs(candle.Close - pocPrice.Value);

            if (distance > PocProximityPoints)
                return 0;

            // Naeherung an den POC von oben mit ueberwiegendem Kaufdelta
            // wird als Unterstuetzung (long) gewertet, von unten mit
            // ueberwiegendem Verkaufsdelta als Widerstand (short).
            if (candle.Close >= pocPrice.Value && candle.Delta > 0)
                return 1;

            if (candle.Close <= pocPrice.Value && candle.Delta < 0)
                return -1;

            return 0;
        }

        private decimal? FindPointOfControl(int fromBar, int toBar)
        {
            if (fromBar < 0)
                fromBar = 0;

            var volumeByPrice = new System.Collections.Generic.Dictionary<decimal, decimal>();

            for (var i = fromBar; i <= toBar; i++)
            {
                var candle = GetCandle(i);

                foreach (var priceLevel in candle.GetAllPriceLevels())
                {
                    if (!volumeByPrice.ContainsKey(priceLevel.Price))
                        volumeByPrice[priceLevel.Price] = 0;

                    volumeByPrice[priceLevel.Price] += priceLevel.Volume;
                }
            }

            if (volumeByPrice.Count == 0)
                return null;

            decimal? pocPrice = null;
            var maxVolume = -1m;

            foreach (var kvp in volumeByPrice)
            {
                if (kvp.Value > maxVolume)
                {
                    maxVolume = kvp.Value;
                    pocPrice = kvp.Key;
                }
            }

            return pocPrice;
        }
    }
}
